"""Phase 2.1b P2.2 测试：discovery retriever（用户 invariant ①②③④ 全覆盖）。

① query 只有 SUCCEEDED 才算执行过；FAILED 可重试
② paper provenance many-to-many（同 paper 多 query 追加不覆盖）
③ retrieved_total / existing_papers / new_unique_papers 单独统计
④ 新论文进 staging 只标 STAGED（不冒充 RELEVANT）——不直接进已确认 KB

execute_query / execute_pending 是 async（支持 OpenAlex async backend）；
测试用 asyncio.run 包同步 mock search_fn。
"""

import asyncio
import json

import pytest

from search_engine.discovery.paper_provenance import (
    DiscoveryPaperProvenance, load_provenance, save_provenance,
    add_provenance, papers_by_query, queries_for_paper,
)
from search_engine.discovery.discovery_retriever import (
    QueryExecution, execute_query, execute_pending, build_existing_universe,
    load_staging, save_staging, summarize,
)


def _paper(pid: str, title: str = ""):
    return type("Paper", (), {"paper_id": pid, "title": title})()


def _record(query_text: str, family: str = "NODE", source_node: str = "bulk-fill",
            qid: str = "q1") -> dict:
    return {"query_id": qid, "query_text": query_text, "query_family": family,
            "source_node": source_node, "origin_promotion": "c1",
            "origin_round": 2, "status": "PENDING",
            "normalized_query": query_text}


def _run(coro):
    return asyncio.run(coro)


# ── ① QueryExecution 状态机 ──

def test_execute_success_counts():
    search = lambda q, limit=20: [_paper("W1"), _paper("W2"), _paper("W3")]
    rec = _record("bulk-fill shrinkage stress")
    exec_ = _run(execute_query(rec, search, existing_ids={"W2"},
                               provenance_records=[], staging=[]))
    assert exec_.status == "SUCCEEDED"
    assert exec_.retrieved_count == 3
    assert exec_.existing_count == 1      # W2 已在 universe
    assert exec_.new_unique_count == 2    # W1, W3 是新
    assert exec_.executed_at is not None
    assert exec_.error is None


def test_execute_async_backend_supported():
    """async backend（OpenAlex search_relevance 同款）也能 await。"""
    async def search(q, limit=20):
        return [_paper("W1")]
    rec = _record("bulk-fill shrinkage stress")
    exec_ = _run(execute_query(rec, search, existing_ids=set(),
                               provenance_records=[], staging=[]))
    assert exec_.status == "SUCCEEDED"
    assert exec_.new_unique_count == 1


def test_execute_failure_retryable():
    def search(q, limit=20):
        raise RuntimeError("network down")
    rec = _record("bulk-fill shrinkage stress")
    exec_ = _run(execute_query(rec, search, existing_ids=set(),
                               provenance_records=[], staging=[]))
    assert exec_.status == "FAILED"
    assert "network down" in (exec_.error or "")
    # FAILED 不产生 staging/provenance
    assert exec_.new_unique_count == 0
    # 重试：search 恢复后 SUCCEEDED
    exec2 = _run(execute_query(rec, lambda q, limit=20: [_paper("W9")],
                               existing_ids=set(), provenance_records=[], staging=[]))
    assert exec2.status == "SUCCEEDED"
    assert exec2.new_unique_count == 1


def test_execute_pending_skips_succeeded_retries_failed():
    registry = [
        {**_record("q-a", qid="a"), "status": "SUCCEEDED"},   # 已完成 → 跳过
        {**_record("q-b", qid="b"), "status": "FAILED", "error": "x"},  # 可重试
        {**_record("q-c", qid="c")},                          # PENDING → 执行
    ]
    def search(q, limit=20):
        return [_paper("W1")]
    execs, _, _ = _run(execute_pending(registry, search, existing_ids=set(),
                                       provenance_records=[], staging=[]))
    assert len(execs) == 2                      # 只执行 b、c
    assert all(e.status == "SUCCEEDED" for e in execs)
    assert registry[0]["status"] == "SUCCEEDED" # 原样
    assert registry[1]["status"] == "SUCCEEDED" # 重试成功
    assert registry[2]["status"] == "SUCCEEDED"
    assert registry[1]["retrieved_count"] == 1


# ── ② provenance many-to-many ──

def test_provenance_many_to_many_no_overwrite(tmp_path):
    path = tmp_path / "prov.json"
    records = []
    p1 = DiscoveryPaperProvenance(paper_id="W1", promoted_node="bulk-fill",
                                  promotion_id="c1", origin_round=2,
                                  query_id="q1", query_text="a", query_family="NODE")
    p2 = DiscoveryPaperProvenance(paper_id="W1", promoted_node="bulk-fill",
                                  promotion_id="c1", origin_round=2,
                                  query_id="q2", query_text="b", query_family="ADJACENT")
    assert add_provenance(records, p1, path=str(path)) is True
    assert add_provenance(records, p2, path=str(path)) is True   # 同 paper 不同 query 追加
    assert add_provenance(records, p1, path=str(path)) is False  # 同组合幂等
    loaded = load_provenance(str(path))
    assert len(loaded) == 2
    assert sorted(queries_for_paper(loaded, "W1")) == ["q1", "q2"]
    assert papers_by_query(loaded, "q1") == ["W1"]


# ── ③ 统计：retrieved / existing / new_unique ──

def test_statistics_separated(tmp_path):
    provenance, staging = [], []
    registry = [
        {**_record("bulk-fill filler content shrinkage stress", family="RELATION", qid="r1")},
        {**_record("bulk-fill stress-relieving monomer", family="ADJACENT", qid="d1")},
    ]
    hits = {
        "bulk-fill filler content shrinkage stress": [_paper("W1"), _paper("W2")],
        "bulk-fill stress-relieving monomer": [_paper("W2"), _paper("W3"), _paper("W4")],
    }
    search = lambda q, limit=20: hits.get(q, [])
    existing = {"W2", "W9"}   # W2 已在 universe
    execs, existing_retrieved, retrieved_all = _run(execute_pending(
        registry, search, existing, provenance, staging))

    summary = summarize(execs, registry, staging, provenance,
                        existing_retrieved, retrieved_all)
    assert summary["raw_hits"] == 5
    assert summary["new_unique_papers"] == 3        # W1, W3, W4
    assert summary["existing_papers"] == 1          # unique：W2 被两个 query 找回只算 1
    assert summary["unique_papers"] == 4            # 数学一致：1 existing + 3 new
    assert sum(e.existing_count for e in execs) == 2  # per-query 口径是 2（r1/d1 各 1）
    # by family（provenance 只记新论文）
    assert summary["by_family"]["RELATION"] == 1     # W1
    assert summary["by_family"]["ADJACENT"] == 2     # W3, W4


def test_build_existing_universe(tmp_path):
    pool = tmp_path / "pool.json"
    pool.write_text(json.dumps({"candidates": [{
        "source_papers": ["W1", "W2"],
        "provenance": {"verification": {"supporting_papers": ["W3"]}},
    }]}), encoding="utf-8")
    prov = tmp_path / "prov.json"
    save_provenance([DiscoveryPaperProvenance(paper_id="W4", query_id="q").to_dict()], str(prov))
    universe = build_existing_universe(pool_path=str(pool), staging_path="nonexistent.json",
                                       provenance_path=str(prov))
    assert universe == {"W1", "W2", "W3", "W4"}


# ── ④ staging 不冒充 RELEVANT（不直接进已确认 KB）──

def test_staging_paper_not_relevant(tmp_path):
    from search_engine.discovery.discovery_retriever import add_staging_paper
    staging = []
    rec = _record("bulk-fill shrinkage stress", qid="q1")
    entry = add_staging_paper(staging, _paper("W1", "A bulk-fill paper"),
                              rec, provenance_records=[])
    assert entry["relevance_status"] == "STAGED"     # 不标 RELEVANT/UNCERTAIN
    assert entry["query_ids"] == ["q1"]
    assert entry["promoted_nodes"] == ["bulk-fill"]
    # 同 paper 被第二个 query 找到 → query_ids 累积不覆盖
    add_staging_paper(staging, _paper("W1"), _record("other", qid="q2"),
                      provenance_records=[])
    assert entry["query_ids"] == ["q1", "q2"]


def test_retriever_does_not_write_confirmed_kb():
    """retriever 输出只有 staging/provenance/registry——不触碰已确认 KB 文件。"""
    import inspect
    from search_engine.discovery import discovery_retriever as mod
    src = inspect.getsource(mod)
    assert "KnowledgeBase" not in src or "get_edges" in src  # 只读 universe，不写 KB
    assert "insert" not in src and "add_edge" not in src
