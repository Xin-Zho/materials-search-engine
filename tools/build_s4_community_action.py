#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""S4 Step 6C：STRONG community action（2026-08-30 用户定）。

只为 C_dental-measurement（唯一 STRONG 社区）生成 community-level action：
  = MEASUREMENT 词汇族 query（6A deferred 的 49 词，拆 2 组：dental 线性/体积测量 +
    imaging/registration）+ dental 代表 seeds 1-hop 展开（P3 同逻辑）+ community-specific
    search entry。

与 6A TERM action 的互补：6A 覆盖非 MEASUREMENT 槽；6C 覆盖 MEASUREMENT 词汇族——
两通道合起来才是 dental-measurement 社区完整入口。

输出：s4_community_action.json
"""

import json
import os
import sys
from collections import defaultdict

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TERM = os.path.join(BASE, "data", "exports", "terminology")
TERM_EV = os.path.join(TERM, "s4_term_evidence.json")
COMM = os.path.join(TERM, "s4_community_discovery.json")
OUT = os.path.join(TERM, "s4_community_action.json")

sys.path.insert(0, os.path.join(BASE, "tools"))
from citation_reachability_audit import load_oa  # noqa: E402

PROBLEM_ANCHOR = "(shrinkage OR contraction OR \"volumetric change\" OR deformation)"

# MEASUREMENT 词族分组（语义，非 token——方法词 token 聚类失效）
MGROUP_DENTAL_LINEAR = [
    "linometer", "custom made linometer", "measured using linometer", "linometer for 90",
    "bonded disc", "bonded disc technique", "cuspal deflection", "custom made cuspal deflection",
    "cuspal deflection measuring", "cuspal deflection effect", "greater cuspal deflection",
    "deflection", "deflection measuring", "deflection measuring machine", "deflection effect",
    "deflection in composite", "deflection of teeth", "cuspal strain", "measuring cuspal strain",
    "cuspal strain material", "10 strain gage", "strain gage", "strain field",
    "video imaging", "video imaging device", "volumetric shrinkage analyzer",
    "density method", "specific density method", "swelling", "shrinkage and swelling",
    "measuring microscope", "measuring microscope biolux",
]
MGROUP_IMAGING = [
    "shrinkage vector", "shrinkage vector field", "polymerization shrinkage vector",
    "elastic registration", "elastic registration algorithm", "block matching",
    "registration block matching", "digital image correlation",
    "x ray ct", "x ray ct image", "micro focu x ray", "micro focu x ray ct",
]


def main() -> None:
    out_path = os.path.join(sys.argv[sys.argv.index("--out") + 1]
                            if "--out" in sys.argv else OUT)
    te = json.load(open(TERM_EV, encoding="utf-8"))
    comm = json.load(open(COMM, encoding="utf-8"))
    dental = next((c for c in comm["communities"]
                   if c["community_id"] == "C_dental-measurement"), None)
    if not dental:
        print("[FATAL] C_dental-measurement 未找到")
        sys.exit(1)
    members = dental["members"]
    # MEASUREMENT eligible 词全集（49）
    meas = [x["term_family"] for x in te["terms"]
            if x.get("eligible") and x["term_type"] == "MEASUREMENT"]
    meas_set = set(meas)

    # 分组 query（只含实际存在于 eligible 的词；剔除含数字的 n-gram 碎片如 'linometer for 90'）
    import re as _re
    _num = lambda w: bool(_re.search(r"\d", w))
    g1 = [w for w in MGROUP_DENTAL_LINEAR if w in meas_set and not _num(w)]
    g2 = [w for w in MGROUP_IMAGING if w in meas_set and not _num(w)]
    leftover = sorted(meas_set - set(g1) - set(g2))
    q1 = (f"TITLE-ABS-KEY((" + " OR ".join(f'"{w}"' for w in g1)
          + f") AND {PROBLEM_ANCHOR})")
    q2 = (f"TITLE-ABS-KEY((" + " OR ".join(f'"{w}"' for w in g2)
          + f") AND {PROBLEM_ANCHOR})")

    # dental 代表 seeds（P3 同逻辑：cluster 内 cited_by 降序 top-3）
    oa = load_oa()
    dent_seeds = sorted(members, key=lambda w: -(oa.get(w, {}).get("cited_by_count") or 0))[:3]
    seed_info = []
    for w in dent_seeds:
        m = oa.get(w, {})
        seed_info.append({"seed_wid": w, "title": m.get("title"),
                          "cited_by": m.get("cited_by_count"),
                          "refs": len(m.get("referenced_works", []))})

    # covered miss（MEASUREMENT 词覆盖的 miss union）
    cov_wids = set()
    for x in te["terms"]:
        if x.get("eligible") and x["term_type"] == "MEASUREMENT":
            cov_wids |= set(x.get("miss_wids", []))
    cov_wids -= {"W7110794929"}

    out = {
        "version": "s4_community_action_v1",
        "frozen_at": "2026-08-30",
        "development_source": "AUDIT_R03",
        "community_id": "C_dental-measurement",
        "verdict": "STRONG（Step 3：cohesion MED / vocab STRONG / undercoverage 93.5%）",
        "members": members,
        "action": {
            "type": "COMMUNITY",
            "components": [
                {"part": "vocabulary", "note": "MEASUREMENT 词汇族 query（6A deferred 的 49 词，"
                                               "拆 dental 线性/体积测量 + imaging/registration 两组）"},
                {"part": "citation", "note": "dental 代表 seeds 1-hop 展开（cited_by 降序 top-3，"
                                             "P3 同逻辑）"},
                {"part": "search_entry", "note": "community-specific entry = PROBLEM anchor × "
                                                 "MEASUREMENT 词汇，非泛化 query"},
            ],
            "query_group_1_dental_linear": {"n_terms": len(g1), "query_string": q1,
                                            "terms": g1},
            "query_group_2_imaging": {"n_terms": len(g2), "query_string": q2,
                                      "terms": g2},
            "leftover_terms": leftover,
            "representative_seeds": seed_info,
            "covered_miss_wids": sorted(cov_wids),
            "covered_count": len(cov_wids),
        },
        "complementarity": "6A 覆盖非 MEASUREMENT 槽（20 family queries）；6C 覆盖 MEASUREMENT "
                           "词汇族——两通道构成 dental-measurement 社区完整入口",
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    def safe(s, n=80):
        return (s or "").encode("ascii", "replace").decode("ascii")[:n]

    print("=" * 78)
    print("S4 Step 6C community action（C_dental-measurement）")
    print("=" * 78)
    print(f"members={len(members)} | MEASUREMENT 词={len(meas)} "
          f"（g1 {len(g1)} + g2 {len(g2)} + leftover {len(leftover)}）")
    print(f"covered_misses={len(cov_wids)}")
    print(f"Q1 {safe(q1)}")
    print(f"Q2 {safe(q2)}")
    print("representative seeds:")
    for s in seed_info:
        print(f"  {s['seed_wid']} cited={s['cited_by']} refs={s['refs']} | {safe(s['title'], 60)}")
    print(f"\n[OK] written: {out_path}")


if __name__ == "__main__":
    main()
