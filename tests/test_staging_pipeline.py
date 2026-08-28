"""Phase 2.1b P2.3 测试：staging pipeline（用户 5 invariant 全覆盖）。

① STAGED 不能直接进 extractor，必须先过 relevance 三态
② UNCERTAIN 不丢，继续进 extraction（recall-first）
③ 新 edge 保留完整 discovery provenance（promoted node / query / new paper）
④ scanner 比较 candidate_before / candidate_after，旧候选重新发现不算成功
⑤ 核心指标单独记录：new_relevant_papers / new_edges / new_candidates /
   new_candidate_not_seen_before
"""

import json

import pytest

from search_engine.discovery.staging_pipeline import (
    query_core_terms, screen_relevance, screen_staging, extract_candidates,
    edges_to_discovery_layer, assert_all_screened, canonical_identity,
    canonical_candidates, audit_new_candidates, build_trace, candidate_diff,
)
from search_engine.models import KnowledgeRecord, RouteMechanismEvidenceEdge


def _paper(title: str, abstract: str = "", pid: str = "W1"):
    return type("Paper", (), {"paper_id": pid, "title": title, "abstract": abstract})()


def _staging(title: str, qid: str = "q1", pid: str = "W1") -> dict:
    return {"paper_id": pid, "title": title, "relevance_status": "STAGED",
            "query_ids": [qid], "promoted_nodes": ["bulk-fill"]}


def _registry(qid: str = "q1", query: str = "bulk-fill filler surface treatment "
                                            "shrinkage stress") -> list[dict]:
    return [{"query_id": qid, "query_text": query,
             "source_node": "bulk-fill composite formulation",
             "query_family": "ADJACENT"}]


# ── relevance 三态 ──

def test_query_core_terms_excludes_node():
    core = query_core_terms(
        "bulk-fill filler surface treatment shrinkage stress",
        "bulk-fill composite formulation")
    assert "filler" in core and "surface" in core and "treatment" in core
    assert "bulk" not in core and "composite" not in core  # node 词排除


def test_screen_relevant_title_two_terms():
    r = screen_relevance(
        _paper("Surface treatment of fillers reduces shrinkage stress"),
        "bulk-fill filler surface treatment shrinkage stress",
        "bulk-fill composite formulation")
    assert r == "RELEVANT"


def test_screen_uncertain_title_one_term():
    r = screen_relevance(
        _paper("Filler effects on dental composites"),
        "bulk-fill filler surface treatment shrinkage stress",
        "bulk-fill composite formulation")
    assert r == "UNCERTAIN"


def test_screen_uncertain_abstract_only_recall_first():
    """title 0 但 abstract 有词 → UNCERTAIN 不丢（recall-first）。"""
    r = screen_relevance(
        _paper("Dental resin composite evaluation",
               abstract="... filler surface treatment reduced shrinkage stress ..."),
        "bulk-fill filler surface treatment shrinkage stress",
        "bulk-fill composite formulation")
    assert r == "UNCERTAIN"


def test_screen_irrelevant_only_when_no_hit():
    r = screen_relevance(
        _paper("Weather forecasting with neural networks",
               abstract="... deep learning ..."),
        "bulk-fill filler surface treatment shrinkage stress",
        "bulk-fill composite formulation")
    assert r == "IRRELEVANT"


# ── ①② screen_staging + extract（只丢 IRRELEVANT）──

def test_screen_staging_three_states():
    staging = [
        _staging("Surface treatment of fillers reduces shrinkage stress", qid="q1"),
        _staging("Filler effects on dental composites", qid="q1", pid="W2"),
        _staging("Weather forecasting", qid="q1", pid="W3"),
    ]
    counts = screen_staging(staging, _registry())
    assert counts["RELEVANT"] == 1
    assert counts["UNCERTAIN"] == 1
    assert counts["IRRELEVANT"] == 1
    assert [p["relevance_status"] for p in staging] == \
        ["RELEVANT", "UNCERTAIN", "IRRELEVANT"]


def test_extract_keeps_relevant_and_uncertain():
    """invariant ②：UNCERTAIN 不丢，只有 IRRELEVANT 被排除。"""
    staging = [
        _staging("Surface treatment of fillers reduces shrinkage stress", qid="q1"),
        _staging("Filler effects on dental composites", qid="q1", pid="W2"),
        _staging("Weather forecasting", qid="q1", pid="W3"),
    ]
    screen_staging(staging, _registry())
    extracted = []
    def fake_extract(paper_objs):
        extracted.extend(p.paper_id for p in paper_objs)   # extractor 契约 = Paper 对象
        return [KnowledgeRecord(paper_id=p.paper_id) for p in paper_objs]
    records = extract_candidates(staging, fake_extract)
    assert sorted(extracted) == ["W1", "W2"]   # RELEVANT + UNCERTAIN
    assert len(records) == 2


def test_staged_must_screen_first():
    """invariant ①：STAGED 状态不直接进 extraction。"""
    staging = [_staging("...")]  # 未 screen，仍 STAGED
    calls = []
    def fake_extract(papers):
        calls.append(len(papers))
        return []
    extract_candidates(staging, fake_extract)
    assert calls == []          # 没有 RELEVANT/UNCERTAIN → 不进 extractor


def test_assert_all_screened_fail_fast():
    """invariant ①（硬断言）：有 STAGED/None 未三态化 → fail-fast 禁止 extractor。"""
    staging = [_staging("paper a"), _staging("paper b")]
    screen_staging(staging, _registry())            # 只 screen 一篇？不——全部 screen
    # 人为制造未 screen 的论文（模拟 query_ids 空导致 screening 跳过）
    staging.append({"paper_id": "W99", "title": "unscreened",
                    "relevance_status": "STAGED", "query_ids": []})
    with pytest.raises(AssertionError, match="remain unscreened"):
        assert_all_screened(staging)


def test_assert_all_screened_passes_after_full_screen():
    staging = [_staging("Surface treatment of fillers reduces shrinkage stress"),
               _staging("Filler effects on dental composites", pid="W2")]
    screen_staging(staging, _registry())
    assert_all_screened(staging)                    # 全三态化 → 通过


# ── canonical identity（用户 invariant：56 个 hash ≠ 56 个新概念）──

def test_canonical_identity_prefers_canonical():
    assert canonical_identity({"raw_name": "reduced polymerization shrinkage stress",
                               "canonical_name": "reduced shrinkage"}) == "reduced shrinkage"
    assert canonical_identity({"raw_name": "Filler Surface Treatment",
                               "canonical_name": None, "canonical_match": None}) == \
        "filler surface treatment"


def test_canonical_candidates_dedup_variants():
    """reduced shrinkage stress / reduced polymerization shrinkage stress 的
    canonical_match 相同 → 只算 1 个新方向（用户 invariant：56 个 hash ≠ 56 个概念）。"""
    before = [{"candidate_id": "a", "raw_name": "old"}]
    after = [
        {"candidate_id": "a", "raw_name": "old"},
        {"candidate_id": "b", "raw_name": "reduced shrinkage stress",
         "canonical_name": None, "canonical_match": "reduced shrinkage"},
        {"candidate_id": "c", "raw_name": "reduced polymerization shrinkage stress",
         "canonical_name": None, "canonical_match": "reduced shrinkage"},
    ]
    cd = canonical_candidates(before, after)
    assert len(cd["new_raw_candidates"]) == 2            # 两个新 id
    assert len(cd["new_canonical_candidates"]) == 1      # 但 canonical 只有 1 个新方向


def test_audit_new_candidates_rows():
    prov = [{"paper_id": "W1", "query_family": "ADJACENT",
             "query_id": "q1", "promoted_node": "bulk-fill"}]
    cands = [{"candidate_id": "x", "raw_name": "filler surface treatment",
              "candidate_type": "FORMULATION_STRATEGY", "canonical_match": None,
              "source_papers": ["W1"]}]
    rows = audit_new_candidates(cands, prov)
    assert rows[0]["raw_name"] == "filler surface treatment"
    assert rows[0]["query_family"] == ["ADJACENT"]
    assert rows[0]["source_paper"] == "W1"


# ── trace_complete 硬判定（用户 invariant：不能'尽量填'）──

def test_trace_complete_true_when_all_fields():
    t = build_trace(
        origin_promotion="bulk-fill composite formulation",
        query_id="q1", query_text="bulk-fill filler surface treatment shrinkage stress",
        query_family="ADJACENT",
        paper_id="W9", relevance="UNCERTAIN",
        edge={"raw_mechanism": "filler surface treatment",
              "raw_route": "bulk-fill composite formulation",
              "relation_type": "direct"},
        candidate="filler surface treatment", seen_before=False)
    assert t["trace_complete"] is True
    assert t["candidate_seen_before"] is False


def test_trace_edge_direction_route_to_mechanism():
    """方向回归（2026-08-27 审计根因）：edge 模型是 route → mechanism，
    trace 显示必须同向——曾因 src/tgt 写反输出
    "increased modulus --direct--> filler loading"（实际存储是
    "filler loading --direct--> increased modulus"，证据句也是前向）。"""
    t = build_trace(
        origin_promotion="bulk-fill composite formulation",
        query_id="q1", query_text='"bulk-fill composite" AND "shrinkage stress"',
        query_family="NODE",
        paper_id="W2058952149", relevance="RELEVANT",
        edge={"raw_mechanism": "increased modulus",
              "raw_route": "filler loading",
              "relation_type": "direct"},
        candidate="increased modulus", seen_before=False)
    assert t["new_edge"] == "filler loading --direct--> increased modulus"
    assert "increased modulus --direct--> filler loading" not in t["new_edge"]


def test_trace_complete_false_incomplete():
    """缺 query_id / relevance → trace_complete=False（不能计入 successful_closed_loop）。"""
    t = build_trace(origin_promotion="", query_id="", paper_id="",
                    relevance="", edge=None, candidate="")
    assert t["trace_complete"] is False


# ── ③ edge provenance ──

def test_edges_carry_discovery_provenance():
    rec = KnowledgeRecord(
        paper_id="W9",
        route_mechanism_edges=[RouteMechanismEvidenceEdge(
            paper_id="W9", raw_route="bulk-fill composite formulation",
            canonical_route="bulk-fill composite formulation",
            raw_mechanism="surface-treated filler",
            canonical_mechanism="surface-treated filler",
            evidence="...", confidence=0.8, relation_type="direct")])
    prov_map = {"W9": {"promoted_node": "bulk-fill composite formulation",
                       "query_id": "q1",
                       "query_text": "bulk-fill filler surface treatment shrinkage stress",
                       "query_family": "ADJACENT", "origin_round": 2}}
    edges = edges_to_discovery_layer([rec], prov_map)
    assert len(edges) == 1
    e = edges[0]
    assert e["raw_mechanism"] == "surface-treated filler"
    dp = e["discovery_provenance"]
    assert dp["promoted_node"] == "bulk-fill composite formulation"
    assert dp["query_id"] == "q1"
    assert dp["paper_id"] == "W9"
    assert dp["origin"] == "knowledge_expansion"


# ── ④ candidate before/after diff ──

def test_candidate_diff_new_only():
    before = {"old1", "old2"}
    after = {"old1", "old2", "new1", "new2"}
    diff = candidate_diff(before, after)
    assert diff["new_candidates"] == ["new1", "new2"]
    assert diff["new_candidate_not_seen_before"] == ["new1", "new2"]


def test_candidate_diff_old_rediscovery_not_success():
    """旧候选重新发现不算成功（invariant ④）。"""
    before = {"old1"}
    after = {"old1"}                       # scan 没带来任何新 candidate
    diff = candidate_diff(before, after)
    assert diff["new_candidates"] == []
    assert diff["new_candidate_count"] == 0


# ── ⑤ core metrics 结构 ──

def test_metric_keys_exist():
    from search_engine.discovery.staging_pipeline import METRIC_KEYS
    assert set(METRIC_KEYS) == {"new_relevant_papers", "new_edges",
                                "new_candidates", "new_candidate_not_seen_before"}


# ── trace ──

def test_build_trace_format():
    trace = build_trace(
        origin_promotion="bulk-fill composite formulation",
        query_text="bulk-fill filler surface treatment shrinkage stress",
        paper_id="W9", relevance="RELEVANT",
        edge={"raw_mechanism": "surface-treated filler",
              "raw_route": "bulk-fill composite formulation",
              "relation_type": "direct"},
        candidate="filler surface treatment", seen_before=False)
    assert trace["origin_promotion"] == "bulk-fill composite formulation"
    assert trace["relevance"] == "RELEVANT"
    assert "surface-treated filler" in trace["new_edge"]
    assert trace["candidate_seen_before"] is False


# ── DiscoveryLayerView（合成层，不写已确认 KB）──

def test_discovery_layer_view_merges_edges():
    from search_engine.discovery.staging_pipeline import DiscoveryLayerView

    class FakeKB:
        def __init__(self):
            self._r = [KnowledgeRecord(paper_id="P1",
                                       route_mechanism_edges=[
                                           RouteMechanismEvidenceEdge(
                                               paper_id="P1", raw_mechanism="old")])]
        def get_all(self):
            return list(self._r)

    edge = {"paper_id": "P1", "raw_mechanism": "surface-treated filler",
            "raw_route": "", "canonical_route": "", "canonical_mechanism": "",
            "evidence": "e", "confidence": 0.7, "relation_type": "direct"}
    view = DiscoveryLayerView(FakeKB(), [edge])
    recs = view.get_all()
    mechs = [e.raw_mechanism for r in recs for e in r.route_mechanism_edges]
    assert "surface-treated filler" in mechs    # discovery edge 合入
    assert "old" in mechs                       # KB 原 edge 保留


def test_discovery_layer_new_paper_record():
    from search_engine.discovery.staging_pipeline import DiscoveryLayerView

    class FakeKB:
        def get_all(self):
            return []

    edge = {"paper_id": "W9", "raw_mechanism": "filler surface treatment",
            "raw_route": "", "canonical_route": "", "canonical_mechanism": "",
            "evidence": "e", "confidence": 0.7, "relation_type": "direct"}
    recs = DiscoveryLayerView(FakeKB(), [edge]).get_all()
    assert len(recs) == 1 and recs[0].paper_id == "W9"
    assert recs[0].route_mechanism_edges[0].raw_mechanism == "filler surface treatment"
