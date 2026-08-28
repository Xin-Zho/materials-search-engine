"""Phase 2.1b Knowledge Expander（用户定 2026-08-26）：新知识 → 新 query。

**输入只吃本轮新 APPROVED/PROMOTED 的 ontology node**（不是旧 ontology 全展开——
否则退化成普通 query expansion）。provenance 必带：
    query_origin = promotion_id + promoted_node + promotion_round

**Expander 只生成 query，不判断新知识**（分层不混，用户定）：
    Expander 生成 query → Retriever 找论文 → Extractor 提知识 →
    Scanner 发现 candidate → Verifier 验证。
Expander 说"low modulus 是新 mechanism"是混层——它只负责产出检索方向。

4 类生成（用户定，以 bulk-fill 为例）：
    A NODE       —— node × 目标概念（bulk-fill × shrinkage stress / polymerization shrinkage）
    B RELATION   —— 沿 grounded relation 的 target（has_design_factor→filler content）：
                     node × target × 目标概念（bulk-fill filler content shrinkage stress）
    C MECHANISM  —— 沿 causal chain（filler content→poly-shrinkage→shrinkage stress）：
                     node × 链节点/设计参数 × mechanism 词
    D ADJACENT   —— 找子方向（最重要）：node × 子方向词（stress-relieving monomer /
                     low modulus formulation / photoinitiator system / polymerization kinetics）
                     ——目标不是重复证明 bulk-fill，而是找它下面 ontology 没表达的子方向
"""

from __future__ import annotations

from .query_registry import QueryRecord, normalize_query

# 目标概念词（node/relation 组合用，对齐 _TARGET_CONCEPTS 语义）
_TARGET_TERMS = (
    "shrinkage stress",
    "polymerization shrinkage",
    "shrinkage strain",
    "interfacial debonding",
    "volumetric shrinkage",
    "elastic modulus",
)

# 机制方向词（mechanism expansion）
_MECHANISM_TERMS = (
    "shrinkage mechanism",
    "polymerization kinetics",
    "network formation",
)

# 子方向词（adjacent discovery：bulk-fill 下面还有什么未表达的）
_ADJACENT_TERMS = (
    "stress-relieving monomer",
    "low modulus formulation",
    "high translucency",
    "photoinitiator system",
    "monomer chemistry",
    "filler surface treatment",
    "polymerization rate",
)

# 设计参数词（mechanism/relation 组合；与 _TARGET_CONCEPTS 的 FORMULATION_PARAMETER 对齐）
_DESIGN_FACTOR_TERMS = (
    "filler content",
    "filler loading",
    "monomer chemistry",
    "photoinitiator system",
)


def _norm_relation(r) -> dict | None:
    """relation dict 容错归一化（多来源键名不同）：
    - proposal.to_dict()：source_node/predicate/target_node/grounding_status
    - revision relations：source/predicate/target（GROUNDED+DIRECT 才写进 revision）
    - 旧 initial：source/relation/target
    """
    if not isinstance(r, dict):
        return None
    tgt = r.get("target_node") or r.get("target")
    if not tgt:
        return None
    return {
        "source_node": r.get("source_node") or r.get("source", ""),
        "predicate": r.get("predicate") or r.get("relation", "affects"),
        "target_node": tgt,
        "evidence_type": r.get("evidence_type", "DIRECT"),
        "grounding_status": r.get("grounding_status", "GROUNDED"),
    }


def _short_node(node: str) -> str:
    """node 检索短语：前 2 个词（'bulk-fill composite formulation' → 'bulk-fill composite'）。

    OpenAlex 引号短语 AND 语义：全名（5 词）作精确短语几乎不与目标短语共现
    （实测 count=0），2 词短语有效（'\"bulk-fill composite\" \"shrinkage stress\"'=92）。
    source_node / provenance 保留全名，只有 query_text 用短短语。
    """
    words = node.strip().split()
    return " ".join(words[:2]) if len(words) > 2 else node


def generate_queries(node_name: str, relations: list[dict] | None = None,
                     causal_chain: list[dict] | None = None,
                     promotion_id: str | None = None,
                     round_id: int | None = None) -> list[QueryRecord]:
    """从 promoted node 生成 4 类新 query（纯规则，无 LLM，无检索）。

    relations：proposal.proposed_relations / ontology_promotions.json 的 revision
    relations（多种键名容错）；causal_chain：verification.causal_chain（可选）。

    query_text 用 **OpenAlex 兼容格式**：'"短语" AND "短语"'（search_relevance
    内部按 AND 拆短语 + 引号包裹——整句会被当单个精确短语导致 0 结果；node 短语
    取前 2 词防长短语 AND 0 结果，均已实测 2026-08-27）。
    normalized_query 不受影响（normalize 去引号分词排序，去重语义不变）。
    """
    queries: list[QueryRecord] = []
    rels = [r for r in (_norm_relation(x) for x in (relations or []))
            if r and r.get("grounding_status") == "GROUNDED"]
    nq = _short_node(node_name)

    # A. NODE expansion：node × 目标概念
    for term in _TARGET_TERMS:
        queries.append(_rec(node_name, f'"{nq}" AND "{term}"', "NODE",
                            "node", promotion_id, round_id))

    # B. RELATION expansion：沿 **design factor 类** relation target × 目标概念
    # （用户例子：has_design_factor→filler content → "bulk-fill filler content shrinkage stress"；
    #  链节点 poly-shrinkage/shrinkage stress 由 MECHANISM 类覆盖，不重复）
    rel_targets = [r["target_node"] for r in rels
                   if r.get("target_node") != node_name
                   and r["target_node"].lower() in _DESIGN_FACTOR_TERMS]
    rel_targets = rel_targets[:2]   # 控制规模
    for tgt in rel_targets:
        for term in _TARGET_TERMS[:3]:
            if term.lower() == tgt.lower():
                continue            # 防 "polymerization shrinkage polymerization shrinkage"
            queries.append(_rec(node_name,
                                f'"{nq}" AND "{tgt}" AND "{term}"', "RELATION",
                                f"relation:{tgt}", promotion_id, round_id))

    # C. MECHANISM expansion：node × 链节点/设计参数 × mechanism 词
    chain_nodes = []
    for step in causal_chain or []:
        t = str(step.get("step", ""))
        for term in _TARGET_TERMS + _DESIGN_FACTOR_TERMS:
            if term in t.lower() and term not in chain_nodes:
                chain_nodes.append(term)
        if len(chain_nodes) >= 3:
            break
    factors = chain_nodes or list(_DESIGN_FACTOR_TERMS[:2])
    for f in factors[:3]:
        for mterm in _MECHANISM_TERMS[:2]:
            queries.append(_rec(node_name,
                                f'"{nq}" AND "{f}" AND "{mterm}"', "MECHANISM",
                                f"mechanism:{f}", promotion_id, round_id))

    # D. ADJACENT discovery：node × 子方向词（找未表达的子方向）
    for term in _ADJACENT_TERMS:
        queries.append(_rec(node_name, f'"{nq}" AND "{term}"', "ADJACENT",
                            "adjacent", promotion_id, round_id))

    return queries


def _rec(node: str, text: str, family: str, source_relation: str,
         promotion_id: str | None, round_id: int | None) -> QueryRecord:
    return QueryRecord(
        query_text=text,
        normalized_query=normalize_query(text),
        source_node=node,
        source_relation=source_relation,
        origin_round=round_id,
        origin_promotion=promotion_id,
        query_family=family,
    )


def count_by_family(records: list[QueryRecord] | list[dict]) -> dict[str, int]:
    """分类统计（CLI 展示用）。"""
    counts: dict[str, int] = {}
    for r in records:
        f = r.get("query_family") if isinstance(r, dict) else r.query_family
        counts[f] = counts.get(f, 0) + 1
    return counts
