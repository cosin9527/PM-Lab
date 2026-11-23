"""FTS5 全文层：jieba 应用层分词。

写入与查询两端一致：jieba.cut_for_search → 空格分隔文本 → FTS5(tokenize=unicode61)。
零编译依赖（无需自定义 SQLite 分词器），用户词典可热补（user_dict.txt 变更后
新查询即用新词典；既有索引不重切，接受渐进一致性）。
"""

from __future__ import annotations

import threading
from pathlib import Path

import jieba

_lock = threading.Lock()
_dict_path_str: str | None = None
_dict_mtime: float | None = None


def _ensure_dict(user_dict: Path | None = None) -> None:
    global _dict_path_str, _dict_mtime
    with _lock:
        if user_dict is None:
            jieba.initialize()
            return
        p = user_dict
        if not p.exists():
            jieba.initialize()
            return
        if str(p) != _dict_path_str:
            jieba.load_userdict(str(p))
            _dict_path_str = str(p)
            _dict_mtime = p.stat().st_mtime
        elif p.stat().st_mtime != _dict_mtime:
            jieba.load_userdict(str(p))  # 词典热补
            _dict_mtime = p.stat().st_mtime


def tokenize(text: str, user_dict: Path | None = None) -> str:
    """索引端：文本 → 空格分隔分词串。"""
    _ensure_dict(user_dict)
    toks = [t.strip() for t in jieba.cut_for_search(text) if t.strip()]
    return " ".join(toks)


def match_expr(text: str, user_dict: Path | None = None) -> str:
    """查询端：文本 → FTS5 MATCH 表达式（每个 token 加引号，防特殊字符）。"""
    _ensure_dict(user_dict)
    toks: list[str] = []
    for t in jieba.cut_for_search(text):
        t = t.replace('"', " ").strip()
        if t:
            toks.append(f'"{t}"')
    return " ".join(toks) if toks else '""'
