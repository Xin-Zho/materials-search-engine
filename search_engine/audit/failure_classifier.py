"""search_engine/audit/failure_classifier.py — v3.0 Failure Classifier（规则化，M0-M6）。

输入：MissEvidence（miss_analyzer 输出）。输出：多标签分类
{primary_failure, secondary_failures, confidence, confidence_level,
 confidence_reason, missing_evidence}。

冻结类（用户 2026-08-29 定稿）：
  M0 UNRESOLVED            证据不足，不硬分类（可审计优先于装作知道原因）
  M1 TERMINOLOGY_GAP       方向已知（community represented）但目标特有语言系统未掌握
  M2 COMMUNITY_GAP         论文所在研究群体没进入已有 community representation
  M3 CITATION_BRIDGE_GAP   citation traversal 没把目标连接进图（相邻图覆盖不足）
  M4 QUERY_FORMULATION_GAP 社区+有效术语都在，但没生成有效 query
  M5 RANK_DEPTH_GAP        正确 query 结果集包含目标但 rank > 导出深度
  M6 EXECUTION_IDENTITY_GAP query 没执行/导出失败/identity mismatch（工程问题）

规则（deterministic，按优先级）：
  1. identity 未解析（无 eid/doi/wid）                    -> M6 (0.95)
  2. query_set_contains_target 且不在深度内                 -> M5 (0.90)
  3. community represented:
       novel_terms 非空 -> M1 (0.85)；无有效 query -> secondary M4
       无 novel 且无有效 query                              -> M4 (0.70)
  4. community 未 represented:
       citation 数据可用且有连接 -> M2 (0.75)
       citation 数据可用且无连接 -> M3 (0.65)
       citation 数据不可用       -> M0（无法区分 M2/M3）

M0 触发（证据不足 -> 不硬分类）：
  - M2/M3 分支但 citation 证据缺失（bridge 未加载 且 enriched 无目标）
  - term 证据为空（目标无 title/abstract，短语提取不出）
  confidence=0.31 + confidence_level=LOW + missing_evidence=[...]
  + confidence_reason=<EVIDENCE>_MISSING

confidence_level：>=0.85 HIGH / >=0.7 MEDIUM / else LOW。
"""
from __future__ import annotations

M0 = "M0_UNRESOLVED"
M1 = "M1_TERMINOLOGY_GAP"
M2 = "M2_COMMUNITY_GAP"
M3 = "M3_CITATION_BRIDGE_GAP"
M4 = "M4_QUERY_FORMULATION_GAP"
M5 = "M5_RANK_DEPTH_GAP"
M6 = "M6_EXECUTION_IDENTITY_GAP"


def _level(conf: float) -> str:
    return "HIGH" if conf >= 0.85 else ("MEDIUM" if conf >= 0.7 else "LOW")


def classify(evidence: dict) -> dict:
    """MissEvidence -> {primary_failure, secondary_failures, confidence,
    confidence_level, confidence_reason, missing_evidence}。"""
    id_ev = evidence.get("identity_evidence", {})
    q_ev = evidence.get("query_evidence", {})
    c_ev = evidence.get("citation_evidence", {})
    comm_ev = evidence.get("community_evidence", {})
    t_ev = evidence.get("term_evidence", {})

    primary = None
    conf = 0.0
    secondary: set[str] = set()

    # 1. identity 未解析 -> M6
    if not id_ev.get("resolved"):
        primary, conf = M6, 0.95

    # 2. query 结果集包含目标但 rank 超深 -> M5
    elif q_ev.get("query_set_contains_target"):
        primary, conf = M5, 0.90

    elif comm_ev.get("represented"):
        # 3. community represented
        if t_ev.get("n_novel", 0) > 0:
            primary, conf = M1, 0.85
            if not q_ev.get("query_set_contains_target"):
                secondary.add(M4)     # 语言缺的同时也没生成有效 query
        elif not q_ev.get("query_set_contains_target"):
            primary, conf = M4, 0.70
        else:
            primary, conf = M5, 0.90  # 兜底（不应到达）
    else:
        # 4. community 未 represented（M2 vs M3 依赖 citation 证据）
        citation_ok = c_ev.get("_data_available", True)
        has_citation = bool(c_ev.get("known_bridge_node")
                            or (c_ev.get("backward_links_to_found") or 0) > 0)
        if not citation_ok:
            primary, conf = M0, 0.31   # 无法区分 M2/M3
        elif has_citation:
            primary, conf = M2, 0.75
        else:
            primary, conf = M3, 0.65

    # ── M0 检查 + evidence coverage ──
    missing: list[str] = []
    if primary not in (M0, M6) and t_ev.get("n_shared", 0) + t_ev.get("n_novel", 0) == 0:
        missing.append("term")
    if primary == M0:
        if not c_ev.get("_data_available", True):
            missing.append("citation")

    if missing:
        return {"primary_failure": M0,
                "secondary_failures": [],
                "confidence": 0.31,
                "confidence_level": "LOW",
                "confidence_reason": "_".join(m.upper() for m in missing)
                + "_EVIDENCE_MISSING",
                "missing_evidence": missing}

    # ── secondary：补充其他可解释断点 ──
    if primary != M1 and t_ev.get("n_novel", 0) > 0:
        secondary.add(M1)
    if primary != M4 and not q_ev.get("query_set_contains_target") \
            and primary not in (M2, M3, M0, M6):
        secondary.add(M4)

    return {"primary_failure": primary,
            "secondary_failures": sorted(secondary),
            "confidence": round(conf, 2),
            "confidence_level": _level(conf),
            "confidence_reason": None,
            "missing_evidence": []}
