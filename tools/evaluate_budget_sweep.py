"""tools/evaluate_budget_sweep.py — v2.0.1 Family Budget Sweep（用户 2026-08-28 拍板）。

问题：F3b FAMILY_BUDGET_GAP 实锤（depth=1000 raw 61.9% → retained K=200 28.4%），
第二瓶颈 = family retention budget K=200 太小。本脚本量化"K 提到多少才够"。

方法（用户定稿，纯离线，不访问 Scopus）：
  固定：depth = 500（默认；v2.0 暂定默认导出深度，1000 留作 diagnostic）
        queries / 50 families = frozen
  只改：K ∈ {200, 300, 500, 1000, ∞}
  重放：per family 按 rank 升序取前 K 个唯一 EID（family_budget_retained 同款语义）
  输出：RR_retained(K) 曲线 + retained unique papers + pipeline rows +
        QGS retained + F3b dropped + dropped 的 best_rank 分布 → 找 knee point

⚠️ 严禁 benchmark leakage：K 分配只允许用 Agent 可观察信号
（unique_before_cap / truncation_rate / duplicate_rate / MFC / total_hits），
绝不允许用 QGS 命中密度。本脚本的 QGS 命中只用于**分析**，不参与任何分配。

用法：
  python tools/evaluate_budget_sweep.py
  python tools/evaluate_budget_sweep.py --depths 500,1000 --k-grid 200,300,500,1000,0
"""
import argparse
import json
import os
import re
from collections import Counter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

RUNS_DEPTH_PATH = os.path.join(BASE, "data", "exports", "query_family_runs_depth.json")
BENCH_PATH = os.path.join(BASE, "data", "exports", "pc_001_external_qgs_v1.json")
OUT_PATH = os.path.join(BASE, "data", "exports", "budget_sweep_results.json")


def norm_eid(e: str) -> str:
    return (e or "").strip()


def norm_wid(pid: str) -> str:
    m = re.search(r"(W\d+)", pid or "")
    return m.group(1) if m else ""


def load_bench() -> dict:
    """134 IN_SCOPUS 篇 + scopus_eid 全集（budget sweep 只需 EID 匹配）。"""
    bench = json.load(open(BENCH_PATH, encoding="utf-8"))
    b_scopus = [p for p in bench["papers"] if p.get("scopus_eligibility") == "IN_SCOPUS"]
    return {"b_scopus": b_scopus, "denom": len(b_scopus)}


def load_depth_records(path: str) -> dict:
    return json.load(open(path, encoding="utf-8"))["records"]


def raw_union(records: dict, d: int) -> set[str]:
    """∪_q Top_d(q)：无 budget 时所有 query 能捞到的唯一 EID。"""
    out: set[str] = set()
    for recs in records.values():
        for r in recs:
            if r["rank"] <= d:
                eid = norm_eid(r["eid"])
                if eid:
                    out.add(eid)
    return out


def budget_retained_stats(records: dict, d: int, K: int):
    """per family 按 rank 升序取前 K 唯一 EID（K=0 → 无 cap）。

    返回 (unique_union, pipeline_rows)：
      unique_union  = ∪_f seen_f（跨 family 去重后的论文数）
      pipeline_rows = ∑_f |seen_f|（per-family 保留量之和，未跨 family 去重，
                      衡量下游逐 family 处理负载）
    """
    fam_pool: dict[str, list[dict]] = {}
    for qid, recs in records.items():
        if not recs:
            continue
        fid = recs[0]["family_id"]
        for r in recs:
            if r["rank"] <= d:
                fam_pool.setdefault(fid, []).append(r)
    union: set[str] = set()
    pipeline = 0
    for fid, rs in fam_pool.items():
        seen: set[str] = set()
        for r in sorted(rs, key=lambda x: x["rank"]):
            eid = norm_eid(r["eid"])
            if not eid or eid in seen:
                continue
            seen.add(eid)
            if K and len(seen) >= K:
                break
        union |= seen
        pipeline += len(seen)
    return union, pipeline


def best_rank_of(records: dict) -> dict[str, int]:
    """eid -> 所有 query 中的最小 rank。"""
    out: dict[str, int] = {}
    for recs in records.values():
        for r in recs:
            eid = norm_eid(r["eid"])
            if not eid:
                continue
            cur = out.get(eid)
            if cur is None or r["rank"] < cur:
                out[eid] = r["rank"]
    return out


def rank_bucket(rank: int) -> str:
    if rank <= 100:
        return "<=100"
    if rank <= 200:
        return "101-200"
    if rank <= 500:
        return "201-500"
    if rank <= 1000:
        return "501-1000"
    return ">1000"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--depth-file", default=RUNS_DEPTH_PATH)
    ap.add_argument("--depths", default="500,1000",
                    help="固定 depth（逗号分隔；数据是 depth=1000 全量，rank<=d 即模拟）")
    ap.add_argument("--k-grid", default="200,300,500,1000,0",
                    help="family budget 扫描（逗号分隔；0 = no cap）")
    args = ap.parse_args()

    depths = [int(x) for x in args.depths.split(",")]
    k_grid = [int(x) for x in args.k_grid.split(",")]

    bench = load_bench()
    denom = bench["denom"]                      # 134
    bench_eids = {norm_eid(p.get("scopus_eid")) for p in bench["b_scopus"]}
    records = load_depth_records(args.depth_file)
    best_rank = best_rank_of(records)

    results = []
    print("=" * 78)
    print("Family Budget Sweep（纯离线重放，queries/families frozen）")
    print("=" * 78)
    for d in depths:
        raw_hits = raw_union(records, d) & bench_eids
        rr_raw = len(raw_hits) / denom
        print(f"\n── depth = {d}（raw RR = {len(raw_hits)}/{denom} = {rr_raw*100:.1f}%）──")
        print(f"  {'K':>6} | {'retained unique':>15} | {'pipeline rows':>13} | "
              f"{'QGS retained':>12} | {'RR_retained':>10} | {'F3b dropped':>11} | {'Δvs_raw':>7}")
        for K in k_grid:
            union, pipeline = budget_retained_stats(records, d, K)
            qgs_ret = union & bench_eids
            f3b = raw_hits - qgs_ret            # raw 搜到但被 K 截掉
            rr_t = len(qgs_ret) / denom
            delta = (rr_t - rr_raw) * 100
            k_label = "no-cap" if K == 0 else str(K)
            print(f"  {k_label:>6} | {len(union):>15} | {pipeline:>13} | "
                  f"{len(qgs_ret):>3}/{denom}={rr_t*100:5.1f}% | "
                  f"{len(f3b):>11} | {delta:+.1f}pp")
            if K != 0 and d == depths[0]:
                bucket = Counter(rank_bucket(best_rank.get(e, 9999)) for e in f3b)
                results.append({
                    "depth": d, "K": K, "retained_unique": len(union),
                    "pipeline_rows": pipeline, "qgs_retained": len(qgs_ret),
                    "rr_retained": round(rr_t, 6), "f3b_dropped": len(f3b),
                    "delta_pp": round(delta, 2),
                    "f3b_rank_distribution": dict(bucket)})
        # no-cap 行也存
        union, pipeline = budget_retained_stats(records, d, 0)
        qgs_ret = union & bench_eids
        results.append({
            "depth": d, "K": 0, "retained_unique": len(union),
            "pipeline_rows": pipeline, "qgs_retained": len(qgs_ret),
            "rr_retained": round(len(qgs_ret) / denom, 6),
            "f3b_dropped": 0, "delta_pp": 0.0,
            "f3b_rank_distribution": {}})

        # F3b dropped 的 best_rank 分布（depth 固定时，K=200 对照行）
        union200, _ = budget_retained_stats(records, d, 200)
        f3b200 = raw_hits - (union200 & bench_eids)
        if f3b200:
            bucket = Counter(rank_bucket(best_rank.get(e, 9999)) for e in f3b200)
            print(f"  → K=200 时 F3b dropped best_rank 分布: "
                  f"{dict(sorted(bucket.items(), key=lambda x: x[0]))}")

    # knee 提示（只看第一个 depth）
    d0 = depths[0]
    raw0 = len(raw_union(records, d0) & bench_eids)
    print("\n── knee 提示（depth=%d, 相对 raw=%d 的恢复率）──" % (d0, raw0))
    for K in [k for k in k_grid if k != 0]:
        union, _ = budget_retained_stats(records, d0, K)
        q = len(union & bench_eids)
        rec = q / raw0 * 100
        print(f"  K={K:>5}: 恢复 raw 的 {rec:5.1f}% ({q}/{raw0})"
              + ("  ← 接近饱和" if rec >= 95 else ""))

    out = {"created_at": "2026-08-28", "denom": denom, "depths": depths,
           "k_grid": k_grid, "note": "K 分配严禁参考 QGS 命中密度（benchmark leakage）",
           "results": results}
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n✓ 已写: {OUT_PATH}")


if __name__ == "__main__":
    main()
