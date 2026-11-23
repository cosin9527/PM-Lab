"""纯文本解析：整文件一节。"""

from __future__ import annotations

from pathlib import Path

from .base import ExtractedDoc, Section


def parse(path: Path) -> ExtractedDoc:
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    sections = [Section(text=text)] if text else []
    return ExtractedDoc(sections=sections)
