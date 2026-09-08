"""tools/run_s3_citation_pilot.py — S3 CITATION repair pilot（2026-08-29 用户定）。

对 45 个 CITATION repair actions 真正展开 1-hop：
  candidates = seed.referenced_works（BACKWARD）∪ {w : seed ∈ w.referenced_works}（FORWARD）
每 action 记录：
  seed_wid / direction / citation_candidates / new_vs_S2 / residual_recovered / recovered_miss_ids
Efficiency_c = ResidualRecovered / NewCandidates

new_vs_S2 判定：candidate 的 DOI ∉ S2 seen dois（WID → openalex DOI 桥）。

输出：data/exports/terminology/s3_citation_pilot_results.json

用法：
  python tools/run_s3_citation_pilot.py [--plan-only]
"""
import argparse
import json
import os
import sys
import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "tools"))

from build_r02_seen import _norm_doi  # noqa: E402

DEFAULT_ACTIONS = os.path.join(BASE, "data", "exports", "terminology",
                               "s3_repair_actions.json")
DEFAULT_MISSES = os.path.join(BASE, "data", "exports", "terminology",
                              "s3_residual_misses.json")
DEFAULT_OUT = os.path.join(BASE, "data", "exports", "terminology",
                           "s3_citation_pilot_results.json")
S2_SEEN = os.path.join(BASE, "data", "exports", "terminology", "s2_seen_set.json")
S2_RAW = os.path.join(BASE, "data", "exports", "terminology", "s2_raw_records.json")
OA_CACHE = os.path.join(BASE, "data", "cache", "openalex_cache.json")


def load_oa() -> dict:
    """{wid: {doi, title, referenced_works}}。"""
    meta = {}
    cache = json.load(open(OA_CACHE, encoding="utf-8"))
    for q, resp in cache.items():
        for w in resp.get("results", []):
            wid = (w.get("id") or "").replace("https://openalex.org/", "")
            if wid and wid not in meta:
                meta[wid] = {
                    "doi": (w.get("doi") or "").lower().replace("https://doi.org/", ""),
                    "title": w.get("title"),
                    "referenced_works": [r.replace("https://openalex.org/", "")
                                         for r in (w.get("referenced_works") or [])],
                }
    return meta


def s2_dois() -> set[str]:
    dois = set()
    seen = json.load(open(S2_SEEN, encoding="utf-8"))["keys"]
    for k in seen:
        k = k.strip().lower()
        if not k.startswith("2-s2.0-") and k:
            dois.add(k)
    raw = json.load(open(S2_RAW, encoding="utf-8"))
    for q, recs in raw.get("records_by_query", {}).items():
        for r in recs:
            if r.get("doi"):
                dois.add(str(r["doi"]).strip().lower())
    return dois


def main():
    ap = argparse.ArgumentParser(description="S3 CITATION repair pilot（1-hop 展开）")
    ap.add_argument("--actions", default=DEFAULT_ACTIONS)
    ap.add_argument("--misses", default=DEFAULT_MISSES)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--plan-only", action="store_true")
    args = ap.parse_args()

    acts = json.load(open(args.actions, encoding="utf-8"))
    c_acts = [a for a in acts["actions"] if a["type"] == "CITATION"]
    print(f"CITATION actions = {len(c_acts)}")

    misses = json.load(open(args.misses, encoding="utf-8"))["misses"]
    miss_wids = {r["wid"] for r in misses}
    oa = load_oa()
    seen_d = s2_dois()

    if args.plan_only:
        print(f"[plan-only] 将展开 {len(c_acts)} 个 CITATION actions（1-hop）：")
        for a in c_acts[:8]:
            print(f"  {a['action_id']} seed={a['source'][5:]} cov={len(a['covered_miss_ids']):>2}")
        return

    # forward 索引：w 引用了 seed（全部 works → 引用者）
    # 对每个 action 的 seed，candidates = backward(seed.refs) ∪ forward(引用 seed 的 works)
    n_rec_total = set()
    total_new = 0
    for a in c_acts:
        seed = a["source"][5:]
        m = oa.get(seed, {})
        backward = list(m.get("referenced_works", []))
        forward = [w for w, wm in oa.items() if seed in wm.get("referenced_works", [])]
        cand = list(dict.fromkeys(backward + forward))
        # new_vs_S2（DOI 桥）+ residual 命中
        new_cnt = 0
        recovered = []
        for wid in cand:
            doi = oa.get(wid, {}).get("doi", "")
            if doi and doi not in seen_d:
                new_cnt += 1
            if wid in miss_wids:
                recovered.append(wid)
        n_rec_total.update(recovered)
        total_new += new_cnt
        a.update({
            "seed_wid": seed,
            "direction": a.get("direction"),
            "citation_candidates": len(cand),
            "backward_count": len(backward),
            "forward_count": len(forward),
            "new_vs_S2": new_cnt,
            "residual_recovered": len(recovered),
            "recovered_miss_ids": sorted(recovered),
            "efficiency": round(len(recovered) / new_cnt, 5) if new_cnt else None,
        })
        print(f"  {a['action_id']} seed={seed} cand={len(cand):>4} new={new_cnt:>4} "
              f"rec={len(recovered):>2}")

    n = len(misses)
    summary = {
        "n_actions": len(c_acts),
        "residual_total": n,
        "actions_with_recovery": sum(1 for a in c_acts if a.get("residual_recovered")),
        "union_residual_recovered": len(n_rec_total),
        "union_recovery_rate": round(len(n_rec_total) / n, 4) if n else None,
        "total_new_candidates_undeduped": total_new,
    }
    out = {
        "version": "s3_citation_pilot_v1", "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "development_source": "AUDIT_R02",
        "note": "1-hop expansion（backward=seed.refs ∪ forward=引用 seed 的 works）；"
                "forward 依赖 openalex cache 覆盖度；Efficiency = ResidualRecovered/NewCandidates",
        "summary": summary,
        "actions": c_acts,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n=== S3 CITATION pilot 汇总 ===")
    for k, v in summary.items():
        print(f"  {k:<28} {v}")
    print(f"\n[OK] {args.out}")


if __name__ == "__main__":
    main()
