"""RemoteSource 飞书实现（lark-cli 子进程，授权共享 ~/.lark-cli）。

Phase C 校准点（实施方案 §六.4）：lark-cli 子命令与输出格式以实测回填。
本模块提供结构完整的行为契约实现：
- 元数据先行增量（remote_token 身份锚 + updated_at 比对，未变化零下载）
- docx/wiki → markdown 快照（frontmatter 携带 token / 原文 URL / updated_at）
- 附件 → attachments/ 落地
- 孤儿分诊三态（403→revoked / 404→deleted_remote / 存在→moved_out_of_scope），
  统一策略：保留可搜 + 状态标注 + 最后可读时间
- 单轮消失 >30% → 冻结、仅告警、不执行删除
- 限速（调用间隔 ≥0.5s）；lark-cli 不可用 / 输出无法解析 → 跳过并记录，
  不阻塞本地同步
"""

from __future__ import annotations

import abc
import json
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .. import db
from ..config import wenqu_home

log = logging.getLogger("wenqu.feishu")

CALL_INTERVAL_SECONDS = 0.5  # API 限速兜底
MASS_DISAPPEAR_RATIO = 0.3

_TOKEN_RE = re.compile(
    r"/(?:docx|wiki|docs|file|folder|sheets|slides|base|drive)/([A-Za-z0-9]{8,})"
)


class FeishuError(Exception):
    """飞书源操作失败（跳过并记录，不阻塞本地同步）。"""


def parse_token(url_or_token: str) -> str | None:
    """从 URL 或裸 token 提取飞书 token。"""
    s = url_or_token.strip()
    m = _TOKEN_RE.search(s)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9]{8,}", s):
        return s
    return None


def parse_scope(url_or_token: str) -> str | None:
    """解析为带类型的 scope：知识空间 → 'wiki:<space_id>'；云盘文件夹 → 裸 token。"""
    s = url_or_token.strip()
    if "/wiki/" in s:
        m = re.search(r"/wiki/([A-Za-z0-9]{8,})", s)
        if m:
            return f"wiki:{m.group(1)}"
        # /wiki/ 主页无 token
        return None
    token = parse_token(s)
    return token


def slugify(title: str) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", (title or "doc").strip())
    return s[:50] or "doc"


class RemoteSource(abc.ABC):
    """远端源接口骨架（§7.5）：接入新线上源 = 新增适配器，不改 wenqu-sync 核心。"""

    @abc.abstractmethod
    def search(self, query: str) -> list[dict[str, Any]]: ...

    @abc.abstractmethod
    def read_doc(self, item: dict[str, Any]) -> str: ...

    @abc.abstractmethod
    def list_meta(self, scope: str) -> list[dict[str, Any]]: ...

    @abc.abstractmethod
    def download(self, item: dict[str, Any], dest: Path) -> Path: ...

    @abc.abstractmethod
    def probe(self, token: str) -> str: ...


class FeishuSource(RemoteSource):
    """lark-cli 子进程封装（命令模板 Phase C 实测校准）。"""

    def __init__(self):
        self._last_call = 0.0
        if shutil.which("lark-cli") is None:
            raise FeishuError("lark-cli 不可用（随 Trae lark 插件分发；Phase C 前置）")

    def _run(self, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
        gap = time.time() - self._last_call
        if gap < CALL_INTERVAL_SECONDS:
            time.sleep(CALL_INTERVAL_SECONDS - gap)
        self._last_call = time.time()
        return subprocess.run(
            ["lark-cli", *args], capture_output=True, text=True, timeout=timeout
        )

    def _run_text(self, *args: str) -> str:
        cp = self._run(*args)
        if cp.returncode != 0:
            raise FeishuError(f"lark-cli {' '.join(args)} 失败: {(cp.stderr or '').strip()[:200]}")
        return cp.stdout

    def _run_json(self, *args: str) -> Any:
        out = self._run_text(*args)
        try:
            return json.loads(out)
        except (ValueError, TypeError):
            raise FeishuError(f"lark-cli 输出无法解析为 JSON: {out[:200]}")

    def _api_get(self, path: str, params: dict[str, Any]) -> Any:
        """raw API GET（lark-cli api GET <path> --params '<json>'）。"""
        cp = self._run(
            "api", "GET", path, "--params", json.dumps(params), timeout=60
        )
        if cp.returncode != 0:
            raise FeishuError(
                f"lark-cli api GET {path} 失败: {(cp.stderr or cp.stdout or '').strip()[:200]}"
            )
        try:
            d = json.loads(cp.stdout)
        except (ValueError, TypeError):
            raise FeishuError(f"lark-cli api 输出无法解析: {cp.stdout[:200]}")
        if not d.get("ok"):
            raise FeishuError(f"lark-cli api {path} 返回错误: {json.dumps(d.get('error', {}), ensure_ascii=False)[:200]}")
        return d

    # ---- 能力集实现（Phase C 实测校准：lark-cli 1.0.98）----

    def search(self, query: str) -> list[dict[str, Any]]:
        """线上关键词搜索（文件级；飞书线上无语义能力）。"""
        data = self._run_json("docs", "+search", "--query", query)
        results = (data.get("data") or {}).get("results") or []
        out: list[dict[str, Any]] = []
        for it in results:
            meta = (it.get("result_meta") or {}) if isinstance(it, dict) else {}
            token = meta.get("token")
            if not token:
                # token 藏在 icon_info JSON 字符串里
                try:
                    token = json.loads(meta.get("icon_info") or "{}").get("token")
                except (ValueError, TypeError):
                    token = None
            if not token:
                continue
            out.append(
                {
                    "token": token,
                    "title": meta.get("title") or meta.get("name") or token,
                    "url": f"https://feishu.cn/docx/{token}",
                    "kind": (meta.get("doc_types") or "docx").lower(),
                }
            )
        return out

    def read_doc(self, item: dict[str, Any]) -> str:
        """读取 docx/wiki 正文为 markdown（docs +fetch --doc --doc-format markdown）。"""
        url = item.get("url") or f"https://feishu.cn/docx/{item.get('token')}"
        data = self._run_json(
            "docs", "+fetch", "--doc", url, "--doc-format", "markdown"
        )
        content = ((data.get("data") or {}).get("document") or {}).get("content")
        if content is None:
            raise FeishuError(f"fetch 返回无 content: {url}")
        return content

    def list_meta(self, scope: str) -> list[dict[str, Any]]:
        """范围内文档元数据清单（scope = 云盘文件夹 token 或 'wiki:<space_id>'）。

        第一版只快照 docx/doc；sheet/slide/base/mindnote/shortcut/file 不快照
        （实施方案 §A.2：结构化内容落地复杂，发现靠文件级搜索覆盖）。
        wiki 节点以 obj_token 为身份锚（文档移动/重命名后不变）。
        """
        out: list[dict[str, Any]] = []
        if scope.startswith("wiki:"):
            self._walk_wiki_space(scope[5:], out, set(), depth=0)
        else:
            self._walk_folder(scope, out, set(), depth=0)
        return out

    def _walk_wiki_space(
        self, space_id: str, out: list[dict[str, Any]],
        seen: set[str], depth: int,
    ) -> None:
        """知识空间根层遍历；子层由 _collect_wiki_node 按 parent_node_token 递归。"""
        page_token = ""
        while True:
            params: dict[str, Any] = {"page_size": 50}
            if page_token:
                params["page_token"] = page_token
            d = self._api_get(
                f"/open-apis/wiki/v2/spaces/{space_id}/nodes", params
            )
            data = d.get("data") or {}
            for n in data.get("items") or []:
                self._collect_wiki_node(space_id, n, out, seen, depth)
            if not data.get("has_more"):
                break
            page_token = data.get("page_token") or ""

    def _collect_wiki_node(
        self, space_id: str, node: dict[str, Any],
        out: list[dict[str, Any]], seen: set[str], depth: int,
    ) -> None:
        obj_token = node.get("obj_token")
        obj_type = node.get("obj_type")
        if obj_token and obj_type in ("docx", "doc") and obj_token not in seen:
            seen.add(obj_token)
            out.append(
                {
                    "token": obj_token,  # 身份锚：文档实体 token
                    "title": node.get("title") or obj_token,
                    "url": f"https://feishu.cn/docx/{obj_token}",
                    "updated_at": str(node.get("obj_edit_time") or ""),
                    "kind": "docx",
                    "attachments": [],
                }
            )
        if node.get("has_child") and node.get("node_token"):
            node_token = node["node_token"]
            page_token = ""
            while True:
                params: dict[str, Any] = {
                    "page_size": 50, "parent_node_token": node_token,
                }
                if page_token:
                    params["page_token"] = page_token
                d = self._api_get(
                    f"/open-apis/wiki/v2/spaces/{space_id}/nodes", params
                )
                data = d.get("data") or {}
                for n in data.get("items") or []:
                    self._collect_wiki_node(space_id, n, out, seen, depth + 1)
                if not data.get("has_more"):
                    break
                page_token = data.get("page_token") or ""

    def _walk_folder(
        self, folder_token: str, out: list[dict[str, Any]],
        seen: set[str], depth: int,
    ) -> None:
        if depth > 8 or folder_token in seen:  # 防过深/防环
            return
        seen.add(folder_token)
        page_token = ""
        while True:
            params: dict[str, Any] = {"page_size": 50, "folder_token": folder_token}
            if page_token:
                params["page_token"] = page_token
            d = self._api_get("/open-apis/drive/v1/files", params)
            data = d.get("data") or {}
            for it in data.get("files") or []:
                t = it.get("type")
                if t == "folder":
                    self._walk_folder(it["token"], out, seen, depth + 1)
                elif t in ("docx", "docs"):
                    out.append(
                        {
                            "token": it["token"],
                            "title": it.get("name") or it["token"],
                            "url": it.get("url") or f"https://feishu.cn/docx/{it['token']}",
                            "updated_at": str(it.get("modified_time") or ""),
                            "kind": t,
                            "attachments": [],
                        }
                    )
            if not data.get("has_more"):
                break
            page_token = data.get("page_token") or ""

    def download(self, item: dict[str, Any], dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        url = item.get("url") or f"https://feishu.cn/file/{item.get('token')}"
        cp = self._run(
            "drive", "+download", "--url", url, "--output", str(dest)
        )
        if cp.returncode != 0 or not dest.exists():
            raise FeishuError(f"附件下载失败: {url}")
        return dest

    def probe(self, token: str) -> str:
        """孤儿分诊：ok | revoked | deleted | unknown。"""
        cp = self._run(
            "docs", "+fetch", "--doc", f"https://feishu.cn/docx/{token}"
        )
        if cp.returncode == 0:
            return "ok"  # 存在且有权限 → moved_out_of_scope
        err = (cp.stderr or "") + (cp.stdout or "")
        # lark-cli 错误输出为 JSON（error.code）；文本兜底
        code = ""
        try:
            code = str((json.loads(err).get("error") or {}).get("code") or "")
        except (ValueError, TypeError):
            pass
        combined = (code + " " + err).lower()
        if "403" in combined or "permission" in combined or "权限" in err:
            return "revoked"
        if "404" in combined or "not found" in combined or "不存在" in err:
            return "deleted"
        return "unknown"  # 限流/断网 → 本轮不动，下轮重试


# ---------------------------------------------------------------------------
# 快照落地与同步周期
# ---------------------------------------------------------------------------

def feishu_root() -> Path:
    return wenqu_home() / "Documents" / "Feishu"


def snapshot_path_for(title: str, token: str) -> Path:
    return feishu_root() / "snapshots" / f"{slugify(title)}.{token[:8]}.md"


def write_snapshot(
    path: Path,
    markdown: str,
    *,
    token: str,
    url: str,
    updated_at: str,
) -> None:
    frontmatter = (
        "---\n"
        f"wenqu_remote_token: {token}\n"
        f"wenqu_remote_url: {url}\n"
        f"wenqu_remote_updated_at: {updated_at}\n"
        f"wenqu_snapshot_at: {__import__('datetime').datetime.now().astimezone().isoformat(timespec='seconds')}\n"
        "---\n\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter + markdown, encoding="utf-8")


def _snapshot_rows(conn) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT file_id, file_path, remote_token, remote_updated_at, status "
        "FROM file_manifest WHERE source_type='feishu_snapshot'"
    ).fetchall()
    return {r["remote_token"]: r for r in rows}


def feishu_sync_cycle(rt) -> dict[str, Any]:
    """一轮远端同步：全 scope 并集清单 → 统一孤儿分诊 → 增量落地。

    分诊必须基于所有 watch scope 的并集（快照按 token 全局存储），
    按单 scope 比对会把其他 scope 的快照误判为孤儿。
    """
    from . import daemon as _daemon  # 延迟导入防循环

    cfg = rt.cfg.data
    tokens = list(cfg["feishu"]["watch"])
    summary = {"scope": len(tokens), "landed": 0, "attachments": 0, "triaged": 0, "frozen": False}
    if not tokens:
        return summary

    try:
        source = FeishuSource()
    except FeishuError as e:
        log.warning("飞书源不可用，本轮跳过：%s", e)
        return summary

    # 1) 收集全部 scope 的清单（token 去重：同一文档出现在多个 scope 时合并）
    all_items: dict[str, dict[str, Any]] = {}
    failed_scopes: list[str] = []
    for scope in tokens:
        try:
            for item in source.list_meta(scope):
                all_items.setdefault(item["token"], item)
        except FeishuError as e:
            log.warning("list_meta 失败（scope=%s）：%s", scope, e)
            failed_scopes.append(scope)

    rows = _snapshot_rows(rt.conn)
    item_tokens = set(all_items)
    manifest_tokens = set(rows)

    # 批量消失防护（单轮 >30% 冻结，仅告警，不执行删除）；
    # 有 scope 拉取失败时缺失不可信 → 跳过分诊
    missing_tokens = manifest_tokens - item_tokens
    if failed_scopes:
        log.warning(
            "%d 个 scope 拉取失败，本轮跳过分诊防误判：%s",
            len(failed_scopes), failed_scopes,
        )
        missing_tokens = set()
    elif manifest_tokens and len(missing_tokens) > MASS_DISAPPEAR_RATIO * len(manifest_tokens):
        log.warning(
            "飞书清单批量消失 %d/%d（>30%%），疑似权限批量故障/断连，本轮冻结",
            len(missing_tokens), len(manifest_tokens),
        )
        summary["frozen"] = True
        missing_tokens = set()

    # 2) 孤儿分诊（三态：保留可搜 + 状态标注）
    for t in missing_tokens:
        try:
            verdict = source.probe(t)
        except FeishuError as e:
            log.warning("probe 失败（token=%s）：%s", t, e)
            continue  # 探测失败 → 本轮不动，下轮重试
        row = rows.get(t)
        if row is None:
            continue
        if verdict == "revoked":
            status = "revoked"
        elif verdict == "deleted":
            status = "deleted_remote"
        elif verdict == "ok":
            status = "moved_out_of_scope"
        else:
            continue  # unknown → 不动
        with rt.conn:
            rt.conn.execute(
                "UPDATE file_manifest SET status=?, error_message=? WHERE file_id=?",
                (status, f"最后可读时间: {db.now_iso()}", row["file_id"]),
            )
        summary["triaged"] += 1
        log.info("孤儿分诊 token=%s → %s", t, status)

    # 3) 增量落地（元数据先行：仅变化文档读取正文；
    #    被误标后恢复的快照 status != indexed → 重新落地即自愈）
    for token, item in all_items.items():
        row = rows.get(token)
        if (
            row is not None
            and row["status"] == "indexed"
            and item["updated_at"]
            and row["remote_updated_at"] == item["updated_at"]
        ):
            continue  # 未变化：零下载、零重索引
        try:
            _land_doc(rt, source, item, row)
            summary["landed"] += 1
            for att in item.get("attachments", []):
                if isinstance(att, dict) and att.get("token"):
                    dest = feishu_root() / "attachments" / f"{slugify(att.get('title', 'file'))}.{att['token'][:8]}{att.get('ext', '')}"
                    try:
                        source.download(att, dest)
                        _daemon.index_file(rt, dest)
                        summary["attachments"] += 1
                    except FeishuError as e:
                        log.warning("附件落地失败：%s", e)
        except FeishuError as e:
            log.warning("快照落地失败（token=%s）：%s", token, e)

    db.set_meta(rt.conn, "last_feishu_snapshot", db.now_iso())
    log.info(
        "feishu 周期完成：scope=%d 落地=%d 附件=%d 分诊=%d frozen=%s",
        summary["scope"], summary["landed"], summary["attachments"],
        summary["triaged"], summary["frozen"],
    )
    return summary


def _land_doc(rt, source: FeishuSource, item: dict[str, Any], row) -> None:
    from . import daemon as _daemon

    token = item["token"]
    new_path = snapshot_path_for(item["title"], token)

    # token 身份锚：title 变化 → 路径变化 → 先迁移 manifest 路径再落地
    if row is not None and row["file_path"] != str(new_path):
        db.update_file_path(
            rt.conn, row["file_id"], file_path=str(new_path),
            relative_path=_rel_snapshot(new_path), source=_rel_snapshot(new_path),
        )
        old = Path(row["file_path"])
        if old.exists():
            old.unlink()

    markdown = source.read_doc(item)
    write_snapshot(
        new_path, markdown,
        token=token, url=item["url"], updated_at=item["updated_at"],
    )
    _daemon.index_file(
        rt, new_path,
        source_type="feishu_snapshot",
        remote_token=token,
        remote_url=item["url"],
        remote_updated_at=item["updated_at"] or None,
    )


def _rel_snapshot(path: Path) -> str:
    root = wenqu_home() / "Documents"
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
