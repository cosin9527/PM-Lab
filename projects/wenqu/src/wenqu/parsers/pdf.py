"""pdf 解析：文本型按页提取；可提取字符过少 → needs_ocr（诚实上报）。

doc_meta={"page": n}。
"""

from __future__ import annotations

from pathlib import Path

from .base import ExtractedDoc, ParseError, Section

# 近似阈值：全文可提取字符低于该值视为扫描件（设计口径为"每页字符数阈值"）
NEEDS_OCR_TOTAL_CHARS = 50


def parse(path: Path) -> ExtractedDoc:
    try:
        from pypdf import PdfReader
    except ImportError as e:  # pragma: no cover
        raise ParseError(f"pypdf 未安装: {e}") from e
    try:
        reader = PdfReader(str(path))
    except Exception as e:
        raise ParseError(f"pdf 打开失败: {e}") from e

    sections: list[Section] = []
    total_chars = 0
    try:
        for i, page in enumerate(reader.pages, 1):
            try:
                t = (page.extract_text() or "").strip()
            except Exception:
                t = ""
            total_chars += len(t)
            if t:
                sections.append(Section(text=t, meta={"page": i}))
    except Exception as e:
        raise ParseError(f"pdf 读取失败: {e}") from e

    if total_chars < NEEDS_OCR_TOTAL_CHARS:
        return ExtractedDoc(status="needs_ocr")
    return ExtractedDoc(sections=sections)
