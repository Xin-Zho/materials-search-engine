"""固定的 route → mechanism ontology（7 个核心 route，人工固定）。

这是 Phase 1 的"标准答案"层：每个核心 route 固定一组 mechanism，
coverage 用这个固定集合做 route × mechanism matrix，不随 LLM 输出波动。

使用方式:
    from .route_mechanism_ontology import CORE_ROUTE_MECHANISMS
    mechs = CORE_ROUTE_MECHANISMS["AFCT"]["mechanisms"]
"""

from .models import RouteMechanismEvidenceEdge

# 7 个核心 route，每个固定 4-6 个 core mechanism + 别名
# aliases = Level 1 sub-route / implementation（raw_route normalization 归并用）：
#   "vinyl sulfonate ester"→AFCT、"benzylidene acetal"→ring-opening 等具体化学实现
#   只用于把 raw route 归并到 canonical route，coverage 仍按 canonical 判定。
#   注意：不要把泛描述当 route（"bulky substituent" 是 design strategy 不是 route）。
CORE_ROUTE_MECHANISMS: dict[str, dict] = {
    "AFCT": {
        # 注意：裸 "addition-fragmentation chain transfer" 恢复为 AFCT alias（用户定：
        # AFCT 的 implementation 级词汇，用于 raw_route normalization 归并）；
        # RAFT 系列（reversible addition-fragmentation chain transfer...）在
        # NON_CORE_ROUTES 优先匹配，不会被误吸（见 match_route 顺序）。
        "aliases": [
            "addition-fragmentation chain transfer",
            "addition fragmentation chain transfer",
            "AFT",
            "chain transfer monomer",
            "allyl sulfide chain transfer agent",
            "beta-allyl sulfone",
            "vinyl sulfonate ester",
            "difunctional vinyl sulfonate ester",
        ],
        "mechanisms": [
            "reversible bond exchange",
            "network rearrangement",
            "stress relaxation",
            "delayed gelation",
            "chain transfer",
        ],
    },
    "ring-opening": {
        "aliases": ["ring-opening polymerization", "silorane", "spiro orthocarbonate",
                    "spiro-orthocarbonate", "expanding monomer", "benzylidene acetal",
                    "cyclic benzylidene acetal", "cyclic monomer"],
        "mechanisms": [
            "volumetric expansion",
            "ring strain relief",
            "reduced shrinkage",
            "cationic polymerization",
        ],
    },
    "thiol-ene": {
        "aliases": ["thiol-ene polymerization", "thiol-ene addition", "thiol-michael addition"],
        "mechanisms": [
            "delayed gelation",
            "step-growth polymerization",
            "oxygen tolerance",
            "uniform network formation",
        ],
    },
    "step-growth": {
        "aliases": ["step-growth polymerization"],
        "mechanisms": [
            "delayed gel point",
            "stress relaxation",
            "late gelation",
        ],
    },
    "filler": {
        "aliases": ["filler loading", "silica", "nanoparticle", "inorganic filler"],
        "mechanisms": [
            "reduced polymerizable fraction",
            "stress transfer",
            "reduced shrinkage strain",
            "modulus increase",
        ],
    },
    "monomer-design": {
        "aliases": ["low-shrinkage monomer", "monomer modification", "oligomer design",
                    "molecular weight increase", "increasing molecular weight",
                    "increasing molar volume"],
        "mechanisms": [
            "low shrinkage monomer",
            "reduced double bond density",
            "reduced reactive-group density",
        ],
    },
    "dual-curing": {
        "aliases": ["dual-cure", "two-stage curing", "gradient polymerization"],
        "mechanisms": [
            "two-stage cure",
            "stress relief",
            "delayed network formation",
            "sequential network formation",
        ],
    },
}


# 非核心 route：归并目标，但不进 coverage matrix（避免污染核心 route 的 coverage）。
# RAFT ≠ AFCT：RAFT 是控制/活性自由基聚合方法；AFCT 是应力松弛化学（链转移单体）。
# 论文讲 RAFT 的不能关闭 AFCT 的 coverage gap。
NON_CORE_ROUTES: dict[str, list[str]] = {
    "RAFT": [
        "raft polymerization",
        "reversible addition-fragmentation chain transfer polymerization",
        "reversible addition-fragmentation chain-transfer polymerization",
        "reversible addition-fragmentation chain transfer",
        "controlled living radical polymerization",
        "controlled/living radical polymerization",
    ],
}


def _alias_hit(alias: str, rl: str) -> bool:
    """alias 词边界匹配：'AFT' 不能命中 'RAFT' 里的 'AFT'（rAFT 不是 AFCT 缩写）；
    容忍复数（'vinyl sulfonate ester' 命中 'vinyl sulfonate esters'）。"""
    import re
    a = alias.lower()
    if re.search(rf"(?<![a-z0-9]){re.escape(a)}(?![a-z0-9])", rl):
        return True
    if re.search(rf"(?<![a-z0-9]){re.escape(a)}s(?![a-z0-9])", rl):
        return True
    return False


def match_route(canonical_route: str) -> str | None:
    """把 canonical route 映射到 7 个核心 route 之一（raw_route normalization 归并）。

    顺序（用户定 route taxonomy：Level 0 canonical route / Level 1 implementation）：
      1. NON_CORE_ROUTES 优先（RAFT 等专有长短语，如 "reversible addition-fragmentation
         chain transfer" —— 防止被 AFCT 通用 alias 误吸）
      2. 核心 route：缩写（AFCT）+ aliases（含 Level 1 implementation：vinyl sulfonate
         ester→AFCT、benzylidene acetal→ring-opening、increasing molar volume→monomer-design）
    别名只用于 raw_route normalization（把实现归并到策略），coverage 仍按 canonical 判定，
    不作为 coverage matcher 的宽松关键词。返回 None = 无法归并。
    """
    rl = canonical_route.lower()
    # 1. 非核心 route 优先（RAFT 等专有名词，避免 core alias 误吸）
    for rc, aliases in NON_CORE_ROUTES.items():
        for alias in aliases:
            if alias.lower() in rl:
                return rc
    # 2. 核心 route
    for core, info in CORE_ROUTE_MECHANISMS.items():
        if core.lower() in rl:
            return core
        for alias in info["aliases"]:
            if _alias_hit(alias, rl) or rl in alias.lower():
                return core
    return None


def assign_route(route_phrases) -> str | None:
    """把论文的 route 短语列表映射到 canonical core route。

    顺序：精确匹配 core key → match_route（aliases 包含/子串）。
    返回 None = 无法归并（可能是 problem/application 词，不是 route）。

    用途（Phase 1.8）：route assignment 不做 `paper.route == gap.route` 硬比较，
    先归并到 canonical 再判。e.g. "nanoparticle-polymer composite" → filler
    （含别名 nanoparticle）；"ring-opening polymerization" → ring-opening。
    """
    for phrase in route_phrases or []:
        p = str(phrase).lower().strip()
        if not p:
            continue  # 空字符串：`"" in alias` 恒 True，会误匹配任意 route
        if p in CORE_ROUTE_MECHANISMS:
            return p
        m = match_route(p)
        if m:
            return m
    return None


# 有方向的 route hierarchy（is_a / subtype-of）。key = 子 route，value = 父 route 列表。
# thiol-ene is_a step-growth：论文讲 thiol-ene 可为更上层 step-growth 的 gap 提供证据，
# 但反过来不行（step-growth 论文不能自动算 thiol-ene 覆盖）。
ROUTE_HIERARCHY: dict[str, list[str]] = {
    "thiol-ene": ["step-growth"],
}


def route_match_type(paper_routes: set[str], target_route: str) -> str:
    """DIRECT / INHERITED / NO_MATCH（有方向的 hierarchy match）。

    DIRECT:      target 就是 paper 的 route
    INHERITED:   target 是 paper route 的祖先（paper route is_a target），沿 ROUTE_HIERARCHY 上溯
    NO_MATCH:    其他
    """
    if target_route in paper_routes:
        return "DIRECT"
    for r in paper_routes:
        stack = list(ROUTE_HIERARCHY.get(r, []))
        while stack:
            anc = stack.pop()
            if anc == target_route:
                return "INHERITED"
            stack.extend(ROUTE_HIERARCHY.get(anc, []))
    return "NO_MATCH"


# mechanism canonicalization / subtype（概念层级，非粗暴字符串 synonym）
# target/paper mechanism → canonical concept；subtype is_a canonical。
# 例：stress relief / pre-gel stress relaxation / relax stress 都归到 stress relaxation。
MECHANISM_CANONICAL: dict[str, str] = {
    "stress relief": "stress relaxation",
    "relax stress": "stress relaxation",
    "pre-gel stress relaxation": "stress relaxation",
    "late gelation": "delayed gelation",   # step-growth checklist 与 thiol-ene 同概念
    # A 类 canonical miss（audit_open_gaps 确认）：论文 evidence 明确讲 reduced shrinkage
    #（体收缩降低），checklist filler 用的是 reduced shrinkage strain（收缩应变）——
    # 术语归一，非跨层次 alias。
    "reduced shrinkage": "reduced shrinkage strain",
}

# coverage target 类型（用户定：不要把三类混成同一个 completeness 指标）。
#   MECHANISM      —— 因果中间机制（"为什么 route 起作用"），Phase 1.7 gap search 只针对这类
#   ROUTE_PROPERTY —— 聚合模式/route 属性（step-growth polymerization、cationic polymerization…）
#   EFFECT         —— 效果/性能（reduced shrinkage、modulus increase、oxygen tolerance…）
# 未列出的默认 MECHANISM。
MECHANISM_TYPES: dict[tuple[str, str], str] = {
    # ── MECHANISM ──
    ("AFCT", "reversible bond exchange"): "MECHANISM",
    ("AFCT", "network rearrangement"): "MECHANISM",
    ("AFCT", "stress relaxation"): "MECHANISM",
    ("AFCT", "delayed gelation"): "MECHANISM",
    ("ring-opening", "volumetric expansion"): "MECHANISM",
    ("ring-opening", "ring strain relief"): "MECHANISM",
    ("thiol-ene", "delayed gelation"): "MECHANISM",
    ("thiol-ene", "uniform network formation"): "EFFECT",  # 用户定：结构结果，非严格 mechanism
    ("step-growth", "delayed gel point"): "MECHANISM",
    ("step-growth", "stress relaxation"): "MECHANISM",
    ("step-growth", "late gelation"): "MECHANISM",
    ("filler", "reduced polymerizable fraction"): "MECHANISM",
    ("filler", "stress transfer"): "MECHANISM",
    ("monomer-design", "reduced double bond density"): "MECHANISM",
    ("monomer-design", "reduced reactive-group density"): "MECHANISM",
    ("dual-curing", "stress relief"): "MECHANISM",
    ("dual-curing", "delayed network formation"): "MECHANISM",
    ("dual-curing", "sequential network formation"): "MECHANISM",  # 用户定 2026-08-25 新增
    # ── ROUTE_PROPERTY ──
    ("AFCT", "chain transfer"): "ROUTE_PROPERTY",
    ("ring-opening", "cationic polymerization"): "ROUTE_PROPERTY",
    ("thiol-ene", "step-growth polymerization"): "ROUTE_PROPERTY",
    ("dual-curing", "two-stage cure"): "ROUTE_PROPERTY",
    # ── EFFECT ──
    ("ring-opening", "reduced shrinkage"): "EFFECT",
    ("filler", "reduced shrinkage strain"): "EFFECT",
    ("filler", "modulus increase"): "EFFECT",
    ("monomer-design", "low shrinkage monomer"): "EFFECT",
    ("thiol-ene", "oxygen tolerance"): "EFFECT",
}


def mechanism_type(route: str, mechanism: str) -> str:
    """coverage target 类型（MECHANISM / ROUTE_PROPERTY / EFFECT），默认 MECHANISM。"""
    return MECHANISM_TYPES.get((route, mechanism), "MECHANISM")


# 知识状态层（用户定：知识库构建 ≠ 完备性验证，两者分离）。
# 每个 checklist target 的科学状态（用户校准 2026-08-25——三个维度分开）：
#   领域知识存在性（domain truth）：这个机制在领域里是不是被认可？
#   KB 覆盖状态（KB evidence state）：当前 KB 有没有直接可审计 edge？
#   证据强度（evidence strength）：论文明确说 / 领域判断 / LLM 推断
# confirmed = 领域确认 + KB 有 DIRECT paper evidence（DIRECT_MODEL / DIRECT_HUMAN）
# domain_confirmed —— 领域确认 + KB 有 DOMAIN_VERIFIED edge（领域知识确认但论文表述弱，
#                     如 "implying"——不伪装 DIRECT，不算 strict）
# confirmed_missing —— 领域确认（文献共识）但 KB 无 DIRECT edge → 补漏
#   missing_reason 子分类（修复路线不同，不能混成一个整体）：
#     SEARCH_GAP      —— KB 无任何证据，论文都没找到 → autonomous search（completeness mode）
#     EXTRACTION_GAP  —— 论文已在库/有 evidence，但 extractor 没结构化出 DIRECT edge
#                        → 修 extractor / 人工 edge（add_human_edge），不让搜索 agent 浪费时间
# hypothesis        —— 领域未确认，根据知识图谱推导的推测链 → discovery mode 探索
# rejected          —— 已验证不存在（当前空集，供未来否证）
# 三个模式（用户定 2026-08-25）：
#   completeness mode —— 只搜 confirmed_missing/SEARCH_GAP，目标提高 coverage（搜索能力）
#   extraction repair —— 处理 EXTRACTION_GAP（extractor/人工，非 loop 模式）
#   discovery mode    —— 只搜 hypothesis，目标发现新机制
# 三个 coverage 口径（用户定 2026-08-25，分母 D = confirmed + domain_confirmed +
#   confirmed_missing_open 守恒）：
#   Strict Evidence Coverage —— DIRECT_MODEL + DIRECT_HUMAN（论文直接支撑）
#   Domain Coverage          —— strict + DOMAIN_VERIFIED（含领域确认）
#   Total Evidence Graph     —— domain + INHERITED（图谱规模展示，不用于 completeness）
# 注意：这是【领域标注】不是自动推断——草案基于当前 KB 证据 + 材料学常识，
# 需人工确认（用户是领域权威）。未列出的默认 unknown。
# 用户拍板 2026-08-25：5 个 confirmed 因无 DIRECT edge 全部降级 confirmed_missing
# （AFCT×chain transfer、ring-opening×cationic polymerization、thiol-ene×step-growth
#  polymerization、monomer-design×low shrinkage monomer、dual-curing×two-stage cure）→
# 最终 confirmed 10 / confirmed_missing 13 / hypothesis 4 / domain_confirmed 0。
# 迁移规则：gap 有 DIRECT edge → confirmed；有 DOMAIN_VERIFIED edge（无 DIRECT）→
# domain_confirmed；无 edge → 维持 confirmed_missing/hypothesis。
KNOWLEDGE_STATUS: dict[tuple[str, str], str] = {
    # ── AFCT ──
    ("AFCT", "stress relaxation"): "confirmed",        # DIRECT_MODEL / DIRECT_HUMAN 已有
    ("AFCT", "network rearrangement"): "confirmed",    # DIRECT_HUMAN 已有
    ("AFCT", "chain transfer"): "confirmed_missing",   # 用户拍板：只有 inferred edge，无 DIRECT
    ("AFCT", "reversible bond exchange"): "hypothesis",   # 用户确认 2026-08-25：可逆键交换→应力释放是推测链
    ("AFCT", "delayed gelation"): "hypothesis",        # chain transfer → 可能延缓凝胶（推测链）
    # ── ring-opening ──
    ("ring-opening", "volumetric expansion"): "confirmed",
    ("ring-opening", "reduced shrinkage"): "confirmed",
    ("ring-opening", "ring strain relief"): "confirmed_missing",  # 开环应变释放是经典机制
    ("ring-opening", "cationic polymerization"): "confirmed_missing",  # 用户拍板：edge unbound 无法证明
    # ── thiol-ene ──
    ("thiol-ene", "delayed gelation"): "confirmed",
    ("thiol-ene", "step-growth polymerization"): "confirmed_missing",  # 用户拍板：无 direct edge
    ("thiol-ene", "oxygen tolerance"): "confirmed_missing",       # 耐氧是经典事实
    ("thiol-ene", "uniform network formation"): "confirmed_missing",  # 均匀网络文献共识
    # ── step-growth ──
    ("step-growth", "delayed gel point"): "confirmed",   # INHERITED 已有
    ("step-growth", "stress relaxation"): "confirmed",   # INHERITED 已有
    ("step-growth", "late gelation"): "confirmed",       # INHERITED 已有
    # ── filler ──
    ("filler", "reduced polymerizable fraction"): "confirmed",    # DIRECT_HUMAN 已有
    ("filler", "reduced shrinkage strain"): "confirmed",
    ("filler", "stress transfer"): "confirmed_missing",  # 应力传递是复合材料经典机制
    ("filler", "modulus increase"): "confirmed_missing", # 模量提升是经典事实
    # ── monomer-design ──
    ("monomer-design", "low shrinkage monomer"): "confirmed_missing",  # 用户拍板：EXTRACTION_MISS 有证据但无 edge
    ("monomer-design", "reduced double bond density"): "confirmed_missing",
    ("monomer-design", "reduced reactive-group density"): "confirmed_missing",
    # ── dual-curing ──
    ("dual-curing", "two-stage cure"): "confirmed_missing",  # 用户拍板：领域事实但无 direct edge
    ("dual-curing", "stress relief"): "hypothesis",      # 两阶段应力释放（推测性更强）
    ("dual-curing", "delayed network formation"): "hypothesis",  # 用户定 2026-08-25：避免过度解释（网络形成"被主动推迟"是推测）
    ("dual-curing", "sequential network formation"): "confirmed_missing",  # 用户定：时间分离双网络是文献事实（W4408330450 evidence 支持）
}

# missing_reason：confirmed_missing 的修复路线分类（用户定 2026-08-25）。
#   SEARCH_GAP     —— KB 无任何证据 → completeness mode 搜索（loop 只吃这个）
#   EXTRACTION_GAP —— 论文已在库/有 evidence，extractor 没结构化 → 修 extractor / add_human_edge
# 基于 KB 实际证据归属（论文在库且有 candidate/evidence → EXTRACTION；否则 SEARCH）。
# 2026-08-25 复核：filler×stress transfer 全库无 stress/load transfer evidence
# （W1860611477 是 debonding、W3215662041 是 stress relaxation）→ 归 SEARCH_GAP。
MISSING_REASON: dict[tuple[str, str], str] = {
    # SEARCH_GAP（TRUE_OPEN：KB 无证据，进 autonomous loop）
    ("ring-opening", "ring strain relief"): "SEARCH_GAP",
    ("thiol-ene", "oxygen tolerance"): "SEARCH_GAP",
    ("filler", "stress transfer"): "SEARCH_GAP",      # 复核：无 stress/load transfer evidence
    ("filler", "modulus increase"): "SEARCH_GAP",
    ("monomer-design", "reduced double bond density"): "SEARCH_GAP",
    ("monomer-design", "reduced reactive-group density"): "SEARCH_GAP",
    # EXTRACTION_GAP（论文在库/evidence 已有，extractor 没结构化出 DIRECT edge）
    ("AFCT", "chain transfer"): "EXTRACTION_GAP",              # W3123510402 在库，edge 是 inferred
    ("ring-opening", "cationic polymerization"): "EXTRACTION_GAP",  # W2335209009 unbound route
    ("thiol-ene", "step-growth polymerization"): "EXTRACTION_GAP",  # W7170061635 抽成 delayed gelation
    ("thiol-ene", "uniform network formation"): "EXTRACTION_GAP",   # W7170061635 抽成 stress relaxation
    ("monomer-design", "low shrinkage monomer"): "EXTRACTION_GAP",  # W7169881141 抽成 reduced shrinkage
    ("dual-curing", "two-stage cure"): "EXTRACTION_GAP",           # W3113545149 抽成 non-linear modulus
    ("dual-curing", "sequential network formation"): "EXTRACTION_GAP",  # W4408330450 evidence 支持，无该 edge
}

# EXTRACTION_GAP 二级分类（用户定 2026-08-25：修复路线和断点位置不同，不能混在一起）：
#   MISSING_EDGE    —— paper evidence exists，但 (route, mechanism) 这条 edge 不存在
#                      （extractor 漏抽 / 抽成别的机制 / route 绑定失败）→ LLM extraction miss
#   EDGE_TYPE_ERROR —— edge 存在（mechanism 对或接近），但 relation_type 判错
#                      （inferred 应为 direct）→ relation_type classification error
EXTRACTION_SUBTYPE: dict[tuple[str, str], str] = {
    ("AFCT", "chain transfer"): "EDGE_TYPE_ERROR",                  # edge inferred，evidence 支持 direct
    ("ring-opening", "cationic polymerization"): "MISSING_EDGE",    # edge unbound（route 绑定失败）
    ("thiol-ene", "step-growth polymerization"): "MISSING_EDGE",    # 无该 edge（抽成 delayed gelation）
    ("thiol-ene", "uniform network formation"): "MISSING_EDGE",     # 无该 edge（抽成 stress relaxation）
    ("monomer-design", "low shrinkage monomer"): "MISSING_EDGE",    # 无该 edge（抽成 reduced shrinkage）
    ("dual-curing", "two-stage cure"): "MISSING_EDGE",              # 无该 edge（抽成 non-linear modulus）
    ("dual-curing", "sequential network formation"): "MISSING_EDGE",  # evidence 在但无该 edge
}


def knowledge_status(route: str, mechanism: str) -> str:
    """coverage target 知识状态（confirmed / confirmed_missing / hypothesis / rejected / unknown）。"""
    return KNOWLEDGE_STATUS.get((route, mechanism), "unknown")


def missing_reason(route: str, mechanism: str) -> str:
    """confirmed_missing 的修复路线（SEARCH_GAP / EXTRACTION_GAP），默认 SEARCH_GAP。

    非 confirmed_missing 状态返回 ""。
    """
    if KNOWLEDGE_STATUS.get((route, mechanism)) != "confirmed_missing":
        return ""
    return MISSING_REASON.get((route, mechanism), "SEARCH_GAP")


def extraction_subtype(route: str, mechanism: str) -> str:
    """EXTRACTION_GAP 的二级分类（MISSING_EDGE / EDGE_TYPE_ERROR），默认 MISSING_EDGE。

    非 EXTRACTION_GAP 返回 ""。
    """
    if missing_reason(route, mechanism) != "EXTRACTION_GAP":
        return ""
    return EXTRACTION_SUBTYPE.get((route, mechanism), "MISSING_EDGE")


# Phase 1.7 验收冻结（用户定 2026-08-26）：completeness loop 最后一次验收通过——
# 剩余 4 个 SEARCH_GAP 无架构 bug（无 canonical/route/identity/commit/inferred 污染），
# 未关闭原因全部是"没搜到目标证据"。最终状态固化，Phase 2 不携带"不确定 gap"：
#   true_search_gap    —— 文献确实没找到该机制的 DIRECT 证据（ring-opening×ring strain
#                          relief：文献说 ring opening reduces shrinkage 但没说 because
#                          release of ring strain，证据等级不足；filler×stress transfer：
#                          debonding/relaxation ≠ stress transfer）。保持 open 记录。
#   fulltext_validation—— abstract 级证据不足（retr=2468/7820 但 gate=0），可能是
#                          full text / 更好 extraction / 人工确认；**不要继续扩大 query**。
FINAL_GAP_STATUS: dict[tuple[str, str], str] = {
    ("ring-opening", "ring strain relief"): "true_search_gap",
    ("filler", "stress transfer"): "true_search_gap",
    ("monomer-design", "reduced double bond density"): "fulltext_validation",
    ("monomer-design", "reduced reactive-group density"): "fulltext_validation",
}


def final_gap_status(route: str, mechanism: str) -> str:
    """Phase 1.7 验收冻结的 gap 最终状态（"" = 非冻结项）。"""
    return FINAL_GAP_STATUS.get((route, mechanism), "")


def _norm_mech(t: str) -> str:
    """mechanism 文本轻归一：小写 + 去尾部 " mechanism" 后缀。

    例："step-growth polymerization mechanism" → "step-growth polymerization"
    （论文直接输出 checklist 机制名 + mechanism 后缀，应正常 canonicalize，不制造 false gap）。
    """
    t = (t or "").strip().lower()
    if t.endswith(" mechanism"):
        t = t[:-len(" mechanism")].strip()
    return t


def mechanism_match_type(paper_mechanism_texts: list[str], target_mech: str) -> str:
    """DIRECT / INHERITED / NO_MATCH（mechanism 概念层级，canonical + 轻归一后比较）。"""
    if not target_mech:
        return "DIRECT"
    target_n = _norm_mech(target_mech)
    target_c = MECHANISM_CANONICAL.get(target_n, target_n).lower()
    for t in paper_mechanism_texts or []:
        tn = _norm_mech(t)
        pm_c = MECHANISM_CANONICAL.get(tn, tn).lower()
        if pm_c == target_c:
            return "DIRECT" if tn == target_n else "INHERITED"
    return "NO_MATCH"


class CoverageMatcher:
    """route + mechanism 统一匹配入口（唯一语义源）。

    global rematch / coverage matrix / diagnostic 都必须用它，不要各自实现匹配逻辑。
    route 用 ROUTE_HIERARCHY（有方向 is_a：thiol-ene → step-growth 单向）；
    mechanism 用 MECHANISM_CANONICAL（概念层级：stress relief → stress relaxation）+ 别名兜底。
    """

    def __init__(self, mech_normalizer=None):
        if mech_normalizer is None:
            from .mechanism_normalizer import MechanismNormalizer
            mech_normalizer = MechanismNormalizer()
        self.mech_normalizer = mech_normalizer

    def route_match(self, paper_routes: set[str], target_route: str) -> str:
        """DIRECT / INHERITED / NO_MATCH。"""
        return route_match_type(paper_routes, target_route)

    def mechanism_match(self, paper_mechanism_texts: list[str], target_mech: str) -> str:
        """DIRECT / INHERITED / NO_MATCH。canonical 层级优先，aliases 子串兜底。"""
        if not target_mech:
            return "DIRECT"  # 策略级 gap 无 mechanism
        m = mechanism_match_type(paper_mechanism_texts, target_mech)
        if m != "NO_MATCH":
            return m
        aliases = self.mech_normalizer.aliases_for(target_mech) or [target_mech.lower()]
        joined = " ".join(paper_mechanism_texts or []).lower()
        return "INHERITED" if any(a.lower() in joined for a in aliases) else "NO_MATCH"

    def supports(self, paper_routes, paper_mechanism_texts, target_route, target_mech) -> bool:
        """paper 是否支持 gap（route 与 mechanism 均 DIRECT/INHERITED）。"""
        return (self.route_match(paper_routes, target_route) != "NO_MATCH"
                and self.mechanism_match(paper_mechanism_texts, target_mech) != "NO_MATCH")

    def edge_supports_gap(self, edge, gap_route: str, gap_mech: str) -> str:
        """edge 对 gap 的支持类型：DIRECT_MODEL / DIRECT_HUMAN / DOMAIN_VERIFIED /
        INHERITED / INFERRED / NO_MATCH。

        coverage 判定（用户定 2026-08-25，四类证据强度）：
          DIRECT_MODEL    → covered ✓（论文明确说，模型抽取——评估 extractor 时计入）
          DIRECT_HUMAN    → covered ✓（人工确认论文明确说，provenance=human_verified，
                            评估 extractor 时必须单独统计，不能用人工修复的 coverage 说 extractor 做到了）
          DOMAIN_VERIFIED → domain covered（领域知识确认但论文表述弱——不伪装 DIRECT，
                            完备性证明用双口径：C_strict=DIRECT_MODEL+DIRECT_HUMAN，
                            C_domain=strict+DOMAIN_VERIFIED）
          INHERITED       → covered ✓（route hierarchy 上溯：thiol-ene edge 支持 step-growth gap）
          INFERRED        → 不关闭 gap（? 显示，LLM 推断论文未直说）
          NO_MATCH        → 无关

        关键约束（edge 模型核心）：
        - unbound edge（无 route，raw_route/canonical_route 均空）恒 NO_MATCH ——
          机制可进 inventory 但永不关闭任何 gap。
        - edge 的 route 与 gap route 必须匹配（DIRECT 或 is_a 继承），
          edge 的 mechanism 与 gap mechanism 必须匹配 —— 杜绝
          paper.routes × paper.mechanisms 笛卡尔组合凭空生成关系。
        - INFERRED 只在 route+mechanism 都匹配后才返回（修复：之前把所有
          inferred edge 都算作每个 gap 的候选 → "55 条 inferred 候选" 假象）。
        - EXTRACTION_MISS 边界（用户定）：证据已存在但 extractor 没结构化
          ≠ 搜索失败 ≠ 真 gap；人工核实后 provenance=human_verified 补 edge，
          DIRECT_HUMAN 单独统计。
        """
        r = (edge.canonical_route or edge.raw_route or "").strip()
        m = (edge.canonical_mechanism or edge.raw_mechanism or "").strip()
        if not r or not m:
            return "NO_MATCH"  # unbound mechanism 永远不关闭 gap
        rm = self.route_match({r}, gap_route)
        if rm == "NO_MATCH":
            return "NO_MATCH"
        mm = self.mechanism_match([m], gap_mech)
        if mm == "NO_MATCH":
            return "NO_MATCH"
        rt = (edge.relation_type or "direct").lower()
        if rt == "inferred":
            return "INFERRED"  # 匹配但 LLM 推断（论文未直说）—— 第一版不关闭 gap
        if rt == "domain_verified":
            return "DOMAIN_VERIFIED"  # 领域知识确认但论文表述弱（不伪装 DIRECT）
        if rm == "INHERITED":
            return "INHERITED"
        if rt == "human_verified" or (edge.provenance or "").lower() in ("manual_audit", "human_verified"):
            return "DIRECT_HUMAN"  # 人工核实补的 edge，评估 extractor 时单独统计
        return "DIRECT_MODEL"


def canonicalize_mechanism(text: str) -> str:
    """mechanism 文本 → canonical（MECHANISM_CANONICAL 概念层级归并）。

    本地确定性归并，与 CoverageMatcher 同一语义源，不依赖 LLM 输出一致的字符串。
    e.g. "pre-gel stress relaxation" / "stress relief" → "stress relaxation"
    """
    t = (text or "").strip().lower()
    if not t:
        return ""
    return MECHANISM_CANONICAL.get(t, t)


def build_edge(paper_id: str, raw_route: str, raw_mechanism: str,
               evidence: str = "", confidence: float = 0.0,
               relation_type: str = "direct") -> RouteMechanismEvidenceEdge:
    """raw route/mechanism → RouteMechanismEvidenceEdge（canonical 本地归并）。

    - raw_route 空 → unbound edge（canonical_route 空，永不关闭 gap）
    - raw_route 归并失败（非 ontology 词）→ canonical_route 空，匹配时回退 raw
    - canonical_mechanism 用 MECHANISM_CANONICAL 归并
    """
    rr = (raw_route or "").strip()
    rm = (raw_mechanism or "").strip()
    return RouteMechanismEvidenceEdge(
        paper_id=paper_id,
        raw_route=rr,
        canonical_route=assign_route([rr]) or "",
        raw_mechanism=rm,
        canonical_mechanism=canonicalize_mechanism(rm),
        evidence=evidence or "",
        confidence=float(confidence or 0.0),
        relation_type=(relation_type or "direct").lower(),
    )


def get_mechanisms(core_route: str) -> list[str]:
    """返回核心 route 的固定 mechanism 集合。"""
    return CORE_ROUTE_MECHANISMS.get(core_route, {}).get("mechanisms", [])


def compute_gap_coverage(all_edges, matcher: "CoverageMatcher | None" = None) -> dict:
    """全局扫描：每个 checklist gap 的覆盖状态（唯一统计口径，工具统一用）。

    两个审计工具（check_edge_quality / print_coverage_matrix）都调它，
    避免一个报 5 一个报 8 的口径分裂。返回 {(route, mech): {status, best, inferred, type}}：
      status   = DIRECT_MODEL / DIRECT_HUMAN / DOMAIN_VERIFIED / INHERITED / OPEN
                 （优先级 DIRECT_MODEL > DIRECT_HUMAN > DOMAIN_VERIFIED > INHERITED——
                 同 gap 有更直接的证据就不算低一级；DIRECT_HUMAN 是人工核实的 edge，
                 评估 extractor 时必须单独统计；DOMAIN_VERIFIED 是领域确认但论文表述弱，
                 完备性证明用双口径 C_strict / C_domain）
      best     = 支持该 gap 的最优 edge（conf 最高）
      inferred = relation_type=inferred 且 route+mechanism 匹配的候选 edge（仅诊断）
      type     = MECHANISM / ROUTE_PROPERTY / EFFECT
    """
    if matcher is None:
        matcher = CoverageMatcher()
    result: dict = {}
    for core in CORE_ROUTE_MECHANISMS:
        for mech in get_mechanisms(core):
            supporting, inferred = [], []
            for e in all_edges:
                st = matcher.edge_supports_gap(e, core, mech)
                if st == "INFERRED":
                    inferred.append(e)
                elif st != "NO_MATCH":
                    supporting.append((st, e))
            status = "OPEN"
            best = None
            if supporting:
                # 优先级：DIRECT_MODEL > DIRECT_HUMAN > DOMAIN_VERIFIED > INHERITED
                for pref in ("DIRECT_MODEL", "DIRECT_HUMAN", "DOMAIN_VERIFIED", "INHERITED"):
                    chosen = [x for x in supporting if x[0] == pref]
                    if chosen:
                        st, best = max(chosen, key=lambda x: x[1].confidence)
                        status = st
                        break
            result[(core, mech)] = {
                "status": status,
                "best": best,
                "inferred": inferred,
                "type": mechanism_type(core, mech),
            }
    return result
