"""wenqu CLI：管理面（附录 E 命令面）。

所有变更命令输出结构化验证摘要；破坏性操作（--purge / prune / rebuild /
archive confirm）先列影响清单、二次确认；用户永不手编 config.yaml /
user_dict.txt / 数据库——一切经本命令面。
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Optional

import typer

from . import db, launchd
from .config import (
    EFFECT_NOTES,
    ConfigError,
    coerce_value,
    load_config,
    models_dir,
    user_dict_path,
    wenqu_home,
)
from .search import status as _status

app = typer.Typer(
    help="问渠 wenqu：轻量跨来源知识检索工具", no_args_is_help=True, add_completion=False
)
sync_app = typer.Typer(help="launchd 守护管理", no_args_is_help=True)
watch_app = typer.Typer(help="本地检索目录管理", no_args_is_help=True)
feishu_app = typer.Typer(help="飞书范围管理", no_args_is_help=True)
config_app = typer.Typer(help="标量配置读写", no_args_is_help=True)
userdict_app = typer.Typer(help="jieba 领域词典", no_args_is_help=True)
app.add_typer(sync_app, name="sync")
app.add_typer(watch_app, name="watch")
app.add_typer(feishu_app, name="feishu")
app.add_typer(config_app, name="config")
app.add_typer(userdict_app, name="userdict")


# ---------------------------------------------------------------------------
# 公共助手
# ---------------------------------------------------------------------------

def _echo_json(obj) -> None:
    typer.echo(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _runtime():
    from .sync.daemon import init_runtime

    return init_runtime(load_config())


def _mcp_abs_path() -> str:
    p = shutil.which("wenqu-mcp")
    if p:
        return str(Path(p).resolve())
    cand = Path.home() / ".local" / "bin" / "wenqu-mcp"
    if cand.exists():
        return str(cand)
    raise typer.BadParameter("未找到 wenqu-mcp（先 uv tool install --editable <源码目录>）")


def _print_mcp_snippets() -> None:
    home = str(wenqu_home())
    try:
        mcp_path = _mcp_abs_path()
    except Exception:
        mcp_path = "<MCP_ABS_PATH>"
        typer.echo(
            "（未在 PATH 找到 wenqu-mcp——完成 uv tool install 后替换下方占位符）"
        )
    typer.echo("\n=== MCP 注册片段（Trae / 通用 JSON） ===")
    typer.echo(
        json.dumps(
            {"mcpServers": {"wenqu": {"command": mcp_path, "env": {"WENQU_HOME": home}}}},
            ensure_ascii=False, indent=2,
        )
    )
    typer.echo("\n=== MCP 注册片段（Codex TOML） ===")
    typer.echo(f"""[mcp_servers.wenqu]
command = "{mcp_path}"

[mcp_servers.wenqu.env]
WENQU_HOME = "{home}"
""")


# ---------------------------------------------------------------------------
# init（Day-0 向导）
# ---------------------------------------------------------------------------

@app.command()
def init(
    watch: Optional[list[str]] = typer.Option(None, "--watch", help="要索引的本地目录（可多次）"),
    mode: Optional[str] = typer.Option(None, "--mode", help="keyword | hybrid"),
    embed_model: Optional[str] = typer.Option(None, "--embed-model", help="hybrid 时的向量模型"),
    sensitive: Optional[list[str]] = typer.Option(None, "--sensitive", help="完全排除的敏感目录"),
    skip_model: bool = typer.Option(False, "--skip-model", help="跳过模型下载"),
    install_launchd: bool = typer.Option(False, "--install-launchd", help="安装 launchd 守护"),
    yes: bool = typer.Option(False, "--yes", help="非交互确认"),
):
    """Day-0 初始化向导：目录树 / config / 强制 watch / 自检 / 模型下载 / 首扫。"""
    from .sync.daemon import is_under_cloud, probe_dir_writable, scan_once, init_runtime

    home = wenqu_home()
    # 1. 目录树（WENQU_HOME 一揽子布局）
    for d in (
        home / "Documents" / "Feishu" / "snapshots",
        home / "Documents" / "Feishu" / "attachments",
        home / ".wenqu" / "tmp",
        home / ".wenqu" / "logs",
        home / ".wenqu" / "models",
    ):
        d.mkdir(parents=True, exist_ok=True)
    user_dict_path().touch(exist_ok=True)

    # 2. 配置（含选型覆盖）
    cfg = load_config()
    try:
        if mode:
            cfg.set("retrieval.mode", mode)
        if embed_model:
            cfg.set("retrieval.embed_model", embed_model)
        if sensitive:
            cur = list(cfg.get("index.sensitive_paths") or [])
            cur += [str(Path(s).expanduser().resolve()) for s in sensitive]
            cfg.set("index.sensitive_paths", cur)
    except ConfigError as e:
        raise typer.BadParameter(str(e))

    # 3. 强制完成 local.watch（仅显式声明模式：空 = 不索引任何本地文件）
    watches: list[str] = []
    if watch:
        watches = [str(Path(w).expanduser().resolve()) for w in watch]
    elif sys.stdin.isatty():
        typer.echo("输入要索引的本地目录（推荐放在 %s 下；空行结束）：" % (home / "Documents"))
        while True:
            line = input().strip()
            if not line:
                break
            watches.append(str(Path(line).expanduser().resolve()))
    if not watches:
        raise typer.BadParameter(
            "local.watch 必须配置（wenqu init --watch <dir>；仅显式声明模式）"
        )
    # 逐目录自检：云盘黑名单 + TCC 写入探测，不过不放行
    for w in watches:
        wp = Path(w)
        if not wp.is_dir():
            raise typer.BadParameter(f"目录不存在：{w}")
        if is_under_cloud(wp):
            raise typer.BadParameter(f"云盘同步区拒绝：{w}（占位文件会触发全量下载）")
        ok, err = probe_dir_writable(wp)
        if not ok:
            raise typer.BadParameter(f"目录不可写（TCC？）：{w}——{err}")
    cfg.set("local.watch", watches)
    cfg.save()
    typer.echo(f"config.yaml 已生成：{cfg.path}")

    # 4. 模型下载（方向二/三）
    if cfg.data["retrieval"]["mode"] == "hybrid" and not skip_model:
        typer.echo(f"下载 embedding 模型 → {models_dir(cfg)} ...")
        from .embed import Embedder

        if Embedder(cfg).warm():
            typer.echo(f"模型就绪：{cfg.data['retrieval']['embed_model']}")
        else:
            typer.echo("模型下载失败——degraded：检索自动降级纯 BM25，可稍后重试")

    # 5. launchd
    if install_launchd:
        launchd.install(str(home))
        typer.echo(f"launchd 已安装：{launchd.PLIST_PATH}")

    # 6. 首次全量索引
    typer.echo("触发首次全量索引...")
    rt = init_runtime(cfg)
    summary = scan_once(rt)
    _echo_json({"init": "ok", "wenqu_home": str(home), "first_scan": summary})

    # 7. 双端 MCP 注册片段
    _print_mcp_snippets()


# ---------------------------------------------------------------------------
# mcp / status / verify
# ---------------------------------------------------------------------------

@app.command(name="mcp")
def mcp_cmd():
    """启动 MCP Server（STDIO）——客户端注册的就是本命令。"""
    from . import mcp_server

    mcp_server.main()


@app.command()
def status(json_out: bool = typer.Option(False, "--json")):
    """知识库状态摘要。"""
    cfg = load_config()
    s = _status(cfg)
    if json_out:
        _echo_json(s)
        return
    typer.echo(f"模式: {s['mode']}" + (f"（{s['embed_model']}）" if s["embed_model"] else ""))
    typer.echo(f"watch 目录: {s['watch_dirs'] or '（空）'}")
    typer.echo(f"文件总数: {s['files_total']}  状态分布: {s['files_by_status']}")
    typer.echo(f"chunks: {s['chunks_total']}  FTS 覆盖: {s['fts_coverage']}"
               + (f"  向量覆盖: {s['vec_coverage']}" if s["vec_coverage"] else ""))
    typer.echo(f"最近同步: {s['last_sync']}  飞书快照: {s['last_feishu_snapshot']}")
    typer.echo(f"归档候选: {s['archive_candidates']}  孤儿: {s['orphans']}"
               f"  needs_ocr: {s['needs_ocr']}  failed: {s['failed']}")


@app.command()
def verify():
    """安装自检：环境 / 配置 / DB / 检索冒烟 / launchd / wenqu-mcp。"""
    from . import fts as _fts

    results: list[tuple[str, bool, str]] = []
    try:
        cfg = load_config()
        results.append(("config", True, str(cfg.path)))
    except Exception as e:
        results.append(("config", False, str(e)))
        for name, _, _ in [
            ("db", False, "config 失败跳过"),
            ("fts", False, "config 失败跳过"),
        ]:
            typer.echo(f"[FAIL] {name}")
        raise typer.Exit(1)

    dbp = Path(cfg.data["storage"]["db"])
    ok = dbp.exists()
    detail = str(dbp)
    if ok:
        try:
            conn = db.connect(dbp, load_vec=False)
            db.init_schema(conn)
            n = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            detail = f"{dbp}（chunks={n}）"
            conn.close()
        except Exception as e:
            ok, detail = False, str(e)
    results.append(("db", ok, detail))

    try:
        toks = _fts.tokenize("达人管理 好友关系监控 决策检索")
        ok = len(toks.split()) > 0
        detail = toks[:60]
    except Exception as e:
        ok, detail = False, str(e)
    results.append(("jieba/fts", ok, detail))

    try:
        from .search import search as _do_search

        res = _do_search(cfg, "自检冒烟", top_k=3, embedder=None)
        ok = isinstance(res.get("results"), list)
        detail = f"mode={res.get('mode')} degraded={res.get('degraded')}"
    except Exception as e:
        ok, detail = False, str(e)
    results.append(("search 冒烟", ok, detail))

    st = launchd.status()
    results.append(("launchd", st["loaded"], f"plist_exists={st['plist_exists']}"))

    try:
        results.append(("wenqu-mcp", True, _mcp_abs_path()))
    except Exception as e:
        results.append(("wenqu-mcp", False, str(e)))

    failed = 0
    for name, ok, detail in results:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        typer.echo(f"[{mark}] {name}: {detail}")
    raise typer.Exit(1 if failed else 0)


# ---------------------------------------------------------------------------
# watch add / remove
# ---------------------------------------------------------------------------

@watch_app.command("add")
def watch_add(
    directory: Path,
    json_out: bool = typer.Option(False, "--json"),
):
    """自检（云盘黑名单 + TCC）→ 嵌套去重 → 写入 → 立即首扫 → 摘要。"""
    from .sync.daemon import init_runtime, is_under_cloud, probe_dir_writable, scan_once

    d = directory.expanduser().resolve()
    if not d.is_dir():
        raise typer.BadParameter(f"目录不存在：{d}")
    if is_under_cloud(d):
        raise typer.BadParameter(f"云盘同步区拒绝：{d}")
    ok, err = probe_dir_writable(d)
    if not ok:
        raise typer.BadParameter(f"目录不可写（TCC？）：{err}")

    cfg = load_config()
    current = [Path(p).expanduser().resolve() for p in cfg.get("local.watch") or []]
    # 嵌套去重：新目录是已有父目录的子目录 → 已覆盖；已有目录被新目录包含 → 替换
    if any(d.is_relative_to(c) for c in current):
        typer.echo("该目录已被现有 watch 父目录覆盖，无需添加")
        raise typer.Exit(0)
    current = [c for c in current if not c.is_relative_to(d)]
    current.append(d)
    cfg.set("local.watch", [str(c) for c in current])
    cfg.save()

    rt = init_runtime(cfg)
    summary = scan_once(rt)
    out = {"watch": str(d), "config_saved": True, "first_scan": summary}
    _echo_json(out) if json_out else typer.echo(
        f"已添加并首扫：{d}（扫描 {summary['scanned']} / 新索引 {summary['indexed']}）"
    )


@watch_app.command("remove")
def watch_remove(
    directory: Path,
    keep: bool = typer.Option(False, "--keep", help="冻结保留（可搜、不再更新）"),
    purge: bool = typer.Option(False, "--purge", help="索引与清单同事务清除"),
):
    """移除 watch 目录。非交互环境必须显式 --keep 或 --purge（拒绝裸跑）。"""
    if keep == purge:
        raise typer.BadParameter("必须显式 --keep（冻结保留）或 --purge（清除），拒绝裸跑")
    d = directory.expanduser().resolve()
    cfg = load_config()
    current = [Path(p).expanduser().resolve() for p in cfg.get("local.watch") or []]
    if d not in current:
        raise typer.BadParameter(f"不在 watch 列表：{d}")
    current.remove(d)
    cfg.set("local.watch", [str(c) for c in current])
    cfg.save()

    conn = db.connect(cfg.data["storage"]["db"], load_vec=False)
    rows = conn.execute(
        "SELECT file_id, file_path FROM file_manifest WHERE file_path LIKE ? AND source_type='local'",
        (f"{str(d)}%",),
    ).fetchall()
    n = len(rows)
    if not typer.confirm(f"影响 {n} 个文件，继续？"):
        raise typer.Exit(1)
    if keep:
        with conn:
            for r in rows:
                conn.execute(
                    "UPDATE file_manifest SET status='moved_out_of_scope' WHERE file_id=?",
                    (r["file_id"],),
                )
        typer.echo(f"已移出 watch 并冻结保留 {n} 个文件（moved_out_of_scope，可搜不再更新）")
    else:
        for r in rows:
            db.delete_file(conn, r["file_id"])
        typer.echo(f"已清除 {n} 个文件的索引与清单")
    conn.close()


# ---------------------------------------------------------------------------
# feishu add / remove
# ---------------------------------------------------------------------------

@feishu_app.command("add")
def feishu_add(
    url: str,
    yes: bool = typer.Option(False, "--yes", help="跳过规模确认（非交互）"),
):
    """解析 URL → scope → 规模预览（确认）→ 写入 → 限速首次落地 → 摘要。"""
    from .sync.feishu import (
        FeishuError, FeishuSource, feishu_sync_cycle, parse_scope,
    )

    token = parse_scope(url)
    if not token:
        raise typer.BadParameter(f"无法解析飞书 token：{url}")
    try:
        source = FeishuSource()
        items = source.list_meta(token)
    except FeishuError as e:
        raise typer.BadParameter(f"范围预览失败（Phase C 前置：lark-cli 授权/scope）：{e}")

    typer.echo(f"范围规模：{len(items)} 个文档（token={token}）")
    if not yes and not typer.confirm("确认接入该范围？"):
        raise typer.Exit(1)

    cfg = load_config()
    cur = list(cfg.get("feishu.watch") or [])
    if token not in cur:
        cur.append(token)
        cfg.set("feishu.watch", cur)
        cfg.save()

    rt = _runtime()
    summary = feishu_sync_cycle(rt)
    _echo_json({"feishu_add": token, "summary": summary})


@feishu_app.command("remove")
def feishu_remove(
    url_or_token: str,
    purge: bool = typer.Option(False, "--purge", help="同时清除快照索引"),
):
    """移出飞书范围（默认冻结保留可搜，对称 §7.4 政策）。"""
    from .sync.feishu import parse_token

    token = parse_token(url_or_token)
    if not token:
        raise typer.BadParameter(f"无法解析 token：{url_or_token}")
    cfg = load_config()
    cur = [t for t in (cfg.get("feishu.watch") or []) if t != token]
    cfg.set("feishu.watch", cur)
    cfg.save()

    conn = db.connect(cfg.data["storage"]["db"], load_vec=False)
    row = db.get_file_by_token(conn, token)
    if row is not None:
        if purge:
            p = Path(row["file_path"])
            if p.exists():
                p.unlink()
            db.delete_file(conn, row["file_id"])
            typer.echo("已清除快照文件与索引")
        else:
            db.mark_file_status(conn, row["file_id"], "moved_out_of_scope")
            typer.echo("已移出范围：快照冻结保留可搜（moved_out_of_scope）")
    else:
        typer.echo("已移出清单（无本地快照）")
    conn.close()


# ---------------------------------------------------------------------------
# config get / set
# ---------------------------------------------------------------------------

@config_app.command("get")
def config_get(key: str):
    """读取标量配置（点路径，如 retrieval.top_k）。"""
    cfg = load_config()
    val = cfg.get(key)
    if val is None:
        raise typer.BadParameter(f"未知配置键：{key}")
    _echo_json({key: val})


@config_app.command("set")
def config_set(key: str, value: str):
    """写入标量配置（校验 + 原子落盘 + 生效时点提示）。"""
    cfg = load_config()
    if key in ("local.watch", "feishu.watch"):
        raise typer.BadParameter("watch 列表请用 wenqu watch / wenqu feishu 命令（带自检）")
    cur = cfg.get(key)
    if cur is None:
        raise typer.BadParameter(f"未知配置键：{key}")
    try:
        new = coerce_value(value, cur)
        cfg.set(key, new)
        cfg.save()
    except ConfigError as e:
        raise typer.BadParameter(str(e))
    root = key.split(".")[0]
    typer.echo(f"已设置 {key} = {new!r}；生效时点：{EFFECT_NOTES.get(root, '即时')}")


# ---------------------------------------------------------------------------
# userdict add / remove / list
# ---------------------------------------------------------------------------

@userdict_app.command("add")
def userdict_add(word: str):
    """添加领域词（改善专名分词，如"达人""企微"）。"""
    p = user_dict_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    words = (
        {w.strip() for w in p.read_text(encoding="utf-8").splitlines() if w.strip()}
        if p.exists()
        else set()
    )
    words.add(word)
    p.write_text("\n".join(sorted(words)) + "\n", encoding="utf-8")
    typer.echo(f"已添加（词典共 {len(words)} 词；新查询即生效）")


@userdict_app.command("remove")
def userdict_remove(word: str):
    p = user_dict_path()
    if not p.exists():
        raise typer.BadParameter("词典不存在")
    words = {w.strip() for w in p.read_text(encoding="utf-8").splitlines() if w.strip()}
    words.discard(word)
    p.write_text("\n".join(sorted(words)) + "\n", encoding="utf-8")
    typer.echo(f"已移除（词典共 {len(words)} 词）")


@userdict_app.command("list")
def userdict_list():
    p = user_dict_path()
    words = sorted(
        w.strip() for w in p.read_text(encoding="utf-8").splitlines() if w.strip()
    ) if p.exists() else []
    _echo_json({"count": len(words), "words": words})


# ---------------------------------------------------------------------------
# archive confirm / prune-feishu / reembed / rebuild
# ---------------------------------------------------------------------------

@app.command("archive")
def archive(
    confirm: bool = typer.Option(False, "--confirm", help="执行确认"),
    confirm_all: bool = typer.Option(False, "--all", help="确认全部候选"),
    path: Optional[str] = typer.Option(None, "--path", help="确认单个文件"),
):
    """归档候选查看与确认（archived 默认不进检索，可显式检索；归档不是删除）。"""
    cfg = load_config()
    conn = db.connect(cfg.data["storage"]["db"], load_vec=False)
    rows = conn.execute(
        "SELECT file_id, relative_path FROM file_manifest WHERE status='archive_candidate'"
    ).fetchall()
    if not confirm and not confirm_all and not path:
        typer.echo(f"归档候选 {len(rows)} 个：")
        for r in rows:
            typer.echo(f"  - {r['relative_path']}")
        typer.echo("确认：wenqu archive --confirm --all 或 --path <文件>")
        conn.close()
        return
    if path:
        rows = [r for r in rows if path in (r["relative_path"],)]
        if not rows:
            raise typer.BadParameter(f"不是归档候选：{path}")
    if not typer.confirm(f"将归档 {len(rows)} 个文件（默认不进检索，可逆）？"):
        raise typer.Exit(1)
    with conn:
        for r in rows:
            conn.execute(
                "UPDATE file_manifest SET status='archived' WHERE file_id=?", (r["file_id"],)
            )
    typer.echo(f"已归档 {len(rows)} 个文件")
    conn.close()


@app.command("prune-feishu")
def prune_feishu(
    status: str = typer.Option(..., help="revoked | deleted_remote | moved_out_of_scope"),
    yes: bool = typer.Option(False, "--yes"),
):
    """清理对应状态的孤儿快照（破坏性：先列清单确认）。"""
    valid = ("revoked", "deleted_remote", "moved_out_of_scope")
    if status not in valid:
        raise typer.BadParameter(f"status 必须是 {' | '.join(valid)}")
    cfg = load_config()
    conn = db.connect(cfg.data["storage"]["db"], load_vec=False)
    rows = conn.execute(
        "SELECT file_id, file_path, remote_token FROM file_manifest "
        "WHERE source_type='feishu_snapshot' AND status=?",
        (status,),
    ).fetchall()
    typer.echo(f"{status} 快照 {len(rows)} 个：")
    for r in rows:
        typer.echo(f"  - {r['file_path']}")
    if not rows:
        conn.close()
        return
    if not yes and not typer.confirm("清除以上快照文件与索引？"):
        raise typer.Exit(1)
    for r in rows:
        p = Path(r["file_path"])
        if p.exists():
            p.unlink()
        db.delete_file(conn, r["file_id"])
    typer.echo(f"已清除 {len(rows)} 个")
    conn.close()


@app.command()
def reembed(yes: bool = typer.Option(False, "--yes")):
    """向量重建（模型切换后执行；FTS 索引不受影响）。"""
    from .config import SUPPORTED_MODELS

    cfg = load_config()
    r = cfg.data["retrieval"]
    if r["mode"] != "hybrid":
        typer.echo("keyword 模式无向量路径，无需 reembed")
        return
    conn = db.connect(cfg.data["storage"]["db"], load_vec=True)
    n = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    conn.close()
    est_min = max(1, round(n / 1000 * (3 if "bge" in r["embed_model"] else 35)))
    typer.echo(f"目标模型：{r['embed_model']}（dim={SUPPORTED_MODELS[r['embed_model']]['dim']}）")
    typer.echo(f"待重嵌块数：{n}，预估 {est_min} 分钟（FTS 检索全程可用）")
    if not yes and not typer.confirm("开始重嵌？"):
        raise typer.Exit(1)

    # 重建 vec 表 + 清空向量 → 泵式回填直至完成
    from .sync.daemon import init_runtime

    rt = init_runtime(cfg)
    db.ensure_vec_table(rt.conn, SUPPORTED_MODELS[r["embed_model"]]["dim"])
    with rt.conn:
        rt.conn.execute("UPDATE chunks SET embedding=NULL")
        rt.conn.execute("UPDATE file_manifest SET embedded_at=NULL")
    done = 0
    while True:
        rows = rt.conn.execute(
            "SELECT c.rowid, c.content FROM chunks c "
            "JOIN file_manifest f ON c.file_id=f.file_id "
            "WHERE c.embedding IS NULL AND f.status='indexed' LIMIT 64"
        ).fetchall()
        if not rows:
            break
        embs = rt.embedder.embed_passages([x[1] for x in rows])
        if embs is None:
            typer.echo("模型不可用（degraded）——daemon 会自动续填；可稍后重跑 reembed")
            break
        use_vec = db.vec_available(rt.conn)
        with rt.conn:
            for (rid, _), emb in zip(rows, embs):
                rt.conn.execute("UPDATE chunks SET embedding=? WHERE rowid=?", (emb, rid))
                if use_vec:
                    rt.conn.execute(
                        "INSERT OR REPLACE INTO chunks_vec(rowid, embedding) VALUES(?,?)",
                        (rid, emb),
                    )
        done += len(rows)
        typer.echo(f"重嵌进度：{done}/{n}")
    with rt.conn:
        rt.conn.execute(
            "UPDATE file_manifest SET embedded_at=? WHERE embedded_at IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM chunks WHERE file_id=file_manifest.file_id "
            "AND embedding IS NULL)",
            (db.now_iso(),),
        )
    typer.echo(f"重嵌完成：{done}")


@app.command()
def rebuild(yes: bool = typer.Option(False, "--yes")):
    """SQLite 全量重建（损坏恢复； Documents 原始文件不受影响）。"""
    cfg = load_config()
    dbp = Path(cfg.data["storage"]["db"])
    typer.echo(f"将删除并重建索引库：{dbp}")
    if not yes and not typer.confirm("破坏性操作，确认？"):
        raise typer.Exit(1)
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(dbp) + suffix)
        if p.exists():
            p.unlink()
    from .sync.daemon import init_runtime, scan_once

    rt = init_runtime(cfg)
    summary = scan_once(rt)
    _echo_json({"rebuild": "done", "first_scan": summary})


# ---------------------------------------------------------------------------
# sync start / stop / restart / status / daemon
# ---------------------------------------------------------------------------

@sync_app.command("start")
def sync_start():
    """安装并启动 launchd 守护（登录自启 + KeepAlive）。"""
    launchd.install(str(wenqu_home()))
    _echo_json(launchd.status())


@sync_app.command("stop")
def sync_stop():
    """停止并卸载 launchd 守护。"""
    launchd.uninstall()
    _echo_json(launchd.status())


@sync_app.command("restart")
def sync_restart():
    """重启守护（storage.* 配置变更后需要）。"""
    _echo_json(launchd.restart())


@sync_app.command("status")
def sync_status():
    """守护运行状态。"""
    _echo_json(launchd.status())


@sync_app.command("daemon")
def sync_daemon():
    """前台运行守护进程（launchd 拉起的就是本命令）。"""
    from .sync.daemon import run_daemon

    run_daemon()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
