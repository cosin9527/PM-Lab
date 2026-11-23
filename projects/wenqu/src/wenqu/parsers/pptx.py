"""pptx 解析：每页一节 = 标题 + 正文 + 备注；doc_meta={"slide": n}。"""

from __future__ import annotations

from pathlib import Path

from .base import ExtractedDoc, ParseError, Section


def parse(path: Path) -> ExtractedDoc:
    try:
        from pptx import Presentation
    except ImportError as e:  # pragma: no cover
        raise ParseError(f"python-pptx 未安装: {e}") from e
    try:
        prs = Presentation(str(path))
    except Exception as e:
        raise ParseError(f"pptx 打开失败: {e}") from e

    sections: list[Section] = []
    for i, slide in enumerate(prs.slides, 1):
        parts: list[str] = []
        title = ""
        try:
            if slide.shapes.title is not None and slide.shapes.title.has_text_frame:
                title = slide.shapes.title.text.strip()
                if title:
                    parts.append(title)
            for shape in slide.shapes:
                if shape is slide.shapes.title:
                    continue
                if getattr(shape, "has_text_frame", False):
                    t = shape.text_frame.text.strip()
                    if t:
                        parts.append(t)
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
                n = slide.notes_slide.notes_text_frame.text.strip()
                if n:
                    parts.append("[备注] " + n)
        except Exception as e:
            raise ParseError(f"slide {i} 读取失败: {e}") from e
        text = "\n".join(parts)
        if text:
            sections.append(Section(text=text, heading_path=title or None, meta={"slide": i}))
    return ExtractedDoc(sections=sections)
