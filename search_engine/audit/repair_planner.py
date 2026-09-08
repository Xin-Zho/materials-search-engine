"""search_engine/audit/repair_planner.py — v3.0 Repair Planner（DRY-RUN ONLY）。

输入：多篇 miss 的 classification。输出：Repair Plan（模块映射 + 触发数 + 优先级）。

原则（用户 2026-08-29 定稿）：
  - repair_mode = DRY_RUN 硬编码；绝不自动执行 Repair、绝不自动改 query
  - primary 用于 Repair Router；secondary 说明修完一层后还有下一层瓶颈
  - 优先级 = 触发该模块的 miss 数降序
"""
from __future__ import annotations

from collections import Counter

# M 类 -> (模块名, 修复策略描述)
MODULE_MAP = {
    "M1_TERMINOLOGY_GAP": ("terminology_repair",
                           "v2.2 terminology repair：historical synonym / 形态变体聚合 / "
                           "citation-backed rare term 绕过 df>=2 约束"),
    "M2_COMMUNITY_GAP": ("community_expansion",
                         "citation/community expansion：新研究群体种子（如 holography/UV-NIL）"),
    "M3_CITATION_BRIDGE_GAP": ("citation_bridge_repair",
                               "forward/backward citation traversal 加深 + 第二来源（OpenAlex cited_by）"),
    "M4_QUERY_FORMULATION_GAP": ("evidence_to_query_repair",
                                 "evidence-to-query translation（v2.1.3-2.1.5：per-community Disc + "
                                 "anchored queryability + pilot + greedy merge）"),
    "M5_RANK_DEPTH_GAP": ("depth_adaptive_export",
                          "depth 调整 / adaptive export（目标在结果集内但 rank>depth）"),
    "M6_EXECUTION_IDENTITY_GAP": ("execution_identity_repair",
                                  "执行/identity 修复：retry、导出 parse、EID/DOI 规范化"),
}


def build_repair_plan(classifications: list[dict], audit_round: str | None,
                      miss_count: int | None = None) -> dict:
    """classifications: [{'primary_failure','secondary_failures','confidence',...}]"""
    cnt = Counter(c["primary_failure"] for c in classifications)
    # secondary 也计入触发（说明会连带受益）
    for c in classifications:
        for s in c.get("secondary_failures", []):
            cnt[s] += 0     # 只统计 primary 触发，避免 double-count

    recommended = []
    for m, n in cnt.most_common():
        if n <= 0 or m not in MODULE_MAP:
            continue
        module, desc = MODULE_MAP[m]
        recommended.append({"module": module, "failure_class": m,
                            "triggered_by": n, "description": desc})

    for i, r in enumerate(recommended, 1):
        r["priority"] = i

    return {
        "audit_round": audit_round,
        "repair_mode": "DRY_RUN",
        "repair_version": None,
        "miss_count": miss_count,
        "summary": {m: n for m, n in cnt.most_common() if n > 0},
        "recommended_repairs": recommended,
        "note": "DRY-RUN ONLY：本 plan 不自动执行任何 Repair；执行需下一阶段人工批准",
    }
