"""xlsx 解析：每 sheet 一节（含表头行，TSV 行文本）；doc_meta={"sheet": 名}。

行数上限 5000 / 节，防大表撑爆索引。
"""

from __future__ import annotations

from pathlib import Path

from .base import ExtractedDoc, ParseError, Section

MAX_ROWS_PER_SHEET = 5000


def parse(path: Path) -> ExtractedDoc:
    try:
        from openpyxl import load_workbook
    except ImportError as e:  # pragma: no cover
        raise ParseError(f"openpyxl 未安装: {e}") from e
    try:
        wb = load_workbook(str(path), read_only=True, data_only=True)
    except Exception as e:
        raise ParseError(f"xlsx 打开失败: {e}") from e

    sections: list[Section] = []
    try:
        for ws in wb.worksheets:
            lines: list[str] = []
            for row in ws.iter_rows(values_only=True):
                cells = ["" if c is None else str(c) for c in row]
                if any(c.strip() for c in cells):
                    lines.append("\t".join(cells))
                if len(lines) >= MAX_ROWS_PER_SHEET:
                    break
            text = "\n".join(lines)
            if text.strip():
                sections.append(Section(text=text, heading_path=ws.title, meta={"sheet": ws.title}))
    except Exception as e:
        raise ParseError(f"xlsx 读取失败: {e}") from e
    finally:
        wb.close()
    return ExtractedDoc(sections=sections)
