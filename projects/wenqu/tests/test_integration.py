"""集成测试（tmpdir 假语料 + 真实同步核心函数）。

覆盖实施方案 §A.3 八项：首扫 / 新增 / 修改 / rename / 删除 / 幂等 /
WAL 并发 / 降级（降级在 test_search.py），另含批量消失冻结、归档候选、
敏感排除、云盘黑名单、防抖与冷却补检。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from wenqu import db
from wenqu.config import load_config
from wenqu.search import search as do_search
from wenqu.sync import daemon as dm


def _docs(wenqu_env) -> Path:
    return wenqu_env / "Documents"


# 1. 首扫入库 + FTS 可搜 ---------------------------------------------------

def test_first_scan_and_search(runtime, wenqu_env):
    docs = _docs(wenqu_env)
    (docs / "a.md").write_text("# 甲\n\n达人管理方案，支持好友关系监控。", encoding="utf-8")
    (docs / "b.md").write_text("# 乙\n\n会议结论：评审通过。", encoding="utf-8")
    s = dm.scan_once(runtime)
    assert s["scanned"] == 2 and s["indexed"] == 2
    res = do_search(load_config(), "好友关系监控")
    assert any("达人管理" in r["content"] for r in res["results"])


# 2/3. 新增与修改 ----------------------------------------------------------

def test_add_and_modify(runtime, wenqu_env):
    docs = _docs(wenqu_env)
    f = docs / "c.md"
    f.write_text("第一版内容 关键词alpha 存在", encoding="utf-8")
    dm.index_file(runtime, f)
    assert do_search(load_config(), "alpha")["results"]

    f.write_text("第二版内容 关键词beta 存在", encoding="utf-8")
    os.utime(f, None)
    dm.index_file(runtime, f)
    assert do_search(load_config(), "beta")["results"]
    assert do_search(load_config(), "alpha")["results"] == []  # 旧内容无残留


# 4. rename → 引用不断裂、不重解析 ------------------------------------------

def test_rename_preserves_identity(runtime, wenqu_env):
    docs = _docs(wenqu_env)
    src = docs / "原名.md"
    src.write_text("# 标题\n\nrename 身份锚测试内容。", encoding="utf-8")
    dm.index_file(runtime, src)
    conn = runtime.conn
    row = db.get_file_by_path(conn, str(src))
    fid = row["file_id"]
    chunk_ids = [
        r[0] for r in conn.execute(
            "SELECT chunk_id FROM chunks WHERE file_id=? ORDER BY seq", (fid,)
        ).fetchall()
    ]

    dst = docs / "新名.md"
    src.rename(dst)
    dm.scan_once(runtime)  # rename 对账（sha 身份锚）

    row2 = db.get_file_by_path(conn, str(dst))
    assert row2 is not None and row2["file_id"] == fid
    assert db.get_file_by_path(conn, str(src)) is None
    ids2 = [
        r[0] for r in conn.execute(
            "SELECT chunk_id FROM chunks WHERE file_id=? ORDER BY seq", (fid,)
        ).fetchall()
    ]
    assert ids2 == chunk_ids, "chunk_id 不变（引用不断裂）"
    n = conn.execute(
        "SELECT COUNT(*) FROM file_manifest WHERE source_type='local'"
    ).fetchone()[0]
    assert n == 1, "不产生重复清单行"


# 5. 删除快路径 -------------------------------------------------------------

def test_delete_purge_after_grace(runtime, wenqu_env):
    docs = _docs(wenqu_env)
    f = docs / "d.md"
    f.write_text("待删除内容 关键词ghost", encoding="utf-8")
    dm.index_file(runtime, f)
    f.unlink()
    # 事件路径：刚消失 → 宽限内不清
    runtime.missing_since[str(f)] = time.time()
    dm.purge_missing(runtime)
    assert db.get_file_by_path(runtime.conn, str(f)) is not None
    # 宽限已过（5 分钟无恢复）→ 同事务清除
    runtime.missing_since[str(f)] = time.time() - 301
    dm.purge_missing(runtime)
    assert db.get_file_by_path(runtime.conn, str(f)) is None
    assert do_search(load_config(), "ghost")["results"] == []  # 无幽灵引用


# 6. 重复扫描幂等 -----------------------------------------------------------

def test_idempotent_rescan(runtime, wenqu_env):
    docs = _docs(wenqu_env)
    (docs / "x.md").write_text("幂等测试 内容一", encoding="utf-8")
    (docs / "y.md").write_text("幂等测试 内容二", encoding="utf-8")
    dm.scan_once(runtime)
    n1 = runtime.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    s2 = dm.scan_once(runtime)
    n2 = runtime.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert n1 == n2 > 0
    assert s2["indexed"] == 0


# 7. WAL 并发：写者唯一 + 并发读不阻塞 --------------------------------------

def test_wal_concurrency(runtime, wenqu_env):
    docs = _docs(wenqu_env)
    (docs / "w.md").write_text("并发测试 内容", encoding="utf-8")
    dm.index_file(runtime, docs / "w.md")
    conn2 = db.connect(load_config().data["storage"]["db"], load_vec=False)
    n = conn2.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert n > 0, "第二个连接可见已提交写入（WAL）"
    # conn2 写入 + runtime 读：WAL 读写不阻塞
    db.set_meta(conn2, "probe_key", "ok")
    assert db.get_meta(runtime.conn, "probe_key") == "ok"
    conn2.close()


# 批量消失冻结（>30% 疑似网盘断连）------------------------------------------

def test_mass_disappear_freeze(runtime, wenqu_env):
    docs = _docs(wenqu_env)
    for i in range(3):
        (docs / f"m{i}.md").write_text(f"冻结测试 内容{i}", encoding="utf-8")
    dm.scan_once(runtime)
    (docs / "m0.md").unlink()
    (docs / "m1.md").unlink()  # 2/3 = 67% > 30%
    dm.scan_once(runtime)
    dm.purge_missing(runtime)
    n = runtime.conn.execute(
        "SELECT COUNT(*) FROM file_manifest WHERE source_type='local'"
    ).fetchone()[0]
    assert n == 3, "冻结不清（疑网盘故障，绝不批量误删）"


# 归档候选（同前缀多版本）----------------------------------------------------

def test_archive_candidates(runtime, wenqu_env):
    docs = _docs(wenqu_env)
    (docs / "report v1.md").write_text("版本共存测试 第一版", encoding="utf-8")
    (docs / "report v2.md").write_text("版本共存测试 第二版", encoding="utf-8")
    dm.scan_once(runtime)
    conn = runtime.conn
    st = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT relative_path, status FROM file_manifest WHERE source_type='local'"
        ).fetchall()
    }
    assert st.get("report v1.md") == "archive_candidate"
    assert st.get("report v2.md") == "indexed"


# 敏感目录与排除项 -----------------------------------------------------------

def test_sensitive_and_exclude(runtime, wenqu_env):
    docs = _docs(wenqu_env)
    cfg = load_config()
    cfg.set("index.sensitive_paths", [str(docs / "secret")])
    cfg.save()
    (docs / "secret").mkdir()
    (docs / "secret" / "s.md").write_text("机密内容 关键词xyzq", encoding="utf-8")
    (docs / ".git").mkdir()
    (docs / ".git" / "g.md").write_text("git文件 关键词gitkw", encoding="utf-8")
    dm.scan_once(runtime)
    assert do_search(load_config(), "xyzq")["results"] == []
    assert do_search(load_config(), "gitkw")["results"] == []


# 云盘黑名单 ------------------------------------------------------------------

def test_cloud_blacklist(tmp_path):
    assert dm.is_under_cloud(Path.home() / "Library" / "Mobile Documents" / "x.md")
    assert dm.is_under_cloud(Path.home() / "Dropbox" / "docs")
    assert not dm.is_under_cloud(tmp_path)


# 防抖与冷却到期补检 -----------------------------------------------------------

def test_debounce_and_cooldown_recheck(runtime, wenqu_env):
    docs = _docs(wenqu_env)
    f = docs / "deb.md"
    f.write_text("防抖测试 第一版 kwdeb1", encoding="utf-8")
    runtime.pending[str(f)] = time.time() - 6  # 防抖窗口已过
    dm.process_pending(runtime)
    assert do_search(load_config(), "kwdeb1")["results"]

    # 冷却期内修改 → 进补检队列而非立即重索引
    f.write_text("防抖测试 第二版 kwdeb2", encoding="utf-8")
    os.utime(f, None)
    runtime.pending[str(f)] = time.time() - 6
    dm.process_pending(runtime)
    assert str(f) in runtime.recheck
    assert do_search(load_config(), "kwdeb2")["results"] == []

    # 冷却到期主动补检（不等全扫）
    runtime.recheck[str(f)] = time.time() - 1
    dm.process_rechecks(runtime)
    assert do_search(load_config(), "kwdeb2")["results"]


# needs_reindex 门：mtime 抖动但内容不变 → 不重索引 ---------------------------

def test_needs_reindex_sha_gate(runtime, wenqu_env):
    docs = _docs(wenqu_env)
    f = docs / "gate.md"
    f.write_text("门控测试 内容固定 kwgate", encoding="utf-8")
    dm.index_file(runtime, f)
    os.utime(f, (time.time() + 5, time.time() + 5))  # mtime 变、内容不变
    assert dm.needs_reindex(runtime, f) is False
    f.write_text("门控测试 内容已变 kwgate2", encoding="utf-8")
    os.utime(f, None)
    assert dm.needs_reindex(runtime, f) is True


# 事件快速路径 rename 锚定（index_file 入口 sha256 锚，不重解析） --------------

def test_rename_event_path_anchor(runtime, wenqu_env):
    docs = _docs(wenqu_env)
    src = docs / "事件前.md"
    src.write_text("# 事件锚定\n\n事件路径 rename 身份保持测试 kwanchorevt。", encoding="utf-8")
    dm.index_file(runtime, src)
    conn = runtime.conn
    fid = db.get_file_by_path(conn, str(src))["file_id"]
    ids1 = [
        r[0] for r in conn.execute(
            "SELECT chunk_id FROM chunks WHERE file_id=? ORDER BY seq", (fid,)
        ).fetchall()
    ]

    dst = docs / "事件后.md"
    src.rename(dst)
    # 模拟事件路径：watchdog on_created → pending → index_file（不跑全扫对账）
    ok = dm.index_file(runtime, dst)
    assert ok is True
    # 身份锚：file_id 保留、无新条目、chunk_id 不变
    row = db.get_file_by_path(conn, str(dst))
    assert row is not None and row["file_id"] == fid
    assert db.get_file_by_path(conn, str(src)) is None
    n = conn.execute(
        "SELECT COUNT(*) FROM file_manifest WHERE source_type='local'"
    ).fetchone()[0]
    assert n == 1, "事件路径 rename 不产生新清单行"
    ids2 = [
        r[0] for r in conn.execute(
            "SELECT chunk_id FROM chunks WHERE file_id=? ORDER BY seq", (fid,)
        ).fetchall()
    ]
    assert ids1 == ids2, "chunk_id 不变（不重解析）"
    assert do_search(load_config(), "kwanchorevt")["results"]


# _anchor_move 目标路径占用冲突 -------------------------------------------------

def test_anchor_move_conflict(runtime, wenqu_env):
    docs = _docs(wenqu_env)
    conn = runtime.conn
    # 原条目
    a = docs / "a.md"
    a.write_text("锚定冲突测试 同内容 kwanchorc", encoding="utf-8")
    dm.index_file(runtime, a)
    row_a = db.get_file_by_path(conn, str(a))
    # 同 sha 重复入库的脏条目（占用目标路径）
    b = docs / "b.md"
    b.write_text("锚定冲突测试 同内容 kwanchorc", encoding="utf-8")
    dm.index_file(runtime, b)
    row_b = db.get_file_by_path(conn, str(b))

    # a 消失（rename 场景），锚定到 b 的路径：应清除占用者 b、保留 a 的 file_id
    a.unlink()
    st = b.stat()
    assert dm._anchor_move(runtime, row_a, b, st) is True
    kept = db.get_file_by_path(conn, str(b))
    assert kept is not None and kept["file_id"] == row_a["file_id"]
    assert conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE file_id=?", (row_b["file_id"],)
    ).fetchone()[0] == 0, "被清除的占用条目 chunks 一并删除"

    # 异内容占用：放弃锚定（不误删他人）
    c = docs / "c.md"
    c.write_text("锚定冲突测试 原始内容 kwanchorc2", encoding="utf-8")
    dm.index_file(runtime, c)
    row_c = db.get_file_by_path(conn, str(c))
    d = docs / "d.md"
    d.write_text("完全不同的内容 kwanchorother", encoding="utf-8")
    dm.index_file(runtime, d)
    row_d = db.get_file_by_path(conn, str(d))
    c.unlink()
    assert dm._anchor_move(runtime, row_c, d, d.stat()) is False
    assert db.get_file_by_path(conn, str(d))["file_id"] == row_d["file_id"], "异内容占用者不受影响"


# 全扫恢复在盘 missing 条目（锚定前标记/守护重启窗口遗留） ----------------------

def test_missing_status_recovery(runtime, wenqu_env):
    docs = _docs(wenqu_env)
    f = docs / "rec.md"
    f.write_text("恢复测试 内容 kwrecover", encoding="utf-8")
    dm.index_file(runtime, f)
    conn = runtime.conn
    # 模拟遗留：在盘但状态 missing
    conn.execute(
        "UPDATE file_manifest SET status='missing' WHERE file_path=?", (str(f),)
    )
    conn.commit()
    dm.scan_once(runtime)
    assert db.get_file_by_path(conn, str(f))["status"] == "indexed"


# 修改重索引：file_id 稳定 + 旧 chunks 替换无残留无幽灵 --------------------------

def test_reindex_stable_file_id(runtime, wenqu_env):
    docs = _docs(wenqu_env)
    f = docs / "stable.md"
    f.write_text("稳定测试 第一版 kwstable1", encoding="utf-8")
    dm.index_file(runtime, f)
    conn = runtime.conn
    fid1 = db.get_file_by_path(conn, str(f))["file_id"]
    assert conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE file_id=?", (fid1,)
    ).fetchone()[0] > 0

    # 修改后重索引（走 upsert DO UPDATE 分支）：file_id 必须保持
    f.write_text("稳定测试 第二版 kwstable2 完全不同的更长内容以确保重新分块处理。", encoding="utf-8")
    os.utime(f, None)
    dm.index_file(runtime, f)
    fid2 = db.get_file_by_path(conn, str(f))["file_id"]
    assert fid1 == fid2, "重索引 file_id 保持稳定（upsert DO UPDATE 不换身份锚）"
    # 全部 chunks 挂在真实 file_id 下（无幽灵 file_id）
    orphans = conn.execute(
        """
        SELECT COUNT(*) FROM chunks c
        WHERE NOT EXISTS (SELECT 1 FROM file_manifest f WHERE f.file_id = c.file_id)
        """
    ).fetchone()[0]
    assert orphans == 0, "无幽灵 file_id 的 chunks"
    # 旧内容替换无残留
    assert do_search(load_config(), "kwstable1")["results"] == []
    assert do_search(load_config(), "kwstable2")["results"]


# watchdog handler 线程安全：回调在 emitter 线程执行，禁触 SQLite 连接 -------

def test_handler_thread_safety(runtime, wenqu_env):
    import threading

    docs = _docs(wenqu_env)
    f = docs / "handler.md"
    f.write_text("handler 线程安全测试 kwhandler", encoding="utf-8")
    dm.index_file(runtime, f)

    handler = dm.make_handler(runtime)
    errors: list[BaseException] = []

    class _Evt:
        def __init__(self, src: str, dst: str | None = None, is_directory: bool = False):
            self.src_path, self.dest_path = src, dst
            self.is_directory = is_directory

    def run_callbacks():
        try:
            handler.on_created(_Evt(str(docs / "new.md")))
            handler.on_modified(_Evt(str(f)))
            handler.on_moved(_Evt(str(f), str(docs / "moved.md")))
            handler.on_deleted(_Evt(str(docs / "moved.md")))
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    t = threading.Thread(target=run_callbacks)
    t.start()
    t.join(timeout=10)
    assert not errors, f"handler 回调在 watcher 线程抛异常（会杀死事件线程）: {errors}"
