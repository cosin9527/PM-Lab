"""pytest 公共夹具：临时 WENQU_HOME + keyword 模式 + Documents watch。"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def wenqu_env(tmp_path, monkeypatch):
    home = tmp_path / "Knowledge"
    docs = home / "Documents"
    docs.mkdir(parents=True)
    monkeypatch.setenv("WENQU_HOME", str(home))
    from wenqu.config import load_config

    cfg = load_config()
    cfg.data["retrieval"]["mode"] = "keyword"
    cfg.data["local"]["watch"] = [str(docs)]
    cfg.save()
    (home / ".wenqu").mkdir(exist_ok=True)
    return home


@pytest.fixture
def runtime(wenqu_env):
    from wenqu.config import load_config
    from wenqu.sync.daemon import init_runtime

    rt = init_runtime(load_config())
    return rt
