"""Phase 2.1 Candidate Prioritizer（用户定 2026-08-26 Phase 2.1）。

评分（归一化 + 显式权重，第一版不 RL）：
    Score(c) = 0.10·N + 0.30·R + 0.20·E + 0.30·S − 0.10·C

分量（全部归一化到 0~1，量纲一致）：
    N Novelty  —— **ontology distance**（用户定：不用 edge_count——edge 少会奖励
                  冷门噪声，且与 E 是同一信号反向使用）。第一版规则近似：
                  canonical_match→0 / SUB_ROUTE→0.35 / EFFECT|MATERIAL_CAPABILITY→0.50 /
                  PROCESS|FORMULATION_STRATEGY→0.70 / MECHANISM→0.85 / ROUTE→1.00
    R Relevance —— HIGH=1.0 / MEDIUM=0.6 / LOW=0.3 / UNKNOWN=0.1
    E Evidence  —— min(1, independent_paper_count / 5)
    S Structural—— action 结构价值 / 5（NEW_ROUTE 5 → 1.0，EFFECT 1 → 0.2）
    C Cost      —— min(1, est_queries / 10)

降权（用户定）：NEED_MORE_EVIDENCE 自动重入 prioritizer；连续无新增证据
（provenance.tracking.no_evidence_gain_streak = r）→
    Score' = Score × 1/(1 + 0.3r)
不冻结——未来 ontology 扩展后它可能突然获得新入口。

选择（diversification = exploration，用户定）：select() 先按 type 配额
（每 type 最多 K 个），配额用完后余量给最高分——防止一轮全探索同一知识簇
（10 个 filler 候选得分都高 → 只选其中 K 个）。

eligible 只考虑：CANDIDATE / VERIFYING / NEED_MORE_EVIDENCE / SEARCH_INCONCLUSIVE(重试≤1)。
冻结：REJECTED / ADJACENT / PROMOTED / VALIDATED / SEARCH_INCONCLUSIVE_FROZEN。
"""

from __future__ import annotations

from .candidate import get_tracking, evidence_signature

# 初始权重（用户拍板 2026-08-26：优先"大概率相关 + 能扩展结构"，不追 novelty）
DEFAULT_WEIGHTS = {
    "novelty": 0.10,
    "relevance": 0.30,
    "evidence": 0.20,
    "structural": 0.30,
    "cost": 0.10,
}

# 每 type 配额（round 选择上限，防同一知识簇霸榜）
DEFAULT_MAX_PER_TYPE = {
    "ROUTE": 2,
    "MECHANISM": 2,
    "PROCESS_STRATEGY": 1,
    "FORMULATION_STRATEGY": 1,
    "SUB_ROUTE": 1,
    "EFFECT": 1,
    "MATERIAL_CAPABILITY": 1,
    "CONTEXT_TERM": 0,
    "UNKNOWN": 0,
    "ALIAS": 0,
}

# Structural value（用户定：新顶层/新机制 > 小 effect variant）
TYPE_TO_STRUCTURAL_SCORE = {
    "ROUTE": 5,
    "MECHANISM": 4,
    "PROCESS_STRATEGY": 3,
    "FORMULATION_STRATEGY": 3,
    "SUB_ROUTE": 2,
    "EFFECT": 1,
    "MATERIAL_CAPABILITY": 1,
    "CONTEXT_TERM": 0,
    "UNKNOWN": 0,
    "ALIAS": 0,
}

_RELEVANCE_SCALE = {"HIGH": 1.0, "MEDIUM": 0.6, "LOW": 0.3, "UNKNOWN": 0.1}

# 冻结状态（用户定：不重复验证，除非新 evidence）
FROZEN_STATUSES = ("REJECTED", "ADJACENT", "PROMOTED", "VALIDATED",
                   "SEARCH_INCONCLUSIVE_FROZEN", "ALIAS", "EXISTING_KNOWLEDGE",
                   "IRRELEVANT")


def novelty_score(c: dict) -> float:
    """ontology distance 规则近似（用户定；N 权重 0.10，宁可低权重不拿 edge_count 冒充）。"""
    if c.get("canonical_match"):
        return 0.0
    t = c.get("candidate_type", "UNKNOWN")
    return {
        "SUB_ROUTE": 0.35,
        "EFFECT": 0.50, "MATERIAL_CAPABILITY": 0.50,
        "PROCESS_STRATEGY": 0.70, "FORMULATION_STRATEGY": 0.70,
        "MECHANISM": 0.85,
        "ROUTE": 1.00,
    }.get(t, 0.10)


def relevance_score(c: dict) -> float:
    return _RELEVANCE_SCALE.get(c.get("domain_relevance", "UNKNOWN"), 0.1)


def evidence_score(c: dict) -> float:
    n = c.get("independent_paper_count", 0) or 0
    return min(1.0, n / 5.0)


def structural_score(c: dict) -> float:
    return TYPE_TO_STRUCTURAL_SCORE.get(c.get("candidate_type", "UNKNOWN"), 0) / 5.0


def estimate_queries(c: dict) -> float:
    """估算 query 成本：重试候选增量跑（已有语料）→ 2；seed → 4；常规 → 3。"""
    v = (c.get("provenance") or {}).get("verification")
    if c.get("status") in ("NEED_MORE_EVIDENCE", "SEARCH_INCONCLUSIVE") and v:
        return 2.0
    if c.get("source") in ("human_seed", "hypothesis_seed"):
        return 4.0
    return 3.0


def cost_score(c: dict) -> float:
    return min(1.0, estimate_queries(c) / 10.0)


def score_candidate(c: dict, weights: dict | None = None) -> float:
    """Score = wN·N + wR·R + wE·E + wS·S − wC·C（未含 NEED_MORE 降权，见 score_with_penalty）。"""
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    return (w["novelty"] * novelty_score(c)
            + w["relevance"] * relevance_score(c)
            + w["evidence"] * evidence_score(c)
            + w["structural"] * structural_score(c)
            - w["cost"] * cost_score(c))


def no_evidence_gain_streak(c: dict) -> int:
    return int(get_tracking(c).get("no_evidence_gain_streak", 0))


def score_with_penalty(c: dict, weights: dict | None = None) -> float:
    """Score' = Score × 1/(1 + 0.3r)（用户定：r = 连续无新增证据的重试次数）。"""
    score = score_candidate(c, weights)
    r = no_evidence_gain_streak(c)
    return score / (1.0 + 0.3 * r)


def eligible(c: dict) -> bool:
    """可参与本轮 prioritizer 的候选（用户定：不重复验证冻结状态）。

    SEARCH_INCONCLUSIVE：自动重试最多 1 次——第一次失败（retries=1）后**允许**
    下一轮自动重试（用户定）；第二次失败后 controller 已转
    SEARCH_INCONCLUSIVE_FROZEN（被 FROZEN_STATUSES 排除）。防御：若出现
    retries>=2 仍未冻结的异常状态，也排除。
    """
    if c.get("status") in FROZEN_STATUSES:
        return False
    if c.get("candidate_type") in ("UNKNOWN", "CONTEXT_TERM", "ALIAS"):
        return False
    if c.get("canonical_match"):
        return False
    if (c.get("status") == "SEARCH_INCONCLUSIVE"
            and int(get_tracking(c).get("search_inconclusive_retries", 0)) >= 2):
        return False
    return True


def select(candidates: list[dict], top_n: int = 8,
           max_per_type: dict | None = None,
           weights: dict | None = None) -> list[dict]:
    """选择 top-N 候选：**硬配额**（用户定：每轮每种 candidate_type 最多 K 个）。

    top_n 是上限不是下限——配额满后不再选该类型（同簇候选再多也不重复探索，
    下轮配额重置可再选）；diversification 就是 exploration。
    """
    pool = [c for c in candidates if eligible(c)]
    if not pool:
        return []
    mpt = {**DEFAULT_MAX_PER_TYPE, **(max_per_type or {})}
    scored = sorted(pool, key=lambda c: score_with_penalty(c, weights), reverse=True)
    chosen: list[dict] = []
    used: dict[str, int] = {}
    for c in scored:
        t = c.get("candidate_type", "UNKNOWN")
        if used.get(t, 0) >= mpt.get(t, 0):
            continue
        chosen.append(c)
        used[t] = used.get(t, 0) + 1
        if len(chosen) >= top_n:
            break
    return chosen


def update_tracking_after_verify(c: dict, round_id: int) -> dict:
    """verify 后更新 tracking（controller 调用；P0 纯函数可测）。

    - verification_attempts + 1
    - last_selected_round = round_id
    - 证据增益判定：当前 evidence_signature ≠ 上次 → 有新证据 → streak 归零；
      无新证据 → streak + 1（NEED_MORE 降权依据）
    - last_evidence_signature = 当前指纹
    """
    track = get_tracking(c)
    track["verification_attempts"] = int(track.get("verification_attempts", 0)) + 1
    track["last_selected_round"] = round_id
    sig_now = evidence_signature(c)
    sig_before = track.get("last_evidence_signature")
    if sig_before is not None and sig_before == sig_now:
        track["no_evidence_gain_streak"] = int(track.get("no_evidence_gain_streak", 0)) + 1
    else:
        track["no_evidence_gain_streak"] = 0
    track["last_evidence_signature"] = sig_now
    return track
