"""chunker 测试：长度上限 / 相邻重叠 / heading 与 meta 保留。"""

from __future__ import annotations

from wenqu.chunker import MAX_LEN, OVERLAP, chunk_document
from wenqu.parsers import Section


def test_chunk_length_and_overlap():
    long_text = "推荐使用方案甲。不推荐使用方案乙。" * 100  # 重复句，远超上限
    chunks = chunk_document(7, [Section(text=long_text)])
    assert len(chunks) > 1
    for c in chunks:
        assert len(c.content) <= MAX_LEN
    # 相邻重叠：下一块以上一块尾部开头（防截断语义反转）
    for a, b in zip(chunks, chunks[1:]):
        tail = a.content[-OVERLAP:]
        assert b.content.startswith(tail)
    # 否定词不被切断丢失：拼接后语义完整
    joined = "\n".join(c.content for c in chunks)
    assert "不推荐使用方案乙" in joined


def test_heading_and_meta_carried():
    sections = [
        Section(text="第三章内容" * 50, heading_path="第三章 > 需求分析"),
        Section(text="第四页内容", meta={"page": 4}),
    ]
    chunks = chunk_document(3, sections)
    assert chunks[0].heading_path == "第三章 > 需求分析"
    assert '"page": 4' in (chunks[-1].doc_meta or "")
    assert all(c.chunk_id.startswith("3:") for c in chunks)
    assert [c.seq for c in chunks] == list(range(len(chunks)))


def test_short_doc_single_chunk():
    chunks = chunk_document(1, [Section(text="只有一句话")])
    assert len(chunks) == 1
    assert chunks[0].content == "只有一句话"


def test_empty_sections():
    assert chunk_document(1, []) == []
    assert chunk_document(1, [Section(text="   ")]) == []
