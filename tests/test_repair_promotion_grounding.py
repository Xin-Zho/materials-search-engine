"""GROUNDING_REPAIR 测试（用户定：revision 不覆盖历史）。

验收 3 条：
  ① 不改变 candidate PROMOTED 状态
  ② 保留旧 revision（原 promotion 记录原样）
  ③ 新 relation revision supersede 旧 relation
"""

import json

from search_engine.discovery import DiscoveryCandidate
from tools.repair_promotion_grounding import repair_grounding, build_grounding_revision


def _promoted_candidate() -> dict:
    """已 PROMOTED 的 bulk-fill：verification（5 步 causal_chain）+ 旧 promotion 记录。"""
    verification = {
        "direct_target_paper_count": 4,
        "causal_status": "EXISTING_MECHANISM_COMPOSITION",
        "causal_chain": [
            {"step": "Bulk-fill composite formulations are designed with specific "
                     "composition (e.g., filler content, monomer type) and curing "
                     "characteristics.", "evidence": "ev1", "paper_id": "W1",
             "evidence_type": "DIRECT"},
            {"step": "The formulation composition directly influences polymerization "
                     "shrinkage magnitude.", "evidence": "ev2", "paper_id": "W2",
             "evidence_type": "DIRECT"},
            {"step": "Polymerization shrinkage generates shrinkage stress at the "
                     "tooth-restoration interface.", "evidence": "ev3", "paper_id": "W3",
             "evidence_type": "DIRECT"},
            {"step": "Shrinkage stress can cause interfacial debonding and restoration "
                     "failure.", "evidence": "ev4", "paper_id": "W4",
             "evidence_type": "DIRECT"},
            {"step": "Bulk-fill composite formulations are specifically engineered to "
                     "reduce shrinkage stress.", "evidence": "ev5", "paper_id": "W5",
             "evidence_type": "DIRECT"},
        ],
        "supporting_papers": ["W1", "W2", "W3", "W4", "W5"],
    }
    # 旧 promotion 记录（grounding 缺陷版：target 是证据句）
    old_promotion = {
        "candidate_id": "c1", "candidate_name": "bulk-fill composite formulation",
        "candidate_type": "FORMULATION_STRATEGY", "action": "NEW_FORMULATION_STRATEGY",
        "status": "APPLIED",
        "proposed_relations": [
            {"source": "bulk-fill composite formulation", "relation": "affects",
             "target": "Bulk-fill composite formulations are designed with specific ",
             "evidence_type": "DIRECT", "paper_ids": ["W1"], "evidence": ["ev1"]},
        ],
        "review_log": [{"action": "APPLIED", "by": "promoter"}],
    }
    cand = {
        "candidate_id": "c1", "raw_name": "bulk-fill composite formulation",
        "candidate_type": "FORMULATION_STRATEGY", "source": "scanner",
        "domain_relevance": "HIGH", "status": "PROMOTED",
        "independent_paper_count": 4,
        "provenance": {"verification": verification, "promotion": old_promotion},
        "review_log": [{"to": "PROMOTED", "action": "PROMOTION_APPLIED", "by": "promoter"}],
    }
    return cand


def _old_promotion_record() -> dict:
    return {
        "candidate_id": "c1", "candidate_name": "bulk-fill composite formulation",
        "candidate_type": "FORMULATION_STRATEGY", "action": "NEW_FORMULATION_STRATEGY",
        "status": "APPLIED",
        "proposed_relations": [
            {"source": "bulk-fill composite formulation", "relation": "affects",
             "target": "Bulk-fill composite formulations are designed with specific ",
             "evidence_type": "DIRECT", "paper_ids": ["W1"], "evidence": ["ev1"]},
        ],
        "review_log": [{"action": "APPLIED", "by": "promoter"}],
    }


# ── 验收 ①：不改变 candidate PROMOTED 状态 ──

def test_repair_keeps_candidate_promoted():
    cand = _promoted_candidate()
    cands, promos = [cand], [_old_promotion_record()]
    cand_out, promos_out, msgs = repair_grounding(
        cands, promos, "bulk-fill composite formulation",
        "relation grounding v2: ordered grounding + self-loop removal + predicate typing")
    assert cand_out is not None
    assert cand_out["status"] == "PROMOTED", "GROUNDING_REPAIR 不能改变 candidate 状态"
    assert any("status 保持 PROMOTED" in m for m in msgs)


def test_repair_rejects_non_promoted():
    cand = _promoted_candidate()
    cand["status"] = "VALIDATED"
    cand_out, _, msgs = repair_grounding(
        [cand], [_old_promotion_record()], "bulk-fill composite formulation", "test")
    assert cand_out is None
    assert any("只针对已 PROMOTED" in m for m in msgs)


# ── 验收 ②：保留旧 revision（原 promotion 记录原样）──

def test_repair_keeps_old_promotion_untouched():
    cand = _promoted_candidate()
    promos = [_old_promotion_record()]
    old_snapshot = json.dumps(promos[0], ensure_ascii=False, sort_keys=True)
    _, promos_out, _ = repair_grounding(
        [cand], promos, "bulk-fill composite formulation", "test")
    # 原字段不动：status 仍 APPLIED、旧 proposed_relations 原样保留
    record = promos_out[0]
    assert record["status"] == "APPLIED"
    assert record["proposed_relations"][0]["target"].startswith("Bulk-fill composite")
    # 只新增 revisions 键
    assert json.dumps({k: v for k, v in record.items() if k != "revisions"},
                      ensure_ascii=False, sort_keys=True) == old_snapshot
    assert len(record["revisions"]) == 1


def test_repair_preserves_old_relation_in_history():
    """候选 provenance.promotion（initial）不动，promotion_history 追加。"""
    cand = _promoted_candidate()
    old_promo_snapshot = json.dumps(cand["provenance"]["promotion"],
                                    ensure_ascii=False, sort_keys=True)
    _, _, _ = repair_grounding([cand], [_old_promotion_record()],
                               "bulk-fill composite formulation", "test")
    assert json.dumps(cand["provenance"]["promotion"],
                      ensure_ascii=False, sort_keys=True) == old_promo_snapshot
    history = cand["provenance"]["promotion_history"]
    assert [h["action"] for h in history] == ["PROMOTION", "GROUNDING_REPAIR"]
    assert history[0]["version"] == 1
    assert history[1]["version"] == 2


# ── 验收 ③：新 relation revision supersede 旧 relation ──

def test_repair_new_relations_supersede_old():
    cand = _promoted_candidate()
    _, promos_out, _ = repair_grounding(
        [cand], [_old_promotion_record()], "bulk-fill composite formulation", "test")
    rev = promos_out[0]["revisions"][0]
    assert rev["action"] == "GROUNDING_REPAIR"
    assert rev["supersedes_revision"] == 1
    assert rev["node_promotion"] == {"status": "UNCHANGED"}
    assert rev["reason"]
    # 新 relations 是 grounding 层重建（writable = GROUNDED+DIRECT），
    # target 全是 node（filler content/polymerization shrinkage/shrinkage stress/interfacial debonding），
    # 没有证据句（旧版缺陷已修复）
    assert len(rev["relations"]) == 5
    targets = [r["target"] for r in rev["relations"]]
    assert all(len(t) <= 40 and t.count(" ") <= 6 for t in targets), \
        "target 必须是 node 不是证据句"
    assert "polymerization shrinkage" in targets
    assert "filler content" in targets
    # 每条都带 paper_ids（provenance 可追溯）
    assert all(r["paper_ids"] for r in rev["relations"])
    # 主链保序：bulk-fill → filler content → polymerization shrinkage → shrinkage stress
    chain = [(r["source"], r["predicate"], r["target"]) for r in rev["relations"]]
    assert ("bulk-fill composite formulation", "has_design_factor", "filler content") in chain
    assert ("polymerization shrinkage", "contributes_to", "shrinkage stress") in chain
    assert ("shrinkage stress", "contributes_to", "interfacial debonding") in chain
    # 旧缺陷不复发：has_design_factor 不能指向 EFFECT
    assert not any(r["predicate"] == "has_design_factor"
                   and r["target"] == "polymerization shrinkage" for r in rev["relations"])


def test_build_grounding_revision_deterministic():
    """revision 构建是纯本地确定性函数（无文件 IO / LLM / 搜索）。"""
    import inspect
    from tools import repair_promotion_grounding as m
    src = inspect.getsource(m.build_grounding_revision) + inspect.getsource(m.repair_grounding)
    assert "DeepSeek" not in src and "openalex" not in src.lower()
