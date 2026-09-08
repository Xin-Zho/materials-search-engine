"""tools/probe_s2_depth.py — S3 第一步：depth 极限诊断（2026-08-29 用户定）。

目标：回答"70 个 residual miss 还有多少只是因为 depth=1000 太浅"。
对 S2 后仍被截断的 4 条 query（composite resin / volume shrinkage / methacrylate /
low shrinkage——总 hits 均 >1000）临时跑 probe_depth（默认 2000 / 可设 full），
看 70 个 residual miss（R02 RELEVANT ∧ Seen_S1=FALSE ∧ Seen_S2=FALSE）中追回多少。

判读（用户定）：
  70 → 68/67（追回 1-3）    → depth 基本到头，正式 depth=1000 可冻结
  追回 10+                    → 考虑提高正式 depth

版本纪律：这是临时诊断（probe），不改任何正式参数；S1/S2 冻结不变。
输出：data/exports/terminology/s3_depth_probe.json（含 per-query 追回明细）

用法：
  python tools/probe_s2_depth.py --residual-misses <s3_residual_misses.json> --plan-only
  python tools/probe_s2_depth.py --residual-misses <s3_residual_misses.json> [--probe-depth 2000]
  python tools/probe_s2_depth.py --offline-cache
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

S2_SNAP = os.path.join(BASE, "data", "exports", "terminology", "s2_candidate_snapshot.json")
DEFAULT_RESIDUAL = os.path.join(BASE, "data", "exports", "terminology",
                                "s3_residual_misses.json")
DEFAULT_OUT = os.path.join(BASE, "data", "exports", "terminology",
                           "s3_depth_probe.json")


def main():
    ap = argparse.ArgumentParser(description="S3 depth 极限诊断（4 条截断 query 临时加深）")
    ap.add_argument("--residual-misses", default=DEFAULT_RESIDUAL)
    ap.add_argument("--probe-depth", type=int, default=2000,
                    help="临时 probe depth（默认 2000；改正式参数前只作诊断）")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--offline-cache", action="store_true")
    ap.add_argument("--engine-data-dir", default="data")
    ap.add_argument("--plan-only", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.residual_misses):
        print(f"[FATAL] residual miss 清单不存在: {args.residual_misses}")
        print("先跑: python tools/build_residual_misses.py --labels <R02 filled.json>")
        sys.exit(2)
    resid = json.load(open(args.residual_misses, encoding="utf-8"))
    misses = resid["misses"]
    print(f"residual miss = {len(misses)}（{resid.get('definition', '')}）")

    # 从 S2 snapshot 找仍截断的 query（depth_saturated at 1000）
    snap = json.load(open(S2_SNAP, encoding="utf-8"))
    sat = [q for q in snap.get("queries", []) if q.get("depth_saturated")]
    print(f"S2 后仍截断的 query = {len(sat)} 条：")
    for q in sat:
        print(f"  [{q['order']:>2}] {q['canonical_term']:<32} hits={q.get('total_hits')}")
    if args.plan_only:
        print(f"\n[plan-only] 将 probe {len(sat)} 条 × depth={args.probe_depth}"
              f"（不改正式参数），判定 {len(misses)} 个 residual miss 追回数。")
        return

    # residual miss 的 identity 通道（WID -> doi/title 已在清单内）
    res_by_doi = {}
    res_by_title = {}
    for r in misses:
        if r.get("doi"):
            res_by_doi[_norm_doi(r["doi"])] = r["wid"]
        if r.get("oa_title") or r.get("title"):
            t = _norm_title(r.get("oa_title") or r.get("title"))
            if t:
                res_by_title[t] = r["wid"]

    def run_offline() -> None:
        import sqlite3
        from pilot_round3_query_utility import connect_cache_ro
        con = connect_cache_ro(os.path.join(BASE, "data", "cache", "scopus_cache.db"))
        rows = {q: j for q, j in con.execute("SELECT query_string, result_json FROM api_cache")}
        con.close()
        from search_engine.cache import SearchCache
        for q in sat:
            cached = rows.get(q["query_string"])
            if cached is None:
                print(f"  MISSING-CACHE {q['canonical_term'][:40]}（probe 需真实检索）")
                continue
            measure(q, SearchCache._deserialize_result(cached))

    async def run_engine() -> None:
        from search_engine.engine import ScopusSearchEngine
        engine = ScopusSearchEngine(data_dir=args.engine_data_dir)
        await engine.start()
        try:
            for q in sat:
                try:
                    res = await engine.search(q["query_string"], limit=args.probe_depth,
                                              skip_cache=True)
                except Exception as e:
                    print(f"    [WARN] {q['canonical_term'][:30]}: probe 失败 {e}")
                    q["error"] = str(e)
                    await asyncio.sleep(1)
                    continue
                measure(q, res)
                await asyncio.sleep(1)
        finally:
            await engine.close()

    def measure(q: dict, res) -> None:
        papers = res.papers
        q["probe_raw"] = len(papers)
        q["probe_recovered"] = []
        for p in papers:
            doi = _norm_doi(getattr(p, "doi", None))
            title = _norm_title(getattr(p, "title", None))
            wid = res_by_doi.get(doi) or res_by_title.get(title)
            if wid and wid not in q["probe_recovered"]:
                q["probe_recovered"].append(wid)
        print(f"  {q['canonical_term'][:36]:<36} raw={len(papers):>4} "
              f"recovered={len(q['probe_recovered']):>2}")

    if args.offline_cache:
        run_offline()
    else:
        asyncio.run(run_engine())

    recovered_all = set()
    per_q = []
    for q in sat:
        rl = q.get("probe_recovered", [])
        recovered_all.update(rl)
        per_q.append({"canonical_term": q["canonical_term"],
                      "query_string": q["query_string"],
                      "probe_depth": args.probe_depth,
                      "raw_returned": q.get("probe_raw"),
                      "recovered_wids": sorted(rl),
                      "recovered_count": len(rl)})
    n_resid = len(misses)
    n_rec = len(recovered_all)
    out = {
        "version": "s3_depth_probe_v1", "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "note": "临时诊断：不改正式参数；S1(depth=500)/S2(depth=1000) 冻结不变",
        "probe_depth": args.probe_depth,
        "residual_total": n_resid,
        "recovered_total": n_rec,
        "recovered_rate": round(n_rec / n_resid, 4) if n_resid else None,
        "remaining": n_resid - n_rec,
        "interpretation": ("depth 基本到头，可冻结 depth=1000" if n_rec <= 3
                           else ("追回可观——考虑提高正式 depth（需 R03 验证）"
                                 if n_rec >= 10 else "追回中等，权衡后决定")),
        "per_query": per_q,
        "recovered_wids": sorted(recovered_all),
        "remaining_wids": sorted(set(r["wid"] for r in misses) - recovered_all),
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n=== depth probe 结果（probe_depth={args.probe_depth}）===")
    print(f"  residual={n_resid} -> recovered={n_rec} -> remaining={n_resid - n_rec}")
    print(f"  判读：{out['interpretation']}")
    print(f"[OK] {args.out}")


if __name__ == "__main__":
    main()
