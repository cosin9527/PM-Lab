"""检索层：wenqu-mcp 五工具的实现核心。

- keyword 模式：BM25 top-k 直出
- hybrid 模式：BM25 top-50 + 向量 top-50 → RRF(k=60) 融合 → 新近度加权
  （只影响排序，不收缩候选池）→ top-k
- 单文档 ≤3 块防霸榜；max_return_tokens 预算内截断 + has_more
- 内容 hash 相同的本地原件与飞书快照合并呈现（双出处）
- archived 默认排除（include_archived 可显式检索）；孤儿快照保留可搜 + 状态标注
- 每查询直读 SQLite（无内存缓存）——文件落盘即可搜，无需重启客户端
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import sqlite3
from pathlib import Path
from typing import Any

from . import fts
from .config import Config, user_dict_path
from .db import connect, file_counts_by_status, get_meta

ORPHAN_STATUSES = ("revoked", "deleted_remote", "moved_out_of_scope")


# ---------------------------------------------------------------------------
# 候选生成与融合
# ---------------------------------------------------------------------------

def _bm25_rowids(conn: sqlite3.Connection, expr: str, limit: int = 50) -> list[int]:
    if not expr or expr == '""':
        return []
    try:
        rows = conn.execute(
            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? "
            "ORDER BY bm25(chunks_fts) LIMIT ?",
            (expr, limit),
        ).fetchall()
        return [int(r[0]) for r in rows]
    except sqlite3.OperationalError:
        return []


def _numpy_bruteforce(conn: sqlite3.Connection, qvec: bytes, limit: int) -> list[int]:
    """sqlite-vec 不可用时的 numpy 暴力检索降级（该规模可接受）。"""
    import numpy as np

    q = np.frombuffer(qvec, dtype=np.float32)
    rows = conn.execute(
        "SELECT rowid, embedding FROM chunks WHERE embedding IS NOT NULL"
    ).fetchall()
    if not rows:
        return []
    ids = np.array([r[0] for r in rows], dtype=np.int64)
    mat = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32)
    mat = mat.reshape(len(rows), -1)
    norms = np.linalg.norm(mat, axis=1) * np.linalg.norm(q)
    sims = (mat @ q) / np.maximum(norms, 1e-9)
    order = np.argsort(-sims)[:limit]
    return [int(ids[i]) for i in order]


def _vec_rowids(conn: sqlite3.Connection, qvec: bytes, limit: int = 50) -> list[int]:
    try:
        rows = conn.execute(
            "SELECT rowid FROM chunks_vec WHERE embedding MATCH ? AND k = ?",
            (qvec, limit),
        ).fetchall()
        return [int(r[0]) for r in rows]
    except sqlite3.OperationalError:
        return _numpy_bruteforce(conn, qvec, limit)


def _rrf_fuse(bm25_rows: list[int], vec_rows: list[int], rrf_k: int) -> dict[int, float]:
    scores: dict[int, float] = {}
    for i, rid in enumerate(bm25_rows):
        scores[rid] = scores.get(rid, 0.0) + 1.0 / (rrf_k + i + 1)
    for i, rid in enumerate(vec_rows):
        scores[rid] = scores.get(rid, 0.0) + 1.0 / (rrf_k + i + 1)
    return scores


# ---------------------------------------------------------------------------
# 过滤与辅助
# ---------------------------------------------------------------------------

def _file_time_ts(row: sqlite3.Row) -> float:
    """文件时间：快照用 remote_updated_at，本地用 mtime。"""
    if row["source_type"] == "feishu_snapshot" and row["remote_updated_at"]:
        try:
            return _dt.datetime.fromisoformat(row["remote_updated_at"]).timestamp()
        except (ValueError, TypeError):
            pass
    return float(row["mtime"] or 0.0)


def _parse_bound(value: str, end_of_day: bool = False) -> float | None:
    try:
        d = _dt.datetime.fromisoformat(value)
        if len(value) == 10:  # 纯日期
            if end_of_day:
                d = d.replace(hour=23, minute=59, second=59)
        return d.timestamp()
    except (ValueError, TypeError):
        return None


def _iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 主检索
# ---------------------------------------------------------------------------

def search(
    cfg: Config,
    query: str,
    *,
    top_k: int | None = None,
    source_filter: str | None = None,
    max_return_tokens: int = 4096,
    after: str | None = None,
    before: str | None = None,
    include_archived: bool = False,
    embedder: Any = None,
) -> dict[str, Any]:
    data = cfg.data
    r = data["retrieval"]
    top_k = int(top_k or r["top_k"])
    mode = r["mode"]
    degraded = False

    conn = connect(data["storage"]["db"], load_vec=(mode == "hybrid"))
    try:
        # 1) 候选生成
        expr = fts.match_expr(query, user_dict_path())
        bm25_rows = _bm25_rowids(conn, expr, 50)
        vec_rows: list[int] = []
        if mode == "hybrid":
            emb = embedder
            if emb is None:
                from .embed import Embedder

                emb = Embedder(cfg)
            qvec = emb.embed_query(query)
            if qvec is None:
                degraded = True  # 模型失败自动降级纯 BM25（方向一为保底）
                mode = "keyword"
            else:
                vec_rows = _vec_rowids(conn, qvec, 50)

        scores = _rrf_fuse(bm25_rows, vec_rows, int(r["rrf_k"]))
        if not scores:
            return {"results": [], "has_more": False, "mode": mode, "degraded": degraded}

        # 2) 取候选明细
        placeholders = ",".join("?" * len(scores))
        rows = conn.execute(
            f"""
            SELECT c.rowid, c.chunk_id, c.file_id, c.source, c.seq, c.content,
                   c.heading_path, c.doc_meta,
                   f.source_type, f.status, f.mtime, f.remote_updated_at,
                   f.remote_url, f.file_path, f.sha256, f.relative_path
            FROM chunks c JOIN file_manifest f ON c.file_id = f.file_id
            WHERE c.rowid IN ({placeholders})
            """,
            tuple(scores.keys()),
        ).fetchall()

        # 3) 过滤（时间 / 来源 / 归档）
        after_ts = _parse_bound(after) if after else None
        before_ts = _parse_bound(before, end_of_day=True) if before else None
        kept: list[sqlite3.Row] = []
        for row in rows:
            if row["status"] == "archived" and not include_archived:
                continue
            ft = _file_time_ts(row)
            if after_ts is not None and ft < after_ts:
                continue
            if before_ts is not None and ft > before_ts:
                continue
            if source_filter:
                sf = source_filter.strip()
                if sf in ("local", "feishu_snapshot"):
                    if row["source_type"] != sf:
                        continue
                elif sf.lower() not in (row["source"] or "").lower() and sf.lower() not in (
                    row["file_path"] or ""
                ).lower():
                    continue
            kept.append(row)

        # 4) 新近度加权（只影响排序，不收缩候选池）
        alpha = float(r["recency_alpha"])
        tau = float(r["recency_tau_days"])
        now = _dt.datetime.now().timestamp()
        weighted: list[tuple[float, sqlite3.Row]] = []
        for row in kept:
            s = scores[row["rowid"]]
            if alpha > 0:
                age_days = max(0.0, (now - _file_time_ts(row)) / 86400.0)
                s *= 1.0 + alpha * math.exp(-age_days / tau)
            weighted.append((s, row))
        weighted.sort(key=lambda t: -t[0])

        # 5) 单文档 ≤3 块防霸榜
        per_file: dict[int, int] = {}
        capped: list[tuple[float, sqlite3.Row]] = []
        for s, row in weighted:
            n = per_file.get(row["file_id"], 0)
            if n >= 3:
                continue
            per_file[row["file_id"]] = n + 1
            capped.append((s, row))

        # 6) 跨源去重：内容 hash 相同且 file_id 不同（本地原件 + 飞书快照）→
        #    合并双出处（保留分高者）；同文件兄弟块不参与合并
        best_by_sha: dict[str, tuple[float, sqlite3.Row]] = {}
        for s, row in capped:
            sha = row["sha256"]
            if not sha:
                continue
            prev = best_by_sha.get(sha)
            if prev is None or s > prev[0]:
                best_by_sha[sha] = (s, row)
        dual: dict[int, list[str]] = {}
        final: list[tuple[float, sqlite3.Row]] = []
        for s, row in capped:
            sha = row["sha256"]
            entry = best_by_sha.get(sha) if sha else None
            if entry is not None and entry[1]["file_id"] != row["file_id"]:
                dual.setdefault(entry[1]["file_id"], []).append(row["source"])
                continue
            final.append((s, row))
        final.sort(key=lambda t: -t[0])

        # 7) token 预算 + top_k + has_more
        results: list[dict[str, Any]] = []
        used = 0
        has_more = False
        for s, row in final:
            if len(results) >= top_k:
                has_more = True
                break
            cost = len(row["content"] or "")
            if used + cost > max_return_tokens and results:
                has_more = True
                break
            used += cost
            meta = None
            if row["doc_meta"]:
                try:
                    meta = json.loads(row["doc_meta"])
                except (ValueError, TypeError):
                    meta = None
            results.append(
                {
                    "chunk_id": row["chunk_id"],
                    "content": row["content"],
                    "source": row["source"],
                    "source_type": row["source_type"],
                    "status": row["status"] if row["status"] in ORPHAN_STATUSES else None,
                    "heading_path": row["heading_path"],
                    "doc_meta": meta,
                    "rank": len(results) + 1,
                    "file_time": _iso(_file_time_ts(row)),
                    "remote_url": row["remote_url"],
                    "dual_sources": (
                        list(dict.fromkeys(dual.get(row["file_id"], []))) or None
                    ),
                }
            )
        return {"results": results, "has_more": has_more, "mode": mode, "degraded": degraded}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 读取工具（get_chunk / toc / get_section）
# ---------------------------------------------------------------------------

def get_chunk(cfg: Config, chunk_id: str, neighbors: int = 0) -> dict[str, Any] | None:
    conn = connect(cfg.data["storage"]["db"], load_vec=False)
    try:
        row = conn.execute(
            """
            SELECT c.*, f.source_type, f.status, f.remote_url, f.file_path
            FROM chunks c JOIN file_manifest f ON c.file_id = f.file_id
            WHERE c.chunk_id = ?
            """,
            (chunk_id,),
        ).fetchone()
        if row is None:
            return None
        ctx: list[dict[str, Any]] = []
        if neighbors > 0:
            n_rows = conn.execute(
                """
                SELECT chunk_id, seq, content, heading_path FROM chunks
                WHERE file_id = ? AND seq BETWEEN ? AND ?
                ORDER BY seq
                """,
                (row["file_id"], row["seq"] - neighbors, row["seq"] + neighbors),
            ).fetchall()
            ctx = [
                {
                    "chunk_id": r["chunk_id"],
                    "seq": r["seq"],
                    "content": r["content"],
                    "heading_path": r["heading_path"],
                }
                for r in n_rows
                if r["chunk_id"] != chunk_id
            ]
        meta = None
        if row["doc_meta"]:
            try:
                meta = json.loads(row["doc_meta"])
            except (ValueError, TypeError):
                meta = None
        return {
            "chunk_id": row["chunk_id"],
            "content": row["content"],
            "source": row["source"],
            "source_type": row["source_type"],
            "status": row["status"] if row["status"] in ORPHAN_STATUSES else None,
            "heading_path": row["heading_path"],
            "doc_meta": meta,
            "file_path": row["file_path"],
            "remote_url": row["remote_url"],
            "context": ctx,
        }
    finally:
        conn.close()


def _file_by_source(conn: sqlite3.Connection, source: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM file_manifest WHERE relative_path = ? OR file_path = ?",
        (source, source),
    ).fetchone()


def toc(cfg: Config, source: str) -> list[dict[str, Any]]:
    conn = connect(cfg.data["storage"]["db"], load_vec=False)
    try:
        f = _file_by_source(conn, source)
        if f is None:
            return []
        rows = conn.execute(
            "SELECT chunk_id, seq, content, heading_path, doc_meta FROM chunks "
            "WHERE file_id = ? ORDER BY seq",
            (f["file_id"],),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            meta = None
            if r["doc_meta"]:
                try:
                    meta = json.loads(r["doc_meta"])
                except (ValueError, TypeError):
                    meta = None
            out.append(
                {
                    "seq": r["seq"],
                    "chunk_id": r["chunk_id"],
                    "heading_path": r["heading_path"],
                    "doc_meta": meta,
                    "preview": (r["content"] or "")[:80],
                }
            )
        return out
    finally:
        conn.close()


def get_section(cfg: Config, source: str, section_id: str) -> dict[str, Any] | None:
    """section_id 为数字 = chunk seq；否则按 heading_path 匹配。

    返回该章节全部 chunk 的拼接内容（防截断语义反转：重要结论引用前读完整章节）。
    """
    conn = connect(cfg.data["storage"]["db"], load_vec=False)
    try:
        f = _file_by_source(conn, source)
        if f is None:
            return None
        if str(section_id).isdigit():
            base = conn.execute(
                "SELECT * FROM chunks WHERE file_id = ? AND seq = ?",
                (f["file_id"], int(section_id)),
            ).fetchone()
            if base is None:
                return None
            if base["heading_path"]:
                rows = conn.execute(
                    "SELECT * FROM chunks WHERE file_id = ? AND heading_path = ? ORDER BY seq",
                    (f["file_id"], base["heading_path"]),
                ).fetchall()
            else:
                rows = [base]
        else:
            rows = conn.execute(
                "SELECT * FROM chunks WHERE file_id = ? AND heading_path = ? ORDER BY seq",
                (f["file_id"], str(section_id)),
            ).fetchall()
            if not rows:
                return None
        content = "\n\n".join((r["content"] or "") for r in rows)
        return {
            "source": f["relative_path"],
            "heading_path": rows[0]["heading_path"],
            "seq_range": [rows[0]["seq"], rows[-1]["seq"]],
            "content": content,
            "file_path": f["file_path"],
            "remote_url": f["remote_url"],
            "status": f["status"] if f["status"] in ORPHAN_STATUSES else None,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 状态汇总（wenqu_status / wenqu status / verify 共用）
# ---------------------------------------------------------------------------

def status(cfg: Config, embedder: Any = None) -> dict[str, Any]:
    data = cfg.data
    r = data["retrieval"]
    conn = connect(data["storage"]["db"], load_vec=False)
    try:
        # 只读路径必须容忍未初始化的空库（wenqu init / sync 未跑过时不崩溃）
        initialized = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='file_manifest'"
        ).fetchone()
        if not initialized:
            counts: dict[str, int] = {}
            chunks_total = fts_covered = vec_covered = files_total = 0
        else:
            counts = file_counts_by_status(conn)
            chunks_total = int(
                conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            )
            fts_covered = int(
                conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
            )
            vec_covered = int(
                conn.execute(
                    "SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL"
                ).fetchone()[0]
            )
            files_total = int(
                conn.execute("SELECT COUNT(*) FROM file_manifest").fetchone()[0]
            )
        return {
            "watch_dirs": [str(p) for p in data["local"]["watch"]],
            "files_total": files_total,
            "files_by_status": counts,
            "chunks_total": chunks_total,
            "fts_coverage": (
                f"{fts_covered}/{chunks_total}" if chunks_total else "0/0"
            ),
            "vec_coverage": (
                f"{vec_covered}/{chunks_total}"
                if r["mode"] == "hybrid" and chunks_total
                else None
            ),
            "mode": r["mode"],
            "embed_model": r["embed_model"] if r["mode"] == "hybrid" else None,
            "last_sync": get_meta(conn, "last_sync"),
            "last_feishu_snapshot": get_meta(conn, "last_feishu_snapshot"),
            "archive_candidates": counts.get("archive_candidate", 0),
            "orphans": {
                s: counts.get(s, 0) for s in ORPHAN_STATUSES
            },
            "needs_ocr": counts.get("needs_ocr", 0),
            "failed": counts.get("failed", 0),
            "degraded": bool(embedder.degraded) if embedder is not None else False,
        }
    finally:
        conn.close()
