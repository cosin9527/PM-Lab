"""MCP 工具面与 CLI 冒烟测试。"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_mcp_singleton():
    """mcp_server 的 Config/Embedder 进程内单例会跨测试泄漏 WENQU_HOME，逐测重置。"""
    from wenqu import mcp_server

    mcp_server._CFG = None
    mcp_server._EMBEDDER = None
    yield
    mcp_server._CFG = None
    mcp_server._EMBEDDER = None


def test_mcp_tools(runtime, wenqu_env):
    from wenqu.sync.daemon import index_file

    f = wenqu_env / "Documents" / "mcp.md"
    f.write_text("# MCP\n\n工具面冒烟 达人管理", encoding="utf-8")
    index_file(runtime, f)

    from wenqu import mcp_server

    st = mcp_server.wenqu_status()
    assert st["files_total"] >= 1
    assert st["mode"] == "keyword"

    res = mcp_server.wenqu_search("达人管理")
    assert isinstance(res, dict) and res["results"]

    got = mcp_server.wenqu_get_chunk(res["results"][0]["chunk_id"], neighbors=0)
    assert got is not None and "达人管理" in got["content"]

    items = mcp_server.wenqu_toc("mcp.md")
    assert items and items[0]["heading_path"] == "MCP"

    sec = mcp_server.wenqu_get_section("mcp.md", "0")
    assert sec is not None and "工具面冒烟" in sec["content"]


def test_cli_config_get_set(wenqu_env):
    from typer.testing import CliRunner
    from wenqu.cli import app

    runner = CliRunner()
    r = runner.invoke(app, ["config", "get", "retrieval.top_k"])
    assert r.exit_code == 0 and "8" in r.output

    r = runner.invoke(app, ["config", "set", "retrieval.top_k", "5"])
    assert r.exit_code == 0 and "下一查询生效" in r.output
    r = runner.invoke(app, ["config", "get", "retrieval.top_k"])
    assert "5" in r.output

    # watch 列表拒绝裸 set（必须走带自检的命令）
    r = runner.invoke(app, ["config", "set", "local.watch", "/tmp"])
    assert r.exit_code != 0


def test_cli_status_and_userdict(wenqu_env):
    from typer.testing import CliRunner
    from wenqu.cli import app

    runner = CliRunner()
    r = runner.invoke(app, ["status", "--json"])
    assert r.exit_code == 0 and "files_total" in r.output

    r = runner.invoke(app, ["userdict", "add", "达人管理"])
    assert r.exit_code == 0
    r = runner.invoke(app, ["userdict", "list"])
    assert "达人管理" in r.output
    r = runner.invoke(app, ["userdict", "remove", "达人管理"])
    assert r.exit_code == 0


def test_cli_config_set_validation(wenqu_env):
    from typer.testing import CliRunner
    from wenqu.cli import app

    runner = CliRunner()
    r = runner.invoke(app, ["config", "set", "retrieval.mode", "bogus"])
    assert r.exit_code != 0
    r = runner.invoke(app, ["config", "set", "no.such.key", "x"])
    assert r.exit_code != 0
