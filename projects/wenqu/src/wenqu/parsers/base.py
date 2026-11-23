"""解析器公共结构与分发。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class Section:
    """一个可分块的最小结构单元（heading 块 / 页 / slide / sheet）。"""

    text: str
    heading_path: str | None = None          # 如 "第三章 > 需求分析"
    meta: dict[str, Any] = field(default_factory=dict)  # {"page":1}/{"slide":2}/{"sheet":"名"}


@dataclass
class ExtractedDoc:
    sections: list[Section] = field(default_factory=list)
    status: str = "ok"  # ok | needs_ocr | skipped_legacy | skipped_oversize


class ParseError(Exception):
    """解析失败（调用方标 failed + 原因，不阻塞队列）。"""


class SkippedFile(Exception):
    """静默跳过（不进 manifest，仅日志）：*.icloud 占位、未知扩展名。"""

    def __init__(self, status: str):
        super().__init__(status)
        self.status = status


MAX_FILE_SIZE = 200 * 1024 * 1024  # >200MB 标 skipped_oversize
LEGACY_EXTS = {".doc", ".ppt", ".xls"}


def _frontmatter_strip(text: str) -> str:
    """剥离 YAML frontmatter（--- ... --- 开头）。"""
    if text.startswith("---"):
        m = re.match(r"^---\s*\n.*?\n---\s*\n", text, flags=re.DOTALL)
        if m:
            return text[m.end():]
    return text


def _parse(path: Path) -> ExtractedDoc:
    from . import docx, md, pdf, pptx, txt, xlsx

    ext = path.suffix.lower()
    parser: Callable[[Path], ExtractedDoc] = {
        ".md": md.parse,
        ".markdown": md.parse,
        ".txt": txt.parse,
        ".docx": docx.parse,
        ".pptx": pptx.parse,
        ".xlsx": xlsx.parse,
        ".pdf": pdf.parse,
    }[ext]
    return parser(path)


def extract(path: Path) -> ExtractedDoc:
    """入口分发：icloud 占位 / 超大 / 老格式在此拦截，其余交对应解析器。"""
    if path.name.endswith(".icloud"):
        raise SkippedFile("skipped_icloud")
    if path.stat().st_size > MAX_FILE_SIZE:
        return ExtractedDoc(status="skipped_oversize")
    ext = path.suffix.lower()
    if ext in LEGACY_EXTS:
        return ExtractedDoc(status="skipped_legacy")
    if ext not in (".md", ".markdown", ".txt", ".docx", ".pptx", ".xlsx", ".pdf"):
        raise SkippedFile("skipped_unknown_ext")
    return _parse(path)
