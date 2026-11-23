"""search 层测试：BM25 / top_k / 单文档限额 / token 预算 / 归档 / 孤儿标注 /
新近度 / 模型降级 / 读取工具。"""

from __future__ import annotations

import os
import time

import pytest

from wenqu import db
from wenqu.config import load_config
from wenqu.search import get_chunk, get_section, search, toc
from wenqu.sync.daemon import index_file


@pytest.fixture
def indexed(runtime, wenqu_env):
    docs = wenqu_env / "Documents"
    f1 = docs / "达人方案.md"
    f1.write_text("# 达人管理\n\n" + "支持达人与企业微信员工的好友关系监控。 " * 20, encoding="utf-8")
    f2 = docs / "会议纪要.md"
    f2.write_text("# 评审会\n\n" + "结论：达人管理方案通过评审。 " * 20, encoding="utf-8")
    f3 = docs / "长文档.md"
    f3.write_text("# 长文\n\n" + "独特关键词出现非常多次。 " * 200, encoding="utf-8")
    for f in (f1, f2, f3):
        index_file(runtime, f)
    return runtime


def test_keyword_search_ranked(indexed):
    res = search(load_config(), "好友关系监控")
    assert res["mode"] == "keyword" and res["degraded"] is False
    assert res["results"], "应命中达人方案"
    assert any("好友关系监控" in r["content"] for r in res["results"])
    top = res["results"][0]
    assert top["rank"] == 1 and top["source"] == "达人方案.md"
    assert top["heading_path"] == "达人管理"


def test_top_k_and_per_file_cap(indexed):
    res = search(load_config(), "独特关键词", top_k=10)
    # 单文档 ≤3 块防霸榜（长文档独有关键词，全部块都命中）
    assert 0 < len(res["results"]) <= 3


def test_token_budget_and_has_more(indexed):
    res = search(load_config(), "独特关键词", top_k=10, max_return_tokens=30)
    assert len(res["results"]) < 3
    assert res["has_more"] is True


def test_archived_excluded_by_default(indexed, wenqu_env):
    cfg = load_config()
    conn = db.connect(cfg.data["storage"]["db"], load_vec=False)
    row = db.get_file_by_path(conn, str(wenqu_env / "Documents" / "达人方案.md"))
    db.mark_file_status(conn, row["file_id"], "archived")
    conn.close()
    assert search(cfg, "好友关系监控")["results"] == []
    res = search(cfg, "好友关系监控", include_archived=True)
    assert res["results"]


def test_orphan_annotated(indexed, wenqu_env):
    cfg = load_config()
    conn = db.connect(cfg.data["storage"]["db"], load_vec=False)
    row = db.get_file_by_path(conn, str(wenqu_env / "Documents" / "会议纪要.md"))
    db.mark_file_status(conn, row["file_id"], "revoked")
    conn.close()
    res = search(cfg, "评审")
    assert res["results"], "孤儿快照保留可搜"
    assert res["results"][0]["status"] == "revoked"


def test_recency_ordering(runtime, wenqu_env):
    docs = wenqu_env / "Documents"
    fa, fb = docs / "旧文.md", docs / "新文.md"
    text = "# 同题\n\n" + "完全相同的关键词组合词组。 " * 20
    fa.write_text(text, encoding="utf-8")
    fb.write_text(text, encoding="utf-8")
    old = time.time() - 400 * 86400
    os.utime(fa, (old, old))
    index_file(runtime, fa)
    index_file(runtime, fb)
    cfg = load_config()
    cfg.set("retrieval.recency_alpha", 1.0)  # 强新近度
    cfg.save()
    res = search(load_config(), "关键词组合")
    assert res["results"], "应有两个文件命中"
    assert res["results"][0]["source"] == "新文.md"


def test_time_filters(indexed):
    cfg = load_config()
    assert search(cfg, "好友关系", before="2000-01-01")["results"] == []
    assert search(cfg, "好友关系", after="2000-01-01")["results"]


def test_source_filter(indexed):
    cfg = load_config()
    res = search(cfg, "达人", source_filter="会议纪要")
    assert all(r["source"] == "会议纪要.md" for r in res["results"])


def test_degraded_falls_back_to_keyword(indexed):
    cfg = load_config()
    cfg.set("retrieval.mode", "hybrid")
    cfg.save()

    class FailEmbedder:
        degraded = True

        def embed_query(self, t):
            return None

        def embed_passages(self, ts):
            return None

    res = search(load_config(), "好友关系监控", embedder=FailEmbedder())
    assert res["degraded"] is True
    assert res["mode"] == "keyword"  # 自动降级纯 BM25
    assert res["results"]


def test_hybrid_rrf_vector_path(runtime, wenqu_env):
    cfg = load_config()
    cfg.set("retrieval.mode", "hybrid")
    cfg.save()
    docs = wenqu_env / "Documents"
    fa = docs / "向量甲.md"
    fa.write_text("# 甲\n\n" + "苹果香蕉水果拼盘。 " * 20, encoding="utf-8")
    fb = docs / "向量乙.md"
    fb.write_text("# 乙\n\n" + "电脑主机硬件配置。 " * 20, encoding="utf-8")
    index_file(runtime, fa)
    index_file(runtime, fb)

    class FakeEmbedder:
        degraded = False
        DIM = 512
        KWS = ["苹果", "香蕉", "水果", "电脑", "主机", "硬件"]

        def _vec(self, text):
            v = [0.0] * self.DIM
            for i, kw in enumerate(self.KWS):
                if kw in text:
                    v[i] = 1.0
            v[self.DIM - 1] = 0.01
            return db.pack_floats(v)

        def embed_query(self, t):
            return self._vec(t)

        def embed_passages(self, ts):
            return [self._vec(t) for t in ts]

    fake = FakeEmbedder()
    from wenqu.sync import daemon as dm

    runtime.embedder = fake
    dm.backfill_embeddings(runtime)       # 第一次：填队列
    runtime.vec_since = time.time() - 11  # 越过 10s 窗口
    dm.backfill_embeddings(runtime)       # 第二次：flush
    conn = runtime.conn
    n_total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    n_emb = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL"
    ).fetchone()[0]
    assert n_total > 0 and n_emb == n_total, "向量回填应全覆盖"

    res = search(load_config(), "苹果", embedder=fake)
    assert res["mode"] == "hybrid" and res["degraded"] is False
    assert res["results"], "向量路径应命中"
    assert res["results"][0]["source"] == "向量甲.md"


def test_get_chunk_and_neighbors(indexed):
    res = search(load_config(), "好友关系监控")
    cid = res["results"][0]["chunk_id"]
    got = get_chunk(load_config(), cid, neighbors=1)
    assert got is not None and got["chunk_id"] == cid
    assert isinstance(got["context"], list)


def test_toc_and_get_section(indexed):
    cfg = load_config()
    items = toc(cfg, "达人方案.md")
    assert items and items[0]["heading_path"] == "达人管理"
    sec = get_section(cfg, "达人方案.md", "0")
    assert sec is not None and "好友关系监控" in sec["content"]
    assert get_section(cfg, "不存在.md", "0") is None
