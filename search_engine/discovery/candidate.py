"""Phase 2.0 核心数据结构：DiscoveryCandidate + 状态机 + 升格规则（用户定 2026-08-26）。

状态机：
    RAW → TYPED → {ALIAS, IRRELEVANT, EXISTING_KNOWLEDGE, CANDIDATE}
                CANDIDATE → VERIFYING → {REJECTED, ADJACENT, VALIDATED} → PROMOTED

ALIAS            —— 拼写变体（monomer design → monomer-design），canonicalize 不算 discovery
EXISTING_KNOWLEDGE —— ontology 已表达但字符串不同（知识新颖性 ≠ 字符串新颖性）
IRRELEVANT       —— 与当前研究问题无关
CANDIDATE        —— 确有可能的新知识
VERIFYING        —— 针对性搜索验证中
ADJACENT         —— 真知识但关系不够直接（如 self-healing vs shrinkage/stress）
VALIDATED        —— 多篇支持 + 与当前问题直接相关
PROMOTED         —— 正式加入 ontology
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict


CANDIDATE_TYPES = (
    "ROUTE",
    "SUB_ROUTE",
    "MECHANISM",
    "PROCESS_STRATEGY",
    "FORMULATION_STRATEGY",
    "EFFECT",
    "MATERIAL_CAPABILITY",
    "CONTEXT_TERM",   # 领域/过程/材料背景概念（photopolymerization/resin），真实概念但不是知识节点
    "ALIAS",
    "UNKNOWN",
)

# 候选来源（用户定 2026-08-26：Phase 2 不能只依赖 scanner）
CANDIDATE_SOURCES = (
    "scanner",         # 从 KB edges 直接扫出（字符串出现在库里）
    "human_seed",      # 人工推导的 seed（如 dynamic covalent bond exchange ← self-healing/DCB）
    "hypothesis_seed", # 从 hypothesis 空间推导的 seed
)

# 状态机：status → 允许的下一状态
# NEED_MORE_EVIDENCE（用户定 2026-08-26）：验证后证据不足（1 篇强 DIRECT 但不足 2 篇），
# 既不能 VALIDATED 也不能 REJECTED → 回到 VERIFYING 下一轮继续验证。
# SEARCH_INCONCLUSIVE（用户定 2026-08-26）：现有检索没形成有效验证语料（retrieval
# failure ≠ scientific irrelevance）——连候选领域论文都没搜对，不能下科学判断。
# SEARCH_INCONCLUSIVE_FROZEN（用户定 2026-08-26 Phase 2.1）：自动重试 1 次仍失败后冻结
# ——不再自动进 prioritizer；重开只允许：人工 MANUAL_REOPEN，或新 evidence/新 ontology
# 导致条件变化（controller 检测后以 reason 非空重开）。防一次 retrieval 偶然失败就
# 永久放弃，也防无限烧 API。
STATUS_FLOW: dict[str, list[str]] = {
    "RAW": ["TYPED"],
    "TYPED": ["ALIAS", "IRRELEVANT", "EXISTING_KNOWLEDGE", "CANDIDATE"],
    "CANDIDATE": ["VERIFYING"],
    "VERIFYING": ["REJECTED", "ADJACENT", "VALIDATED", "NEED_MORE_EVIDENCE", "SEARCH_INCONCLUSIVE"],
    "NEED_MORE_EVIDENCE": ["VERIFYING"],
    "SEARCH_INCONCLUSIVE": ["VERIFYING", "SEARCH_INCONCLUSIVE_FROZEN"],   # 重试 1 次后冻结
    "SEARCH_INCONCLUSIVE_FROZEN": [],   # 冻结终态（只允许人工/新证据重开，见 transition）
    "VALIDATED": ["PROMOTED"],
}

STATUS_LABELS = {
    "RAW": "scanner 刚发现（高召回，不判断）",
    "TYPED": "已完成 10 类分类",
    "ALIAS": "拼写变体，canonicalize 不算 discovery",
    "IRRELEVANT": "与当前研究问题无关",
    "EXISTING_KNOWLEDGE": "ontology 已表达（字符串不同）",
    "CANDIDATE": "确有可能的新知识",
    "VERIFYING": "针对性搜索验证中",
    "REJECTED": "验证后否决（同样证明 pipeline 有效）",
    "ADJACENT": "真知识但关系不够直接（搜到了候选概念论文，但无 target relation）",
    "NEED_MORE_EVIDENCE": "证据不足（如 1 篇强 DIRECT 但不足 2 篇）——下一轮继续验证",
    "SEARCH_INCONCLUSIVE": "检索没形成有效验证语料（连候选领域论文都没搜对）——retrieval failure ≠ 领域无关",
    "SEARCH_INCONCLUSIVE_FROZEN": "检索重试 1 次仍失败——冻结（只允许人工 MANUAL_REOPEN 或新 evidence 重开）",
    "VALIDATED": "多篇支持 + 与当前问题直接相关",
    "PROMOTED": "正式加入 ontology",
}

# Promotion 条件（用户定 2026-08-26，第一版保守）
PROMOTION_RULES = {
    "not_alias": "不是 alias",
    "not_rename": "不是 existing ontology 的简单重命名",
    "min_papers": "≥2 篇独立论文支持",
    "direct_evidence": "至少 1 条 DIRECT evidence",
    "domain_relevance": "与当前研究问题有明确关系（domain_relevance ≥ MEDIUM）",
    "typed": "candidate_type 明确（≠ UNKNOWN）",
    "explainable": "与已有 node 的关系可解释（canonical_match / 相邻关系）",
}


@dataclass
class RawCandidate:
    """Scanner 高召回输出（宁可多发现，不要负责判断）。

    kind: route / mechanism（来源维度）
    route_assoc / mechanism_assoc：该候选出现的 canonical routes / mechanisms
    evidence_samples：最多 3 条 evidence 片段（供候选池展示）
    """

    raw_name: str
    kind: str = "mechanism"
    paper_ids: set = field(default_factory=set)
    edge_count: int = 0
    route_assoc: set = field(default_factory=set)
    mechanism_assoc: set = field(default_factory=set)
    evidence_samples: list = field(default_factory=list)

    @property
    def paper_count(self) -> int:
        return len(self.paper_ids)


class InvalidTransition(ValueError):
    """状态机非法迁移（自动流程不可 reopen；人工审计可 MANUAL_REOPEN）。"""


@dataclass
class DiscoveryCandidate:
    """Phase 2.0 Candidate Pool 统一结构（用户定 schema）。

    domain_relevance: HIGH / MEDIUM / LOW / UNKNOWN（字符串分级）
    provenance: 来源轨迹（scanner 统计 / typer 依据 / filter 命中）
    """

    candidate_id: str
    raw_name: str
    canonical_name: str | None = None
    candidate_type: str = "UNKNOWN"
    source: str = "scanner"          # scanner / human_seed / hypothesis_seed
    source_papers: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    independent_paper_count: int = 0
    canonical_match: str | None = None
    novelty_score: float | None = None
    domain_relevance: str = "UNKNOWN"
    status: str = "RAW"
    provenance: dict = field(default_factory=dict)
    review_log: list = field(default_factory=list)  # 人工/verifier 记录（persistence 保留）

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_raw(cls, raw: RawCandidate, provenance_extra: dict | None = None,
                 **overrides) -> "DiscoveryCandidate":
        """RawCandidate → DiscoveryCandidate（TYPED 前，candidate_id 稳定生成）。"""
        import hashlib
        cid = hashlib.md5(raw.raw_name.encode("utf-8")).hexdigest()[:12]
        prov = {
            "kind": raw.kind,
            "edge_count": raw.edge_count,
            "route_assoc": sorted(raw.route_assoc),
            "mechanism_assoc": sorted(raw.mechanism_assoc),
        }
        if provenance_extra:
            prov.update(provenance_extra)
        base = {
            "candidate_id": cid,
            "raw_name": raw.raw_name,
            "source_papers": sorted(raw.paper_ids),
            "independent_paper_count": raw.paper_count,
            "provenance": prov,
        }
        base.update(overrides)
        return cls(**base)

    def transition(self, next_status: str, manual_override: bool = False,
                   reason: str | None = None) -> bool:
        """状态机迁移校验（用户定 2026-08-26：自动流程不能 reopen，人工审计可以）。

        正常迁移：STATUS_FLOW（严格）——ADJACENT / REJECTED 是终态，
        防止 autonomous discovery 形成 ADJACENT↔VERIFYING 无意义循环。

        人工审计 override（manual_override=True 且 reason 非空，review --direct）：
          MANUAL_REOPEN   —— {ADJACENT, REJECTED, SEARCH_INCONCLUSIVE_FROZEN} → VERIFYING
                             （旧错误 verdict / 冻结候选的人工重开）
          REVALIDATION    —— VALIDATED → VERIFYING（已验证节点的重审，如旧 verifier bug
                             产生的错误 VALIDATED；用户定：低频人工操作 + reason 必填，
                             不会形成无意义循环）
          MANUAL_OVERRIDE —— CANDIDATE → {REJECTED, ADJACENT}（人工快速判定）

        SEARCH_INCONCLUSIVE_FROZEN 重开（用户定 Phase 2.1）：只允许 ① 人工
        MANUAL_REOPEN（--direct + --reason），或 ② controller 检测到新 evidence/
        新 ontology 后以 reason 非空重开（自动路径复用同一通道，reason 记录
        "auto_reopen: new evidence"）——防无限烧 API，也防一次偶然失败永久放弃。

        非法迁移 raise InvalidTransition。
        """
        if next_status in STATUS_FLOW.get(self.status, []):
            self.status = next_status
            return True
        if manual_override and reason:
            if (next_status == "VERIFYING"
                    and self.status in ("ADJACENT", "REJECTED", "VALIDATED",
                                        "SEARCH_INCONCLUSIVE_FROZEN")):
                self.status = next_status  # MANUAL_REOPEN / REVALIDATION（人工重开验证）
                return True
            if (self.status == "CANDIDATE"
                    and next_status in ("REJECTED", "ADJACENT")):
                self.status = next_status  # MANUAL_OVERRIDE（人工快速判定）
                return True
        raise InvalidTransition(
            f"{self.status} → {next_status} 非法迁移"
            f"（正常允许: {STATUS_FLOW.get(self.status, [])}；"
            f"人工 reopen 需 --direct + --reason 非空）")


def merge_pool(old_cands: list[dict], new_cands: list["DiscoveryCandidate"]) -> list[dict]:
    """按 candidate_id merge（用户定 2026-08-26，persistence 规则）：

    更新：source_papers / evidence / independent_paper_count / provenance.edge_count
    保留：status / source / review_log / candidate_type / canonical_match /
          domain_relevance / provenance（人工 typing/relevance override 不被扫描重置）
    human_seed / hypothesis_seed 跨 scanner rerun 永久保留——旧池里存在的
    seed 不在新扫描结果中时不会被删除。
    """
    merged = {c["candidate_id"]: c for c in old_cands}
    for cand in new_cands:
        cid = cand.candidate_id
        if cid in merged:
            old = merged[cid]
            papers = sorted(set(old.get("source_papers", [])) | set(cand.source_papers))
            old["source_papers"] = papers
            old["independent_paper_count"] = len(papers)
            old_ev = set(old.get("evidence", []))
            for e in cand.evidence:
                if e not in old_ev:
                    old.setdefault("evidence", []).append(e)
            old.setdefault("provenance", {})["edge_count"] = \
                cand.provenance.get("edge_count", 0)
            # 人工字段（status/source/review_log/type/rel/canonical_match/provenance）不动
        else:
            merged[cid] = cand.to_dict()
    return list(merged.values())


def can_verify(candidate: "DiscoveryCandidate") -> bool:
    """验证入口门槛（用户定 2026-08-26：入口宽）。

    **不要求** ≥2 篇 / rel≥MEDIUM / DIRECT evidence——那些是 verifier 要验证的
    （Q2 本来就是回答"相关不相关"，预先要求 rel≥MEDIUM 是循环定义）。

    只要求：status == CANDIDATE 且 type 是"可验证的知识节点"
    （≠ UNKNOWN 未分类、≠ CONTEXT_TERM 领域背景、≠ ALIAS 拼写变体）
    且非 existing knowledge（canonical_match 为空）。
    因此 human_seed（0 篇）与 rel=UNKNOWN 的候选都合法进 VERIFYING。
    """
    return (candidate.status == "CANDIDATE"
            and candidate.candidate_type not in ("UNKNOWN", "CONTEXT_TERM", "ALIAS")
            and not candidate.canonical_match)


# ── Phase 2.1 候选追踪（用户定：区分"以前验证过但有新证据" vs "每轮都被重复选中"）──

def get_tracking(candidate: dict) -> dict:
    """读取/初始化 provenance.tracking（Phase 2.1 prioritizer 依据，用户定字段）。

    last_selected_round     —— 上次被 prioritizer 选中的 round（None = 从未选过）
    verification_attempts   —— 累计验证尝试次数
    no_evidence_gain_streak —— 连续 N 轮选中后无新增证据（NEED_MORE 降权用，r 参数）
    last_evidence_signature —— 上次验证时的证据指纹（对比判定有无新证据）
    search_inconclusive_retries —— SEARCH_INCONCLUSIVE 重试次数（≥2 → 冻结 FROZEN）
    """
    prov = candidate.setdefault("provenance", {})
    return prov.setdefault("tracking", {
        "last_selected_round": None,
        "verification_attempts": 0,
        "no_evidence_gain_streak": 0,
        "last_evidence_signature": None,
        "search_inconclusive_retries": 0,
    })


def evidence_signature(candidate: dict) -> str:
    """候选当前证据指纹：source_papers + verification 三层 DIRECT 论文计数。

    用于判定"重试后有无新证据"：
      - NEED_MORE_EVIDENCE 降权（连续无增益 streak +1 → Score × 1/(1+0.3r)）
      - SEARCH_INCONCLUSIVE_FROZEN 自动重开（指纹变化 = 新 evidence 条件成立）
    """
    import hashlib
    papers = sorted(candidate.get("source_papers", []) or [])
    v = (candidate.get("provenance") or {}).get("verification") or {}
    parts = papers + [f"dc:{v.get('direct_concept_paper_count', 0)}",
                      f"dr:{v.get('direct_relation_paper_count', 0)}",
                      f"dt:{v.get('direct_target_paper_count', 0)}"]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]


def verification_priority(candidate: "DiscoveryCandidate") -> bool:
    """自动验证优先池（shortlist）：值得优先验证的候选。

    在 can_verify 之上叠加：≥2 篇独立论文 + rel≥MEDIUM。
    只用于"自动挑优先验证谁"，不是入口门槛，更不是 promotion 预检。
    """
    return (can_verify(candidate)
            and candidate.independent_paper_count >= 2
            and candidate.domain_relevance in ("HIGH", "MEDIUM"))


def can_promote(candidate: "DiscoveryCandidate",
                verification: dict | None = None) -> tuple[bool, list[str]]:
    """严格升格出口（用户定 2026-08-26：出口严，7 条）。

    verification: verifier 输出聚合（direct_evidence_count / causal_chain / ontology_position）。
    返回 (ok, 未满足理由列表)。
    """
    missing: list[str] = []
    if candidate.canonical_match:
        missing.append("① novel_to_ontology（非 alias / 非 existing）")
    if candidate.independent_paper_count < 2:
        missing.append(f"② ≥2 篇独立论文（当前 {candidate.independent_paper_count}）")
    v = verification or {}
    # 升格只看 target 层 DIRECT **paper 数**（用户定：evidence count ≠ paper count，
    # 独立性按 canonical paper identity 去重；模型 paraphrase 不能算 DIRECT）
    if v.get("direct_target_paper_count", 0) < 1:
        missing.append("③ ≥1 篇 DIRECT target 证据论文（可追溯 paper_id+原文——"
                       "不是 evidence 条数，不是模型总结）")
    if candidate.domain_relevance not in ("HIGH", "MEDIUM"):
        missing.append(f"④ domain_relevance ≥ MEDIUM（当前 {candidate.domain_relevance}）")
    # ⑤ causal/structural relationship：至少 PARTIAL（有部分因果/结构关系即可；
    # bulk-fill 场景 causal_status=PARTIAL_CAUSAL_EVIDENCE 仍可 promotion，
    # 因果细节由后续知识图谱继续表达——用户定 2026-08-26）
    if v.get("causal_status") in ("NO_CAUSAL_EVIDENCE", None, ""):
        missing.append("⑤ causal/structural relationship 明确（causal_status 不能是 "
                       "NO_CAUSAL_EVIDENCE——PARTIAL/COMPOSITION/NOVEL 均可）")
    if candidate.candidate_type in ("UNKNOWN", "CONTEXT_TERM", "ALIAS"):
        missing.append("⑥ candidate_type 明确")
    if not v.get("ontology_position"):
        missing.append("⑦ 与现有 ontology 的位置明确")
    return (not missing, missing)
