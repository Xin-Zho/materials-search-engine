"""Phase 2.0 promoter 测试：promotion action 决策 + proposal 结构 + relation grounding + 验收 6 条。"""

import pytest

from search_engine.discovery import (
    DiscoveryCandidate,
    decide_action,
    build_proposal,
    PromotionRelation,
    PromotionProposal,
)
from search_engine.discovery.promoter import (
    ground_causal_chain, extract_grounded_nodes, default_known_nodes, _is_sentence_like,
)


def _validated_candidate(**kw) -> DiscoveryCandidate:
    base = dict(candidate_id="c1", raw_name="bulk-fill composite formulation",
                candidate_type="FORMULATION_STRATEGY", source="scanner",
                domain_relevance="HIGH", status="VALIDATED",
                independent_paper_count=5)
    base.update(kw)
    return DiscoveryCandidate(**base)


def _verification(**kw) -> dict:
    base = {
        "direct_target_paper_count": 5,
        "direct_relation_paper_count": 5,
        "causal_status": "PARTIAL_CAUSAL_EVIDENCE",
        "causal_chain": [
            {"step": "bulk-fill 改变 filler loading", "evidence": "原文",
             "paper_id": "W1", "evidence_type": "DIRECT"},
            {"step": "影响 polymerization shrinkage", "evidence": "原文",
             "paper_id": "W2", "evidence_type": "DIRECT"},
        ],
        "supporting_papers": ["W1", "W2", "W3"],
    }
    base.update(kw)
    return base


# ── 验收 ①：非 VALIDATED 不能 promotion ──

def test_no_promotion_if_not_validated():
    c = _validated_candidate(status="NEED_MORE_EVIDENCE")
    action, parent, warnings = decide_action(c, _verification())
    assert action == "NO_PROMOTION"
    assert any("非 VALIDATED" in w for w in warnings)


def test_no_promotion_if_unknown_type():
    c = _validated_candidate(candidate_type="UNKNOWN")
    action, _, _ = decide_action(c, _verification())
    assert action == "NO_PROMOTION"


# ── 验收 ③：candidate_type 与 ontology layer 对齐 ──

def test_bulkfill_formulation_strategy():
    """bulk-fill：FORMULATION_STRATEGY → NEW_FORMULATION_STRATEGY（不是 NEW_MECHANISM）。"""
    c = _validated_candidate()
    action, parent, warnings = decide_action(c, _verification())
    assert action == "NEW_FORMULATION_STRATEGY"
    assert parent is None
    assert any("不要解释成" in w for w in warnings)  # causal PARTIAL 警告


def test_mechanism_action():
    c = _validated_candidate(candidate_type="MECHANISM",
                             raw_name="dynamic covalent bond exchange")
    action, parent, _ = decide_action(c, _verification())
    assert action in ("NEW_CHILD_NODE", "NEW_TOP_LEVEL_NODE")


# ── 语义等价 → RELATION_ONLY ──

def test_relation_only_if_canonical_match():
    c = _validated_candidate(canonical_match="reduced shrinkage")
    action, parent, warnings = decide_action(c, _verification())
    assert action == "RELATION_ONLY"
    assert parent == "reduced shrinkage"


# ── 验收 ④⑤ + grounding：target 必须是 node，证据句不能当 node ──

def test_relation_target_must_be_node():
    """causal_chain 步骤是句子 → 不能当 target_node（UNRESOLVED，evidence 只进 raw_evidence）。"""
    c = _validated_candidate()
    v = _verification(causal_chain=[
        {"step": "Bulk-fill composite formulations are designed with specific composition",
         "evidence": "原文", "paper_id": "W1", "evidence_type": "DIRECT"},
        {"step": "The formulation composition directly influences polymerization shrinkage",
         "evidence": "原文", "paper_id": "W2", "evidence_type": "DIRECT"},
    ])
    p = build_proposal(c, v)
    unresolved = [r for r in p.proposed_relations if r.grounding_status != "GROUNDED"]
    grounded = [r for r in p.proposed_relations if r.grounding_status == "GROUNDED"]
    # 第一句（designed with specific composition）无节点 → UNRESOLVED
    assert unresolved, "句子步骤必须 UNRESOLVED 而不是当 node"
    # 第二句命中 polymerization shrinkage → GROUNDED
    assert any(r.target_node == "polymerization shrinkage" and r.grounding_status == "GROUNDED"
               for r in grounded)
    # target 不能是完整句子
    for r in p.proposed_relations:
        assert not _is_sentence_like(r.target_node), "target_node 不能是句子"


def test_chain_not_flattened():
    """causal chain A→B→C 保序成 A→B、B→C，禁止扁平成 A→B、A→C（用户核心）。"""
    c = _validated_candidate()
    v = _verification(causal_chain=[
        {"step": "polymerization shrinkage generates shrinkage stress",
         "evidence": "原文", "paper_id": "W1", "evidence_type": "DIRECT"},
        {"step": "shrinkage stress contributes to interfacial debonding",
         "evidence": "原文", "paper_id": "W2", "evidence_type": "DIRECT"},
    ])
    p = build_proposal(c, v, known_nodes=["polymerization shrinkage",
                                          "shrinkage stress", "interfacial debonding"])
    grounded = [r for r in p.proposed_relations if r.grounding_status == "GROUNDED"]
    pairs = [(r.source_node, r.predicate, r.target_node) for r in grounded]
    # 链式：polymerization shrinkage → shrinkage stress → interfacial debonding
    assert ("polymerization shrinkage", "contributes_to", "shrinkage stress") in pairs
    assert ("shrinkage stress", "contributes_to", "interfacial debonding") in pairs
    # 禁止扁平化：candidate 直接指向链尾
    assert not any(r.source_node == "bulk-fill composite formulation"
                   and r.target_node == "interfacial debonding" for r in grounded)


def test_grounded_chain_from_candidate():
    """链首锚定 candidate：bulk-fill --affects--> polymerization shrinkage。"""
    c = _validated_candidate()
    v = _verification(causal_chain=[
        {"step": "Bulk-fill formulation influences polymerization shrinkage",
         "evidence": "原文", "paper_id": "W1", "evidence_type": "DIRECT"},
    ])
    p = build_proposal(c, v, known_nodes=["polymerization shrinkage", "shrinkage stress"])
    grounded = [r for r in p.proposed_relations if r.grounding_status == "GROUNDED"]
    assert any(r.source_node == "bulk-fill composite formulation"
               and r.predicate == "affects"
               and r.target_node == "polymerization shrinkage" for r in grounded)


def test_inferred_not_writable():
    """INFERRED relation：可记录，但 writable=False（不能写正式 ontology）。"""
    c = _validated_candidate()
    v = _verification(causal_chain=[
        {"step": "可能通过 elastic modulus 影响 shrinkage stress",
         "evidence": "推测", "paper_id": "", "evidence_type": "INFERRED"},
    ])
    p = build_proposal(c, v, known_nodes=["elastic modulus", "shrinkage stress"])
    for r in p.proposed_relations:
        assert r.evidence_type == "INFERRED"
        assert not r.writable


def test_direct_grounded_writable():
    """DIRECT + GROUNDED → writable（可进 approval）。"""
    c = _validated_candidate()
    v = _verification(causal_chain=[
        {"step": "bulk-fill affects polymerization shrinkage",
         "evidence": "原文", "paper_id": "W1", "evidence_type": "DIRECT"},
    ])
    p = build_proposal(c, v, known_nodes=["polymerization shrinkage"])
    grounded = [r for r in p.proposed_relations if r.grounding_status == "GROUNDED"]
    assert grounded
    assert all(r.writable for r in grounded)
    assert any(r.paper_ids and r.raw_evidence for r in grounded)  # provenance 必带


def test_extra_relation_grounding_check():
    """人工 --relation：target 是 node → GROUNDED；非 node → NEW_NODE_REQUIRED/UNRESOLVED。"""
    c = _validated_candidate()
    known = default_known_nodes([c.raw_name])
    p = build_proposal(c, _verification(), extra_relations=[
        PromotionRelation(source_node="bulk-fill composite formulation",
                          predicate="affects", target_node="polymerization shrinkage"),
        PromotionRelation(source_node="bulk-fill composite formulation",
                          predicate="affects",
                          target_node="some free text sentence that is not a node"),
    ], known_nodes=known)
    by_target = {r.target_node: r for r in p.proposed_relations}
    assert by_target["polymerization shrinkage"].grounding_status == "GROUNDED"
    assert by_target["some free text sentence that is not a node"].grounding_status in (
        "NEW_NODE_REQUIRED", "UNRESOLVED")


# ── node_status / relation_status 拆分（用户定：node 与 relation 分开验收）──

def test_node_relation_status_split():
    c = _validated_candidate()
    p = build_proposal(c, _verification(), known_nodes=["polymerization shrinkage",
                                                        "shrinkage stress"])
    assert p.node_status == "PROPOSED"
    assert p.approve()
    assert p.node_status == "APPROVED"
    # 有 UNRESOLVED relation → relation_status = NEEDS_GROUNDING（node 不受影响）
    assert p.relation_status == "NEEDS_GROUNDING"
    assert p.apply()
    assert p.node_status == "APPLIED"
    assert p.relation_status == "NEEDS_GROUNDING"  # relation 未全部 grounded 不 APPLIED


def test_proposal_state_machine():
    """PROPOSED → APPROVED → APPLIED；REJECTED 从 PROPOSED/APPROVED 均可。"""
    c = _validated_candidate()
    p = build_proposal(c, _verification())
    assert p.status == "PROPOSED"
    assert not p.apply()  # 未 APPROVED 不能 APPLY
    assert p.approve()
    assert p.status == "APPROVED"
    assert p.apply()
    assert p.status == "APPLIED"
    assert not p.apply()  # 已 APPLIED 不能再 APPLY


def test_proposal_reject():
    c = _validated_candidate()
    p = build_proposal(c, _verification())
    assert p.reject("人工否决：证据不足")
    assert p.status == "REJECTED"
    assert p.node_status == "REJECTED"
    assert p.review_log[-1]["action"] == "REJECTED"


# ── 验收 ②：promoter 不重做验证（只读 verification dict，无 LLM/搜索调用）──

def test_build_proposal_no_llm():
    """build_proposal 是纯本地确定性函数（无外部调用），verification 直接传入。"""
    import inspect
    from search_engine.discovery import promoter
    src = inspect.getsource(promoter)
    assert "DeepSeek" not in src and "search(" not in src


# ── grounding 辅助函数 ──

def test_sentence_like_detection():
    assert _is_sentence_like("The formulation composition directly influences polymerizati")
    assert not _is_sentence_like("polymerization shrinkage")
    assert not _is_sentence_like("shrinkage stress")


def test_extract_nodes_longest_first():
    """长词优先：'polymerization shrinkage generates stress' 命中 shrinkage stress 而非 shrinkage。"""
    known = ["shrinkage", "shrinkage stress", "polymerization shrinkage"]
    hits = extract_grounded_nodes("polymerization shrinkage generates shrinkage stress", known)
    assert "polymerization shrinkage" in hits
    assert "shrinkage stress" in hits


# ── Predicate type constraint（用户定：predicate 不能只看动词，还要看 source/target 类型）──

def test_predicate_conflict_has_design_factor_to_effect():
    """FORMULATION_STRATEGY --has_design_factor--> EFFECT（polymerization shrinkage）
    → PREDICATE_TYPE_CONFLICT → 自动回退 affects（用户验收主链修复）。"""
    c = _validated_candidate()
    v = _verification(causal_chain=[
        {"step": "Bulk-fill formulations are designed with composition "
                 "influencing polymerization shrinkage",
         "evidence": "原文", "paper_id": "W1", "evidence_type": "DIRECT"},
    ])
    p = build_proposal(c, v, known_nodes=["polymerization shrinkage"])
    grounded = [r for r in p.proposed_relations if r.grounding_status == "GROUNDED"]
    assert grounded, "必须产出 GROUNDED relation"
    assert any(r.predicate == "affects" and r.target_node == "polymerization shrinkage"
               for r in grounded)
    # has_design_factor 绝不能指向 EFFECT
    assert not any(r.predicate == "has_design_factor" and r.grounding_status == "GROUNDED"
                   for r in grounded)


def test_predicate_affects_to_effect_allowed():
    """FORMULATION_STRATEGY --affects--> EFFECT → 允许（不回退）。"""
    c = _validated_candidate()
    v = _verification(causal_chain=[
        {"step": "bulk-fill formulation affects polymerization shrinkage",
         "evidence": "原文", "paper_id": "W1", "evidence_type": "DIRECT"},
    ])
    p = build_proposal(c, v, known_nodes=["polymerization shrinkage"])
    grounded = [r for r in p.proposed_relations if r.grounding_status == "GROUNDED"]
    assert any(r.predicate == "affects" and r.target_node == "polymerization shrinkage"
               and r.grounding_status == "GROUNDED" for r in grounded)


def test_predicate_has_design_factor_to_parameter_allowed():
    """has_design_factor → FORMULATION_PARAMETER（filler content）→ 允许不回退。"""
    c = _validated_candidate()
    v = _verification(causal_chain=[
        {"step": "Bulk-fill formulations are designed with specific filler content",
         "evidence": "原文", "paper_id": "W1", "evidence_type": "DIRECT"},
    ])
    p = build_proposal(c, v, known_nodes=["filler content"])
    grounded = [r for r in p.proposed_relations if r.grounding_status == "GROUNDED"]
    assert any(r.predicate == "has_design_factor" and r.target_node == "filler content"
               and r.grounding_status == "GROUNDED" for r in grounded)


def test_hyphenated_node_not_sentence_like():
    """含连字符的节点名（bulk-fill composite formulation）不能误判为句子——
    "-" 是术语连字符不是句子标点（修复：step1/step5 曾因此整步 UNRESOLVED）。"""
    assert not _is_sentence_like("bulk-fill composite formulation")
    assert not _is_sentence_like("methacrylate-based composite")


def test_extract_nodes_substring_dedup():
    """子串去重：'filler' 是 'filler content'/'filler loading' 的子串 → 只保留更具体的。"""
    known = ["filler", "filler content", "filler loading", "polymerization shrinkage"]
    hits = extract_grounded_nodes("designed with filler content and filler loading "
                                  "influencing polymerization shrinkage", known)
    assert "filler" not in hits, "filler 被子串去重丢弃"
    assert "filler content" in hits and "filler loading" in hits
    assert "polymerization shrinkage" in hits


def test_chain_source_reset_to_candidate():
    """主语重置：步骤文本命中 candidate 自身 → 该步 source=candidate，
    防 'interfacial debonding --reduces--> bulk-fill' 跨句错连。"""
    c = _validated_candidate()
    v = _verification(causal_chain=[
        {"step": "shrinkage stress can cause interfacial debonding",
         "evidence": "原文", "paper_id": "W1", "evidence_type": "DIRECT"},
        {"step": "Bulk-fill composite formulations are engineered to reduce shrinkage stress",
         "evidence": "原文", "paper_id": "W2", "evidence_type": "DIRECT"},
    ])
    p = build_proposal(c, v, known_nodes=["shrinkage stress", "interfacial debonding"])
    grounded = [r for r in p.proposed_relations if r.grounding_status == "GROUNDED"]
    assert any(r.source_node == "bulk-fill composite formulation"
               and r.predicate == "can_reduce" and r.target_node == "shrinkage stress"
               for r in grounded)
    assert not any(r.source_node == "interfacial debonding"
                   and r.target_node == "bulk-fill composite formulation" for r in grounded)


# ── Predicate strength（用户定：paper-level DIRECT ≠ ontology-level universal）──

def test_single_paper_strong_predicate_downgraded():
    """类别节点 + 单篇 DIRECT 的 reduces → 降级 can_reduce（evidence 强度不变）。"""
    c = _validated_candidate()
    v = _verification(causal_chain=[
        {"step": "Bulk-fill composite formulations are specifically engineered "
                 "to reduce shrinkage stress",
         "evidence": "原文", "paper_id": "W1911271487", "evidence_type": "DIRECT"},
    ])
    p = build_proposal(c, v, known_nodes=["shrinkage stress"])
    grounded = [r for r in p.proposed_relations if r.grounding_status == "GROUNDED"]
    assert len(grounded) == 1
    r = grounded[0]
    assert r.predicate == "can_reduce"          # 强谓词降级
    assert r.evidence_type == "DIRECT"          # evidence strength 不变
    assert r.paper_ids == ["W1911271487"]       # paper_id 保留
    assert r.writable                            # 仍可写正式 ontology


def test_multi_paper_strong_predicate_kept():
    """类别节点 + ≥2 篇独立 DIRECT → 保留类别级强谓词 reduces。"""
    c = _validated_candidate()
    v = _verification(causal_chain=[
        {"step": "Bulk-fill composite formulations reduce shrinkage stress",
         "evidence": "e1", "paper_id": "W1", "evidence_type": "DIRECT"},
        {"step": "Bulk-fill composite formulations reduce shrinkage stress",
         "evidence": "e2", "paper_id": "W2", "evidence_type": "DIRECT"},
    ])
    p = build_proposal(c, v, known_nodes=["shrinkage stress"])
    grounded = [r for r in p.proposed_relations if r.grounding_status == "GROUNDED"]
    assert any(r.predicate == "reduces" and r.target_node == "shrinkage stress"
               for r in grounded)


def test_non_candidate_source_strong_predicate_kept():
    """链中间节点（具体已知概念）作 source → 不降级（规则只针对类别节点 candidate）。"""
    c = _validated_candidate()
    v = _verification(causal_chain=[
        {"step": "shrinkage stress can cause interfacial debonding",
         "evidence": "原文", "paper_id": "W1", "evidence_type": "DIRECT"},
    ])
    p = build_proposal(c, v, known_nodes=["shrinkage stress", "interfacial debonding"])
    grounded = [r for r in p.proposed_relations if r.grounding_status == "GROUNDED"]
    # "cause" 在 contributes_to 组（不是强谓词 causes），方向不受 strength 影响
    assert any(r.predicate == "contributes_to" and r.target_node == "interfacial debonding"
               for r in grounded)
