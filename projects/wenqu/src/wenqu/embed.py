"""fastembed 封装：懒加载 / 前缀常量 / 降级 / 闲置卸载 / cache_dir 覆盖。

关键约束（实施方案 §A.2）：
- 初始化 TextEmbedding 时须显式传 cache_dir，覆盖 fastembed 默认缓存路径，
  确保模型只落 $WENQU_HOME/.wenqu/models/（维持 ".wenqu 整目录可删 = 索引清零"
  的布局原则）。
- 模型未缓存时自动下载（init 选型后首次下载、运行期切换 embed_model）：
  下载失败走 §0.6 重试与降级链。
- 加载/推理失败 → degraded（检索层自动退化为纯 BM25），60 秒后允许重试。
- 闲置 10 分钟卸载（wenqu-sync 循环调用 maybe_unload；wenqu-mcp 启动即后台预热）。
- ONNX 线程数 = min(4, 核数)；低进程优先级由 launchd plist（Nice）兜底。
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Sequence

from .config import SUPPORTED_MODELS, Config, models_dir
from .db import pack_floats

IDLE_UNLOAD_SECONDS = 600.0   # 闲置 10 分钟卸载
FAILURE_RETRY_SECONDS = 60.0  # 失败后 60s 才重试，避免死循环打日志


class Embedder:
    """进程内向量编码器（wenqu-mcp 与 wenqu-sync 各持一个实例）。"""

    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._model = None
        self._model_key: str | None = None
        self._lock = threading.Lock()
        self._last_used = 0.0
        self._failed_at = 0.0
        self.degraded = False

    # ---- 内部 ----

    def _ensure(self) -> bool:
        r = self._cfg.data["retrieval"]
        if r["mode"] != "hybrid":
            return False
        key = r["embed_model"]
        if self._model is not None and key == self._model_key:
            return True
        if self._failed_at and time.time() - self._failed_at < FAILURE_RETRY_SECONDS:
            return False
        with self._lock:
            if self._model is not None and key == self._model_key:
                return True
            try:
                from fastembed import TextEmbedding

                spec = SUPPORTED_MODELS[key]
                mdir = models_dir(self._cfg)
                mdir.mkdir(parents=True, exist_ok=True)
                self._model = TextEmbedding(
                    model_name=spec["fastembed_name"],
                    cache_dir=str(mdir),  # 显式覆盖 fastembed 默认缓存路径
                    threads=min(4, os.cpu_count() or 4),
                )
                self._model_key = key
                self.degraded = False
                self._failed_at = 0.0
                return True
            except Exception:
                traceback.print_exc(file=sys.stderr)
                self.degraded = True
                self._failed_at = time.time()
                self._model = None
                return False

    # ---- 对外 ----

    def embed_query(self, text: str) -> bytes | None:
        """查询向量（含模型前缀常量）。失败返回 None（调用方降级纯 BM25）。"""
        if not self._ensure():
            return None
        spec = SUPPORTED_MODELS[self._model_key]
        try:
            vec = next(self._model.embed([spec["query_prefix"] + text]))
            self._last_used = time.time()
            return pack_floats([float(x) for x in vec])
        except Exception:
            self.degraded = True
            self._failed_at = time.time()
            return None

    def embed_passages(self, texts: Sequence[str]) -> list[bytes] | None:
        """批量索引向量（含 passage 前缀）。失败返回 None。"""
        if not texts:
            return []
        if not self._ensure():
            return None
        spec = SUPPORTED_MODELS[self._model_key]
        try:
            prefixed = [spec["passage_prefix"] + t for t in texts]
            vecs = list(self._model.embed(prefixed))
            self._last_used = time.time()
            return [pack_floats([float(x) for x in v]) for v in vecs]
        except Exception:
            self.degraded = True
            self._failed_at = time.time()
            return None

    def warm(self) -> bool:
        """预热（触发懒加载/下载）；wenqu-mcp 启动时后台线程调用。"""
        return self.embed_query("预热") is not None

    def maybe_unload(self) -> bool:
        """闲置卸载（wenqu-sync 循环调用；µs 级检查成本）。"""
        if self._model is None:
            return False
        if time.time() - self._last_used > IDLE_UNLOAD_SECONDS:
            with self._lock:
                if self._model is not None:
                    self._model = None
                    self._model_key = None
            return True
        return False
