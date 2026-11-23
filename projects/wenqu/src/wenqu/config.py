"""wenqu 配置层：加载 / 校验 / 热重载 / 原子写入。

唯一配置文件：$WENQU_HOME/config.yaml（wenqu-mcp 与 wenqu-sync 共读，
从机制上杜绝双端配置漂移）。

- 热重载：Config.reload_if_changed() 基于 mtime stat（µs 级成本）——
  retrieval.* 下一查询/下一轮任务生效；storage.* 例外，需 `wenqu sync restart`。
- 原子写：先写 tmp 再 rename，避免半写状态被并发读者看到。
"""

from __future__ import annotations

import copy
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_EXTENSIONS = [".md", ".txt", ".docx", ".xlsx", ".pptx", ".pdf"]
BUILTIN_EXCLUDES = [".git", "node_modules", ".DS_Store", "~$*", ".~*", ".venv"]

# 向量模型注册表：config 名 → fastembed 名 / 维度 / 查询与索引端前缀常量
SUPPORTED_MODELS: dict[str, dict[str, Any]] = {
    "bge-small-zh-v1.5": {
        "fastembed_name": "BAAI/bge-small-zh-v1.5",
        "dim": 512,
        "query_prefix": "为这个句子生成表示以用于检索相关文章：",
        "passage_prefix": "",
    },
    "intfloat/multilingual-e5-large": {
        "fastembed_name": "intfloat/multilingual-e5-large",
        "dim": 1024,
        "query_prefix": "query: ",
        "passage_prefix": "passage: ",
    },
}

# 归档候选检测的路径模式（目录名 / 文件名片段，小写匹配）
ARCHIVE_PATTERNS = ["archive", "归档", "旧版", "废弃", "backup", "bak"]

# config set 的生效时点说明（用于 CLI 输出）
EFFECT_NOTES = {
    "retrieval": "下一查询生效（wenqu-mcp 每次工具调用前热重载）",
    "local": "下轮扫描生效（wenqu-sync 每轮任务前热重载）",
    "index": "下轮扫描生效",
    "feishu": "下轮远端周期生效",
    "storage": "需 `wenqu sync restart`（唯一需重启项）",
}


class ConfigError(Exception):
    """配置非法。"""


def wenqu_home() -> Path:
    """WENQU_HOME 解析：环境变量优先，默认 ~/Knowledge。"""
    home = os.environ.get("WENQU_HOME")
    return Path(home).expanduser() if home else Path.home() / "Knowledge"


def config_path() -> Path:
    return wenqu_home() / "config.yaml"


def user_dict_path() -> Path:
    return wenqu_home() / "user_dict.txt"


def default_config() -> dict[str, Any]:
    home = wenqu_home()
    return {
        "local": {
            "watch": [],  # 必填：仅显式声明，无默认目录
            "extensions": list(DEFAULT_EXTENSIONS),
            "exclude": list(BUILTIN_EXCLUDES),  # 追加式，不覆盖内置排除
        },
        "index": {"sensitive_paths": []},  # 完全排除、不进索引
        "retrieval": {
            "mode": "hybrid",  # keyword | hybrid
            "embed_model": "bge-small-zh-v1.5",
            "top_k": 8,
            "rrf_k": 60,
            "recency_alpha": 0.2,  # 0 = 关闭
            "recency_tau_days": 180,
        },
        "feishu": {"watch": [], "sync_interval": "daily"},  # daily | hourly
        "storage": {
            "db": str(home / ".wenqu" / "index.db"),
            "models": str(home / ".wenqu" / "models") + "/",
            "tmp": str(home / ".wenqu" / "tmp") + "/",
            "logs": str(home / ".wenqu" / "logs") + "/",
        },
    }


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def validate(cfg: dict[str, Any]) -> None:
    """校验配置，非法即抛 ConfigError。"""
    r = cfg.get("retrieval", {})
    if r.get("mode") not in ("keyword", "hybrid"):
        raise ConfigError(f"retrieval.mode 必须是 keyword | hybrid，当前: {r.get('mode')!r}")
    if r.get("embed_model") not in SUPPORTED_MODELS:
        raise ConfigError(
            f"retrieval.embed_model 必须是 {' | '.join(SUPPORTED_MODELS)}，当前: {r.get('embed_model')!r}"
        )
    if not isinstance(r.get("top_k"), int) or not 1 <= r["top_k"] <= 50:
        raise ConfigError("retrieval.top_k 必须是 1..50 的整数")
    if not isinstance(r.get("rrf_k"), int) or r["rrf_k"] <= 0:
        raise ConfigError("retrieval.rrf_k 必须是正整数")
    alpha = r.get("recency_alpha")
    if not isinstance(alpha, (int, float)) or not 0 <= alpha <= 1:
        raise ConfigError("retrieval.recency_alpha 必须在 [0, 1]")
    tau = r.get("recency_tau_days")
    if not isinstance(tau, (int, float)) or tau <= 0:
        raise ConfigError("retrieval.recency_tau_days 必须为正数")
    if cfg.get("feishu", {}).get("sync_interval") not in ("daily", "hourly"):
        raise ConfigError("feishu.sync_interval 必须是 daily | hourly")
    watch = cfg.get("local", {}).get("watch")
    if watch is not None and not isinstance(watch, list):
        raise ConfigError("local.watch 必须是目录列表")


def normalize(raw: dict[str, Any] | None) -> dict[str, Any]:
    """与默认值合并 + 展开存储路径，返回规范化配置。"""
    cfg = _deep_merge(default_config(), raw or {})
    storage = cfg["storage"]
    for key in ("db", "models", "tmp", "logs"):
        p = Path(str(storage[key])).expanduser()
        if not p.is_absolute():  # 相对路径锚定到 WENQU_HOME
            p = wenqu_home() / p
        storage[key] = str(p)
    validate(cfg)
    return cfg


class Config:
    """带 mtime 热重载的配置对象。

    用法：cfg = load_config(); cfg.data（每次访问自动 stat，变更即重载）。
    """

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else config_path()
        self._mtime: float | None = None
        self._data: dict[str, Any] = {}
        self.reload(force=True)

    # ---- 加载与热重载 ----

    def reload(self, force: bool = False) -> bool:
        """stat mtime，变更即重载。返回是否发生了重载。"""
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            mtime = None
        if not force and mtime == self._mtime:
            return False
        raw: dict[str, Any] | None = None
        if mtime is not None:
            try:
                raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError as e:  # 半写/坏 yaml：保留旧配置，不崩溃
                raise ConfigError(f"config.yaml 解析失败: {e}") from e
            if not isinstance(raw, dict):
                raise ConfigError("config.yaml 顶层必须是映射")
        self._data = normalize(raw)
        self._mtime = mtime
        return True

    def reload_if_changed(self) -> bool:
        return self.reload(force=False)

    # ---- 访问 ----

    @property
    def data(self) -> dict[str, Any]:
        self.reload_if_changed()
        return self._data

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value: Any) -> None:
        """内存中设置嵌套值（需再 save() 落盘）。"""
        parts = dotted.split(".")
        node = self._data
        for part in parts[:-1]:
            if part not in node or not isinstance(node[part], dict):
                node[part] = {}
            node = node[part]
        node[parts[-1]] = value
        validate(self._data)

    # ---- 落盘（原子写） ----

    def save(self) -> None:
        validate(self._data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_dir = Path(self._data["storage"]["tmp"])
        tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(tmp_dir), prefix="config-", suffix=".yaml")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                yaml.safe_dump(self._data, f, allow_unicode=True, sort_keys=False)
            os.replace(tmp_name, self.path)  # 原子替换
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        self._mtime = None  # 下次访问重新 stat
        self.reload(force=True)


def load_config(path: Path | None = None) -> Config:
    """加载配置（文件不存在时返回默认配置的运行态，不自动落盘）。"""
    return Config(path)


# ---------------------------------------------------------------------------
# 便捷访问
# ---------------------------------------------------------------------------

def db_path(cfg: Config | dict[str, Any]) -> Path:
    data = cfg.data if isinstance(cfg, Config) else cfg
    return Path(data["storage"]["db"])


def models_dir(cfg: Config | dict[str, Any]) -> Path:
    data = cfg.data if isinstance(cfg, Config) else cfg
    return Path(data["storage"]["models"])


def watch_dirs(cfg: Config | dict[str, Any]) -> list[Path]:
    data = cfg.data if isinstance(cfg, Config) else cfg
    return [Path(str(p)).expanduser() for p in data["local"]["watch"]]


def coerce_value(value: str, current: Any) -> Any:
    """CLI config set 的类型推断：按现值类型转换字符串输入。"""
    if isinstance(current, bool):
        if value.lower() in ("true", "1", "yes"):
            return True
        if value.lower() in ("false", "0", "no"):
            return False
        raise ConfigError(f"布尔值期望 true/false，当前: {value!r}")
    if isinstance(current, int) and not isinstance(current, bool):
        try:
            return int(value)
        except ValueError as e:
            raise ConfigError(f"整数期望，当前: {value!r}") from e
    if isinstance(current, float):
        try:
            return float(value)
        except ValueError as e:
            raise ConfigError(f"浮点数期望，当前: {value!r}") from e
    if isinstance(current, list):
        items = [x.strip() for x in value.split(",") if x.strip()]
        return items or current
    return value
