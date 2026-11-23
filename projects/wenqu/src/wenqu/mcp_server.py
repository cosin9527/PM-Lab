"""wenqu-mcp：只读 STDIO MCP Server（五工具）。

设计要点（实施方案 §A.2 mcp_server.py 契约）：
- **不暴露任何写/删工具**——"wenqu-sync 唯一写入者"由接口面强制，而非 Skill 约定
- 每次工具调用前 config 热重载（Config.data 属性 stat mtime）
- 每查询直读 SQLite（无内存缓存）——文件落盘即可搜，无需重启客户端
- hybrid 模式进程启动即后台预热模型（冷启动 2-5s 提前发生）
- Embedder 进程内单例：查询间保持热模型；degraded 状态经 wenqu_status 上报
"""

from __future__ import annotations

import threading
from typing import Any

try:  # mcp v1
    from mcp.server.fastmcp import FastMCP
except ImportError:  # mcp 2.x：FastMCP 更名为 MCPServer（API 同形）
    from mcp.server.mcpserver import MCPServer as FastMCP

from .config import load_config
from .embed import Embedder
from . import search as _search

mcp = FastMCP("wenqu")

_CFG = None
_EMBEDDER: Embedder | None = None


def _cfg():
    global _CFG, _EMBEDDER
    if _CFG is None:
        _CFG = load_config()
        _EMBEDDER = Embedder(_CFG)
    return _CFG


def _embedder() -> Embedder:
    _cfg()
    assert _EMBEDDER is not None
    return _EMBEDDER


# ---------------------------------------------------------------------------
# 五工具（全部只读）
# ---------------------------------------------------------------------------

@mcp.tool()
def wenqu_search(
    query: str,
    top_k: int = 8,
    source_filter: str | None = None,
    max_return_tokens: int = 4096,
    after: str | None = None,
    before: str | None = None,
    include_archived: bool = False,
) -> dict[str, Any]:
    """跨来源知识检索（本地文件 + 飞书快照统一索引）。

    返回片段内容（max_return_tokens 预算内截断）+ 来源 + 章节 + 文件时间 +
    状态标注（孤儿快照）+ has_more。单文档最多 3 块防霸榜；内容相同的本地
    原件与飞书快照合并为双出处。
    source_filter：'local' / 'feishu_snapshot'，或来源路径子串。
    after/before：ISO 日期（YYYY-MM-DD）或日期时间。
    """
    return _search.search(
        _cfg(), query,
        top_k=top_k, source_filter=source_filter,
        max_return_tokens=max_return_tokens, after=after, before=before,
        include_archived=include_archived, embedder=_embedder(),
    )


@mcp.tool()
def wenqu_get_chunk(chunk_id: str, neighbors: int = 0) -> dict[str, Any] | None:
    """读取指定片段及上下文邻块（neighbors = 前后各 N 块）。"""
    return _search.get_chunk(_cfg(), chunk_id, neighbors=neighbors)


@mcp.tool()
def wenqu_toc(source: str) -> list[dict[str, Any]]:
    """读取文档结构（章节 / 页 / sheet 列表，含预览）。source 为相对路径。"""
    return _search.toc(_cfg(), source)


@mcp.tool()
def wenqu_get_section(source: str, section_id: str) -> dict[str, Any] | None:
    """按章节/页面读取完整内容。section_id 为数字（chunk seq）或章节标题路径。

    重要结论引用前必须读完整章节（防截断语义反转）。
    """
    return _search.get_section(_cfg(), source, section_id)


@mcp.tool()
def wenqu_status() -> dict[str, Any]:
    """知识库健康检查：watch 目录 / 文件数 / 索引与向量覆盖率 / 归档候选 /
    孤儿态统计 / needs_ocr / failed / degraded。"""
    return _search.status(_cfg(), embedder=_embedder())


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main() -> None:
    cfg = _cfg()
    if cfg.data["retrieval"]["mode"] == "hybrid":
        # 主线程先完成 fastembed/numpy 导入：消除后台预热与首个查询并发
        # import 时 numpy 多级子模块 "partially initialized" 竞态
        try:
            import fastembed  # noqa: F401
        except Exception:
            pass  # 导入失败由 warm/degraded 降级链路兜底
        # 进程启动即后台预热（冷启动成本前移）
        threading.Thread(target=_embedder().warm, daemon=True).start()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
