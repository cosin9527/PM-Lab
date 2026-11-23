"""config 层测试：默认值 / 校验 / 热重载 / 原子写 / 类型转换。"""

from __future__ import annotations

import pytest

from wenqu.config import Config, ConfigError, coerce_value, load_config, normalize


def test_defaults_without_file(wenqu_env):
    cfg = load_config()
    assert cfg.get("retrieval.mode") == "keyword"  # fixture 已写入
    assert cfg.get("retrieval.top_k") == 8
    assert cfg.get("retrieval.rrf_k") == 60
    assert cfg.get("feishu.sync_interval") == "daily"
    assert cfg.get("storage.db").startswith(str(wenqu_env))


def test_validate_rejects_bad_mode(wenqu_env):
    with pytest.raises(ConfigError):
        normalize({"retrieval": {"mode": "bogus"}})
    with pytest.raises(ConfigError):
        normalize({"retrieval": {"embed_model": "gpt-4"}})
    with pytest.raises(ConfigError):
        normalize({"retrieval": {"top_k": 999}})


def test_hot_reload(wenqu_env):
    cfg1 = load_config()
    assert cfg1.get("retrieval.top_k") == 8
    cfg1.set("retrieval.top_k", 5)
    cfg1.save()
    cfg2 = load_config()
    assert cfg2.get("retrieval.top_k") == 5
    # 热重载：cfg2 变更后 cfg1 感知
    cfg2.set("retrieval.top_k", 12)
    cfg2.save()
    assert cfg1.reload_if_changed() is True
    assert cfg1.get("retrieval.top_k") == 12


def test_set_and_save_roundtrip(wenqu_env):
    cfg = load_config()
    cfg.set("retrieval.recency_alpha", 0.5)
    cfg.set("feishu.sync_interval", "hourly")
    cfg.save()
    cfg2 = load_config()
    assert cfg2.get("retrieval.recency_alpha") == 0.5
    assert cfg2.get("feishu.sync_interval") == "hourly"


def test_coerce_value():
    assert coerce_value("5", 8) == 5
    assert coerce_value("0.3", 0.2) == 0.3
    assert coerce_value("true", False) is True
    assert coerce_value("hybrid", "keyword") == "hybrid"
    assert coerce_value("a.md, b.txt", ["x"]) == ["a.md", "b.txt"]
    with pytest.raises(ConfigError):
        coerce_value("abc", 1)


def test_unknown_key_get(wenqu_env):
    cfg = load_config()
    assert cfg.get("no.such.key") is None


def test_missing_file_uses_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("WENQU_HOME", str(tmp_path / "none"))
    cfg = load_config()
    assert cfg.get("local.watch") == []
    assert cfg.get("retrieval.mode") == "hybrid"
