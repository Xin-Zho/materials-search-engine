#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""S4 Step 6A：TERM → family-level query actions（2026-08-30 用户定，防 80 词→80 query）。

设计原则：
- 只对 MissSupport>=2 的 eligible 词做 family 聚类（singleton 留 per-miss niche，
  由 community action / 人工复核覆盖——不生成 development-specific query）。
- 聚类：同 term_type 槽内，归一化后共享显著 token（>=5 字符且非噪音）→ 连通分量。
- query 模板（延续 v1.1/v2）：
    PROBLEM 自带 shrink/contraction 词根 → DIRECT 短语
    PROBLEM 替代表达（distortion/debonding）→ DOMAIN anchor AND term
    其他槽 family → PROBLEM anchor AND (OR members)
- 成员词质量过滤：畸形 token（<=3 字符如 'focu'）剔除，用 family 内干净代表词进 OR 组。

输出：s4_query_family_actions.json（actions + covered_miss_wids + 待 pilot 清单）
"""

import json
import os
import re
import sys
from collections import defaultdict

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TERM = os.path.join(BASE, "data", "exports", "terminology")
TERM_EV = os.path.join(TERM, "s4_term_evidence.json")
OUT = os.path.join(TERM, "s4_query_family_actions.json")

PROBLEM_ANCHOR = "(shrinkage OR contraction OR \"volumetric change\" OR deformation)"
DOMAIN_ANCHOR = ("(photopolymer* OR photocur* OR \"light cur*\" OR \"UV cur*\" "
                 "OR polymerization)")
SHRINK_ROOT = ("shrink", "contraction", "shrinkage")

# 显著 token 黑名单（与 S3 gate 同源，防止泛词当聚类锚）
TOKEN_NOISE = {
    "effect", "effects", "influence", "study", "studies", "using", "based", "new",
    "novel", "high", "low", "the", "of", "and", "for", "with", "during", "after",
    "before", "into", "from", "between", "their", "this", "that", "these", "those",
    "measurement", "measuring", "measured", "polymerization", "shrinkage", "resin",
    "composite", "dental", "curing", "light", "cured", "material", "materials",
    "technique", "method", "methods", "analysis", "properties", "property",
    "behavior", "behaviour", "test", "tests", "tested", "results", "result",
}


def norm_term(t: str) -> str:
    x = t.strip().lower().replace("-", " ")
    x = re.sub(r"\s+", " ", x)
    toks = []
    for w in x.split():
        if w.endswith("ies") and len(w) > 4:
            w = w[:-3] + "y"
        elif w.endswith("ses") and len(w) > 4:
            w = w[:-2]
        elif w.endswith("s") and len(w) > 3 and not w.endswith("ss"):
            w = w[:-1]
        toks.append(w)
    return " ".join(toks)


def sig_tokens(t: str) -> set:
    out = set()
    for w in norm_term(t).split():
        if len(w) >= 5 and w not in TOKEN_NOISE:
            out.add(w)
    return out


def clean_tokens_ok(t: str) -> bool:
    """family 成员进 OR 组前检查词形健康（无 <=3 字符畸形 token 如 'focu'）。"""
    for w in norm_term(t).split():
        if len(w) <= 3:
            return False
    return True


def cluster_families(terms: list[dict]) -> list[list[dict]]:
    """同槽内按共享显著 token 做连通分量聚类。"""
    by_slot: dict[str, list[dict]] = defaultdict(list)
    for t in terms:
        by_slot[t["term_type"]].append(t)

    families = []
    for slot, ts in by_slot.items():
        toks_of = {t["term_family"]: sig_tokens(t["term_family"]) for t in ts}
        parent = {i: i for i in range(len(ts))}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for i in range(len(ts)):
            for j in range(i + 1, len(ts)):
                if toks_of[ts[i]["term_family"]] & toks_of[ts[j]["term_family"]]:
                    union(i, j)
        groups: dict[int, list[dict]] = defaultdict(list)
        for i, t in enumerate(ts):
            groups[find(i)].append(t)
        families.extend(sorted(groups.values(), key=lambda g: -len(g)))
    return families


def make_query(family: list[dict]) -> str:
    slot = family[0]["term_type"]
    members = [t["term_family"] for t in family if clean_tokens_ok(t["term_family"])]
    # 质量过滤后若空，退回 slot 内 miss_support 最高的健康词
    if not members:
        healthy = [t["term_family"] for t in family]
        members = healthy[:1] if healthy else []
    if not members:
        return None
    # 多成员 OR 组必须加括号（A OR B AND C 在 Scopus 中语义错误）
    quoted = " OR ".join(f'"{m}"' for m in members)
    if len(members) > 1:
        quoted = f"({quoted})"
    if slot == "PROBLEM":
        if any(any(r in m for r in SHRINK_ROOT) for m in members):
            return f"TITLE-ABS-KEY({quoted})"
        return f"TITLE-ABS-KEY({quoted} AND {DOMAIN_ANCHOR})"
    return f"TITLE-ABS-KEY({quoted} AND {PROBLEM_ANCHOR})"


def main() -> None:
    out_path = os.path.join(sys.argv[sys.argv.index("--out") + 1]
                            if "--out" in sys.argv else OUT)
    t = json.load(open(TERM_EV, encoding="utf-8"))
    eligible = [x for x in t["terms"] if x.get("eligible")]
    # MEASUREMENT 槽整体交 6C community action（dental-measurement 词汇族 + seeds 展开），
    # 不进 6A——方法词 token 聚类失效（linometer/shrinkage vector/cuspal deflection 不共享 token
    # 但属同一语义家族），且 46/49 是 singleton niche，独立 query 会 development-specific。
    strong = [x for x in eligible if x.get("miss_support", 0) >= 2
              and x["term_type"] != "MEASUREMENT"]
    measurement_all = [x for x in eligible if x["term_type"] == "MEASUREMENT"]
    print(f"eligible={len(eligible)} | 非MEASUREMENT sup>=2（进 family）={len(strong)} | "
          f"MEASUREMENT 交 6C={len(measurement_all)}")

    families = cluster_families(strong)
    print(f"families={len(families)}")

    actions = []
    covered_all: set[str] = set()
    for fam in families:
        slot = fam[0]["term_type"]
        q = make_query(fam)
        miss_wids: set[str] = set()
        for m in fam:
            miss_wids |= set(m.get("miss_wids", []))
        covered_all |= miss_wids
        actions.append({
            "action_id": f"QF_{len(actions) + 1:02d}",
            "type": "QUERY_FAMILY",
            "category": slot,
            "members": sorted({m["term_family"] for m in fam}),
            "n_members": len(fam),
            "max_miss_support": max(m.get("miss_support", 0) for m in fam),
            "query_string": q,
            "covered_miss_wids": sorted(miss_wids),
            "covered_count": len(miss_wids),
            "development_source": "AUDIT_R03",
        })

    # singleton eligible（不进 query，标注待 community/人工）
    singles = [x for x in eligible if x.get("miss_support", 0) < 2]
    out = {
        "version": "s4_query_family_actions_v1",
        "frozen_at": "2026-08-30",
        "development_source": "AUDIT_R03",
        "design": "family-level query（防 80 词→80 query）：非 MEASUREMENT 槽 sup>=2 词同槽共享"
                  "显著 token 聚类；MEASUREMENT 槽整体交 6C community action（方法词 token 聚类失效"
                  "+ singleton niche）；singleton 不生成 query（防 development-specific overfit）",
        "n_eligible": len(eligible),
        "n_strong_familied": len(strong),
        "n_families": len(families),
        "union_covered_misses": len(covered_all),
        "measurement_deferred_to_6C": {
            "n": len(measurement_all),
            "sup2_words": sorted({x["term_family"] for x in measurement_all
                                  if x.get("miss_support", 0) >= 2}),
            "singleton_eligible": len([x for x in measurement_all
                                       if x.get("miss_support", 0) < 2]),
            "covered_miss_wids": sorted({w for x in measurement_all
                                         for w in x.get("miss_wids", [])}),
            "note": "MEASUREMENT 词汇族 + dental seeds 展开并入 6C COMM_DENTAL_MEASUREMENT action",
        },
        "singleton_eligible_note": f"{len(singles)} singleton eligible（MEASUREMENT niche）"
                                   "留 community/人工复核，不单独生成 query",
        "actions": actions,
        "singletons": sorted({x["term_family"] for x in singles}),
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    def safe(s, n=78):
        return (s or "").encode("ascii", "replace").decode("ascii")[:n]

    print("=" * 78)
    print("S4 Step 6A query family actions")
    print("=" * 78)
    print(f"union covered misses = {len(covered_all)} / 37")
    print(f"{'id':<8}{'cat':<12}{'n':>2}{'sup':>4}{'cov':>4}  query")
    for a in actions:
        print(f"{a['action_id']:<8}{a['category']:<12}{a['n_members']:>2}"
              f"{a['max_miss_support']:>4}{a['covered_count']:>4}  {safe(a['query_string'])}")
    print(f"\n[OK] written: {out_path}")


if __name__ == "__main__":
    main()
