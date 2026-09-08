#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""S4 Step 6E：组装 22 条 query pilot 输入（2026-08-30 用户拍板）。

= 20 family queries（s4_query_family_actions.json）
+ 2 dental-measurement community queries（s4_community_action.json）
全部 type=QUERY_FAMILY，depth=1000，带 quality-gate 元数据字段：
  family_support（词族 miss 覆盖数）/ community_support（社区成员数）/ source

输出：s4_query_pilot_queries.json（供 run_s3_query_pilot --actions 使用）
"""

import json
import os
import sys

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TERM = os.path.join(BASE, "data", "exports", "terminology")
FAM = os.path.join(TERM, "s4_query_family_actions.json")
COMM = os.path.join(TERM, "s4_community_action.json")
OUT = os.path.join(TERM, "s4_query_pilot_queries.json")


def main() -> None:
    out_path = os.path.join(sys.argv[sys.argv.index("--out") + 1]
                            if "--out" in sys.argv else OUT)
    fam = json.load(open(FAM, encoding="utf-8"))
    comm = json.load(open(COMM, encoding="utf-8"))

    actions = []
    for a in fam["actions"]:
        actions.append({
            "action_id": a["action_id"],
            "type": "QUERY_FAMILY",
            "category": a["category"],
            "query_string": a["query_string"],
            "members": a["members"],
            "family_support": a["covered_count"],
            "community_support": None,
            "source": "6A_term_family",
        })
    # 6C 两条 community query
    for gkey, gname in (("query_group_1_dental_linear", "Q1_dental_linear"),
                        ("query_group_2_imaging", "Q2_imaging")):
        g = comm["action"][gkey]
        actions.append({
            "action_id": f"CM_{gname}",
            "type": "QUERY_FAMILY",
            "category": "MEASUREMENT",
            "query_string": g["query_string"],
            "members": g["terms"],
            "family_support": None,
            "community_support": len(comm["members"]),
            "source": "6C_community_dental-measurement",
        })

    out = {
        "version": "s4_query_pilot_queries_v1",
        "frozen_at": "2026-08-30",
        "development_source": "AUDIT_R03",
        "policy": "query quality-gate（无 miss-specific set cover）；depth=1000 冻结",
        "n_queries": len(actions),
        "composition": {"6A_family": len(fam["actions"]), "6C_community": 2},
        "actions": actions,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print("=" * 78)
    print("S4 query pilot input（22 queries）")
    print("=" * 78)
    for a in actions:
        src = "6A" if a["source"] == "6A_term_family" else "6C"
        print(f"  {a['action_id']:<10}[{src}] {a['query_string'][:66]}")
    print(f"\n[OK] written: {out_path}")


if __name__ == "__main__":
    main()
