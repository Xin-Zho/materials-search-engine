"""tools/adjudicate_pilot_relevance.py — Pilot MCG 文献的 Relevance layer（2026-08-29）。

对 516 篇 MCG 文献（R01-derived development，非独立审计）做主题相关性三态判定，
得到 RMCG / UMCG / RelevantYield，并按四维（AuditEvidence, RMCG, RelevantYield,
TotalHits）分类诊断 DIRECT vs ANCHOR。

主题词表（冻结，recall-first——只有明确无关才 IRRELEVANT）：
  PROBLEM（强）: shrinkage, shrink, contraction, "shrinkage stress",
                 "contraction stress", "polymerization stress", "curing stress"
  DOMAIN（弱） : polymerization, photopolymerization, photopolymer, photocuring,
                 "light curing", photocurable, "composite resin", "resin composite",
                 dental, restorative, monomer, resin, adhesive

判定规则（v1，审计性优先）：
  title 含 PROBLEM 词            -> RELEVANT
  title 无 + abstract 含 PROBLEM -> RELEVANT
  title 含 DOMAIN 词 >=2         -> UNCERTAIN
  其他                            -> IRRELEVANT

输入：pilot_results.json（queries[].mcg_papers，含 key/title/abstract）
输出：data/exports/terminology/pilot_relevance.json
      data/exports/terminology/pilot_relevance.csv
"""
import argparse
import csv
import json
import math
import os
import re

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_IN = os.path.join(BASE, "data", "exports", "terminology", "pilot_results.json")
DEFAULT_OUT = os.path.join(BASE, "data", "exports", "terminology",
                           "pilot_relevance.json")
DEFAULT_CSV = os.path.join(BASE, "data", "exports", "terminology",
                           "pilot_relevance.csv")

PROBLEM_TERMS = ["shrinkage", "shrink", "contraction", "shrinkage stress",
                 "contraction stress", "polymerization stress", "curing stress"]
DOMAIN_TERMS = ["polymerization", "photopolymerization", "photopolymer",
                "photocuring", "light curing", "photocurable", "composite resin",
                "resin composite", "dental", "restorative", "monomer", "resin",
                "adhesive"]


def _has_any(text: str, terms: list[str]) -> bool:
    t = (text or "").lower()
    return any(re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", t)
               for w in terms)


def theme_label(title: str, abstract: str) -> str:
    """主题三态（recall-first）。"""
    if _has_any(title, PROBLEM_TERMS):
        return "RELEVANT"
    if _has_any(abstract, PROBLEM_TERMS):
        return "RELEVANT"
    # DOMAIN 词计数（title 为主）
    dom = sum(1 for w in DOMAIN_TERMS
              if re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])",
                           (title or "").lower()))
    if dom >= 2:
        return "UNCERTAIN"
    return "IRRELEVANT"


def classify(row: dict) -> str:
    """四维分类（诊断标记；阈值不预设，供用户看分布后调整）。"""
    mcg = row.get("MCG") or 0
    rmc = row.get("RMCG") or 0
    yield_ = row.get("rel_yield") or 0.0
    hits = row.get("total_hits") or 0
    if mcg <= 0:
        return "REDUNDANT"
    if rmc == 0 or (hits >= 500 and yield_ < 0.3):
        return "NOISY_EXPLOSIVE"      # yield=0 即 noisy（无论 hits）
    if yield_ >= 0.5 and hits <= 100:
        return "PRECISION_REPAIR"
    return "BROAD_DISCOVERY"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=DEFAULT_IN)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--csv", default=DEFAULT_CSV)
    args = ap.parse_args()

    d = json.load(open(args.json, encoding="utf-8"))
    queries = d["queries"]

    rows = []
    n_rel = n_unc = n_irr = 0
    for q in queries:
        papers = q.get("mcg_papers", [])
        r = 0
        u = 0
        for p in papers:
            lab = theme_label(p.get("title"), p.get("abstract"))
            if lab == "RELEVANT":
                r += 1
            elif lab == "UNCERTAIN":
                u += 1
        mcg = q.get("MCG") or 0
        rows.append({
            "order": q.get("order"),
            "canonical_term": q.get("canonical_term"),
            "query_mode": q.get("query_mode"),
            "query_string": q.get("query_string"),
            "audit_miss_gain": q.get("audit_miss_gain"),
            "MCG": mcg,
            "RMCG": r,
            "UMCG": u,
            "rel_yield": round(r / mcg, 4) if mcg else 0.0,
            "total_hits": q.get("total_hits"),
            "depth_saturated": q.get("depth_saturated"),
            "efficiency_mcg": q.get("efficiency_mcg"),
            "category": classify({
                "MCG": mcg, "RMCG": r, "rel_yield": (r / mcg if mcg else 0.0),
                "total_hits": q.get("total_hits")}),
        })
        n_rel += r
        n_unc += u
        n_irr += mcg - r - u

    rows.sort(key=lambda x: (-x["RMCG"], -x["rel_yield"]))

    print(f"MCG 文献 {sum(len(q.get('mcg_papers', [])) for q in queries)} 篇："
          f"RELEVANT {n_rel} | UNCERTAIN {n_unc} | IRRELEVANT {n_irr}")
    print(f"{'term':<38} {'mode':<7} {'MCG':>4} {'RMCG':>4} {'UNC':>4} "
          f"{'yield':>6} {'hits':>7} {'cat':<17}")
    for r in rows:
        print(f"{r['canonical_term'][:38]:<38} {r['query_mode']:<7} {r['MCG']:>4} "
              f"{r['RMCG']:>4} {r['UMCG']:>4} {r['rel_yield']:>6.1%} "
              f"{r['total_hits']:>7} {r['category']:<17}")

    # DIRECT vs ANCHOR
    print("\n=== DIRECT vs ANCHOR ===")
    for mode in ["DIRECT", "ANCHOR"]:
        sub = [r for r in rows if r["query_mode"] == mode]
        if not sub:
            continue
        m = sum(r["MCG"] for r in sub)
        rm = sum(r["RMCG"] for r in sub)
        h = sum(r["total_hits"] or 0 for r in sub)
        print(f"  {mode:<7} n={len(sub):>2} | ΣMCG={m:>4} ΣRMCG={rm:>4} "
              f"overall_yield={rm/m:>6.1%} | median_yield="
              f"{sorted(r['rel_yield'] for r in sub)[len(sub)//2]:.1%} | "
              f"ΣTotalHits={h}")

    # 四维分类汇总
    from collections import Counter
    print("\n=== 四维分类 ===")
    print(dict(Counter(r["category"] for r in rows)))

    out = {
        "version": "pilot_relevance_v1",
        "development_source": "AUDIT_R01",
        "search_baseline": "S0",
        "eligible_for_r01_evaluation": False,
        "note": "主题相关性三态（recall-first）；PROBLEM 词=shrinkage/shrink/contraction/"
                "shrinkage stress 等；DOMAIN 词=光固化/牙科域词；非独立审计（R01-derived dev）",
        "total": {"MCG": sum(r["MCG"] for r in rows),
                  "RMCG": n_rel, "UMCG": n_unc, "IRRELEVANT": n_irr,
                  "overall_yield": round(n_rel / sum(r["MCG"] for r in rows), 4)},
        "rows": rows,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    with open(args.csv, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["order", "canonical_term", "query_mode", "query_string",
                    "audit_miss_gain", "MCG", "RMCG", "UMCG", "rel_yield",
                    "total_hits", "depth_saturated", "efficiency_mcg", "category"])
        for r in rows:
            w.writerow([r["order"], r["canonical_term"], r["query_mode"],
                        r["query_string"], r["audit_miss_gain"], r["MCG"], r["RMCG"],
                        r["UMCG"], r["rel_yield"], r["total_hits"],
                        r["depth_saturated"], r["efficiency_mcg"], r["category"]])
    print(f"\n[OK] JSON: {args.out}")
    print(f"[OK] CSV : {args.csv}")


if __name__ == "__main__":
    main()
