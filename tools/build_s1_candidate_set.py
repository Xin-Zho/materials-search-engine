"""tools/build_s1_candidate_set.py — S1 Candidate Set 三层冻结（2026-08-29 用户定稿）。

S1_CORE（16 冻结） / S1_REWRITE_REPILOT（3） / S1_RESERVE（2）/ S1_DROP（4）。
另生成：s1_core_union_keys.json（S1_CORE 16 条检索结果的 NCG keys 并集，相对 S0）
——作为 3 条 rewrite query 的 MCG_after_core 基准（S0 ∪ S1_CORE）。

Queryability v1.1 规则（用户冻结）：
  DIRECT phrase 只有在自身包含 shrinkage/problem semantics 时才允许裸搜；
  否则 BroadDomainTerm -> Anchor AND Term。

输出：data/exports/terminology/s1_candidate_set.json
      data/exports/terminology/s1_core_union_keys.json
      data/exports/terminology/s1_rewrite_pilot_queries.json
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tools"))

OUT_DIR = os.path.join(BASE, "data", "exports", "terminology")

S1_CORE = ["acrylate", "composite resin", "volume shrinkage", "photocuring",
           "methacrylate", "low shrinkage", "photopolymer",
           "pressure sensitive adhesives", "thiol ene", "meth acrylates",
           "shrinkage during photopolymerization", "curing shrinkage",
           "light curing", "lithography", "modified calcium sulfate",
           "laser beam scanning"]
S1_REWRITE = ["vat photopolymerization", "synthesis and photopolymerization",
              "cationic photopolymerization"]
S1_DROP = ["stereolithography", "stress", "physico mechanical properties",
           "solvent free"]
S1_RESERVE = ["dental resins", "spiroorthocarbonate"]


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=OUT_DIR)
    args = ap.parse_args()
    out_dir = args.out_dir

    from pilot_round3_query_utility import (  # noqa: E402
        build_r_old, connect_cache_ro, paper_key_and_info,
    )
    from search_engine.cache import SearchCache  # noqa: E402

    pilot = json.load(open(os.path.join(out_dir, "pilot_results.json"),
                           encoding="utf-8"))
    by_term = {q["canonical_term"]: q for q in pilot["queries"]}

    # ── S1 candidate set（三层）──
    def _q(term):
        q0 = by_term[term]
        return {"canonical_term": term, "family_id": q0.get("family_id"),
                "query_mode": q0.get("query_mode"),
                "query": q0["query_string"],
                "pilot": {"MCG": q0.get("MCG"), "total_hits": q0.get("total_hits"),
                          "depth_saturated": q0.get("depth_saturated")}}

    core = [_q(t) for t in S1_CORE]
    rewrite = [{"canonical_term": t, "query": f'TITLE-ABS-KEY(shrinkage AND "{t}")',
                "rewrite_from": by_term[t]["query_string"],
                "pilot_original": {"MCG": by_term[t].get("MCG"),
                                   "total_hits": by_term[t].get("total_hits")}}
               for t in S1_REWRITE]
    drop = [_q(t) for t in S1_DROP]
    reserve = [{"canonical_term": "dental resins",
                "reason": "TotalHits=383 MCG=1 dup=98%——即使 1 篇 relevant 也无 marginal "
                          "value；DROP/RESERVE 待判"},
               {"canonical_term": "spiroorthocarbonate",
                "reason": "MECHANISM_RESCUE_RESERVE：surface form/query formulation 无价值≠"
                          "SOC 机制方向无价值；后续试 spiro-orthocarbonate / spiro "
                          "orthocarbonate / SOC / expanding monomer"}]

    s1 = {
        "version": "s1_candidate_set_v1",
        "frozen_at": "2026-08-29",
        "development_source": "AUDIT_R01",
        "search_baseline": "S0",
        "eligible_for_r01_evaluation": False,
        "queryability_version": "v1.1",
        "queryability_v11_rule": ("DIRECT phrase 只有在自身包含 shrinkage/problem "
                                  "semantics 时才允许裸搜；否则 BroadDomainTerm -> "
                                  "Anchor AND Term（Pilot 证据：ANCHOR yield 98.7% vs "
                                  "DIRECT 49.6%）"),
        "S1_CORE": {"count": len(core), "queries": core},
        "S1_REWRITE_REPILOT": {"count": len(rewrite), "queries": rewrite},
        "S1_DROP": {"count": len(drop), "queries": drop},
        "S1_RESERVE": {"count": len(reserve), "queries": reserve},
    }
    with open(os.path.join(out_dir, "s1_candidate_set.json"), "w",
              encoding="utf-8") as f:
        json.dump(s1, f, ensure_ascii=False, indent=1)

    # ── S1_CORE union NCG keys（offline 重放 16 条 core，相对 S0）──
    resolver, r_old = build_r_old()
    con = connect_cache_ro(os.path.join(BASE, "data", "cache", "scopus_cache.db"))
    cache = {q: j for q, j in con.execute(
        "SELECT query_string, result_json FROM api_cache")}
    con.close()
    core_union: set[str] = set()
    for term in S1_CORE:
        q = by_term[term]["query_string"]
        cached = cache.get(q)
        if cached is None:
            print(f"  [WARN] NO_CACHE {term}")
            continue
        res = SearchCache._deserialize_result(cached)
        for p in res.papers:
            key, info = paper_key_and_info(p, resolver, r_old)
            if key and not info["in_old"]:
                core_union.add(key)
    with open(os.path.join(out_dir, "s1_core_union_keys.json"), "w",
              encoding="utf-8") as f:
        json.dump(sorted(core_union), f, ensure_ascii=False, indent=0)

    # ── rewrite pilot queries（3 条，covered baseline = S0 ∪ S1_CORE）──
    rew_qs = [{"order": i + 1, "canonical_term": r["canonical_term"],
               "query_mode": "REWRITE_ANCHOR",
               "query_string": r["query"],
               "rewrite_from": r["rewrite_from"],
               "pilot_original": r["pilot_original"]}
              for i, r in enumerate(rewrite)]
    with open(os.path.join(out_dir, "s1_rewrite_pilot_queries.json"), "w",
              encoding="utf-8") as f:
        json.dump({"version": "s1_rewrite_pilot", "depth": 50,
                   "development_source": "AUDIT_R01", "search_baseline": "S0",
                   "covered_baseline": "S0 ∪ S1_CORE(16)",
                   "queries": rew_qs}, f, ensure_ascii=False, indent=1)

    print(f"S1_CORE={len(core)} | REWRITE={len(rewrite)} | DROP={len(drop)} | "
          f"RESERVE={len(reserve)}")
    print(f"S1_CORE union NCG keys = {len(core_union)}（相对 S0）")
    print(f"[OK] 输出: {out_dir}")


if __name__ == "__main__":
    main()
