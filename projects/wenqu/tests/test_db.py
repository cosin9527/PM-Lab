"""db 层测试：schema / upsert / chunk 替换 / 级联删除 / vec 表维度。"""

from __future__ import annotations

import pytest

from wenqu import db
from wenqu.chunker import ChunkData, chunk_document
from wenqu.parsers import Section


def _mk_chunks(file_id: int, n: int, tag: str) -> list[ChunkData]:
    return chunk_document(file_id, [Section(text=f"{tag}内容段落。" * 30)] * n)


def test_schema_and_upsert(tmp_path):
    conn = db.connect(tmp_path / "t.db", load_vec=False)
    db.init_schema(conn)
    fid = db.upsert_file(
        conn, file_path="/a.md", relative_path="a.md", file_size=10, mtime=1.0,
        sha256="x", status="parsing",
    )
    # 同路径再 upsert → 同 file_id（身份锚）
    fid2 = db.upsert_file(
        conn, file_path="/a.md", relative_path="a.md", file_size=11, mtime=2.0,
        sha256="y", status="indexed",
    )
    assert fid == fid2
    row = db.get_file_by_path(conn, "/a.md")
    assert row["status"] == "indexed"
    conn.close()


def test_replace_chunks_no_residue(tmp_path):
    conn = db.connect(tmp_path / "t.db", load_vec=False)
    db.init_schema(conn)
    fid = db.upsert_file(
        conn, file_path="/a.md", relative_path="a.md", file_size=1, mtime=1.0,
        sha256="x", status="parsing",
    )
    chunks1 = _mk_chunks(fid, 3, "第一版")
    db.replace_file_chunks(conn, fid, "a.md", chunks1, [f"tok{i}" for i in range(len(chunks1))])
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == len(chunks1)
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0] == len(chunks1)

    chunks2 = _mk_chunks(fid, 5, "第二版")
    db.replace_file_chunks(conn, fid, "a.md", chunks2, [f"tk{i}" for i in range(len(chunks2))])
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == len(chunks2)
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0] == len(chunks2)
    # 无残留：旧内容完全消失（chunk_id 按 {file_id}:{seq} 身份锚设计，
    # 同文件重建时 seq 重计，故以内容为准校验替换语义）
    contents = [r[0] for r in conn.execute("SELECT content FROM chunks")]
    assert not any("第一版" in c for c in contents)
    assert any("第二版" in c for c in contents)
    conn.close()


def test_delete_file_cascade(tmp_path):
    conn = db.connect(tmp_path / "t.db", load_vec=False)
    db.init_schema(conn)
    fid = db.upsert_file(
        conn, file_path="/a.md", relative_path="a.md", file_size=1, mtime=1.0,
        sha256="x", status="indexed",
    )
    chunks = _mk_chunks(fid, 4, "待删")
    db.replace_file_chunks(conn, fid, "a.md", chunks, ["t"] * len(chunks))
    db.delete_file(conn, fid)
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0] == 0
    assert db.get_file_by_path(conn, "/a.md") is None
    conn.close()


def test_update_file_path_keeps_file_id(tmp_path):
    """rename：只 UPDATE 路径，file_id / chunk_id 不变。"""
    conn = db.connect(tmp_path / "t.db", load_vec=False)
    db.init_schema(conn)
    fid = db.upsert_file(
        conn, file_path="/old.md", relative_path="old.md", file_size=1, mtime=1.0,
        sha256="x", status="indexed",
    )
    chunks = _mk_chunks(fid, 2, "锚")
    db.replace_file_chunks(conn, fid, "old.md", chunks, ["t"] * len(chunks))
    chunk_ids = [c.chunk_id for c in chunks]
    db.update_file_path(conn, fid, file_path="/new.md", relative_path="new.md", source="new.md")
    assert db.get_file_by_path(conn, "/new.md")["file_id"] == fid
    assert db.get_file_by_path(conn, "/old.md") is None
    ids = [r[0] for r in conn.execute("SELECT chunk_id FROM chunks ORDER BY seq")]
    assert ids == chunk_ids
    assert conn.execute("SELECT source FROM chunks LIMIT 1").fetchone()[0] == "new.md"
    conn.close()


def test_snapshot_token_anchor(tmp_path):
    conn = db.connect(tmp_path / "t.db", load_vec=False)
    db.init_schema(conn)
    db.upsert_file(
        conn, file_path="/s.md", relative_path="s.md", file_size=1, mtime=1.0,
        sha256="x", source_type="feishu_snapshot", remote_token="tok12345",
        remote_url="https://feishu.cn/docx/tok12345", remote_updated_at="2026-01-01",
        status="indexed",
    )
    assert db.get_file_by_token(conn, "tok12345") is not None
    conn.close()


def test_vec_table_dim_switch(tmp_path):
    conn = db.connect(tmp_path / "t.db", load_vec=True)
    db.init_schema(conn)
    if not db.ensure_vec_table(conn, 512):
        pytest.skip("sqlite-vec 扩展不可用")
    assert db.vec_available(conn)
    db.ensure_vec_table(conn, 1024)  # 维度变化 → 重建
    row = conn.execute("SELECT value FROM wenqu_meta WHERE key='vec_dim'").fetchone()
    assert row[0] == "1024"
    conn.close()
