"""tools/build_s3_query_v2.py — Query Formulation v2（2026-08-29 用户定，pilot 教训）。

pilot 结论：Good Term ≠ Good Query。96 条 v1 只追回 23/70（32.9%），裸搜（contraction
hits=460k / deformation 1.07M / distortion 440k / expansion 1.14M）全部 rec=0——
目标论文被百万级结果淹没，depth=1000 根本碰不到。

v2 规则（用户定）：
  1. TermFamily 去重：同族合并（ring opening/ring-opening、thiol ene/thiol-ene、
     shrinkage stress/shrinkage stresses、filler/fillers、nanocomposite(s)、
     adhesive(s)、restoration(s)）→ 一个 OR query
  2. 按类别 formulation（新术语 + 已有领域语义）：
     PROBLEM（自带 shrink 词根）→ 短语直接搜："shrinkage stress"（v1.1 DIRECT 规则）
     PROBLEM（替代表达）      → "term" AND DOMAIN_ANCHOR
       DOMAIN_ANCHOR = (photopolymer* OR photocur* OR "light cur*" OR "UV cur*" OR polymerization)
       ——contraction 是 shrinkage 替代表达，AND shrinkage 会漏只写 contraction 的论文
     METHOD/REACTION/MATERIAL/CONTEXT → "term" AND PROBLEM_ANCHOR
       PROBLEM_ANCHOR = (shrinkage OR contraction OR "volumetric change" OR deformation)

输出：data/exports/terminology/s3_query_v2_actions.json
  每条：{action_id, type: QUERY_V2, category, family, query_string,
        source_actions, covered_miss_ids, development_source}

用法：
  python tools/build_s3_query_v2.py [--term-gate <s3_term_gate.json>] [--out <path>]
"""
import argparse
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_GATE = os.path.join(BASE, "data", "exports", "terminology", "s3_term_gate.json")
DEFAULT_OUT = os.path.join(BASE, "data", "exports", "terminology",
                           "s3_query_v2_actions.json")

DOMAIN_ANCHOR = '(photopolymer* OR photocur* OR "light cur*" OR "UV cur*" OR polymerization)'
PROBLEM_ANCHOR = '(shrinkage OR contraction OR "volumetric change" OR deformation)'
SHRINK_ROOT = ("shrink", "contraction", "volumetric", "dimensional", "stress",
               "strain", "expansion", "deformation", "distortion", "warpage",
               "marginal", "gap", "crack")


def norm_term(t: str) -> str:
    """TermFamily 归并：小写、去连字符、复数归一（stresses->stress, composites->composite）。"""
    x = t.strip().lower().replace("-", " ")
    x = re.sub(r"\s+", " ", x)
    toks = x.split()
    out = []
    for w in toks:
        if w.endswith("ies") and len(w) > 4:
            w = w[:-3] + "y"                     # studies -> study
        elif w.endswith("ses") and len(w) > 4:
            w = w[:-2]                           # stresses -> stress, classes -> class
        elif w.endswith("s") and len(w) > 3 and not w.endswith("ss"):
            w = w[:-1]                           # composites -> composite
        out.append(w)
    return " ".join(out)


def family_query(members: list[str]) -> str:
    """族内 OR：("a" OR "b")。"""
    if len(members) == 1:
        return f'"{members[0]}"'
    return '(' + ' OR '.join(f'"{m}"' for m in members) + ')'


def main():
    ap = argparse.ArgumentParser(description="S3 Query Formulation v2")
    ap.add_argument("--term-gate", default=DEFAULT_GATE)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    gate = json.load(open(args.term_gate, encoding="utf-8"))
    miss_terms = {p["wid"]: p.get("repairable_terms", {}) for p in gate["per_miss"]}

    # 1) TermFamily 归并（按类别内 norm）
    families = {}   # (category, norm) -> [original terms]
    term_source = {}  # term -> action 覆盖 miss
    for cat in ("PROBLEM", "METHOD", "REACTION", "MATERIAL", "CONTEXT"):
        for term in gate["discriminative_terms"].get(cat, []):
            n = norm_term(term)
            families.setdefault((cat, n), []).append(term)
            term_source[term] = [wid for wid, rep in miss_terms.items()
                                 if term in rep.get(cat, [])]

    # 2) v2 formulation
    actions = []
    for (cat, _n), members in sorted(families.items()):
        quoted = family_query(members)
        if cat == "PROBLEM" and any(s in m for m in members for s in SHRINK_ROOT) \
           and any("shrink" in m for m in members):
            # 自带 shrink 词根 → 短语直接（v1.1 DIRECT 规则）
            q = f'TITLE-ABS-KEY({quoted})'
        elif cat == "PROBLEM":
            # 替代表达 → 领域锚点（不能 AND shrinkage——漏只写 contraction 的）
            q = f'TITLE-ABS-KEY({quoted} AND {DOMAIN_ANCHOR})'
        else:
            # 其他类 → 问题锚点
            q = f'TITLE-ABS-KEY({quoted} AND {PROBLEM_ANCHOR})'
        covered = sorted(set().union(*[set(term_source[m]) for m in members]))
        actions.append({
            "action_id": f"Q2_{len(actions) + 1:03d}",
            "type": "QUERY_V2",
            "category": cat,
            "family": members,
            "query_string": q,
            "covered_miss_ids": covered,
            "development_source": "AUDIT_R02",
        })

    summary = {
        "v1_actions": sum(len(v) for v in
                          {c: [t for t in gate["discriminative_terms"].get(c, [])]
                           for c in ("PROBLEM", "METHOD", "REACTION", "MATERIAL", "CONTEXT")}.values()),
        "v2_family_actions": len(actions),
        "per_category": {c: sum(1 for a in actions if a["category"] == c)
                         for c in ("PROBLEM", "METHOD", "REACTION", "MATERIAL", "CONTEXT")},
        "union_covered_miss": len(set().union(*[set(a["covered_miss_ids"]) for a in actions])) if actions else 0,
    }
    out = {
        "version": "s3_query_v2_v1", "frozen_at": "2026-08-29",
        "development_source": "AUDIT_R02",
        "note": "v2：TermFamily 去重 + 按类别 formulation（PROBLEM 替代表达→领域锚点；"
                "其他→问题锚点）；pilot v1 教训：裸搜百万 hits 无意义",
        "domain_anchor": DOMAIN_ANCHOR,
        "problem_anchor": PROBLEM_ANCHOR,
        "summary": summary,
        "actions": actions,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print(f"=== Query Formulation v2 ===")
    for k, v in summary.items():
        print(f"  {k:<22} {v}")
    print("\n样例：")
    for a in actions[:8]:
        print(f"  {a['action_id']} [{a['category']:<8}] {a['query_string'][:78]}")
    print(f"\n[OK] {args.out}")


if __name__ == "__main__":
    main()
