"""文档解析层：多格式正文提取。

统一输出 ExtractedDoc（sections 列表，每个 section 携带文本 + heading_path +
doc_meta {page|slide|sheet}），供 chunker 分块。

格式支持矩阵（对应实施方案 §A.2）：
- md/txt 直读（md 解析 heading 层级）
- docx 按 heading 结构
- pptx 每页 = 标题 + 正文 + 备注
- xlsx 每 sheet 一节（含表头）
- pdf 文本型按页；全文可提取字符过少 → needs_ocr（诚实上报，不假装可搜）
- .doc/.ppt/.xls → skipped_legacy；>200MB → skipped_oversize；*.icloud → 静默跳过
"""

from .base import (
    ExtractedDoc,
    ParseError,
    Section,
    SkippedFile,
    extract,
)

__all__ = ["ExtractedDoc", "ParseError", "Section", "SkippedFile", "extract"]
