"""tools/run_s3_query_pilot.py — S3 QUERY repair pilot（2026-08-29 用户定）。

对 96 个 QUERY repair actions 统一 depth=1000 pilot（先测实际效果，不加入 S3）。
每 action 记录：
  query / total_hits / raw / unique / new_vs_S2 / residual_recovered / recovered_miss_ids
Efficiency_q = ResidualRecovered / NewCandidates

residual 判定：residual miss 是 OpenAlex WID；query 结果（Scopus）经 DOI/title 桥匹配。

输出：data/exports/terminology/s3_query_pilot_results.json

用法：
  python tools/run_s3_query_pilot.py --plan-only
  python tools/run_s3_query_pilot.py [--depth 1000]
  python tools/run_s3_query_pilot.py --offline-cache
"""
import argparse
import asyncio
import json
import os
import sys
import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tools"))

from build_r02_seen import _norm_doi, _norm_title  # noqa: E402

DEFAULT_ACTIONS = os.path.join(BASE, "data", "exports", "terminology",
                               "s3_repair_actions.json")
DEFAULT_MISSES = os.path.join(BASE, "data", "exports", "terminology",
                              "s3_residual_misses.json")
DEFAULT_OUT = os.path.join(BASE, "data", "exports", "terminology",
                           "s4_query_pilot_results.json")
S2_SEEN = os.path.join(BASE, "data", "exports", "terminology", "s2_seen_set.json")
DEFAULT_DEPTH = 1000
# S4 canonical 口径（2026-08-30 用户冻结：37 canonical，raw 38 含 W7110794929 duplicate）
DUP_WID = "W7110794929"


def main():
    ap = argparse.ArgumentParser(description="S3 QUERY repair pilot（depth=1000）")
    ap.add_argument("--actions", default=DEFAULT_ACTIONS)
    ap.add_argument("--misses", default=DEFAULT_MISSES)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--depth", type=int, default=DEFAULT_DEPTH)
    ap.add_argument("--offline-cache", action="store_true")
    ap.add_argument("--engine-data-dir", default="data")
    ap.add_argument("--plan-only", action="store_true")
    ap.add_argument("--new-basis", default="s2", choices=["s2", "s3", "s4"],
                    help="new 判定基准：s2（build_s2_found_sets）| s3（build_s3_found_sets）| "
                         "s4（build_s4_found_sets，canonical S4_SEEN_SET=19194，S5 pilot 用）")
    args = ap.parse_args()

    acts = json.load(open(args.actions, encoding="utf-8"))
    q_acts = [a for a in acts["actions"]
              if a["type"] in ("QUERY", "QUERY_V2", "QUERY_FAMILY")]
    print(f"QUERY actions = {len(q_acts)}")

    misses = json.load(open(args.misses, encoding="utf-8"))["misses"]

    def _wid(r):
        """兼容两种 miss schema：s3/s4 用 wid；r04_misses 用 paper_id（=OpenAlex WID）。"""
        return r.get("wid") or r.get("paper_id")

    res_by_doi = {_norm_doi(r.get("doi")): _wid(r) for r in misses if r.get("doi")}
    res_by_title = {}
    for r in misses:
        t = _norm_title(r.get("oa_title") or r.get("title"))
        if t:
            res_by_title[t] = _wid(r)

    # new 判定 found sets（三通道 eids/dois/titles——与 seen join 同口径，比 key 集合更准）
    if args.new_basis == "s4":
        from build_r04_seen import build_s4_found_sets
        found = build_s4_found_sets()
        s4_seen = json.load(open(os.path.join(
            BASE, "data", "exports", "terminology", "s4_seen_set.json"),
            encoding="utf-8"))
        n_s4 = s4_seen.get("size", len(s4_seen.get("keys", [])))
        print(f"[new-basis] s4（canonical S4_SEEN_SET；identity reconciliation 后）")
        print(f"[new-basis-size] {n_s4}")
        assert n_s4 == 19194, \
            f"[FATAL] S4_SEEN_SET size={n_s4} ≠ 19194——可能误用 nominal 19445 或 S3 13430，禁止继续"
    elif args.new_basis == "s3":
        from build_r03_seen import build_s3_found_sets
        found = build_s3_found_sets()
    else:
        from build_residual_misses import build_s2_found_sets
        found = build_s2_found_sets()
    found_eids = found["eids"]
    found_dois = found["dois"]
    found_titles = found["titles"]

    if args.plan_only:
        print(f"[plan-only] 将 pilot {len(q_acts)} 个 QUERY actions × depth={args.depth}：")
        for a in q_acts[:10]:
            cov = len(a.get("covered_miss_ids") or a.get("covered_miss_wids") or [])
            print(f"  {a['action_id']} cov={cov:>2} "
                  f"-> {a['query_string'][:60]}")
        print(f"  ... 共 {len(q_acts)} 个（不加入 S4，先测实际效果）")
        return

    # ── 检索 ──
    from pilot_round3_query_utility import (IdentityResolver, connect_cache_ro,
                                            paper_key_and_info)
    resolver = IdentityResolver()
    # 预注册 S2 seen 的 key 判定：in_S2 用 canonical ∈ s2_keys

    def run_offline() -> None:
        con = connect_cache_ro(os.path.join(BASE, "data", "cache", "scopus_cache.db"))
        rows = {q: j for q, j in con.execute("SELECT query_string, result_json FROM api_cache")}
        con.close()
        from search_engine.cache import SearchCache
        for a in q_acts:
            cached = rows.get(a["query_string"])
            if cached is None:
                a["error"] = "NO_CACHE"
                print(f"  MISSING-CACHE {a['action_id']} {a['query_string'][:40]}")
                continue
            measure(a, SearchCache._deserialize_result(cached))

    async def run_engine() -> None:
        from search_engine.engine import ScopusSearchEngine
        engine = ScopusSearchEngine(data_dir=args.engine_data_dir)
        await engine.start()
        try:
            for a in q_acts:
                try:
                    res = await engine.search(a["query_string"], limit=args.depth,
                                              skip_cache=True)
                except Exception as e:
                    print(f"    [WARN] {a['action_id']}: {e}")
                    a["error"] = str(e)
                    await asyncio.sleep(1)
                    continue
                measure(a, res)
                await asyncio.sleep(1)
        finally:
            await engine.close()

    def measure(a: dict, res) -> None:
        from pilot_round3_query_utility import paper_key_and_info
        papers = res.papers
        total_hits = getattr(res, "total_count", len(papers))
        rows = []
        for p in papers:
            key, info = paper_key_and_info(p, resolver, set())
            rows.append({"key": key, "eid": info["eid"], "doi": info["doi"],
                         "title": (getattr(p, "title", None) or "").strip()})
        unique = {r["key"] for r in rows if r["key"]}
        # new 判定：三通道 found sets（eid/doi/title）
        new_keys = set()
        for r in rows:
            k = r["key"]
            if not k:
                continue
            already = (k in found_eids or
                       (r["doi"] and r["doi"] in found_dois) or
                       (r["title"] and _norm_title(r["title"]) in found_titles))
            if not already:
                new_keys.add(k)
        # residual 命中
        recovered = []
        for r in rows:
            wid = res_by_doi.get(r["doi"]) if r["doi"] else None
            if not wid and r["title"]:
                wid = res_by_title.get(_norm_title(r["title"]))
            if wid and wid not in recovered:
                recovered.append(wid)
        dup_rate = 1 - (len(unique) / len(rows)) if rows else None
        saturated = len(rows) >= args.depth and (total_hits or 0) > args.depth
        a.update({
            "total_hits": total_hits,
            "raw": len(rows),
            "unique": len(unique),
            f"new_vs_{args.new_basis.upper()}": len(new_keys),
            "residual_recovered": len(recovered),
            "recovered_miss_ids": sorted(recovered),
            "efficiency": round(len(recovered) / len(new_keys), 5) if new_keys else None,
            "duplicate_rate": round(dup_rate, 4) if dup_rate is not None else None,
            "saturation_at_1000": saturated,
            "candidate_cost": len(new_keys),
            "family_support": a.get("family_support"),
            "community_support": a.get("community_support"),
        })
        print(f"  {a['action_id']} hits={total_hits:>6} raw={len(rows):>4} "
              f"new={len(new_keys):>4} rec={len(recovered):>2} {a['query_string'][:46]}")

    if args.offline_cache:
        run_offline()
    else:
        asyncio.run(run_engine())

    # ── 汇总 ──
    n_rec_total = len(set().union(*[set(a.get("recovered_miss_ids", []))
                                    for a in q_acts])) if q_acts else 0
    n_rec_canon = len({w for w in
                       (set().union(*[set(a.get("recovered_miss_ids", []))
                                      for a in q_acts]) if q_acts else set())
                       if w != DUP_WID})
    n_canon_miss = len({(_wid(m)) for m in misses if _wid(m) != DUP_WID})
    summary = {
        "n_actions": len(q_acts),
        "depth": args.depth,
        "residual_raw": len(misses),                 # 38（含 W7110794929 duplicate）
        "residual_canonical": n_canon_miss,          # 37（正式 S4 diagnosis 口径）
        "actions_with_recovery": sum(1 for a in q_acts if a.get("residual_recovered")),
        "union_recovered_raw": n_rec_total,
        "union_recovered_canonical": n_rec_canon,
        "union_recovery_rate_raw": round(n_rec_total / len(misses), 4) if misses else None,
        "union_recovery_rate_canonical": (round(n_rec_canon / n_canon_miss, 4)
                                          if n_canon_miss else None),
        "total_new_candidates_undeduped": sum(a.get("candidate_cost", 0)
                                              for a in q_acts),
        "saturated_at_1000": sum(1 for a in q_acts if a.get("saturation_at_1000")),
        "quality_gate_note": "S4 纪律：pilot 后只做 quality gate（语义退化/候选爆炸/重复），"
                             "不做 miss-specific set cover；R03_recovery 仅 dev diagnostic",
    }
    out = {
        "version": {"s2": "s3_query_pilot_v1", "s3": "s4_query_pilot_v1",
                    "s4": "s5_query_pilot_v1"}[args.new_basis],
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "depth": args.depth,
        "development_source": {"s2": "AUDIT_R02", "s3": "AUDIT_R03",
                               "s4": "AUDIT_R04"}[args.new_basis],
        "new_basis": args.new_basis,
        "note": "pilot 结果——Efficiency = ResidualRecovered/NewCandidates（dev diagnostic）；"
                "quality gate 后进 S4（无 miss-specific set cover）",
        "summary": summary,
        "actions": q_acts,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n=== QUERY pilot 汇总（new-basis={args.new_basis}）===")
    for k, v in summary.items():
        print(f"  {k:<28} {v}")
    print(f"\n[OK] {args.out}")


if __name__ == "__main__":
    main()
