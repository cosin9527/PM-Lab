"""Markdown 解析：heading 层级 → heading_path；代码围栏内不识别标题。"""

from __future__ import annotations

import re
from pathlib import Path

from .base import ExtractedDoc, Section, _frontmatter_strip

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def parse(path: Path) -> ExtractedDoc:
    text = path.read_text(encoding="utf-8", errors="replace")
    text = _frontmatter_strip(text)
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    buf: list[str] = []
    cur_heading: str | None = None
    in_fence = False

    def flush() -> None:
        body = "\n".join(buf).strip()
        if body:
            sections.append(Section(text=body, heading_path=cur_heading))
        buf.clear()

    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            buf.append(line)
            continue
        m = None if in_fence else _HEADING.match(line)
        if m:
            flush()
            level, title = len(m.group(1)), m.group(2).strip()
            stack = [s for s in stack if s[0] < level]
            stack.append((level, title))
            cur_heading = " > ".join(t for _, t in stack)
        else:
            buf.append(line)
    flush()
    if not sections and text.strip():
        sections.append(Section(text=text.strip()))
    return ExtractedDoc(sections=sections)
