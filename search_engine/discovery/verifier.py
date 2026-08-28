"""Candidate Verifier：验证候选三问（Q1 Concept / Q2 Relevance / Q3 Causal）。

用户定（2026-08-26）：
  Q1 Concept   —— 独立概念？ontology 真正没表达过？→ concept_independent / novel_to_ontology
  Q2 Relevance —— 与目标 photopolymerization shrinkage / shrinkage stress 直接相关？
                  → domain_relevance + relevance_evidence[]（不是"是不是 polymer 领域概念"）
  Q3 Causal    —— 它为什么影响 shrinkage/stress？→ evidence-backed causal chain
                  （禁止"论文同时出现两个关键词就建 edge"）

verdict（用户定 2026-08-26 修正）：
  VALIDATED / ADJACENT / REJECTED / NEED_MORE_EVIDENCE / SEARCH_INCONCLUSIVE

关键语义：
  - retrieval failure ≠ scientific irrelevance：连候选领域论文都没搜对 → SEARCH_INCONCLUSIVE
    （不能下领域判断）；搜到了候选概念论文但无 target relation → ADJACENT。
  - direct evidence 三层拆分（防止"有论文证明机制存在"被误当成"有论文证明它能降低光固化收缩"）：
      direct_concept_evidence_count  —— 论文直接证明候选概念本身
      direct_relation_evidence_count —— 论文直接证明概念→机制链（如 DCB→network rearrangement）
      direct_target_evidence_count   —— 论文直接证明概念→降低光固化收缩应力（promotion 真正需要）
  - **evidence count ≠ paper count**：每层同时记 *_paper_count（canonical paper identity 去重）。
    promotion 看 target_direct_paper_count ≥ 1 + total_independent_support ≥ 2——"两句话 ≠ 两篇论文"。
  - **candidate validation ≠ causal novelty**（用户定 2026-08-26）：
      candidate_validated —— 这个新 node 值不值得加入 ontology（概念独立 + 概念证据 + 相关）
      causal_status       —— 是否带来新机制：NOVEL_CAUSAL_CHAIN / EXISTING_MECHANISM_COMPOSITION
                             / PARTIAL_CAUSAL_EVIDENCE / NO_CAUSAL_EVIDENCE
    ROUTE / PROCESS_STRATEGY / FORMULATION_STRATEGY / SUB_ROUTE 不要求发现新 mechanism——
    新 formulation/strategy 节点（由已有机制组合作用）也是 discovery。
  - **DIRECT 必须 raw-source traceable**：每条 DIRECT 必须 paper_id + 论文原文 evidence；
    模型 paraphrase / 结构化总结句只能标 INFERRED / STRUCTURED_SUMMARY，不能进 DIRECT 计数。
  - Phase 2 discovery 已发现的证据（candidate.source_papers/evidence）必须进入验证语料，
    绝不能因 verifier 重新搜索没搜到就丢掉。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict

from .canonical_filter import canonical_match, existing_knowledge_match

VERDICTS = ("VALIDATED", "ADJACENT", "REJECTED", "NEED_MORE_EVIDENCE", "SEARCH_INCONCLUSIVE")

# causal_status（用户定 2026-08-26）：候选是否带来新机制
CAUSAL_STATUS = (
    "NOVEL_CAUSAL_CHAIN",              # 候选带来 ontology 中没有的新机制链
    "EXISTING_MECHANISM_COMPOSITION",  # 候选节点有效，但作用由已有机制组合而成
    "PARTIAL_CAUSAL_EVIDENCE",         # 有部分因果证据（前半段链条）
    "NO_CAUSAL_EVIDENCE",              # 无因果证据
)

# Target lexical family（与候选词族做笛卡尔组合；用户定 2026-08-26）
TARGET_FAMILY = [
    "polymerization shrinkage",
    "polymerization shrinkage stress",
    "shrinkage stress",
    "photopolymerization stress",
]

# 候选词族手动别名（lexical family；用户定：不能只 raw_name × 固定后缀）
_MANUAL_FAMILY = {
    "bulk-fill composite formulation": [
        "bulk-fill composite", "bulk fill composite", "bulk-fill resin composite",
        "bulk fill resin-based composite", "bulk-fill composite resin",
    ],
    "dynamic covalent bond exchange": [
        "dynamic covalent bond exchange", "dynamic covalent bonds", "vitrimer",
        "covalent adaptable network", "bond exchange reaction",
    ],
    "incremental curing": [
        "incremental curing", "incremental polymerization", "layered curing",
        "incremental light curing",
    ],
}


def build_candidate_family(name: str) -> list[str]:
    """候选词族：手动别名 + 规则变体（去括号内容 / 去尾部 formulation 类词 / 连字符归一）。"""
    fam = [name]
    if name in _MANUAL_FAMILY:
        fam = [name] + list(_MANUAL_FAMILY[name])
    # 规则变体
    base = re.sub(r"\s*\(.*?\)\s*", " ", name).strip()
    if base and base != name:
        fam.append(base)
    for suffix in (" formulation", " strategy", " approach", " technique"):
        if name.endswith(suffix):
            fam.append(name[: -len(suffix)])
    out, seen = [], set()
    for f in fam:
        f = f.strip()
        if f and f.lower() not in seen:
            seen.add(f.lower())
            out.append(f)
    return out


def build_verification_queries(candidate_name: str) -> list[dict]:
    """生成三问验证查询：Candidate lexical family × Target lexical family。

    返回 [{type, purpose, queries: [...]}]，concept 类用候选家族×光聚合域，
    relevance/causal 类用候选家族×target 家族。
    """
    fam = build_candidate_family(candidate_name)
    concept_targets = ["photopolymerization", "photopolymer", "photocuring"]
    out = []
    for f in fam:
        for t in concept_targets:
            out.append(f"{f} {t}")
    rel_queries = [f"{f} {t}" for f in fam for t in TARGET_FAMILY]
    causal_queries = [
        f"{f} shrinkage stress reduction mechanism",
        f"{f} network shrinkage",
    ] + [f"{f} {t}" for f in fam[:2] for t in TARGET_FAMILY[:2]]
    return [
        {"type": "concept", "purpose": "Q1 独立概念：候选词族 × 光聚合域",
         "queries": out[:8]},
        {"type": "relevance", "purpose": "Q2 相关性：候选词族 × target 词族",
         "queries": rel_queries[:10]},
        {"type": "causal", "purpose": "Q3 因果链：候选如何影响 shrinkage/stress",
         "queries": causal_queries[:8]},
    ]


@dataclass
class VerificationResult:
    """一次验证的输出（写回 candidate.provenance["verification"] + 决定 status）。"""

    candidate_id: str
    candidate_name: str = ""
    # Q1
    concept_independent: bool | None = None
    novel_to_ontology: bool | None = None
    # Q2
    domain_relevance: str = "UNKNOWN"
    relevance_evidence: list[str] = field(default_factory=list)
    # Q3
    causal_chain: list[dict] = field(default_factory=list)  # [{"step", "evidence", "paper_id", "evidence_type"}]
    # DIRECT 三层计数（用户定：概念/机制/target 分开）
    direct_concept_evidence_count: int = 0
    direct_relation_evidence_count: int = 0
    direct_target_evidence_count: int = 0
    # DIRECT 三层 paper count（evidence count ≠ paper count，用户定：独立性按论文去重）
    direct_concept_paper_count: int = 0
    direct_relation_paper_count: int = 0
    direct_target_paper_count: int = 0
    supporting_papers: list[str] = field(default_factory=list)
    # 节点有效性 ≠ 因果新颖性（用户定 2026-08-26）
    candidate_validated: bool | None = None   # 新 node 值不值得加入 ontology
    causal_status: str = "NO_CAUSAL_EVIDENCE"  # NOVEL_CAUSAL_CHAIN / EXISTING_MECHANISM_COMPOSITION / ...
    # corpus 统计（验证语料有效性——seed 与 search 完全分开，用户定 2026-08-26）
    corpus_total: int = 0                  # 统一语料论文数（seed + 新搜）
    seed_evidence_count: int = 0           # 候选自带 evidence 条数（scanner 已发现）
    seed_concept_related_count: int = 0    # seed 论文提及候选概念数（只证明概念存在）
    search_concept_related_count: int = 0  # 新搜论文提及候选概念数（证明本次检索有效）
    search_target_related_count: int = 0   # 新搜论文提及 target 家族数
    search_direct_target_count: int = 0    # 新搜论文直接支撑 target 关系数（LLM 判定）
    # 检索质量 vs 验证充分性（用户定 2026-08-26：两个概念不能混）
    #   retrieval_quality        —— 搜索本身质量（lexical precision）：INVALID / PARTIAL / GOOD
    #                               只作 diagnostic caveat，不拥有 verdict 一票否决权
    #   verification_sufficiency —— 是否获得足够证据做判断：INSUFFICIENT / SUFFICIENT
    #                               （verdict 门槛）
    retrieval_quality: str = "INVALID"
    verification_sufficiency: str = "INSUFFICIENT"
    # verdict
    verdict: str = ""
    note: str = ""

    @property
    def total_independent_support(self) -> int:
        """独立支撑论文数（concept/relation/target 层的去重 union——promotion 的 ②）。"""
        return len(set(self.supporting_papers))

    @property
    def effective_concept_paper_count(self) -> int:
        """effective concept support：DIRECT_target ⇒ DIRECT_relation ⇒ DIRECT_concept
        向上蕴含（用户定 2026-08-26）——一篇直接讨论 candidate→target 的论文当然也
        直接证明 candidate 是真实概念。取三层 union（报告口径，不影响 verdict 判定）。"""
        return len(set(self.supporting_papers))

    @property
    def direct_evidence_count(self) -> int:
        """兼容旧字段：三层总和（新代码请用三层各自计数）。"""
        return (self.direct_concept_evidence_count
                + self.direct_relation_evidence_count
                + self.direct_target_evidence_count)

    def to_dict(self) -> dict:
        return asdict(self)


def q1_concept_check(name: str) -> tuple[bool, bool, str]:
    """Q1 本地判定：概念独立性 + ontology 新颖性（canonical filter）。

    返回 (concept_independent, novel_to_ontology, matched_node)。
    matched_node 非空 = ontology 已表达（非独立 / 非新颖）。
    """
    cm = canonical_match(name)
    existing = existing_knowledge_match(name)
    if cm or existing:
        return False, False, cm or existing or ""
    return True, True, ""


def verification_sufficiency_level(result: "VerificationResult") -> str:
    """验证充分性（用户定 2026-08-26）：
    SUFFICIENT —— ≥2 篇独立候选相关论文 且 ≥1 篇 DIRECT target 论文
                  （与 promotion rules ②③ 对齐）
    PARTIAL    —— 有候选概念命中但 target 证据不足
    INSUFFICIENT —— 无候选概念命中
    """
    if result.direct_target_paper_count >= 2 and result.total_independent_support >= 2:
        return "SUFFICIENT"
    if result.search_concept_related_count >= 1 or result.direct_target_paper_count >= 1:
        return "PARTIAL"
    return "INSUFFICIENT"


def decide_verdict(result: VerificationResult) -> str:
    """verdict 决策（用户定 2026-08-26 定稿版）。

    retrieval_quality（搜索质量）≠ verification_sufficiency（证据充分性）：
      bulk-fill 场景 retrieval_quality=PARTIAL（36% 命中）但 sufficiency=SUFFICIENT
      （5 篇 DIRECT target）→ 可以 VALIDATED；retrieval_quality 只作 caveat。

    - Q1 非独立 → REJECTED
    - S0 检索完全失败（search 无候选概念也无 target 命中，seed 不能掩盖）→ SEARCH_INCONCLUSIVE
    - candidate_validated + 独立支持 ≥2 + DIRECT target ≥1 → VALIDATED
      （causal_status 单独记录：PARTIAL/COMPOSITION 不影响节点有效性）
    - target/relation DIRECT ≥1 但独立支持 <2 → NEED_MORE_EVIDENCE
    - 检索到候选概念且 relevance 高但无直接关系 → NEED_MORE_EVIDENCE
    - 检索充分（GOOD）+ 大量候选文献 + 无 candidate↔target 关系 → ADJACENT
    - 其余（候选文献召回不足/检索 PARTIAL 无 target）→ SEARCH_INCONCLUSIVE
    """
    if result.novel_to_ontology is False:
        return "REJECTED"
    # S0：本次验证检索连候选概念论文都没召回（seed 只证明概念存在，不能掩盖检索失败）
    if (result.search_concept_related_count == 0
            and result.search_target_related_count == 0):
        return "SEARCH_INCONCLUSIVE"
    # 有 DIRECT target 证据
    if result.direct_target_paper_count >= 1:
        if (result.candidate_validated is not False
                and result.total_independent_support >= 2):
            return "VALIDATED"
        return "NEED_MORE_EVIDENCE"   # 1 篇直接支持但独立支撑不足
    # 有机制前半链（relation）
    if result.direct_relation_paper_count >= 1:
        return "NEED_MORE_EVIDENCE"
    # 检索充分（GOOD）+ 概念相关但无直接关系 → 相关度判定：相关高=证据不足待补，
    # 不相关=ADJACENT（候选领域论文有但 candidate↔target 关系缺失）
    if result.retrieval_quality == "GOOD":
        if result.domain_relevance in ("HIGH", "MEDIUM"):
            return "NEED_MORE_EVIDENCE"
        if result.search_concept_related_count >= 2:
            return "ADJACENT"
    # 其余（候选文献召回不足 / 检索 PARTIAL 无 target 证据）→ 检索本身不充分，
    # 不能下科学判断（用户定：SEARCH_INCONCLUSIVE 表示"由于检索不充分无法判断关系"）
    return "SEARCH_INCONCLUSIVE"
