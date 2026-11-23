"""docx 解析：按 heading 结构分节；表格转 TSV 行。"""

from __future__ import annotations

import re
from pathlib import Path

from .base import ExtractedDoc, ParseError, Section

_HEADING_STYLE = re.compile(r"^(?:Heading|标题)\s*(\d+)$", re.IGNORECASE)


def parse(path: Path) -> ExtractedDoc:
    try:
        from docx import Document
    except ImportError as e:  # pragma: no cover
        raise ParseError(f"python-docx 未安装: {e}") from e
    try:
        doc = Document(str(path))
    except Exception as e:
        raise ParseError(f"docx 打开失败: {e}") from e

    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    buf: list[str] = []
    cur_heading: str | None = None

    def flush() -> None:
        body = "\n".join(buf).strip()
        if body:
            sections.append(Section(text=body, heading_path=cur_heading))
        buf.clear()

    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        style_name = (para.style.name if para.style is not None else "") or ""
        m = _HEADING_STYLE.match(style_name.strip())
        if m:
            flush()
            level = int(m.group(1))
            stack = [s for s in stack if s[0] < level]
            stack.append((level, text))
            cur_heading = " > ".join(t for _, t in stack)
        else:
            buf.append(text)
    flush()

    for table in doc.tables:
        try:
            lines = ["\t".join(c.text.strip() for c in row.cells) for row in table.rows]
        except Exception as e:
            raise ParseError(f"docx 表格读取失败: {e}") from e
        if lines:
            sections.append(Section(text="\n".join(lines), heading_path=cur_heading or "表格"))

    return ExtractedDoc(sections=sections)
