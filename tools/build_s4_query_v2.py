#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""S4 Query Formulation v2（2026-08-30 用户 quality gate 判定，冻结输入）。

v1 pilot 教训：22 条中 15 条 saturation、12 条 formulation 过宽——
  泛 Context/Reaction term + (shrinkage/contraction) 仍太宽（recording/conversion/radical/
  calorimetry 跨领域）；CM_Q1/CM_Q2 mega-OR（30+12 词）54k/12k hits。

用户四档判定（冻结，非程序再判）：
  KEEP          QF03 QF11 QF12 QF14 QF19（rec=0 的 QF11/12/14 不删——quality > miss）
  KEEP/REVIEW   QF04 QF07 QF13 QF17（语义明确但 candidate cost 高，待 relevance/overlap QA）
  REDUNDANT     QF15（new=1：marginal novelty≈0，DROP 理由=冗余非质量差）
  REWRITE       QF01 QF02 QF05 QF06 QF08 QF09 QF10 QF16 QF18 QF20 CM_Q1 CM_Q2

v2 formulation 规则（程序化，目标降 SaturationRate / CandidateExpansionRatio）：
  A. 泛词类（conversion/radical/crosslinked/urethane...）→ 用单一强短语 "polymerization shrinkage"
     替代 4 词 problem OR，必要时加 domain/material 锚
  B. 语境词（recording→holography；photoresist→lithography/3DP；restoration/enamel→dental）
     → 加 context 锚
  C. PROBLEM 替代表达（distortion）→ 补 problem 锚（v1 只有 domain 锚）
  D. Community mega-OR → 拆 4 方法学子族（volumetric/imaging/deformation/registration，
     用户指定分组），每族独立 query

优化目标（v2 pilot 验收）：hits<5000、尽量 raw<1000、避免 new/raw>0.9；不做 miss set-cover。

输出：s4_query_v2_actions.json（KEEP+REVIEW 原样 + REWRITE 新 formulation；REDUNDANT 单独记录）
"""

import json
import os
import sys

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TERM = os.path.join(BASE, "data", "exports", "terminology")
PILOT = os.path.join(TERM, "s4_query_pilot_results.json")
PILOT_LEGACY = os.path.join(TERM, "s3_query_pilot_results.json")  # v1 迁移前原文件
OUT = os.path.join(TERM, "s4_query_v2_actions.json")

PROBLEM_OR = "(shrinkage OR contraction OR \"volumetric change\" OR deformation)"
PSHRINK = "\"polymerization shrinkage\""
DOMAIN_ANCHOR = ("(photopolymer* OR photocur* OR \"light cur*\" OR \"UV cur*\" "
                 "OR polymerization)")

# ── 用户冻结判定（2026-08-30 16:22）──
KEEP = {"QF_03", "QF_11", "QF_12", "QF_14", "QF_19"}
REVIEW = {"QF_04", "QF_07", "QF_13", "QF_17"}
REDUNDANT = {"QF_15"}
REWRITE = {"QF_01", "QF_02", "QF_05", "QF_06", "QF_08", "QF_09", "QF_10",
           "QF_16", "QF_18", "QF_20", "CM_Q1_dental_linear", "CM_Q2_imaging"}

# ── v2 rewrite 规则表（action_id -> 新 formulation）──
REWRITE_RULES = {
    "QF_01": {  # 去裸 radical（跨领域），只留短语成员
        "members": ["radical photopolymerization", "free radical photopolymerization"],
        "query": ("TITLE-ABS-KEY((\"radical photopolymerization\" OR "
                  "\"free radical photopolymerization\") AND (shrinkage OR contraction))"),
        "rule": "A: 去裸 radical，只保留短语成员"},
    "QF_02": {
        "members": ["conversion"],
        "query": "TITLE-ABS-KEY(\"conversion\" AND \"polymerization shrinkage\")",
        "rule": "A: 单一强 problem 短语替代 4 词 OR"},
    "QF_05": {
        "members": ["crosslinked"],
        "query": ("TITLE-ABS-KEY(\"crosslinked\" AND \"polymerization shrinkage\" "
                  "AND (photopolymer* OR resin OR composite))"),
        "rule": "A+B: 强短语 + material 锚"},
    "QF_06": {
        "members": ["restoration"],
        "query": ("TITLE-ABS-KEY(\"restoration\" AND (shrinkage OR contraction) "
                  "AND (dental OR composite OR resin))"),
        "rule": "B: dental 语境锚"},
    "QF_08": {
        "members": ["recording"],
        "query": ("TITLE-ABS-KEY(\"recording\" AND (shrinkage OR contraction) "
                  "AND (holographic OR photopolymer OR \"data storage\"))"),
        "rule": "B: holography/data-storage 语境锚"},
    "QF_09": {
        "members": ["enamel"],
        "query": ("TITLE-ABS-KEY(\"enamel\" AND \"polymerization shrinkage\" "
                  "AND (composite OR restoration OR dental))"),
        "rule": "A+B: 强短语 + dental 锚"},
    "QF_10": {
        "members": ["photoresist"],
        "query": ("TITLE-ABS-KEY(\"photoresist\" AND (shrinkage OR contraction) "
                  "AND (lithograph* OR \"3d print*\" OR photopolymer* OR \"UV cur*\"))"),
        "rule": "B: lithography/3DP 语境锚"},
    "QF_16": {
        "members": ["distortion"],
        "query": ("TITLE-ABS-KEY(\"distortion\" AND (shrinkage OR contraction) "
                  "AND (photopolymer* OR photocur* OR \"light cur*\" OR \"UV cur*\" "
                  "OR polymerization))"),
        "rule": "C: 补 problem 锚（v1 只有 domain 锚）"},
    "QF_18": {
        "members": ["urethane"],
        "query": ("TITLE-ABS-KEY(\"urethane\" AND \"polymerization shrinkage\" "
                  "AND (photopolymer* OR methacrylate OR dimethacrylate))"),
        "rule": "A+B: 强短语 + material 锚"},
    "QF_20": {
        "members": ["calorimetry", "differential scanning calorimetry"],
        "query": ("TITLE-ABS-KEY((\"calorimetry\" OR \"differential scanning calorimetry\") "
                  "AND (shrinkage OR contraction) AND (photopolymer* OR polymerization))"),
        "rule": "A: 加 domain 锚；去冗余 scanning calorimetry"},
}

# Community 4 方法学子族（用户指定分组）
CM_SUBFAMILIES = [
    ("CM_V1_volumetric",
     ["linometer", "custom made linometer", "measured using linometer", "bonded disc",
      "bonded disc technique", "volumetric shrinkage analyzer", "density method",
      "specific density method"],
     "dental volumetric measurement（linometer/bonded disc/density）"),
    ("CM_V2_imaging",
     ["x ray ct", "x ray ct image", "micro focu x ray", "micro focu x ray ct",
      "shrinkage vector", "shrinkage vector field", "polymerization shrinkage vector",
      "video imaging", "video imaging device"],
     "dental imaging（µCT/shrinkage vector/video）"),
    ("CM_V3_deformation",
     ["cuspal deflection", "custom made cuspal deflection", "cuspal deflection measuring",
      "cuspal deflection effect", "greater cuspal deflection", "cuspal strain",
      "measuring cuspal strain", "cuspal strain material", "strain gage", "strain field"],
     "dental deformation（cuspal deflection/strain）"),
    ("CM_V4_registration",
     ["elastic registration", "elastic registration algorithm", "block matching",
      "registration block matching", "digital image correlation"],
     "image registration（elastic registration/block matching/DIC）"),
]


def ensure_pilot_artifact() -> str:
    """S4 artifact 名规范：若 s4_query_pilot_results.json 不存在，从 s3 版迁移（双口径 summary）。"""
    if os.path.exists(PILOT):
        return PILOT
    if not os.path.exists(PILOT_LEGACY):
        raise FileNotFoundError(f"pilot 结果缺失: {PILOT} 与 {PILOT_LEGACY}")
    d = json.load(open(PILOT_LEGACY, encoding="utf-8"))
    acts = d["actions"]
    rec_raw = set()
    for a in acts:
        rec_raw |= set(a.get("recovered_miss_ids", []))
    rec_canon = {w for w in rec_raw if w != "W7110794929"}
    d["version"] = "s4_query_pilot_v1_canonicalized"
    d["summary"] = {
        "n_actions": d["summary"].get("n_actions", len(acts)),
        "depth": d["summary"].get("depth"),
        "residual_raw": 38, "residual_canonical": 37,
        "actions_with_recovery": sum(1 for a in acts if a.get("residual_recovered")),
        "union_recovered_raw": len(rec_raw),
        "union_recovered_canonical": len(rec_canon),
        "union_recovery_rate_raw": round(len(rec_raw) / 38, 4),
        "union_recovery_rate_canonical": round(len(rec_canon) / 37, 4),
        "total_new_candidates_undeduped": sum(a.get("candidate_cost", 0) for a in acts),
        "saturated_at_1000": sum(1 for a in acts if a.get("saturation_at_1000")),
        "quality_gate_note": "S4 纪律：quality gate（语义退化/候选爆炸/重复），不做 miss-specific set cover",
    }
    with open(PILOT, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    print(f"[migrate] {PILOT_LEGACY} -> {PILOT}（双口径 summary，不重跑检索）")
    return PILOT


def main() -> None:
    out_path = os.path.join(sys.argv[sys.argv.index("--out") + 1]
                            if "--out" in sys.argv else OUT)
    pilot = json.load(open(ensure_pilot_artifact(), encoding="utf-8"))
    acts = {a["action_id"]: a for a in pilot["actions"]}

    # 校验用户判定覆盖全部 22 条
    all_ids = set(acts.keys())
    judged = KEEP | REVIEW | REDUNDANT | REWRITE
    assert judged == all_ids, f"判定集不完整: {judged ^ all_ids}"
    assert KEEP & REVIEW & REDUNDANT & REWRITE == set(), "判定档重叠"

    v2_actions = []
    redundant_log = []
    for aid in sorted(all_ids):
        a = acts[aid]
        if aid in KEEP:
            v2_actions.append({**a, "verdict": "KEEP"})
        elif aid in REVIEW:
            v2_actions.append({**a, "verdict": "KEEP/REVIEW"})
        elif aid in REDUNDANT:
            redundant_log.append({
                "action_id": aid, "verdict": "REDUNDANT",
                "query_string": a["query_string"],
                "new_vs_S3": a.get("new_vs_S3"), "raw": a.get("raw"),
                "reason": "marginal novelty≈0（new=1，999/1000 已在 S3）——DROP 理由=冗余非质量差",
                "note": "query 本身可能是好 query，只是已被现有 Search 完全覆盖",
            })
        else:  # REWRITE（CM 两条由 CM_SUBFAMILIES 拆分，不在此处）
            if aid.startswith("CM_"):
                continue
            rule = REWRITE_RULES[aid]
            v2_actions.append({
                "action_id": f"{aid}_V2", "type": "QUERY_FAMILY",
                "category": a.get("category"), "query_string": rule["query"],
                "members": rule["members"], "family_support": a.get("family_support"),
                "community_support": a.get("community_support"),
                "source": a.get("source"), "verdict": "REWRITE",
                "rewrite_from": aid, "v2_rule": rule["rule"],
                "v1_pilot": {"total_hits": a.get("total_hits"), "raw": a.get("raw"),
                             "new_vs_S3": a.get("new_vs_S3"),
                             "saturation_at_1000": a.get("saturation_at_1000")},
            })
    # Community 4 子族（替代 CM_Q1/CM_Q2 两个 mega-OR）
    for fid, members, note in CM_SUBFAMILIES:
        quoted = " OR ".join(f'"{m}"' for m in members)
        v2_actions.append({
            "action_id": fid, "type": "QUERY_FAMILY", "category": "MEASUREMENT",
            "query_string": f"TITLE-ABS-KEY(({quoted}) AND {PROBLEM_OR})",
            "members": members, "family_support": None, "community_support": 14,
            "source": "6C_community_dental-measurement", "verdict": "REWRITE",
            "rewrite_from": "CM_Q1_dental_linear/CM_Q2_imaging", "v2_rule": note,
            "v1_pilot": {"note": "mega-OR 拆分（v1: CM_Q1 hits=54735 / CM_Q2 hits=12574）"},
        })

    out = {
        "version": "s4_query_v2_actions_v1",
        "frozen_at": "2026-08-30",
        "development_source": "AUDIT_R03",
        "policy": "Query Formulation v2——quality gate 四档（用户冻结判定）；只重写 REWRITE；"
                  "不做 miss set-cover",
        "quality_gate": {
            "KEEP": sorted(KEEP), "KEEP_REVIEW": sorted(REVIEW),
            "REDUNDANT": sorted(REDUNDANT), "REWRITE": sorted(REWRITE),
            "rules": [
                "A. 泛词→单一强短语 'polymerization shrinkage' 替代 4 词 problem OR（必要时加锚）",
                "B. 语境词→context 锚（recording→holography；photoresist→lithography/3DP；"
                "restoration/enamel→dental）",
                "C. PROBLEM 替代表达→补 problem 锚",
                "D. Community mega-OR→4 方法学子族（volumetric/imaging/deformation/registration）",
            ],
        },
        "v2_acceptance": {"hits_lt": 5000, "raw_target": "<1000",
                          "avoid_new_raw_gt": 0.9,
                          "note": "R03_recovery 仅诊断项，不是筛选目标"},
        "redundant_dropped": redundant_log,
        "n_actions_v2": len(v2_actions),
        "actions": v2_actions,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    def safe(s, n=66):
        return (s or "").encode("ascii", "replace").decode("ascii")[:n]

    print("=" * 84)
    print("S4 Query Formulation v2")
    print("=" * 84)
    print(f"v2 actions = {len(v2_actions)}（KEEP 5 + REVIEW 4 + REWRITE 12→12+4子族；"
          f"REDUNDANT {len(redundant_log)} 已剔除）")
    print(f"{'id':<22}{'verdict':<12}  query")
    for a in v2_actions:
        print(f"{a['action_id']:<22}{a['verdict']:<12}  {safe(a['query_string'])}")
    print(f"\n[OK] written: {out_path}")


if __name__ == "__main__":
    main()
