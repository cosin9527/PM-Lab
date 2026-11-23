"""wenqu 存储层：单一 SQLite（WAL）。

- WAL + busy_timeout：双客户端并发读 + 唯一写入者，读写不阻塞
- chunks_fts：FTS5 全文表；写入与查询两端均为 jieba 预分词后的空格分隔文本
  （零编译依赖，词典可热补）。
  实现说明：设计文档附录 C 为 external-content 形态；本实现改为普通 FTS5 表
  （含 UNINDEXED chunk_id 列）——external content 的 delete 命令要求精确重放
  旧分词文本，用户词典变更后不可靠；普通表支持按 rowid 可靠删除，
  行为契约（两端预分词 / 零编译依赖 / 词典热补）不变。
- chunks_vec：仅 hybrid 方向创建（vec0 虚表，维度随模型）；模型切换 =
  drop + 重建 + 全量重嵌。sqlite-vec 扩展不可加载时自动跳过建表，
  检索层走 numpy 暴力降级（chunks.embedding BLOB 始终有数据）。
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
import struct
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA_VERSION = 1

# file_manifest.status 取值：
#   pending|parsing|indexed|archive_candidate|archived|needs_ocr|skipped_oversize
#   |skipped_legacy|failed|missing|revoked|deleted_remote|moved_out_of_scope
DDL = """
CREATE TABLE IF NOT EXISTS file_manifest (
  file_id INTEGER PRIMARY KEY,
  file_path TEXT UNIQUE,
  relative_path TEXT, file_size INTEGER, mtime REAL, sha256 TEXT,
  source_type TEXT,
  remote_token TEXT UNIQUE,
  remote_url TEXT, remote_updated_at TEXT,
  status TEXT,
  indexed_at TEXT, embedded_at TEXT,
  error_message TEXT
);

CREATE TABLE IF NOT EXISTS chunks (
  chunk_id TEXT PRIMARY KEY,
  file_id INTEGER NOT NULL,
  source TEXT NOT NULL,
  seq INTEGER, content TEXT NOT NULL,
  heading_path TEXT,
  doc_meta TEXT,
  embedding BLOB
);

CREATE TABLE IF NOT EXISTS wenqu_meta (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  content, chunk_id UNINDEXED, tokenize='unicode61'
);
"""


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 连接与扩展
# ---------------------------------------------------------------------------

def _load_vec_extension(conn: sqlite3.Connection) -> bool:
    """尝试加载 sqlite-vec 扩展；不可用返回 False（keyword 模式不受影响）。"""
    try:
        import sqlite_vec  # type: ignore

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception:
        return False


def connect(db_path: str | Path, load_vec: bool = True) -> sqlite3.Connection:
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    if load_vec:
        _load_vec_extension(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript(DDL)
        conn.execute(
            "INSERT OR REPLACE INTO wenqu_meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )


def vec_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT rowid FROM chunks_vec LIMIT 1").fetchone()
        return True
    except sqlite3.OperationalError:
        return False


def ensure_vec_table(conn: sqlite3.Connection, dim: int) -> bool:
    """确保 chunks_vec 存在且维度匹配；维度变化时 drop + 重建。

    返回 vec 表是否可用（扩展加载失败则 False）。
    连接未加载扩展时先尝试加载（运行期 keyword → hybrid 切换的场景）。
    """
    if not vec_available(conn):
        if not _load_vec_extension(conn):
            return False
    current: str | None = None
    try:
        row = conn.execute("SELECT value FROM wenqu_meta WHERE key='vec_dim'").fetchone()
        current = row[0] if row else None
    except sqlite3.OperationalError:
        pass
    if current == str(dim) and vec_available(conn):
        return True
    with conn:
        conn.execute("DROP TABLE IF EXISTS chunks_vec")
        conn.execute(f"CREATE VIRTUAL TABLE chunks_vec USING vec0(embedding float[{int(dim)}])")
        conn.execute("INSERT OR REPLACE INTO wenqu_meta(key, value) VALUES('vec_dim', ?)", (str(dim),))
    return True


def drop_vec_table(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("DROP TABLE IF EXISTS chunks_vec")
        conn.execute("DELETE FROM wenqu_meta WHERE key='vec_dim'")


# ---------------------------------------------------------------------------
# 向量序列化
# ---------------------------------------------------------------------------

def pack_floats(vec: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack_floats(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


# ---------------------------------------------------------------------------
# manifest 操作
# ---------------------------------------------------------------------------

def upsert_file(
    conn: sqlite3.Connection,
    *,
    file_path: str,
    relative_path: str,
    file_size: int,
    mtime: float,
    sha256: str,
    source_type: str = "local",
    remote_token: str | None = None,
    remote_url: str | None = None,
    remote_updated_at: str | None = None,
    status: str = "pending",
) -> int:
    """按 file_path upsert，返回 file_id（稳定身份锚：rename 后不变）。"""
    with conn:
        conn.execute(
            """
            INSERT INTO file_manifest(
                file_path, relative_path, file_size, mtime, sha256,
                source_type, remote_token, remote_url, remote_updated_at,
                status, indexed_at, embedded_at, error_message
            ) VALUES(?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL)
            ON CONFLICT(file_path) DO UPDATE SET
                relative_path=excluded.relative_path,
                file_size=excluded.file_size,
                mtime=excluded.mtime,
                sha256=excluded.sha256,
                remote_url=COALESCE(excluded.remote_url, file_manifest.remote_url),
                remote_updated_at=COALESCE(excluded.remote_updated_at, file_manifest.remote_updated_at),
                status=excluded.status
            """,
            (file_path, relative_path, file_size, mtime, sha256,
             source_type, remote_token, remote_url, remote_updated_at, status),
        )
        # 注意：ON CONFLICT DO UPDATE 分支的 lastrowid 返回"假想的下一个 rowid"，
        # 不等于冲突行的 file_id——按路径回查才是稳定身份锚
        row = conn.execute(
            "SELECT file_id FROM file_manifest WHERE file_path=?", (file_path,)
        ).fetchone()
        return int(row[0])


def mark_file_status(
    conn: sqlite3.Connection,
    file_id: int,
    status: str,
    *,
    error: str | None = None,
    indexed: bool = False,
) -> None:
    with conn:
        if indexed:
            conn.execute(
                "UPDATE file_manifest SET status=?, error_message=?, indexed_at=? WHERE file_id=?",
                (status, error, now_iso(), file_id),
            )
        else:
            conn.execute(
                "UPDATE file_manifest SET status=?, error_message=? WHERE file_id=?",
                (status, error, file_id),
            )


def update_file_path(
    conn: sqlite3.Connection,
    file_id: int,
    *,
    file_path: str,
    relative_path: str,
    source: str,
) -> None:
    """rename/移动：只 UPDATE 路径与展示字段——file_id、chunk_id 不变，
    不重解析、不重嵌入、检索引用不断裂。"""
    with conn:
        conn.execute(
            "UPDATE file_manifest SET file_path=?, relative_path=? WHERE file_id=?",
            (file_path, relative_path, file_id),
        )
        conn.execute(
            "UPDATE chunks SET source=? WHERE file_id=?", (source, file_id)
        )


def get_file_by_path(conn: sqlite3.Connection, file_path: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM file_manifest WHERE file_path=?", (file_path,)
    ).fetchone()


def get_file_by_sha(
    conn: sqlite3.Connection, sha256: str, source_type: str = "local"
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM file_manifest WHERE sha256=? AND source_type=?",
        (sha256, source_type),
    ).fetchone()


def get_file_by_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM file_manifest WHERE remote_token=?", (token,)
    ).fetchone()


def get_file(conn: sqlite3.Connection, file_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM file_manifest WHERE file_id=?", (file_id,)
    ).fetchone()


# ---------------------------------------------------------------------------
# chunks 操作（索引与清单同事务）
# ---------------------------------------------------------------------------

def replace_file_chunks(
    conn: sqlite3.Connection,
    file_id: int,
    source: str,
    chunks: Sequence[Any],          # ChunkData 列表（含 chunk_id/seq/content/heading_path/doc_meta）
    fts_texts: Sequence[str],       # 与 chunks 对齐的 jieba 预分词文本
    embeddings: Sequence[bytes | None] | None = None,
    use_vec: bool = True,
) -> int:
    """整文件替换 chunks（旧块全删 + 新块全插，单事务，无幽灵引用）。

    rowid 对齐策略：chunks 与 chunks_fts / chunks_vec 使用同一 rowid，
    保证 FTS / vec 可按 rowid 精确删除。
    """
    with conn:
        old_rowids = [
            r[0] for r in conn.execute(
                "SELECT rowid FROM chunks WHERE file_id=?", (file_id,)
            ).fetchall()
        ]
        for rid in old_rowids:
            conn.execute("DELETE FROM chunks_fts WHERE rowid=?", (rid,))
            if use_vec:
                try:
                    conn.execute("DELETE FROM chunks_vec WHERE rowid=?", (rid,))
                except sqlite3.OperationalError:
                    use_vec = False
        conn.execute("DELETE FROM chunks WHERE file_id=?", (file_id,))
        for i, ch in enumerate(chunks):
            emb = embeddings[i] if embeddings else None
            cur = conn.execute(
                """
                INSERT INTO chunks(chunk_id, file_id, source, seq, content,
                                   heading_path, doc_meta, embedding)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (ch.chunk_id, file_id, source, ch.seq, ch.content,
                 ch.heading_path, ch.doc_meta, emb),
            )
            rid = cur.lastrowid
            conn.execute(
                "INSERT INTO chunks_fts(rowid, content, chunk_id) VALUES(?,?,?)",
                (rid, fts_texts[i], ch.chunk_id),
            )
            if use_vec and emb is not None:
                try:
                    conn.execute(
                        "INSERT INTO chunks_vec(rowid, embedding) VALUES(?,?)",
                        (rid, emb),
                    )
                except sqlite3.OperationalError:
                    use_vec = False
    return len(chunks)


def delete_file(conn: sqlite3.Connection, file_id: int) -> None:
    """删除快路径：索引与清单同事务 DELETE。"""
    with conn:
        rowids = [
            r[0] for r in conn.execute(
                "SELECT rowid FROM chunks WHERE file_id=?", (file_id,)
            ).fetchall()
        ]
        for rid in rowids:
            conn.execute("DELETE FROM chunks_fts WHERE rowid=?", (rid,))
            try:
                conn.execute("DELETE FROM chunks_vec WHERE rowid=?", (rid,))
            except sqlite3.OperationalError:
                pass
        conn.execute("DELETE FROM chunks WHERE file_id=?", (file_id,))
        conn.execute("DELETE FROM file_manifest WHERE file_id=?", (file_id,))


# ---------------------------------------------------------------------------
# meta kv
# ---------------------------------------------------------------------------

def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO wenqu_meta(key, value) VALUES(?,?)", (key, value)
        )


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    try:
        row = conn.execute("SELECT value FROM wenqu_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return None


def file_counts_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM file_manifest GROUP BY status"
    ).fetchall()
    return {r[0]: int(r[1]) for r in rows}
