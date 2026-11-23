"""wenqu-sync 守护进程：唯一索引写入者（launchd 常驻）。

机制清单（对应实施方案 §A.2 sync/daemon.py 契约）：
- 事件源：Watchdog(FSEvents) + 30 分钟全量扫描 + 睡眠唤醒补扫 + 启动对账全扫
- 写入稳定判定：事件后 size/mtime 5 秒不变（防抖）
- 变化判定：size+mtime 快筛 → SHA-256 定案
- 每文件 5 分钟冷却 + 冷却到期主动补检（有变化立即处理，不等全扫）
- 身份锚定：file_id 稳定；rename/移动只 UPDATE 路径与展示字段（不重解析/不重嵌入）
- 删除快路径：单文件消失 5 分钟无恢复 → 索引与清单同事务 DELETE；
  目录级批量消失（>30%）视为网盘断连 → 冻结不清 + 告警
- 云盘路径黑名单：启动时检测（iCloud/OneDrive/Dropbox），命中拒绝启动
- 归档候选检测：名称模式 / 同前缀多版本；只标候选，确认权留给人
- 向量阶段（hybrid）：FTS 先行（入库即可搜）；embedding ≥32 块或 10 秒窗口批量推理；
  每轮全扫做嵌入覆盖对账（补 embedded_at IS NULL，防向量黑洞）
- 配置热重载：每轮任务前 stat config.yaml
"""

from __future__ import annotations

import datetime as _dt
import fnmatch
import hashlib
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import db, fts
from ..chunker import chunk_document
from ..config import (
    ARCHIVE_PATTERNS,
    Config,
    SUPPORTED_MODELS,
    load_config,
    user_dict_path,
    watch_dirs,
    wenqu_home,
)
from ..embed import Embedder
from ..parsers import ParseError, SkippedFile, extract

DEBOUNCE_SECONDS = 5.0        # 事件后 size/mtime 稳定窗口
FULL_SCAN_INTERVAL = 1800.0   # 30 分钟全量扫描
COOLDOWN_SECONDS = 300.0      # 每文件 5 分钟冷却
DELETE_GRACE_SECONDS = 300.0  # 删除宽限：5 分钟无恢复即清
WAKE_GAP_SECONDS = 120.0      # 时钟跳变 >2 分钟视为睡眠唤醒 → 补扫
VEC_BATCH = 32                # 向量回填批量阈值
VEC_WINDOW_SECONDS = 10.0     # 向量回填时间窗口
MASS_DISAPPEAR_RATIO = 0.3    # 批量消失冻结阈值

log = logging.getLogger("wenqu.sync")


# ---------------------------------------------------------------------------
# 运行时状态
# ---------------------------------------------------------------------------

@dataclass
class SyncRuntime:
    cfg: Config
    conn: sqlite3.Connection
    embedder: Embedder
    pending: dict[str, float] = field(default_factory=dict)        # path → 事件时间
    cooldown_until: dict[str, float] = field(default_factory=dict)  # path → 冷却截止
    recheck: dict[str, float] = field(default_factory=dict)         # path → 补检到期
    missing_since: dict[str, float] = field(default_factory=dict)   # path → 消失时刻
    vec_queue: list[tuple[int, str]] = field(default_factory=list)  # (rowid, content)
    vec_since: float | None = None
    last_scan: float = 0.0
    observed_dirs: set[str] = field(default_factory=set)

    @property
    def user_dict(self) -> Path:
        return user_dict_path()


def setup_logging(logs_dir: Path) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    log.setLevel(logging.INFO)
    if not log.handlers:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)
        fh = logging.FileHandler(str(logs_dir / "sync.log"), encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)


def init_runtime(cfg: Config | None = None) -> SyncRuntime:
    cfg = cfg or load_config()
    data = cfg.data
    conn = db.connect(data["storage"]["db"], load_vec=(data["retrieval"]["mode"] == "hybrid"))
    db.init_schema(conn)
    if data["retrieval"]["mode"] == "hybrid":
        db.ensure_vec_table(conn, SUPPORTED_MODELS[data["retrieval"]["embed_model"]]["dim"])
    setup_logging(Path(data["storage"]["logs"]))
    return SyncRuntime(cfg=cfg, conn=conn, embedder=Embedder(cfg))


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def cloud_roots() -> list[Path]:
    """云盘同步目录根（iCloud / OneDrive / Dropbox）。"""
    home = Path.home()
    roots = [
        home / "Library" / "Mobile Documents",   # iCloud
        home / "Library" / "CloudStorage",       # OneDrive/Dropbox 等现代布局
        home / "Dropbox",
        home / "OneDrive",
    ]
    cs = home / "Library" / "CloudStorage"
    if cs.exists():
        roots.extend(p for p in cs.iterdir() if p.is_dir())
    return roots


def is_under_cloud(path: Path) -> bool:
    try:
        p = path.resolve()
        for root in cloud_roots():
            if p.is_relative_to(root.resolve()):
                return True
    except OSError:
        pass
    return False


def probe_dir_writable(d: Path) -> tuple[bool, str]:
    """TCC 写入探测：受保护路径（Desktop/Documents/Downloads）会失败。"""
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".wenqu_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.read_text(encoding="utf-8")
        probe.unlink()
        return True, ""
    except PermissionError:
        return False, "无写入权限（可能需要系统设置→隐私与安全性→完全磁盘访问）"
    except OSError as e:
        return False, str(e)


def _is_excluded(rt: SyncRuntime, path: Path) -> bool:
    parts = [p.lower() for p in path.parts]
    for pat in rt.cfg.data["local"]["exclude"]:
        pat_l = pat.lower()
        if any(fnmatch.fnmatch(part, pat_l) for part in parts):
            return True
    return False


def _is_sensitive(rt: SyncRuntime, path: Path) -> bool:
    for sp in rt.cfg.data["index"]["sensitive_paths"]:
        try:
            if path.is_relative_to(Path(str(sp)).expanduser().resolve()):
                return True
        except (OSError, ValueError):
            continue
    return False


def _allowed(rt: SyncRuntime, path: Path) -> bool:
    if _is_excluded(rt, path) or _is_sensitive(rt, path):
        return False
    if path.name.endswith(".icloud"):
        return False
    return path.suffix.lower() in rt.cfg.data["local"]["extensions"]


def _relative(rt: SyncRuntime, path: Path) -> str:
    roots = watch_dirs(rt.cfg) + [wenqu_home() / "Documents"]
    for root in roots:
        try:
            return str(path.relative_to(root))
        except ValueError:
            continue
    return path.name


# ---------------------------------------------------------------------------
# 索引路径（parse → chunk → FTS 入库；向量留给回填泵）
# ---------------------------------------------------------------------------

def index_file(
    rt: SyncRuntime,
    path: Path,
    *,
    source_type: str = "local",
    remote_token: str | None = None,
    remote_url: str | None = None,
    remote_updated_at: str | None = None,
) -> bool:
    """解析并入库一个文件。返回是否进入 manifest。"""
    path = Path(path)
    if not _allowed(rt, path) or not path.is_file():
        return False
    st = path.stat()
    rel = _relative(rt, path)
    sha = sha256_file(path)

    # rename/移动锚定（事件快速路径）：同 sha256 的本地条目存在、路径不同、
    # 且旧路径已从盘上消失（区分 rename 与 copy）→ 只 UPDATE 路径与展示字段，
    # 不重解析、不重嵌入、file_id / chunk_id 稳定。与全扫 _reconcile_moves 双保险。
    if source_type == "local":
        anchor = db.get_file_by_sha(rt.conn, sha, source_type="local")
        if (
            anchor is not None
            and anchor["file_path"] != str(path)
            and not Path(anchor["file_path"]).exists()
        ):
            if _anchor_move(rt, anchor, path, st):
                return True

    try:
        doc = extract(path)
    except SkippedFile as e:
        log.info("跳过 %s（%s）", path, e.status)
        return False
    except ParseError as e:
        file_id = db.upsert_file(
            rt.conn, file_path=str(path), relative_path=rel, file_size=st.st_size,
            mtime=st.st_mtime, sha256=sha, source_type=source_type,
            remote_token=remote_token, remote_url=remote_url,
            remote_updated_at=remote_updated_at, status="failed",
        )
        db.mark_file_status(rt.conn, file_id, "failed", error=str(e))
        log.warning("解析失败 %s: %s", path, e)
        return True

    if doc.status != "ok":
        # needs_ocr / skipped_legacy / skipped_oversize：诚实上报，不假装可搜
        db.upsert_file(
            rt.conn, file_path=str(path), relative_path=rel, file_size=st.st_size,
            mtime=st.st_mtime, sha256=sha, source_type=source_type,
            remote_token=remote_token, remote_url=remote_url,
            remote_updated_at=remote_updated_at, status=doc.status,
        )
        log.info("标记 %s → %s", path.name, doc.status)
        return True

    file_id = db.upsert_file(
        rt.conn, file_path=str(path), relative_path=rel, file_size=st.st_size,
        mtime=st.st_mtime, sha256=sha, source_type=source_type,
        remote_token=remote_token, remote_url=remote_url,
        remote_updated_at=remote_updated_at, status="parsing",
    )
    chunks = chunk_document(file_id, doc.sections)
    fts_texts = [fts.tokenize(c.content, rt.user_dict) for c in chunks]
    db.replace_file_chunks(rt.conn, file_id, rel, chunks, fts_texts)
    db.mark_file_status(rt.conn, file_id, "indexed", indexed=True)
    rt.cooldown_until[str(path)] = time.time() + COOLDOWN_SECONDS
    log.info("已索引 %s（%d 块）", rel, len(chunks))
    return True


def needs_reindex(rt: SyncRuntime, path: Path) -> bool:
    """size+mtime 快筛 → SHA-256 定案。"""
    try:
        st = path.stat()
    except OSError:
        return False
    row = db.get_file_by_path(rt.conn, str(path))
    if row is None:
        return True
    if int(row["file_size"] or 0) == st.st_size and abs(
        float(row["mtime"] or 0) - st.st_mtime
    ) < 1e-6:
        return False
    if sha256_file(path) == row["sha256"]:
        with rt.conn:
            rt.conn.execute(
                "UPDATE file_manifest SET mtime=?, file_size=? WHERE file_id=?",
                (st.st_mtime, st.st_size, row["file_id"]),
            )
        return False
    return True


# ---------------------------------------------------------------------------
# 全量扫描 + rename 对账 + 删除处理
# ---------------------------------------------------------------------------

def _local_rows(rt: SyncRuntime) -> list[sqlite3.Row]:
    return rt.conn.execute(
        "SELECT * FROM file_manifest WHERE source_type='local'"
    ).fetchall()


def _under(path: str, root: Path) -> bool:
    try:
        return Path(path).is_relative_to(root)
    except (OSError, ValueError):
        return False


def scan_once(rt: SyncRuntime) -> dict[str, Any]:
    """全量扫描（启动对账 / 周期 / 唤醒补扫共用）。"""
    t0 = time.time()
    summary = {"scanned": 0, "indexed": 0, "missing": 0, "purged": 0, "moves": 0}
    for d in watch_dirs(rt.cfg):
        _scan_dir(rt, Path(d), summary)
    detect_archive_candidates(rt)
    backfill_embeddings(rt)
    purge_missing(rt)
    rt.last_scan = time.time()
    db.set_meta(rt.conn, "last_sync", db.now_iso())
    log.info(
        "全扫完成：扫描 %d / 新索引 %d / 缺失 %d / 清除 %d / rename %d（%.1fs）",
        summary["scanned"], summary["indexed"], summary["missing"],
        summary["purged"], summary["moves"], time.time() - t0,
    )
    return summary


def _scan_dir(rt: SyncRuntime, root: Path, summary: dict[str, Any]) -> None:
    if not root.exists():
        log.warning("watch 目录不存在，本轮跳过：%s", root)
        return
    disk: dict[str, Path] = {}
    for p in root.rglob("*"):
        try:
            if p.is_file() and _allowed(rt, p):
                disk[str(p)] = p
        except OSError:
            continue
    summary["scanned"] += len(disk)

    rows = [r for r in _local_rows(rt) if _under(r["file_path"], root)]
    entries = {r["file_path"]: r for r in rows}

    new_paths = [p for p in disk if p not in entries]
    missing_rows = [entries[p] for p in entries if p not in disk]

    # 批量消失防护（>30% 视为网盘断连/权限故障：冻结、仅告警、不删除）
    frozen = False
    if entries and len(missing_rows) > MASS_DISAPPEAR_RATIO * len(entries):
        log.warning(
            "目录 %s 单轮消失 %d/%d（>30%%），疑似网盘断连，本轮冻结不清",
            root, len(missing_rows), len(entries),
        )
        frozen = True

    if not frozen:
        for row in missing_rows:
            rt.missing_since.setdefault(row["file_path"], time.time())
            with rt.conn:
                rt.conn.execute(
                    "UPDATE file_manifest SET status='missing' WHERE file_id=? AND status='indexed'",
                    (row["file_id"],),
                )
        summary["missing"] += len(missing_rows)

    # rename/移动对账：身份锚 sha256（只 UPDATE 路径，不重解析）
    consumed = _reconcile_moves(rt, missing_rows, disk, summary)

    for p in new_paths:
        if str(p) in consumed:
            continue
        if index_file(rt, p):
            summary["indexed"] += 1

    now = time.time()
    for p in disk.values():
        sp = str(p)
        if sp in entries and now >= rt.cooldown_until.get(sp, 0) and needs_reindex(rt, p):
            index_file(rt, p)
        elif (
            sp in entries
            and entries[sp]["status"] == "missing"
            and not needs_reindex(rt, p)
        ):
            # 在盘上且内容未变，但状态遗留 missing（锚定前标记/守护重启窗口）
            # → 恢复 indexed
            with rt.conn:
                rt.conn.execute(
                    "UPDATE file_manifest SET status='indexed' "
                    "WHERE file_id=? AND status='missing'",
                    (entries[sp]["file_id"],),
                )
        elif sp in entries and now < rt.cooldown_until.get(sp, 0):
            rt.recheck[sp] = rt.cooldown_until[sp]  # 冷却到期主动补检


def _anchor_move(
    rt: SyncRuntime, row: sqlite3.Row, new_path: Path, st: os.stat_result
) -> bool:
    """rename/移动锚定：把 missing 条目的身份锚转移到 new_path。

    目标路径被其他条目占用时：同 sha256 视为重复入库的脏数据（清除占用
    条目，保留本条目原 file_id——引用不断裂）；内容不同则放弃锚定。"""
    pstr = str(new_path)
    occ = db.get_file_by_path(rt.conn, pstr)
    if occ is not None and occ["file_id"] != row["file_id"]:
        if occ["sha256"] != row["sha256"]:
            return False
        db.delete_file(rt.conn, occ["file_id"])
        log.info(
            "锚定前清除重复条目 file_id=%d（%s，同 sha256 重复入库）",
            occ["file_id"], pstr,
        )
    rel = _relative(rt, new_path)
    db.update_file_path(
        rt.conn, row["file_id"],
        file_path=pstr, relative_path=rel, source=rel,
    )
    with rt.conn:
        rt.conn.execute(
            "UPDATE file_manifest SET mtime=?, file_size=?, "
            "status=CASE WHEN status='missing' THEN 'indexed' ELSE status END "
            "WHERE file_id=?",
            (st.st_mtime, st.st_size, row["file_id"]),
        )
    rt.missing_since.pop(row["file_path"], None)
    rt.cooldown_until[pstr] = time.time() + COOLDOWN_SECONDS
    log.info("rename 锚定：%s → %s（file_id=%d 保留）", row["file_path"], rel, row["file_id"])
    return True


def _reconcile_moves(
    rt: SyncRuntime,
    missing_rows: list[sqlite3.Row],
    disk: dict[str, Path],
    summary: dict[str, Any],
) -> set[str]:
    consumed: set[str] = set()
    for row in missing_rows:
        for pstr, p in disk.items():
            if pstr in consumed:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if int(row["file_size"] or -1) != st.st_size:
                continue
            if sha256_file(p) == row["sha256"]:
                if _anchor_move(rt, row, p, st):
                    consumed.add(pstr)
                    summary["moves"] += 1
                break
    return consumed


def purge_missing(rt: SyncRuntime) -> None:
    """删除快路径：单文件消失 5 分钟无恢复 → 同事务清除（无幽灵引用）。"""
    now = time.time()
    for path, since in list(rt.missing_since.items()):
        if now - since < DELETE_GRACE_SECONDS:
            continue
        if Path(path).exists():  # 恢复了
            rt.missing_since.pop(path, None)
            continue
        row = db.get_file_by_path(rt.conn, path)
        if row is not None:
            db.delete_file(rt.conn, row["file_id"])
            log.info("删除清除：%s", path)
        rt.missing_since.pop(path, None)


# ---------------------------------------------------------------------------
# 归档候选检测（只标候选，确认权留给人）
# ---------------------------------------------------------------------------

_VERSION_SUFFIX = re.compile(r"[-_. ]v(\d+)$", re.IGNORECASE)


def detect_archive_candidates(rt: SyncRuntime) -> None:
    rows = rt.conn.execute(
        "SELECT file_id, relative_path, mtime FROM file_manifest "
        "WHERE source_type='local' AND status='indexed'"
    ).fetchall()
    # 1) 名称模式
    for row in rows:
        low = (row["relative_path"] or "").lower()
        if any(pat in low for pat in ARCHIVE_PATTERNS):
            db.mark_file_status(rt.conn, row["file_id"], "archive_candidate")
    # 2) 同前缀多版本共存（旧版标候选）
    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        p = Path(row["relative_path"] or "")
        m = _VERSION_SUFFIX.search(p.stem)
        if m:
            base = _VERSION_SUFFIX.sub("", p.stem)
            groups.setdefault((str(p.parent), base.lower()), []).append(row)
    for _, grp in groups.items():
        if len(grp) < 2:
            continue

        def _ver(r: sqlite3.Row) -> tuple[int, float]:
            p = Path(r["relative_path"] or "")
            m = _VERSION_SUFFIX.search(p.stem)
            return (int(m.group(1)) if m else 0, float(r["mtime"] or 0))

        ordered = sorted(grp, key=_ver)
        for row in ordered[:-1]:
            db.mark_file_status(rt.conn, row["file_id"], "archive_candidate")


# ---------------------------------------------------------------------------
# 向量回填（FTS 先行；≥32 块或 10 秒窗口批量；每轮全扫做覆盖对账）
# ---------------------------------------------------------------------------

def _vec_dim_ok(rt: SyncRuntime) -> bool:
    r = rt.cfg.data["retrieval"]
    if r["mode"] != "hybrid":
        return False
    want = str(SUPPORTED_MODELS[r["embed_model"]]["dim"])
    have = db.get_meta(rt.conn, "vec_dim")
    if have != want:
        if not db.ensure_vec_table(rt.conn, int(want)):
            return False
        return True
    return db.vec_available(rt.conn)


def backfill_embeddings(rt: SyncRuntime) -> None:
    if rt.cfg.data["retrieval"]["mode"] != "hybrid":
        return
    if not _vec_dim_ok(rt):
        log.warning("向量维度不匹配（模型已切换？），回填暂停——请执行 wenqu reembed")
        return
    # 覆盖对账：捞取未嵌入块（防崩溃/跳批留下的向量黑洞）
    if not rt.vec_queue:
        rows = rt.conn.execute(
            """
            SELECT c.rowid, c.content FROM chunks c
            JOIN file_manifest f ON c.file_id = f.file_id
            WHERE c.embedding IS NULL AND f.status = 'indexed'
            LIMIT 256
            """
        ).fetchall()
        if rows:
            rt.vec_queue = [(int(r[0]), r[1]) for r in rows]
            rt.vec_since = time.time()
    if not rt.vec_queue:
        return
    if len(rt.vec_queue) < VEC_BATCH and (rt.vec_since or 0) + VEC_WINDOW_SECONDS > time.time():
        return
    batch = rt.vec_queue[:256]
    embs = rt.embedder.embed_passages([t for _, t in batch])
    if embs is None:
        return  # degraded：下轮重试
    use_vec = db.vec_available(rt.conn)
    with rt.conn:
        for (rid, _), emb in zip(batch, embs):
            rt.conn.execute("UPDATE chunks SET embedding=? WHERE rowid=?", (emb, rid))
            if use_vec:
                try:
                    rt.conn.execute(
                        "INSERT OR REPLACE INTO chunks_vec(rowid, embedding) VALUES(?,?)",
                        (rid, emb),
                    )
                except sqlite3.OperationalError:
                    use_vec = False
    rt.vec_queue = rt.vec_queue[len(batch):]
    if not rt.vec_queue:
        rt.vec_since = None
        with rt.conn:
            rt.conn.execute(
                """
                UPDATE file_manifest SET embedded_at=?
                WHERE embedded_at IS NULL
                  AND NOT EXISTS (SELECT 1 FROM chunks
                                  WHERE file_id = file_manifest.file_id
                                    AND embedding IS NULL)
                """,
                (db.now_iso(),),
            )


# ---------------------------------------------------------------------------
# watchdog 事件接入
# ---------------------------------------------------------------------------

def make_handler(rt: SyncRuntime):
    from watchdog.events import FileSystemEventHandler

    class Handler(FileSystemEventHandler):
        def _push(self, kind: str, path: str) -> None:
            p = Path(path)
            if p.name.endswith(".icloud") or not _allowed(rt, p):
                return
            rt.pending[str(p)] = time.time()
            if kind == "delete":
                rt.missing_since.setdefault(str(p), time.time())

        def on_created(self, event) -> None:
            if not event.is_directory:
                self._push("create", event.src_path)

        def on_modified(self, event) -> None:
            if not event.is_directory:
                self._push("modify", event.src_path)

        def on_moved(self, event) -> None:
            # 只入队，不触 DB：watchdog emitter 线程使用主线程的 SQLite 连接
            # 会抛 ProgrammingError 并杀死事件线程（此后所有文件事件静默失效）。
            # rename 锚定由主循环 process_pending → index_file 的 sha256
            # 锚定分支完成（同锚定效果，且线程安全）。
            self._push("delete", event.src_path)
            self._push("create", event.dest_path)

        def on_deleted(self, event) -> None:
            if not event.is_directory:
                self._push("delete", event.src_path)

    return Handler()


def process_pending(rt: SyncRuntime) -> None:
    """防抖处理：事件后 5 秒稳定才动。"""
    now = time.time()
    for path, t in list(rt.pending.items()):
        if now - t < DEBOUNCE_SECONDS:
            continue
        del rt.pending[path]
        p = Path(path)
        if p.exists() and p.is_file():
            if path in rt.missing_since:  # 恢复
                rt.missing_since.pop(path, None)
                row = db.get_file_by_path(rt.conn, path)
                if row and row["status"] == "missing":
                    db.mark_file_status(rt.conn, row["file_id"], "indexed")
            if not _allowed(rt, p):
                continue
            if now < rt.cooldown_until.get(path, 0):
                rt.recheck[path] = rt.cooldown_until[path]
                continue
            if needs_reindex(rt, p):
                index_file(rt, p)
        else:
            rt.missing_since.setdefault(path, now)


def process_rechecks(rt: SyncRuntime) -> None:
    """冷却到期主动补检（有变化立即处理，不等全扫）。"""
    now = time.time()
    for path, due in list(rt.recheck.items()):
        if now < due:
            continue
        del rt.recheck[path]
        p = Path(path)
        if p.exists() and p.is_file() and needs_reindex(rt, p):
            index_file(rt, p)


def _feishu_due(rt: SyncRuntime) -> bool:
    data = rt.cfg.data
    if not data["feishu"]["watch"]:
        return False
    period = 86400.0 if data["feishu"]["sync_interval"] == "daily" else 3600.0
    last = db.get_meta(rt.conn, "last_feishu_run")
    if last is None:
        return True
    try:
        return time.time() - _dt.datetime.fromisoformat(last).timestamp() > period
    except ValueError:
        return True


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------

def _resync_observer(rt: SyncRuntime, observer) -> None:
    """watch add/remove 后的热重载：对比 config 与已注册目录，重建 observer。"""
    if observer is None:
        return
    want = {str(d) for d in watch_dirs(rt.cfg)}
    if want == rt.observed_dirs:
        return
    try:
        observer.stop()
        observer.join(timeout=5)
    except Exception:
        pass


def run_daemon() -> None:
    """wenqu sync daemon 入口（launchd 拉起）。"""
    cfg = load_config()
    setup_logging(Path(cfg.data["storage"]["logs"]))
    data = cfg.data

    if not data["local"]["watch"]:
        log.warning("local.watch 为空：不索引任何本地文件（仅显式声明模式）")

    # 云盘黑名单：启动检测，命中拒绝启动
    for d in watch_dirs(cfg):
        if is_under_cloud(Path(d)):
            raise SystemExit(
                f"拒绝启动：watch 目录落在云盘同步区内（{d}）——存在占位文件触发"
                "全量下载与误删风险。请改用本地目录（推荐 $WENQU_HOME/Documents/）"
            )

    rt = init_runtime(cfg)
    scan_once(rt)  # 启动对账全扫（兜住崩溃窗口）

    observer = None
    try:
        from watchdog.observers import Observer

        handler = make_handler(rt)
        observer = Observer()
        for d in watch_dirs(cfg):
            observer.schedule(handler, str(d), recursive=True)
            rt.observed_dirs.add(str(d))
        observer.start()
    except Exception as e:  # watchdog 不可用 → 退化为纯周期扫描
        log.warning("watchdog 不可用，退化为周期扫描：%s", e)
        observer = None

    log.info("wenqu-sync 已启动（pid=%d）", os.getpid())
    last_tick = time.time()
    try:
        while True:
            time.sleep(1.0)
            now = time.time()
            if now - last_tick > WAKE_GAP_SECONDS:  # 睡眠唤醒补扫
                log.info("检测到唤醒，补扫")
                scan_once(rt)
            last_tick = now

            if cfg.reload_if_changed():  # 配置热重载
                log.info("配置已热重载")
                _resync_observer(rt, observer)

            process_pending(rt)
            process_rechecks(rt)
            purge_missing(rt)

            if now - rt.last_scan > FULL_SCAN_INTERVAL:
                scan_once(rt)

            if _feishu_due(rt):
                try:
                    from .feishu import feishu_sync_cycle

                    feishu_sync_cycle(rt)
                except Exception as e:
                    log.warning("feishu 周期任务失败（不阻塞本地同步）：%s", e)
                db.set_meta(rt.conn, "last_feishu_run", db.now_iso())

            backfill_embeddings(rt)
            rt.embedder.maybe_unload()
    except KeyboardInterrupt:
        log.info("收到中断，退出")
    finally:
        if observer is not None:
            observer.stop()
            observer.join(timeout=5)
