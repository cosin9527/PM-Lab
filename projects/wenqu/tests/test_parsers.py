"""parsers 测试：全格式覆盖 + 状态标注（needs_ocr / legacy / icloud）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from wenqu.parsers import ExtractedDoc, ParseError, SkippedFile, extract


def test_md_headings(tmp_path):
    p = tmp_path / "doc.md"
    p.write_text(
        "---\ntitle: 测试\n---\n"
        "# 第一章\n\n概述内容。\n\n## 1.1 背景\n\n背景描述。\n\n"
        "```bash\n# 注释不是标题\n```\n",
        encoding="utf-8",
    )
    doc = extract(p)
    assert doc.status == "ok"
    paths = [s.heading_path for s in doc.sections]
    assert any("第一章" in (h or "") for h in paths)
    assert any("1.1 背景" in (h or "") for h in paths)
    assert all("bash" not in (h or "") for h in paths)  # 围栏内 # 不当标题


def test_txt(tmp_path):
    p = tmp_path / "note.txt"
    p.write_text("纯文本笔记内容", encoding="utf-8")
    doc = extract(p)
    assert doc.status == "ok"
    assert doc.sections[0].text == "纯文本笔记内容"


def test_docx(tmp_path):
    docx = pytest.importorskip("docx")
    p = tmp_path / "需求文档.docx"
    d = docx.Document()
    d.add_heading("达人管理需求", level=1)
    d.add_paragraph("支持达人与企业微信员工的好友关系监控。")
    d.add_heading("接口设计", level=2)
    d.add_paragraph("接口按 REST 风格设计。")
    d.save(str(p))
    doc = extract(p)
    assert doc.status == "ok"
    joined = "\n".join(s.text for s in doc.sections)
    assert "好友关系监控" in joined
    assert doc.sections[0].heading_path == "达人管理需求"


def test_pptx(tmp_path):
    pptx = pytest.importorskip("pptx")
    p = tmp_path / "评审.pptx"
    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[0])
    slide.shapes.title.text = "产品评审结论"
    tb = slide.shapes.add_textbox(0, 0, 100, 100)
    tb.text_frame.text = "结论：通过"
    prs.save(str(p))
    doc = extract(p)
    assert doc.status == "ok"
    sec = doc.sections[0]
    assert "产品评审结论" in sec.text and "结论：通过" in sec.text
    assert sec.meta.get("slide") == 1


def test_xlsx(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    p = tmp_path / "指标表.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "核心指标"
    ws.append(["指标", "数值"])
    ws.append(["DAU", "12000"])
    wb.save(str(p))
    doc = extract(p)
    assert doc.status == "ok"
    sec = doc.sections[0]
    assert "12000" in sec.text and sec.meta.get("sheet") == "核心指标"


def _make_pdf(text: str) -> bytes:
    """手工构造最小单页文本 PDF（pypdf 可提取）。"""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 6\n0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n" + str(xref).encode() + b"\n%%EOF"
    return out


def test_pdf_text(tmp_path):
    pytest.importorskip("pypdf")
    p = tmp_path / "报告.pdf"
    p.write_bytes(_make_pdf("wenqu smoke test content padding 0123456789 abcdefgh"))
    doc = extract(p)
    assert doc.status == "ok"
    assert "smoke test" in doc.sections[0].text
    assert doc.sections[0].meta.get("page") == 1


def test_pdf_needs_ocr(tmp_path):
    pytest.importorskip("pypdf")
    p = tmp_path / "扫描件.pdf"
    # 无文本操作符的页面（模拟扫描件）
    p.write_bytes(_make_pdf("").replace(b"BT /F1 12 Tf 72 720 Td () Tj ET", b""))
    doc = extract(p)
    assert doc.status == "needs_ocr"


def test_legacy_and_icloud(tmp_path):
    legacy = tmp_path / "老文档.doc"
    legacy.write_bytes(b"\xd0\xcf")
    assert extract(legacy).status == "skipped_legacy"
    icloud = tmp_path / "报告.md.icloud"
    icloud.write_text("placeholder", encoding="utf-8")
    with pytest.raises(SkippedFile):
        extract(icloud)


def test_corrupt_file_marks_failed(tmp_path):
    p = tmp_path / "坏文件.docx"
    p.write_bytes(b"not a docx")
    with pytest.raises(ParseError):
        extract(p)
