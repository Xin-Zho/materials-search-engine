"""Phase 2.0 discovery 模块测试：DiscoveryCandidate / scanner / typer / canonical_filter / 状态机。"""

import pytest

from search_engine.discovery import (
    DiscoveryCandidate, RawCandidate,
    canonical_match, existing_knowledge_match,
    type_candidate, domain_relevance_level, can_verify, verification_priority, can_promote,
    InvalidTransition,
)
from search_engine.discovery.scanner import scan_kb
from search_engine.discovery.candidate import STATUS_FLOW
from search_engine.knowledge_base import KnowledgeBase


# ── canonical_filter ──

def test_alias_monomer_design():
    """'monomer design' → monomer-design（route key 拼写变体）。"""
    assert canonical_match("monomer design") == "monomer-design"


def test_alias_mechanism_exact():
    """canonical mechanism 精确匹配。"""
    assert canonical_match("stress relaxation") == "stress relaxation"


def test_existing_knowledge_family():
    """reduced polymerization shrinkage → reduced shrinkage（修饰语 + 家族名词，无维度后缀）。"""
    assert existing_knowledge_match("reduced polymerization shrinkage") == "reduced shrinkage"


def test_shrinkage_stress_not_merged():
    """reduced shrinkage stress 是独立 EFFECT，不归 reduced shrinkage（≠，不能合并）。"""
    assert existing_knowledge_match("reduced shrinkage stress") is None


def test_no_shrinkage_overmatch():
    """共享 shrinkage 不等于同一知识：rate / time-to-peak / force 维度独立（用户定 2026-08-26）。"""
    assert existing_knowledge_match("increased shrinkage rate") is None
    assert existing_knowledge_match("reduced time to maximum shrinkage force rate") is None
    assert existing_knowledge_match("reduced volumetric shrinkage stress") is None
    assert existing_knowledge_match("reduced polymerization shrinkage stress") is None


def test_no_false_merge():
    """reduced shrinkage stress ≠ reduced shrinkage strain（用户定：不能合并）。"""
    assert existing_knowledge_match("reduced shrinkage stress") != "reduced shrinkage strain"


# ── typer ──

def test_type_process_strategy():
    assert type_candidate("incremental curing") == "PROCESS_STRATEGY"


def test_type_mechanism():
    assert type_candidate("dynamic covalent bond exchange") == "MECHANISM"


def test_type_formulation():
    assert type_candidate("bulk-fill composite formulation") == "FORMULATION_STRATEGY"


def test_type_effect():
    assert type_candidate("reduced polymerization shrinkage") == "EFFECT"


def test_type_capability():
    assert type_candidate("self-healing") == "MATERIAL_CAPABILITY"


def test_type_design_variable():
    assert type_candidate("molecular weight control") == "SUB_ROUTE"


def test_type_context_term():
    """photopolymerization 是领域背景概念（不是需扩展 ontology 的知识节点），用户定 2026-08-26。"""
    assert type_candidate("photopolymerization") == "CONTEXT_TERM"
    assert type_candidate("resin") == "CONTEXT_TERM"
    assert type_candidate("composite") == "CONTEXT_TERM"
    # 机制组合不误判 context（ring-opening polymerization 是核心机制路径）
    assert type_candidate("ring-opening polymerization") != "CONTEXT_TERM"


# ── domain relevance ──

def test_relevance_high():
    level, score = domain_relevance_level(
        "reduced shrinkage stress",
        ["photopolymerization reduces polymerization shrinkage stress during curing"])
    assert level == "HIGH"
    assert score >= 0.75


def test_relevance_unknown():
    level, score = domain_relevance_level("trans-linkage isomerism", [])
    assert level in ("UNKNOWN", "LOW")


# ── 状态机 ──

def test_status_flow_shape():
    assert STATUS_FLOW["CANDIDATE"] == ["VERIFYING"]
    assert set(STATUS_FLOW["VERIFYING"]) == {
        "REJECTED", "ADJACENT", "VALIDATED", "NEED_MORE_EVIDENCE", "SEARCH_INCONCLUSIVE"}
    assert STATUS_FLOW["VALIDATED"] == ["PROMOTED"]
    assert STATUS_FLOW["NEED_MORE_EVIDENCE"] == ["VERIFYING"]  # 下一轮继续验证
    # Phase 2.1：SEARCH_INCONCLUSIVE 可重试 1 次或直接冻结（自动重试 1 次后走 FROZEN）
    assert set(STATUS_FLOW["SEARCH_INCONCLUSIVE"]) == {"VERIFYING", "SEARCH_INCONCLUSIVE_FROZEN"}
    assert STATUS_FLOW["SEARCH_INCONCLUSIVE_FROZEN"] == []


def test_transition():
    c = DiscoveryCandidate(candidate_id="x", raw_name="incremental curing", status="CANDIDATE")
    assert c.transition("VERIFYING")
    assert c.transition("VALIDATED")
    assert c.transition("PROMOTED")
    assert c.status == "PROMOTED"


def test_transition_invalid():
    c = DiscoveryCandidate(candidate_id="x", raw_name="a", status="CANDIDATE")
    with pytest.raises(InvalidTransition):
        c.transition("PROMOTED")  # 必须走 VERIFYING
    assert c.status == "CANDIDATE"


# ── MANUAL_REOPEN（用户定：自动流程不能 reopen，人工审计可以）──

def test_terminal_no_reopen_without_override():
    """ADJACENT → VERIFYING 无 manual_override → reject（终态防无意义循环）。"""
    c = DiscoveryCandidate(candidate_id="x", raw_name="a", status="ADJACENT")
    with pytest.raises(InvalidTransition):
        c.transition("VERIFYING")
    assert c.status == "ADJACENT"


def test_terminal_reopen_with_override():
    """ADJACENT → VERIFYING + manual_override + reason → allow（MANUAL_REOPEN）。"""
    c = DiscoveryCandidate(candidate_id="x", raw_name="a", status="ADJACENT")
    assert c.transition("VERIFYING", manual_override=True,
                        reason="verifier 修复：seed evidence 进语料，重跑")
    assert c.status == "VERIFYING"


def test_rejected_reopen_with_override():
    """REJECTED → VERIFYING + manual_override + reason → allow。"""
    c = DiscoveryCandidate(candidate_id="x", raw_name="a", status="REJECTED")
    assert c.transition("VERIFYING", manual_override=True, reason="新证据出现，重审")
    assert c.status == "VERIFYING"


def test_terminal_reopen_requires_reason():
    """ADJACENT → VERIFYING + override 但 reason 为空 → reject（审计必须留痕）。"""
    c = DiscoveryCandidate(candidate_id="x", raw_name="a", status="ADJACENT")
    with pytest.raises(InvalidTransition):
        c.transition("VERIFYING", manual_override=True, reason="")
    assert c.status == "ADJACENT"


def test_validated_reopen_requires_override():
    """VALIDATED → VERIFYING 无 override → reject（自动流程不能重审已验证节点）。"""
    c = DiscoveryCandidate(candidate_id="x", raw_name="a", status="VALIDATED")
    with pytest.raises(InvalidTransition):
        c.transition("VERIFYING")
    assert c.status == "VALIDATED"


def test_validated_revalidation():
    """VALIDATED → VERIFYING + override + reason → allow（REVALIDATION：旧 bug 产生的错误
    VALIDATED 需要重审；用户定：低频人工操作 + reason 必填，不会形成无意义循环）。"""
    c = DiscoveryCandidate(candidate_id="x", raw_name="bulk-fill composite formulation",
                           status="VALIDATED")
    assert c.transition("VERIFYING", manual_override=True,
                        reason="旧 verifier bug 虚 target evidence，重审")
    assert c.status == "VERIFYING"


def test_validated_revalidation_requires_reason():
    """REVALIDATION 必须 reason 非空（审计留痕）。"""
    c = DiscoveryCandidate(candidate_id="x", raw_name="a", status="VALIDATED")
    with pytest.raises(InvalidTransition):
        c.transition("VERIFYING", manual_override=True, reason="")
    assert c.status == "VALIDATED"


def test_candidate_manual_override():
    """CANDIDATE → REJECTED 人工快速判定（MANUAL_OVERRIDE）。"""
    c = DiscoveryCandidate(candidate_id="x", raw_name="a", status="CANDIDATE")
    assert c.transition("REJECTED", manual_override=True, reason="人工判断无关")
    assert c.status == "REJECTED"


def test_need_more_evidence_loop():
    """NEED_MORE_EVIDENCE 允许回 VERIFYING（1 篇强 DIRECT 但不足 2 篇的中间态）。"""
    c = DiscoveryCandidate(candidate_id="x", raw_name="a", status="VERIFYING")
    assert c.transition("NEED_MORE_EVIDENCE")
    assert c.transition("VERIFYING")
    assert c.status == "VERIFYING"


def test_search_inconclusive_loop():
    """SEARCH_INCONCLUSIVE 允许回 VERIFYING（修正检索后再验证）。"""
    c = DiscoveryCandidate(candidate_id="x", raw_name="a", status="VERIFYING")
    assert c.transition("SEARCH_INCONCLUSIVE")
    assert c.transition("VERIFYING")
    assert c.status == "VERIFYING"


# ── verifier：decide_verdict / 词族 / 三层计数 ──

def _vr(**kw) -> "VerificationResult":
    from search_engine.discovery.verifier import VerificationResult
    defaults = dict(candidate_id="x", candidate_name="c",
                    concept_independent=True, novel_to_ontology=True)
    defaults.update(kw)
    return VerificationResult(**defaults)


def test_verdict_search_inconclusive():
    """新搜连候选概念论文都没召回（search_concept=0 且 search_target=0）→ SEARCH_INCONCLUSIVE。"""
    from search_engine.discovery.verifier import decide_verdict
    r = _vr(domain_relevance="LOW", retrieval_quality="INVALID",
            verification_sufficiency="INSUFFICIENT",
            search_concept_related_count=0, search_target_related_count=0,
            direct_target_paper_count=0)
    assert decide_verdict(r) == "SEARCH_INCONCLUSIVE"


def test_verdict_seed_cannot_mask_retrieval_failure():
    """seed 命中 >0 但 search 全失败 → 仍是 SEARCH_INCONCLUSIVE（bulk-fill S0 场景）。"""
    from search_engine.discovery.verifier import decide_verdict
    r = _vr(domain_relevance="LOW", retrieval_quality="INVALID",
            verification_sufficiency="INSUFFICIENT",
            seed_concept_related_count=2,   # seed 证明概念存在
            search_concept_related_count=0, # 但本次验证检索完全失败
            search_target_related_count=0,
            direct_target_paper_count=0)
    assert decide_verdict(r) == "SEARCH_INCONCLUSIVE"  # 不能跳成 ADJACENT


def test_verdict_adjacent():
    """检索充分（GOOD）+ 大量候选文献 + 无 candidate↔target 关系 → ADJACENT。"""
    from search_engine.discovery.verifier import decide_verdict
    r = _vr(domain_relevance="LOW", retrieval_quality="GOOD",
            verification_sufficiency="PARTIAL",
            search_concept_related_count=5, search_target_related_count=1,
            direct_target_paper_count=0, direct_relation_paper_count=0)
    assert decide_verdict(r) == "ADJACENT"


def test_verdict_partial_retrieval_no_target_inconclusive():
    """检索 PARTIAL 且无 target 证据 → SEARCH_INCONCLUSIVE（候选文献召回不足不能下判断）。"""
    from search_engine.discovery.verifier import decide_verdict
    r = _vr(domain_relevance="MEDIUM", retrieval_quality="PARTIAL",
            verification_sufficiency="PARTIAL",
            search_concept_related_count=1, search_target_related_count=0,
            direct_target_paper_count=0)
    assert decide_verdict(r) == "SEARCH_INCONCLUSIVE"


def test_verdict_bulkfill_partial_quality_validated():
    """bulk-fill 场景：retrieval_quality=PARTIAL 但证据充分（5 target + 5 独立 + validated）
    → VALIDATED。检索质量 ≠ 验证充分性，PARTIAL 不否决（用户定 2026-08-26 核心修正）。"""
    from search_engine.discovery.verifier import decide_verdict
    r = _vr(domain_relevance="HIGH", retrieval_quality="PARTIAL",   # 36% 命中
            verification_sufficiency="SUFFICIENT",
            search_concept_related_count=8, search_target_related_count=5,
            candidate_validated=True, causal_status="PARTIAL_CAUSAL_EVIDENCE",
            direct_target_paper_count=5, direct_relation_paper_count=5,
            supporting_papers=[f"W{i}" for i in range(5)])
    assert decide_verdict(r) == "VALIDATED"


def test_verdict_validated():
    """节点有效 + target DIRECT ≥2 篇独立论文 + 检索 GOOD → VALIDATED（paper count 口径）。"""
    from search_engine.discovery.verifier import decide_verdict
    r = _vr(domain_relevance="HIGH", retrieval_quality="GOOD",
            verification_sufficiency="SUFFICIENT",
            search_concept_related_count=5, search_target_related_count=3,
            candidate_validated=True,
            direct_target_paper_count=2,
            direct_target_evidence_count=3,
            causal_chain=[{"step": "a", "evidence_type": "DIRECT"}],
            supporting_papers=["W1", "W2"])
    assert decide_verdict(r) == "VALIDATED"


def test_verdict_evidence_count_not_paper_count():
    """evidence 2 条但只有 1 篇独立论文 → 不能 VALIDATED（两句话 ≠ 两篇论文）。"""
    from search_engine.discovery.verifier import decide_verdict
    r = _vr(domain_relevance="HIGH", retrieval_quality="GOOD",
            verification_sufficiency="PARTIAL",
            search_concept_related_count=2,
            candidate_validated=True,
            direct_target_evidence_count=2,   # 2 条 evidence
            direct_target_paper_count=1,      # 但只有 1 篇论文
            causal_chain=[{"step": "a", "evidence_type": "DIRECT"}],
            supporting_papers=["W1"])
    assert decide_verdict(r) != "VALIDATED"
    assert decide_verdict(r) == "NEED_MORE_EVIDENCE"


def test_verdict_need_more_evidence():
    """只有 mechanism 层证据（relation）没有 target → NEED_MORE_EVIDENCE（不能 VALIDATED）。"""
    from search_engine.discovery.verifier import decide_verdict
    r = _vr(domain_relevance="MEDIUM", retrieval_quality="GOOD",
            verification_sufficiency="PARTIAL",
            search_concept_related_count=2,
            direct_relation_paper_count=1, direct_target_paper_count=0,
            supporting_papers=["W1"])
    assert decide_verdict(r) == "NEED_MORE_EVIDENCE"


def test_verdict_existing_composition():
    """FORMULATION_STRATEGY：causal_status=EXISTING_MECHANISM_COMPOSITION 不影响节点有效性。"""
    from search_engine.discovery.verifier import decide_verdict
    r = _vr(domain_relevance="HIGH", retrieval_quality="GOOD",
            verification_sufficiency="SUFFICIENT",
            search_concept_related_count=3,
            candidate_validated=True, causal_status="EXISTING_MECHANISM_COMPOSITION",
            direct_target_paper_count=2, supporting_papers=["W1", "W2"])
    assert decide_verdict(r) == "VALIDATED"


def test_retrieval_quality_and_sufficiency():
    """retrieval_quality（搜索质量）与 verification_sufficiency（证据充分性）分开。"""
    from tools.verify_candidate import _retrieval_quality as rq
    from search_engine.discovery.verifier import verification_sufficiency_level
    assert rq(0, 49) == "INVALID"      # bulk-fill 旧场景 0/49
    assert rq(8, 22) == "PARTIAL"      # bulk-fill 新场景 36%
    assert rq(6, 10) == "GOOD"
    # 5 篇 DIRECT target + 5 独立支撑 → SUFFICIENT（即使 retrieval PARTIAL）
    r = _vr(domain_relevance="HIGH", direct_target_paper_count=5,
            direct_target_evidence_count=5,
            search_concept_related_count=8,
            supporting_papers=[f"W{i}" for i in range(5)])
    assert verification_sufficiency_level(r) == "SUFFICIENT"
    # 只有 1 篇 target → PARTIAL
    r2 = _vr(domain_relevance="HIGH", direct_target_paper_count=1,
             search_concept_related_count=2, supporting_papers=["W1"])
    assert verification_sufficiency_level(r2) == "PARTIAL"
    # 无命中 → INSUFFICIENT
    r3 = _vr(domain_relevance="UNKNOWN", search_concept_related_count=0,
             direct_target_paper_count=0)
    assert verification_sufficiency_level(r3) == "INSUFFICIENT"


def test_effective_concept_implication():
    """DIRECT_target ⇒ DIRECT_relation ⇒ DIRECT_concept 向上蕴含（报告口径）。"""
    from search_engine.discovery.verifier import VerificationResult
    r = VerificationResult(candidate_id="x", candidate_name="bulk-fill composite formulation",
                           supporting_papers=["W1", "W2", "W3", "W4", "W5"],
                           direct_concept_paper_count=0,   # LLM 没单列 concept
                           direct_relation_paper_count=5, direct_target_paper_count=5)
    assert r.effective_concept_paper_count == 5  # union 向上蕴含


def test_candidate_family_bulkfill():
    """bulk-fill 词族：不能只 raw_name × 固定后缀。"""
    from search_engine.discovery.verifier import build_candidate_family
    fam = build_candidate_family("bulk-fill composite formulation")
    assert "bulk-fill composite" in fam
    assert "bulk fill composite" in fam
    assert "bulk-fill resin composite" in fam
    assert "bulk-fill composite formulation" in fam  # 原词保留


def test_candidate_family_rule_variants():
    """规则变体：去括号内容 + 去尾部 formulation。"""
    from search_engine.discovery.verifier import build_candidate_family
    fam = build_candidate_family("monomer design (tricyclic oxanorbornenes)")
    assert any("tricyclic oxanorbornenes" not in f for f in fam)  # 括号内容被去掉的变体存在


def test_query_plan_family():
    """查询用词族组合（relevance 类含 target family 词）。"""
    from search_engine.discovery.verifier import build_verification_queries
    plan = build_verification_queries("incremental curing")
    rel_qs = [q for p in plan if p["type"] == "relevance" for q in p["queries"]]
    assert any("polymerization shrinkage stress" in q for q in rel_qs)
    assert any("shrinkage stress" in q for q in rel_qs)


# ── can_verify 验证入口（用户定 2026-08-26：入口宽）──

def test_can_verify_seed_zero_papers():
    """human_seed + 0 篇 + MECHANISM → 可验证（verifier 主动找证据）。"""
    c = DiscoveryCandidate(candidate_id="x", raw_name="dynamic covalent bond exchange",
                           candidate_type="MECHANISM", source="human_seed",
                           independent_paper_count=0, status="CANDIDATE")
    assert can_verify(c)


def test_can_verify_rel_unknown():
    """FORMULATION_STRATEGY + rel=UNKNOWN → 可验证（Q2 由 verifier 回答，不循环定义）。"""
    c = DiscoveryCandidate(candidate_id="x", raw_name="bulk-fill composite formulation",
                           candidate_type="FORMULATION_STRATEGY", domain_relevance="UNKNOWN",
                           independent_paper_count=2, status="CANDIDATE")
    assert can_verify(c)


def test_can_verify_unknown_blocked():
    """UNKNOWN type 不允许进 VERIFYING（regression：refractive index modulation 场景）。"""
    c = DiscoveryCandidate(candidate_id="x", raw_name="refractive index modulation",
                           candidate_type="UNKNOWN", domain_relevance="HIGH",
                           independent_paper_count=2, status="CANDIDATE")
    assert not can_verify(c)


def test_can_verify_context_term_blocked():
    """CONTEXT_TERM 不允许进 VERIFYING（photopolymerization：领域背景，novel discovery = no）。"""
    c = DiscoveryCandidate(candidate_id="x", raw_name="photopolymerization",
                           candidate_type="CONTEXT_TERM", domain_relevance="HIGH",
                           independent_paper_count=2, status="CANDIDATE")
    assert not can_verify(c)


def test_can_verify_existing_blocked():
    """canonical_match 非空（existing knowledge）不允许进 VERIFYING。"""
    c = DiscoveryCandidate(candidate_id="x", raw_name="a", candidate_type="EFFECT",
                           canonical_match="reduced shrinkage", independent_paper_count=2,
                           status="CANDIDATE")
    assert not can_verify(c)


def test_can_verify_single_paper_ok():
    """1 篇也允许进验证（入口宽；≥2 篇是 verification_priority 和 promotion 的事）。"""
    c = DiscoveryCandidate(candidate_id="x", raw_name="a", candidate_type="MECHANISM",
                           domain_relevance="HIGH", independent_paper_count=1, status="CANDIDATE")
    assert can_verify(c)
    assert not verification_priority(c)  # 但不在自动优先池


def test_verification_priority():
    """自动验证优先池：can_verify + ≥2 篇 + rel≥MEDIUM（shortlist，非门槛）。"""
    c = DiscoveryCandidate(candidate_id="x", raw_name="incremental curing",
                           candidate_type="PROCESS_STRATEGY", domain_relevance="MEDIUM",
                           independent_paper_count=2, status="CANDIDATE")
    assert can_verify(c) and verification_priority(c)


# ── can_promote 严格出口（用户定 7 条）──

def test_can_promote_seed_not_enough():
    """seed 无 2 篇/target paper evidence → can_promote=False（出口严，和入口宽不冲突）。"""
    c = DiscoveryCandidate(candidate_id="x", raw_name="dynamic covalent bond exchange",
                           candidate_type="MECHANISM", source="human_seed",
                           independent_paper_count=1, domain_relevance="MEDIUM",
                           status="VALIDATED")
    ok, missing = can_promote(c, verification={
        "direct_target_paper_count": 1,   # 只有 1 篇，② 不满足
        "causal_chain": [{"step": "a"}],
        "causal_status": "NOVEL_CAUSAL_CHAIN",
        "ontology_position": "x"})
    assert not ok
    assert any("②" in m for m in missing)  # ≥2 篇未满足


def test_can_promote_target_evidence_required():
    """只有机制层证据（relation）没有 target 证据 → 不能 promote（用户定：不能冒充）。"""
    c = DiscoveryCandidate(candidate_id="x", raw_name="dynamic covalent bond exchange",
                           candidate_type="MECHANISM", domain_relevance="MEDIUM",
                           independent_paper_count=2, status="VALIDATED")
    ok, missing = can_promote(c, verification={
        "direct_concept_paper_count": 1,   # 概念存在 ≠ 能降光固化收缩
        "direct_relation_paper_count": 1,  # 机制链存在 ≠ target 证据
        "direct_target_paper_count": 0,
        "causal_chain": [{"step": "a"}],
        "causal_status": "PARTIAL_CAUSAL_EVIDENCE",
        "ontology_position": "x"})
    assert not ok
    assert any("③" in m for m in missing)


def test_can_promote_full():
    """全部 7 条满足（target paper ≥1 + causal_status 明确 + 位置）→ can_promote=True。"""
    c = DiscoveryCandidate(candidate_id="x", raw_name="bulk-fill composite formulation",
                           candidate_type="FORMULATION_STRATEGY", domain_relevance="MEDIUM",
                           independent_paper_count=2, status="VALIDATED")
    ok, missing = can_promote(c, verification={
        "direct_target_paper_count": 2,
        "causal_chain": [{"step": "filler loading"}, {"step": "reduced shrinkage stress"}],
        "causal_status": "EXISTING_MECHANISM_COMPOSITION",
        "ontology_position": "FORMULATION_STRATEGY 层",
    })
    assert ok, missing


# ── source 字段（scanner / human_seed / hypothesis_seed）──

def test_source_default_scanner():
    c = DiscoveryCandidate(candidate_id="x", raw_name="a")
    assert c.source == "scanner"


def test_source_seed():
    c = DiscoveryCandidate(candidate_id="x", raw_name="dynamic covalent bond exchange",
                           candidate_type="MECHANISM", source="human_seed", status="CANDIDATE")
    assert c.source == "human_seed"
    assert can_verify.__name__  # sanity


# ── RawCandidate / from_raw ──

def test_from_raw():
    raw = RawCandidate(raw_name="incremental curing", kind="mechanism",
                       paper_ids={"p1", "p2"}, edge_count=3,
                       route_assoc={"filler"}, mechanism_assoc={"reduced shrinkage"})
    c = DiscoveryCandidate.from_raw(raw, candidate_type="PROCESS_STRATEGY")
    assert c.candidate_id  # 稳定生成
    assert c.independent_paper_count == 2
    assert c.source_papers == ["p1", "p2"]
    assert c.provenance["edge_count"] == 3
    assert c.provenance["route_assoc"] == ["filler"]
    assert c.source == "scanner"  # scanner 来源


# ── merge persistence（用户定：scanner rerun 不重置人工状态）──

def test_merge_pool_preserves_human_state():
    """rerun scanner 后：VERIFYING/status/review_log/human_seed 全部保留（regression）。"""
    from search_engine.discovery import merge_pool
    # 旧池：人工已处理过的候选（VERIFYING + review_log）和 human_seed
    old = [
        {"candidate_id": "aa", "raw_name": "incremental curing",
         "candidate_type": "PROCESS_STRATEGY", "source": "scanner",
         "status": "VERIFYING", "source_papers": ["p1"], "independent_paper_count": 1,
         "evidence": ["ev1"], "canonical_match": None, "domain_relevance": "MEDIUM",
         "provenance": {"edge_count": 2}, "review_log": [{"to": "VERIFYING", "by": "human_review"}]},
        {"candidate_id": "bb", "raw_name": "dynamic covalent bond exchange",
         "candidate_type": "MECHANISM", "source": "human_seed",
         "status": "VERIFYING", "source_papers": [], "independent_paper_count": 0,
         "evidence": [], "canonical_match": None, "domain_relevance": "MEDIUM",
         "provenance": {"seed_reason": "from self-healing / DCB"}, "review_log": []},
    ]
    # 新扫描：incremental curing 又发现 1 篇新论文；seed 不在扫描结果中（KB 无该词）
    new = [
        DiscoveryCandidate(candidate_id="aa", raw_name="incremental curing",
                           candidate_type="PROCESS_STRATEGY", source="scanner",
                           status="CANDIDATE", source_papers=["p1", "p2"],
                           independent_paper_count=2, evidence=["ev1", "ev2"],
                           domain_relevance="MEDIUM", provenance={"edge_count": 4}),
    ]
    merged = merge_pool(old, new)
    by_id = {c["candidate_id"]: c for c in merged}
    # 1) incremental curing：统计更新（2 篇），状态保留（VERIFYING 不被重置 CANDIDATE）
    assert by_id["aa"]["independent_paper_count"] == 2
    assert by_id["aa"]["source_papers"] == ["p1", "p2"]
    assert by_id["aa"]["status"] == "VERIFYING"
    assert by_id["aa"]["review_log"] == [{"to": "VERIFYING", "by": "human_review"}]
    # 2) human_seed：扫描没扫到也不消失，状态保留
    assert "bb" in by_id
    assert by_id["bb"]["source"] == "human_seed"
    assert by_id["bb"]["status"] == "VERIFYING"


def test_merge_pool_new_candidate_added():
    """新扫描发现的候选正常加入。"""
    from search_engine.discovery import merge_pool
    new = [DiscoveryCandidate(candidate_id="cc", raw_name="new thing",
                              candidate_type="MECHANISM", source="scanner",
                              status="CANDIDATE", source_papers=["p9"],
                              independent_paper_count=1, domain_relevance="LOW")]
    merged = merge_pool([], new)
    assert len(merged) == 1
    assert merged[0]["candidate_id"] == "cc"


# ── scanner（真实 KB，只读）──

def test_scan_kb_runs():
    kb = KnowledgeBase()
    raws = scan_kb(kb)
    kb.close()
    assert isinstance(raws, list)
    assert len(raws) > 50  # 高召回
    kinds = {r.kind for r in raws}
    assert kinds == {"route", "mechanism"}
    # 高召回：raw_name 非空
    assert all(r.raw_name for r in raws)
