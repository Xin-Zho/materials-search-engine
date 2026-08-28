"""Phase 2.1 P0 测试：round_state / prioritizer / metrics / stopping + 状态机扩展。

覆盖用户拍板（2026-08-26）：
  ① SEARCH_INCONCLUSIVE 自动重试最多 1 次，第二次失败冻结 SEARCH_INCONCLUSIVE_FROZEN，
     可人工 MANUAL_REOPEN / 新 evidence 重开
  ② NEED_MORE_EVIDENCE 自动重入 + 无证据增益逐轮降权（Score × 1/(1+0.3r)）
  ③ Novelty = ontology distance 规则近似（不用 edge_count），权重 0.10
  ④ 追踪字段 last_selected_round / verification_attempts / no_evidence_gain_streak /
     last_evidence_signature
"""

import json

import pytest

from search_engine.discovery.candidate import (
    DiscoveryCandidate, InvalidTransition, get_tracking, evidence_signature,
    STATUS_FLOW,
)
from search_engine.discovery.round_state import (
    DiscoveryRound, file_sha256, kb_version, ontology_version,
    load_rounds, save_rounds, append_round,
)
from search_engine.discovery.prioritizer import (
    DEFAULT_WEIGHTS, novelty_score, relevance_score, evidence_score,
    structural_score, cost_score, score_candidate, score_with_penalty,
    eligible, select, update_tracking_after_verify,
)
from search_engine.discovery.metrics import (
    compute_round_metrics, count_new_nodes_by_type, format_round_report,
)
from search_engine.discovery.stopping import should_stop, stop_reason_label


def _cand(name: str, ctype: str = "MECHANISM", status: str = "CANDIDATE",
          rel: str = "HIGH", papers: int = 3, **kw) -> dict:
    c = {"candidate_id": name, "raw_name": name, "candidate_type": ctype,
         "status": status, "domain_relevance": rel,
         "independent_paper_count": papers, "source": "scanner",
         "canonical_match": None, "provenance": {}}
    c.update(kw)
    return c


# ── 状态机：SEARCH_INCONCLUSIVE_FROZEN ──

def test_search_inconclusive_frozen_in_flow():
    assert "SEARCH_INCONCLUSIVE_FROZEN" in STATUS_FLOW["SEARCH_INCONCLUSIVE"]


def test_search_inconclusive_retry_then_freeze():
    c = DiscoveryCandidate(candidate_id="c1", raw_name="x", status="SEARCH_INCONCLUSIVE")
    assert c.transition("SEARCH_INCONCLUSIVE_FROZEN")  # 自动冻结路径
    assert c.status == "SEARCH_INCONCLUSIVE_FROZEN"


def test_frozen_reopen_requires_override_reason():
    c = DiscoveryCandidate(candidate_id="c1", raw_name="x",
                           status="SEARCH_INCONCLUSIVE_FROZEN")
    with pytest.raises(InvalidTransition):
        c.transition("VERIFYING")                      # 无 override 拒绝
    with pytest.raises(InvalidTransition):
        c.transition("VERIFYING", manual_override=True)  # 无 reason 拒绝
    assert c.transition("VERIFYING", manual_override=True,
                        reason="auto_reopen: new evidence")  # 新证据/人工重开
    assert c.status == "VERIFYING"


# ── 追踪字段 ──

def test_tracking_fields_init():
    c = _cand("c1")
    t = get_tracking(c)
    assert t["last_selected_round"] is None
    assert t["verification_attempts"] == 0
    assert t["no_evidence_gain_streak"] == 0
    assert t["search_inconclusive_retries"] == 0


def test_evidence_signature_changes_with_papers():
    c1 = _cand("c1", papers=2)
    c2 = _cand("c1", papers=3)
    c1["source_papers"] = ["W1"]
    c2["source_papers"] = ["W1", "W2"]
    assert evidence_signature(c1) != evidence_signature(c2)


def test_update_tracking_gain_and_streak():
    c = _cand("c1", status="NEED_MORE_EVIDENCE", papers=2)
    c["source_papers"] = ["W1"]
    update_tracking_after_verify(c, round_id=1)
    assert c["provenance"]["tracking"]["verification_attempts"] == 1
    assert c["provenance"]["tracking"]["last_selected_round"] == 1
    # 无新增证据重跑 → streak=1
    update_tracking_after_verify(c, round_id=2)
    assert c["provenance"]["tracking"]["no_evidence_gain_streak"] == 1
    # 新增证据 → streak 归零
    c["source_papers"] = ["W1", "W2"]
    update_tracking_after_verify(c, round_id=3)
    assert c["provenance"]["tracking"]["no_evidence_gain_streak"] == 0


# ── 评分分量（用户拍板权重 N0.10/R0.30/E0.20/S0.30/C0.10）──

def test_default_weights():
    assert DEFAULT_WEIGHTS == {"novelty": 0.10, "relevance": 0.30,
                               "evidence": 0.20, "structural": 0.30, "cost": 0.10}


def test_novelty_ontology_distance():
    assert novelty_score(_cand("a", ctype="ROUTE")) == 1.00
    assert novelty_score(_cand("a", ctype="MECHANISM")) == 0.85
    assert novelty_score(_cand("a", ctype="FORMULATION_STRATEGY")) == 0.70
    assert novelty_score(_cand("a", ctype="SUB_ROUTE")) == 0.35
    assert novelty_score(_cand("a", ctype="EFFECT")) == 0.50
    assert novelty_score(_cand("a", ctype="EFFECT", canonical_match="reduced shrinkage")) == 0.0


def test_novelty_not_edge_count():
    """edge_count 不参与 novelty（用户拍板：edge 少会奖励冷门噪声）。"""
    a = _cand("noise", ctype="EFFECT", papers=1)
    b = _cand("dcb", ctype="MECHANISM", papers=8)
    a["provenance"]["edge_count"] = 1
    b["provenance"]["edge_count"] = 8
    # 真正的机制候选 novelty 高于噪声 effect
    assert novelty_score(b) > novelty_score(a)


def test_relevance_evidence_structural_scale():
    assert relevance_score(_cand("a", rel="HIGH")) == 1.0
    assert relevance_score(_cand("a", rel="LOW")) == 0.3
    assert evidence_score(_cand("a", papers=10)) == 1.0
    assert evidence_score(_cand("a", papers=2)) == 0.4
    assert structural_score(_cand("a", ctype="ROUTE")) == 1.0
    assert structural_score(_cand("a", ctype="EFFECT")) == 0.2


def test_score_prefers_relevant_structural_over_noise():
    """优先'大概率相关 + 能扩展结构'（用户拍板），而非纯 novelty。"""
    good = _cand("good mechanism", ctype="MECHANISM", rel="HIGH", papers=4)
    noise = _cand("noise", ctype="EFFECT", rel="UNKNOWN", papers=0)
    assert score_candidate(good) > score_candidate(noise)


# ── NEED_MORE 降权（Score × 1/(1+0.3r)）──

def test_penalty_reduces_score():
    c = _cand("c1", rel="HIGH", papers=3)
    t = get_tracking(c)
    t["no_evidence_gain_streak"] = 2
    base, penalized = score_candidate(c), score_with_penalty(c)
    assert penalized < base
    assert abs(penalized - base / (1 + 0.3 * 2)) < 1e-9


def test_penalty_grows_with_streak():
    c1, c2 = _cand("a"), _cand("b")
    get_tracking(c1)["no_evidence_gain_streak"] = 0
    get_tracking(c2)["no_evidence_gain_streak"] = 5
    assert score_with_penalty(c1) > score_with_penalty(c2)


# ── eligible / select ──

def test_eligible_excludes_frozen():
    for s in ("REJECTED", "ADJACENT", "PROMOTED", "VALIDATED",
              "SEARCH_INCONCLUSIVE_FROZEN", "ALIAS", "EXISTING_KNOWLEDGE"):
        assert not eligible(_cand("a", status=s)), s


def test_eligible_seach_inconclusive_retry_once():
    c = _cand("a", status="SEARCH_INCONCLUSIVE")
    assert eligible(c)                      # 第一次失败后（retries=0）下一轮允许自动重试
    get_tracking(c)["search_inconclusive_retries"] = 1
    assert eligible(c)                      # 第一次重试进行中仍可被选
    get_tracking(c)["search_inconclusive_retries"] = 2
    assert not eligible(c)                  # 重试 1 次后不再自动进 prioritizer（等 FROZEN）


def test_select_diversifies_by_type():
    """10 个 filler 相关候选得分都高 → 每 type 配额限制，不会一轮全探索同一簇。"""
    cands = [ _cand(f"filler{i}", ctype="EFFECT", rel="HIGH", papers=5) for i in range(10)]
    cands += [_cand("m1", ctype="MECHANISM", rel="HIGH", papers=5),
              _cand("m2", ctype="MECHANISM", rel="HIGH", papers=5),
              _cand("f1", ctype="FORMULATION_STRATEGY", rel="HIGH", papers=5)]
    picked = select(cands, top_n=8)
    types = [c["candidate_type"] for c in picked]
    assert types.count("EFFECT") <= 1       # 配额 1
    assert types.count("MECHANISM") <= 2    # 配额 2
    assert "MECHANISM" in types and "FORMULATION_STRATEGY" in types


def test_select_respects_hard_quota():
    """硬配额：20 个同 type 候选 top_n=5 → 只选配额数（2 个 MECHANISM）。"""
    cands = [_cand(f"c{i}", ctype="MECHANISM") for i in range(20)]
    picked = select(cands, top_n=5)
    assert len(picked) == 2  # MECHANISM 配额 2（top_n 是上限不是下限）
    assert len({c["candidate_type"] for c in picked}) == 1

    # 配额内不同 type 共存
    mixed = [_cand("m", ctype="MECHANISM"), _cand("e", ctype="EFFECT"),
             _cand("f", ctype="FORMULATION_STRATEGY"), _cand("r", ctype="ROUTE"),
             _cand("s", ctype="SUB_ROUTE")]
    picked2 = select(mixed, top_n=5)
    assert len(picked2) == 5


def test_select_empty_pool():
    assert select([]) == []
    assert select([_cand("a", status="PROMOTED")]) == []


# ── round_state ──

def test_round_serialize_roundtrip(tmp_path):
    r = DiscoveryRound(round_id=1, kb_version_before="abc",
                       ontology_version_before="def",
                       selected_candidates=["a", "b"],
                       verification_results={"a": "VALIDATED"},
                       promotions=["a"], new_nodes=["a|MECHANISM"],
                       queries_used=6, papers_retrieved=12, new_unique_papers=5,
                       kb_version_after="abc2", ontology_version_after="def2",
                       stop_reason=None)
    path = tmp_path / "rounds.json"
    save_rounds([r], str(path))
    loaded = load_rounds(str(path))
    assert len(loaded) == 1
    assert loaded[0].verification_results == {"a": "VALIDATED"}
    assert loaded[0].new_nodes == ["a|MECHANISM"]
    assert loaded[0].round_id == 1


def test_append_round_idempotent(tmp_path):
    path = tmp_path / "rounds.json"
    r1 = DiscoveryRound(round_id=1, kb_version_before="a", ontology_version_before="b")
    r2 = DiscoveryRound(round_id=2, kb_version_before="a", ontology_version_before="b")
    append_round(r1, str(path))
    append_round(r2, str(path))
    append_round(r1, str(path))   # 同 id 重跑 → 替换不双写
    rounds = load_rounds(str(path))
    assert [r.round_id for r in rounds] == [1, 2]


def test_version_hash_content_based(tmp_path):
    f = tmp_path / "kb.db"
    f.write_bytes(b"v1")
    h1 = file_sha256(str(f))
    f.write_bytes(b"v2")
    assert file_sha256(str(f)) != h1
    f.write_bytes(b"v1")
    assert file_sha256(str(f)) == h1   # 内容不变 → 版本不变


def test_kb_ontology_version_functions():
    assert isinstance(kb_version(), str)
    assert isinstance(ontology_version(), str)


# ── metrics ──

def _round(**kw) -> DiscoveryRound:
    base = dict(round_id=1, kb_version_before="a", ontology_version_before="b",
                new_candidates=10, queries_used=20, new_unique_papers=8,
                verification_results={"a": "VALIDATED", "b": "NEED_MORE_EVIDENCE",
                                      "c": "REJECTED"},
                promotions=["a"], new_nodes=["x|MECHANISM", "y|ROUTE", "z|EFFECT"])
    base.update(kw)
    return DiscoveryRound(**base)


def test_compute_round_metrics():
    m = compute_round_metrics(_round())
    assert m["candidate_yield"] == 0.5          # 10/20
    assert m["validation_yield"] == pytest.approx(1 / 3)   # 1/3
    assert m["promotion_yield"] == pytest.approx(1 / 3)    # 1/3
    assert m["novel_node_yield"] == 0.15        # 3/20
    assert m["paper_yield"] == 0.4              # 8/20


def test_count_nodes_by_type():
    counts = count_new_nodes_by_type(["a|ROUTE", "b|MECHANISM", "c|MECHANISM",
                                      "d|EFFECT", "plain"])
    assert counts["new_routes"] == 1
    assert counts["new_mechanisms"] == 2
    assert counts["new_effects"] == 1
    assert counts["other"] == 1


def test_round_report_format():
    r = _round()
    report = format_round_report(r, compute_round_metrics(r))
    assert "Phase 2.1 Round 1" in report
    assert "VALIDATED" in report and "REJECTED" in report
    assert "Novel node/query:" in report
    assert "new mechanism" in report


# ── stopping ──

def _barren_round(rid: int) -> DiscoveryRound:
    return DiscoveryRound(round_id=rid, kb_version_before="a",
                          ontology_version_before="b",
                          verification_results={"a": "NEED_MORE_EVIDENCE"})

def _productive_round(rid: int) -> DiscoveryRound:
    return DiscoveryRound(round_id=rid, kb_version_before="a",
                          ontology_version_before="b",
                          verification_results={"a": "VALIDATED"},
                          promotions=["a"], new_nodes=["x|ROUTE"])


def test_stop_after_3_barren_rounds():
    rounds = [_barren_round(1), _barren_round(2), _barren_round(3)]
    stop, reason = should_stop(rounds)
    assert stop and reason == "discovery_saturation"


def test_no_stop_under_3_rounds():
    stop, reason = should_stop([_barren_round(1), _barren_round(2)])
    assert not stop and reason is None


def test_productive_round_resets_saturation():
    rounds = [_barren_round(1), _barren_round(2), _productive_round(3), _barren_round(4)]
    stop, _ = should_stop(rounds)
    assert not stop   # 第 3 轮有产出，连续 3 轮被打破


def test_stop_label_not_literature_complete():
    label = stop_reason_label("discovery_saturation")
    assert "边际收益" in label
    assert "搜全" in label and "不代表" in label
