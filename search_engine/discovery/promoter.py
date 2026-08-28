"""Promoter：决定 VALIDATED candidate 以什么结构进入 ontology（用户定 2026-08-26）。

职责边界：verifier 负责"证据够不够"，promoter 负责"放哪里、怎么连"。
promoter **不重新做 scientific verification**——输入是 VALIDATED 候选 + verification 结果。

Promotion actions（9 类）：
    NEW_TOP_LEVEL_NODE / NEW_CHILD_NODE / NEW_SUB_ROUTE / NEW_PROCESS_STRATEGY /
    NEW_FORMULATION_STRATEGY / NEW_EFFECT / NEW_MATERIAL_CAPABILITY /
    RELATION_ONLY / NO_PROMOTION

状态机：PROPOSED → APPROVED → APPLIED（人工批准保留——Phase 2 不让模型自己扩 ontology）。

type → action 默认映射（最终 action 还检查语义等价/child/relation-only/是否真改结构）：
    ROUTE → NEW_TOP_LEVEL_NODE / NEW_CHILD_NODE
    SUB_ROUTE → NEW_SUB_ROUTE
    PROCESS_STRATEGY → NEW_PROCESS_STRATEGY
    FORMULATION_STRATEGY → NEW_FORMULATION_STRATEGY
    MECHANISM → NEW_CHILD_NODE / RELATION_ONLY
    EFFECT → NEW_EFFECT
    MATERIAL_CAPABILITY → NEW_MATERIAL_CAPABILITY

验收 6 条（用户定）：
    ① 非 VALIDATED 不能 promotion
    ② promotion 不重新做 scientific verification
    ③ candidate_type 与 ontology layer 对齐
    ④ relation 必须带 evidence provenance
    ⑤ inferred relation 不能冒充 DIRECT
    ⑥ APPLY 前必须有明确 proposal / approval 记录
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict

# 9 类 promotion action
PROMOTION_ACTIONS = (
    "NEW_TOP_LEVEL_NODE",
    "NEW_CHILD_NODE",
    "NEW_SUB_ROUTE",
    "NEW_PROCESS_STRATEGY",
    "NEW_FORMULATION_STRATEGY",
    "NEW_EFFECT",
    "NEW_MATERIAL_CAPABILITY",
    "RELATION_ONLY",
    "NO_PROMOTION",
)

# 状态机
PROPOSAL_STATUS = ("PROPOSED", "APPROVED", "REJECTED", "APPLIED")

# type → action 默认映射（候选类型与 ontology layer 对齐，用户定）
TYPE_TO_ACTION = {
    "ROUTE": "NEW_TOP_LEVEL_NODE",
    "SUB_ROUTE": "NEW_SUB_ROUTE",
    "PROCESS_STRATEGY": "NEW_PROCESS_STRATEGY",
    "FORMULATION_STRATEGY": "NEW_FORMULATION_STRATEGY",
    "EFFECT": "NEW_EFFECT",
    "MATERIAL_CAPABILITY": "NEW_MATERIAL_CAPABILITY",
    "MECHANISM": "NEW_CHILD_NODE",   # 有 parent 时；无 parent 且独立 → NEW_TOP_LEVEL_NODE
}


@dataclass
class PromotionRelation:
    """一条带证据 provenance + grounding 的关系（用户定 2026-08-26 修正版）。

    target_node 必须是：① existing ontology node ② 本次 proposal 的新 node
    ③ 明确标记的 candidate node——**绝不能是自由文本句子/evidence**。

    grounding_status（Relation Grounding 层输出）：
      GROUNDED        —— source/target 都锚定到 node，且 evidence_type=DIRECT → 可写正式 ontology
      NEW_NODE_REQUIRED —— target 是有效概念但 ontology 没有该 node（需要新建或后续处理）
      UNRESOLVED      —— target 无法锚定（句子/自由文本/无匹配）→ 只保留记录，不写正式
    """

    source_node: str
    predicate: str = "affects"      # affects / contributes_to / reduces / has_design_factor ...
    target_node: str = ""
    source_type: str = ""
    target_type: str = ""
    evidence_type: str = "DIRECT"   # DIRECT / INFERRED
    paper_ids: list[str] = field(default_factory=list)
    raw_evidence: list[str] = field(default_factory=list)
    grounding_status: str = "UNRESOLVED"   # GROUNDED / NEW_NODE_REQUIRED / UNRESOLVED

    @property
    def writable(self) -> bool:
        """只有 GROUNDED + DIRECT 才允许写正式 ontology（用户定）。"""
        return self.grounding_status == "GROUNDED" and self.evidence_type == "DIRECT"

    def to_dict(self) -> dict:
        return asdict(self)


# ── Relation Grounding 层（用户定：evidence sentence ≠ ontology node）──

# 常用 target 概念（不在 route×mechanism checklist 但可作 relation target 的节点）
_TARGET_CONCEPTS = (
    "polymerization shrinkage", "polymerization shrinkage stress",
    "shrinkage strain", "shrinkage stress", "photopolymerization shrinkage",
    "interfacial debonding", "filler loading", "filler content",
    "monomer composition", "elastic modulus", "degree of conversion",
    "gelation", "network formation", "volumetric shrinkage",
)


def default_known_nodes(extra: list[str] | None = None) -> list[str]:
    """relation target 的候选 node 全集：checklist（route+mechanism）+ target 概念 + 额外。"""
    from .typer import _norm as _norm_
    from ..route_mechanism_ontology import get_mechanisms, CORE_ROUTE_MECHANISMS
    nodes = set()
    for r in CORE_ROUTE_MECHANISMS:
        nodes.add(r)
        nodes.update(get_mechanisms(r))
    nodes.update(_TARGET_CONCEPTS)
    nodes.update(extra or [])
    return sorted(nodes, key=len, reverse=True)   # 长词优先（防 "shrinkage" 截胡 "shrinkage stress"）


def extract_grounded_nodes(step_text: str, known_nodes: list[str]) -> list[str]:
    """从 causal chain 步骤文本提取命中的 known node（**按文本出现顺序**，去重）。

    default_known_nodes 用长词优先排序是为了防 "shrinkage" 截胡 "shrinkage stress"
    （子串匹配时先试长词），但**命中顺序**必须按文本出现位置——否则
    "Shrinkage stress causes interfacial debonding" 会被连成反向的
    "interfacial debonding → shrinkage stress"（interfacial debonding 更长，长度序排前面）。

    **子串去重**：'filler' 是 'filler content'/'filler loading' 的子串、'shrinkage' 是
    'shrinkage stress' 的子串——重叠命中只保留更具体的（更长的）节点，
    防 "filler → filler content" 这类假父子链。
    """
    t = step_text.lower()
    found = [(t.index(node.lower()), node) for node in known_nodes
             if node.lower() in t]
    kept = []
    for pos, node in sorted(found):
        if any(node.lower() != other.lower() and node.lower() in other.lower()
               for _, other in found):
            continue  # 被更具体的节点包含 → 丢弃
        kept.append(node)
    return kept


def _is_sentence_like(text: str) -> bool:
    """句子检测：target 若为完整句子（长度/空格数/句子标点）→ 不能当 node。

    注意：**"-"（连字符）不是句子标点**——"bulk-fill composite formulation"、
    "methacrylate-based" 等节点名/术语常含连字符，误判会把整步打成 UNRESOLVED。
    真正的句子标记是句号/分号/逗号/em dash/系动词。
    """
    t = (text or "").strip()
    if not t:
        return False
    if len(t) > 40 or t.count(" ") > 6:       # 短语上限：~6 词
        return True
    if any(ch in t for ch in (".", ";", ",", "—", "are ", "is ")):
        return True
    return False


# ── Predicate type constraint（用户定：predicate 不能只看证据句动词，还要看 source/target 语义类型）──

# known node → ontology 类型（predicate constraint 用；未标注 → 无法约束 → 不拦截）
_NODE_TYPES = {
    # 行为/结果（EFFECT 语义：formulation 影响的对象）
    "polymerization shrinkage": "EFFECT",
    "photopolymerization shrinkage": "EFFECT",
    "volumetric shrinkage": "EFFECT",
    "shrinkage strain": "EFFECT",
    "shrinkage stress": "EFFECT",
    "polymerization shrinkage stress": "EFFECT",
    "interfacial debonding": "EFFECT",
    # 配方/材料参数（design factor 语义）
    "filler loading": "FORMULATION_PARAMETER",
    "filler content": "FORMULATION_PARAMETER",
    "monomer composition": "FORMULATION_PARAMETER",
    "monomer chemistry": "FORMULATION_PARAMETER",
    "photoinitiator system": "FORMULATION_PARAMETER",
    # 材料性能
    "elastic modulus": "MATERIAL_CAPABILITY",
    # 过程/机制
    "degree of conversion": "PROCESS_PARAMETER",
    "gelation": "MECHANISM",
    "network formation": "MECHANISM",
}

# predicate 的 source/target 类型约束（用户定 2026-08-26 第一版）：
#   has_design_factor 只能指向参数/组分（FORMULATION_PARAMETER/MATERIAL_COMPONENT/PROCESS_PARAMETER）
#   affects / contributes_to 面向 MECHANISM/EFFECT/MATERIAL_CAPABILITY（行为与结果）
PREDICATE_CONSTRAINTS = {
    "has_design_factor": {
        "source_types": {"FORMULATION_STRATEGY", "PROCESS_STRATEGY"},
        "target_types": {"FORMULATION_PARAMETER", "MATERIAL_COMPONENT", "PROCESS_PARAMETER"},
    },
    "affects": {
        "target_types": {"MECHANISM", "EFFECT", "MATERIAL_CAPABILITY"},
    },
    "contributes_to": {
        "target_types": {"MECHANISM", "EFFECT", "MATERIAL_CAPABILITY"},
    },
}


def _node_type(node: str, candidate_type: str | None = None) -> str | None:
    """known node → ontology 类型（predicate constraint 用）。

    candidate 自身 → candidate_type；checklist route → ROUTE / mechanism → MECHANISM；
    _TARGET_CONCEPTS 在 _NODE_TYPES 标注；未标注 → None（类型未知，不拦截）。
    """
    from ..route_mechanism_ontology import get_mechanisms, CORE_ROUTE_MECHANISMS
    n = node.lower()
    if n in _NODE_TYPES:
        return _NODE_TYPES[n]
    for r in CORE_ROUTE_MECHANISMS:
        if n == r.lower():
            return "ROUTE"
        for m in get_mechanisms(r):
            if n == m.lower():
                return "MECHANISM"
    return None


# ── Predicate strength（用户定：paper-level DIRECT ≠ ontology-level universal assertion）──

# 类别级强谓词：只有综述/多篇独立证据支持类别级关系时才允许；
# 单篇/实例级证据 → 降级为弱谓词（can_reduce / may_affect / may_prevent）。
# 变化的是 **predicate strength**，不是 evidence strength（evidence_type=DIRECT、
# paper_id 原样保留）。
STRONG_PREDICATES = {"reduces", "causes", "prevents"}
WEAK_PREDICATE_MAP = {
    "reduces": "can_reduce",
    "causes": "may_affect",
    "prevents": "may_prevent",
}
# 类别级证据阈值：≥2 篇独立论文（或综述）才允许类别级强谓词
CATEGORY_LEVEL_PAPER_THRESHOLD = 2


def _apply_predicate_strength(predicate: str, source_is_candidate: bool,
                              paper_ids: list[str]) -> str:
    """predicate strength：类别节点（candidate）上的强谓词，单篇证据 → 降级弱谓词。

    论文直接证明某一种/某几种 bulk-fill formulation 降低 stress，不等于
    "bulk-fill 这个类别普遍降低 stress"（用户定 invariant）——不把部分产品/配方
    表现写成整个类别的必然性质。source 是链中间节点（具体已知概念）不降级。
    """
    if predicate not in STRONG_PREDICATES:
        return predicate
    if not source_is_candidate:
        return predicate
    unique_papers = {p for p in (paper_ids or []) if p}
    if len(unique_papers) >= CATEGORY_LEVEL_PAPER_THRESHOLD:
        return predicate
    return WEAK_PREDICATE_MAP.get(predicate, predicate)


def _apply_predicate_strength_batch(relations: list[PromotionRelation],
                                    source_candidate: str) -> list[PromotionRelation]:
    """统一 predicate strength：同一条 (source, predicate, target) 边的多篇独立证据合并判强度。

    跨步累计：'bulk-fill reduce stress' 被 W1、W2 两篇独立论文支持（两条 step 表达
    同一断言）→ 合并后 ≥2 篇 → 保留 reduces；单篇 → 统一降级 can_reduce
    （同一边多条 relation 的 predicate 保持一致，不会一条 can_reduce 一条 reduces）。

    按 (source, **predicate**, target) 聚合：'bulk-fill --contributes_to--> shrinkage stress'
    （W1 证据是 "cause interfacial debonding"）不能算进 'bulk-fill --reduces--> shrinkage stress'
    的支持数——不同断言的证据不互相背书。
    """
    papers_by_edge: dict[tuple, set] = {}
    for r in relations:
        key = (r.source_node.lower(), r.predicate, r.target_node.lower())
        papers_by_edge.setdefault(key, set()).update(p for p in r.paper_ids if p)
    for r in relations:
        if r.source_node == source_candidate and r.predicate in STRONG_PREDICATES:
            key = (r.source_node.lower(), r.predicate, r.target_node.lower())
            if len(papers_by_edge.get(key, set())) < CATEGORY_LEVEL_PAPER_THRESHOLD:
                r.predicate = WEAK_PREDICATE_MAP.get(r.predicate, r.predicate)
    return relations


def _apply_predicate_constraints(predicate: str, source_type: str | None,
                                 target_type: str | None) -> str:
    """predicate type constraint：冲突（PREDICATE_TYPE_CONFLICT）→ 回退 affects。

    affects 是最宽松的通用关系（几乎总是科学上安全）；target/source 类型未知（None）
    时不拦截——只拦"明确违反"的情况。用户定：promoter 不产生"语法正确、科学语义错误"的 relation。
    """
    rule = PREDICATE_CONSTRAINTS.get(predicate)
    if not rule:
        return predicate
    if rule.get("source_types") and source_type and source_type not in rule["source_types"]:
        return "affects"
    if rule.get("target_types") and target_type and target_type not in rule["target_types"]:
        return "affects"
    return predicate


def _predicate_for(step_text: str) -> str:
    """predicate typing：从步骤措辞判断关系类型。"""
    t = step_text.lower()
    if any(w in t for w in ("generate", "generates", "leads", "leads to", "contributes",
                            "creates", "causes", "cause", "result in", "results in", "induces",
                            "produce", "produces")):
        return "contributes_to"
    if any(w in t for w in ("reduce", "reduces", "lower", "lowers", "decrease",
                            "inhibit", "relieve", "relieves", "alleviate")):
        return "reduces"
    if any(w in t for w in ("compos", "comprise", "consist", "made of", "designed with",
                            "design with", "based on", "factor", "determin", "characteriz")):
        return "has_design_factor"
    if any(w in t for w in ("influence", "affect", "change", "alter", "modulate",
                            "impact", "control", "govern")):
        return "affects"
    return "affects"


def ground_causal_chain(chain: list[dict], source_candidate: str,
                        known_nodes: list[str],
                        candidate_type: str | None = None) -> list[PromotionRelation]:
    """链式 grounding（用户定：A→B→C 保序成 A→B、B→C，禁止扁平成 A→B、A→C）。

    - 步骤文本命中 known node → 链式 relation（source=prev，prev 沿链推进）
    - 步骤无命中 / target 是句子 → UNRESOLVED（raw_evidence 保留，不进正式 ontology）
    - 效果方向：步骤说 "polymerization shrinkage generates stress" → 连 polymerization
      shrinkage（生成者），不是 reduced shrinkage（效果词）——由命中节点自然决定
    - 自环防护：命中节点 == 当前 source 时跳过（不生成 source==target 的无意义关系）
    - 命中顺序按文本出现顺序（extract_grounded_nodes）——防止同句并列提及被连成回跳
    - **主语重置**：步骤文本命中 candidate 自身（如 "Bulk-fill composite formulations are
      engineered to reduce shrinkage stress"）→ 该步 source 重置为 candidate（句子主语），
      防 "interfacial debonding --reduces--> bulk-fill" 这类跨句错连
    - **predicate type constraint**：关键词判出的 predicate 再按 source/target 语义类型
      校验（has_design_factor 指向 EFFECT → PREDICATE_TYPE_CONFLICT → 回退 affects）
    """
    relations: list[PromotionRelation] = []
    prev = source_candidate
    for step in chain or []:
        text = str(step.get("step", "")).strip()
        if not text:
            continue
        etype = "DIRECT" if step.get("evidence_type") == "DIRECT" else "INFERRED"
        paper_ids = [step["paper_id"]] if step.get("paper_id") else []
        raw = [str(step.get("evidence", ""))[:120]] if step.get("evidence") else []
        nodes = extract_grounded_nodes(text, known_nodes)
        # 主语重置：candidate 名出现在句中（不依赖 known_nodes 是否含它）→ 该步从
        # candidate 出发（句子主语是 candidate）；否则从链尾 prev 继续。
        # 步内游标 cur 沿命中推进，步末写回 prev。
        start = source_candidate if source_candidate.lower() in text.lower() else prev
        start_type = candidate_type if start == source_candidate else _node_type(start)
        if not nodes or any(_is_sentence_like(n) for n in nodes):
            relations.append(PromotionRelation(
                source_node=start, predicate=_predicate_for(text), target_node="",
                source_type=start_type, evidence_type=etype,
                paper_ids=paper_ids, raw_evidence=raw,
                grounding_status="UNRESOLVED"))
            continue
        cur = start
        advanced = False
        for n in nodes:
            if n.lower() == cur.lower():
                continue  # 自环防护：当前游标已是该节点（如步骤主语复述上一个节点）
            target_type = _node_type(n, candidate_type)
            pred = _apply_predicate_constraints(_predicate_for(text),
                                                start_type, target_type)
            relations.append(PromotionRelation(
                source_node=cur, predicate=pred, target_node=n,
                source_type=start_type, target_type=target_type,
                evidence_type=etype, paper_ids=paper_ids, raw_evidence=raw,
                grounding_status="GROUNDED"))
            cur = n
            advanced = True
        if not advanced:
            # 全部命中 == start（无新推进）——只保留证据记录，不写正式 ontology
            relations.append(PromotionRelation(
                source_node=start, predicate=_predicate_for(text), target_node="",
                source_type=start_type, evidence_type=etype,
                paper_ids=paper_ids, raw_evidence=raw,
                grounding_status="UNRESOLVED"))
            continue
        prev = cur  # 步末推进链尾
    return relations


@dataclass
class PromotionProposal:
    """promoter 输出：不是直接改 ontology，而是先产出提案（用户定 schema + grounding 修正）。

    node_status / relation_status 拆分（用户定：NODE_PROMOTION 与 RELATION_PROMOTION
    分开验收——bulk-fill 场景 node 可 APPLIED，relation 可能 NEEDS_GROUNDING）。
    """

    candidate_id: str
    candidate_name: str
    candidate_type: str
    action: str = "NO_PROMOTION"
    parent_node: str | None = None
    new_node_name: str | None = None
    new_node_type: str | None = None
    proposed_relations: list[PromotionRelation] = field(default_factory=list)
    evidence_papers: list[str] = field(default_factory=list)
    direct_target_paper_count: int = 0
    causal_status: str = "NO_CAUSAL_EVIDENCE"
    rationale: str = ""
    warnings: list[str] = field(default_factory=list)
    status: str = "PROPOSED"        # 整体状态（兼容）= node_status
    node_status: str = "PROPOSED"   # PROPOSED / APPROVED / REJECTED / APPLIED
    relation_status: str = "PROPOSED"  # PROPOSED / APPROVED / NEEDS_GROUNDING / REJECTED / APPLIED
    review_log: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def approve(self, approver: str = "human") -> bool:
        """PROPOSED → APPROVED（人工批准 node 与 relation 分离）。"""
        if self.status != "PROPOSED":
            return False
        self.status = "APPROVED"
        self.node_status = "APPROVED"
        # relation 单独验收：存在 UNRESOLVED/INFERRED → NEEDS_GROUNDING（不整体批准）
        if any(r.grounding_status != "GROUNDED" for r in self.proposed_relations):
            self.relation_status = "NEEDS_GROUNDING"
        else:
            self.relation_status = "APPROVED"
        self.review_log.append({"action": "APPROVED", "by": approver,
                                "node_status": self.node_status,
                                "relation_status": self.relation_status})
        return True

    def apply(self) -> bool:
        """APPROVED → APPLIED（写 ontology 扩展记录；代码层修改由人工执行）。"""
        if self.status != "APPROVED":
            return False
        self.status = "APPLIED"
        self.node_status = "APPLIED"
        if self.relation_status == "APPROVED":
            self.relation_status = "APPLIED"
        self.review_log.append({"action": "APPLIED", "by": "promoter",
                                "node_status": self.node_status,
                                "relation_status": self.relation_status})
        return True

    def reject(self, reason: str = "") -> bool:
        """PROPOSED/APPROVED → REJECTED。"""
        if self.status not in ("PROPOSED", "APPROVED"):
            return False
        self.status = "REJECTED"
        self.node_status = "REJECTED"
        self.relation_status = "REJECTED"
        self.review_log.append({"action": "REJECTED", "reason": reason, "by": "human"})
        return True


def decide_action(candidate, verification: dict | None = None,
                  preview: bool = False) -> tuple[str, str | None, list[str]]:
    """决定 promotion action（用户定：最终 action 不能只看 type）。

    返回 (action, parent_node, warnings)。
    检查链：VALIDATED → 语义等价（canonical_match）→ type 映射 → child/parent → 真改结构？

    preview=True（plan-only 只读重预览）：候选已 PROMOTED 也放行——状态机单向
    VALIDATED → PROMOTED，PROMOTED 蕴含曾经 VALIDATED；其余非 VALIDATED 状态仍拒绝。
    """
    warnings: list[str] = []

    # ① 非 VALIDATED 不能 promotion（PROMOTED + preview 只读重预览除外）
    if candidate.status != "VALIDATED":
        if preview and candidate.status == "PROMOTED":
            warnings.append("候选已 PROMOTED——plan-only 只读重预览（不写任何东西，不能再次 approve）")
        else:
            return "NO_PROMOTION", None, ["候选非 VALIDATED（当前 " + candidate.status + "）——不能 promotion"]
    # ② promotion 不重新做验证：读 verification 结果（不调用 LLM/搜索）
    # ③ type 对齐
    if candidate.candidate_type not in TYPE_TO_ACTION:
        return "NO_PROMOTION", None, [f"候选类型 {candidate.candidate_type} 无映射 action"]
    # 语义等价：已有等价 node → RELATION_ONLY（不新建节点）
    if candidate.canonical_match:
        return "RELATION_ONLY", candidate.canonical_match, \
            [f"与已有 node '{candidate.canonical_match}' 语义等价——只新增关系不新建节点"]
    v = verification or {}
    causal = v.get("causal_status", "NO_CAUSAL_EVIDENCE")
    if candidate.candidate_type == "MECHANISM":
        # MECHANISM：有明确 parent（如 DCB → dynamic covalent network 下）→ NEW_CHILD_NODE；
        # 无 parent 且独立新顶层 → NEW_TOP_LEVEL_NODE
        parent = _find_mechanism_parent(candidate, v)
        if parent:
            return "NEW_CHILD_NODE", parent, []
        return "NEW_TOP_LEVEL_NODE", None, ["MECHANISM 无现有 parent 匹配——按新顶层节点提案（人工确认）"]
    action = TYPE_TO_ACTION[candidate.candidate_type]
    # causal 警告：非 MECHANISM 但 causal 只 PARTIAL——不创建 mechanism 语义
    if causal in ("PARTIAL_CAUSAL_EVIDENCE", "NO_CAUSAL_EVIDENCE"):
        warnings.append("causal_status=" + causal + "——本 promotion 只建节点/关系，"
                        "不要解释成'发现新机制'；机制分解留给知识图谱后续表达")
    return action, None, warnings


def _find_mechanism_parent(candidate, verification: dict | None) -> str | None:
    """MECHANISM 候选的 parent 匹配：从 relations/causal_chain 找已有 ontology 概念。"""
    v = verification or {}
    for rel in v.get("proposed_relations", []):
        if rel.get("target") and rel.get("relation") in ("is_a", "associated_with"):
            return rel["target"]
    for step in v.get("causal_chain", []):
        t = str(step.get("step", ""))
        for known in ("reversible bond exchange", "network rearrangement",
                      "stress relaxation", "delayed gelation"):
            if known in t:
                return known
    return None


def build_proposal(candidate, verification: dict | None = None,
                   extra_relations: list[PromotionRelation] | None = None,
                   known_nodes: list[str] | None = None,
                   preview: bool = False) -> PromotionProposal:
    """VALIDATED candidate + verification → PromotionProposal（PROPOSED）。

    Relation Grounding 层（用户定 2026-08-26）：
      verifier causal evidence → 节点提取 → 链式 grounding（保序）→ predicate typing
      → PromotionRelation（target 必须是 node，evidence sentence 只能进 raw_evidence）
    人工 extra_relations（--relation）同样做 grounding 检查（target 非 node → UNRESOLVED）。

    preview=True：PROMOTED 候选只读重预览（不写任何东西，用于验证 grounding 效果）。
    """
    action, parent, warnings = decide_action(candidate, verification, preview=preview)
    v = verification or {}
    if known_nodes is None:
        known_nodes = default_known_nodes([candidate.raw_name])

    # 自动：causal_chain → 链式 grounding（A→B→C 不扁平）
    relations = ground_causal_chain(v.get("causal_chain", []), candidate.raw_name,
                                    known_nodes, candidate_type=candidate.candidate_type)
    # 人工附加 relations：grounding 检查（target 必须是 node 或本次新 node）
    # + predicate type constraint（人工显式指定同样校验，冲突 → 回退 affects）
    for r in extra_relations or []:
        if not r.target_node:
            r.grounding_status = "UNRESOLVED"
        elif any(n.lower() == r.target_node.lower() for n in known_nodes):
            r.grounding_status = "GROUNDED"
            r.source_type = candidate.candidate_type if r.source_node == candidate.raw_name \
                else _node_type(r.source_node)
            r.target_type = _node_type(r.target_node, candidate.candidate_type)
            r.predicate = _apply_predicate_constraints(r.predicate,
                                                       r.source_type, r.target_type)
        else:
            r.grounding_status = "NEW_NODE_REQUIRED"
        relations.append(r)

    # predicate strength 统一后处理：同一边多篇独立证据合并判强度
    # （paper-level DIRECT ≠ ontology-level universal，用户定 invariant）
    relations = _apply_predicate_strength_batch(relations, candidate.raw_name)

    return PromotionProposal(
        candidate_id=candidate.candidate_id,
        candidate_name=candidate.raw_name,
        candidate_type=candidate.candidate_type,
        action=action,
        parent_node=parent,
        new_node_name=candidate.raw_name if action.startswith("NEW_") else None,
        new_node_type=candidate.candidate_type if action.startswith("NEW_") else None,
        proposed_relations=relations,
        evidence_papers=list(getattr(candidate, "source_papers", []) or []) + list(v.get("supporting_papers", []) or []),
        direct_target_paper_count=v.get("direct_target_paper_count", 0),
        causal_status=v.get("causal_status", "NO_CAUSAL_EVIDENCE"),
        rationale=(f"{candidate.raw_name} 已 VALIDATED（target DIRECT "
                   f"{v.get('direct_target_paper_count', 0)} 篇独立论文）；"
                   f"以 {action} 进入 ontology 的 {candidate.candidate_type} 层"),
        warnings=warnings,
    )
