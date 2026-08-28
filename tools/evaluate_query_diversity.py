"""tools/evaluate_query_diversity.py — v2.0.1 Depth Diagnosis 评估（用户 2026-08-28 定稿）。

输入：query_family_runs_depth.json（run_query_families.py --depth 1000 的 raw 全量，
每条含 rank/eid/doi/title/year/venue/leakage）。

输出（用户要求）：
  A. QUERY DIVERSITY     AC / FC / MUC / MFC（拆分，修 2026-08-28 bug）
  B. RETRIEVAL REACH     unique papers / venues / years / PRE_2006（metadata 已补）
  C. RECALL CURVES       曲线 A RR_raw(d)：∪_q Top_d(q)∩QGS/134（无 budget）
                         曲线 B RR_retained(d,K=200)：过 family budget 后
                         判据（用户冻结）：ΔRR(100→500/1000)<3pp 非主瓶颈；
                         3-10pp 有影响；≥10pp 严重 export-depth bottleneck
                         CLEAN-only vs FULL 双曲线（架构 vs QGS-learned 区分）
                         RR_PRE2006^retrieval（v1 最大盲区是否被穿透）
  D. QGS PROVENANCE      134 篇每篇 {matched?, best_query, best_rank,
                         raw_exported?, family_retained?} → failure taxonomy
                         F2/F1/F3a/F3b/F4/F5/F6 升级版分类

⚠️ rank 语义：Scopus export 行序（relevance 排序近似）；best_rank=该论文在
所有 query 中的最小 rank。

用法：
  python tools/evaluate_query_diversity.py [--depth-file ...] [--K 200]
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from search_engine.query.family_scheduler import (anchor_concentration,
                                                  family_coverage)

REGISTRY_PATH = os.path.join(BASE, "data", "query_registry_v2.json")
RUNS_DEPTH_PATH = os.path.join(BASE, "data", "exports", "query_family_runs_depth.json")
BENCH_PATH = os.path.join(BASE, "data", "exports", "pc_001_external_qgs_v1.json")
KB_PATH = os.path.join(BASE, "data", "cache", "knowledge_base.db")
DEPTH_GRID = (100, 200, 500, 1000)


def norm_wid(pid: str) -> str:
    m = re.search(r"(W\d+)", pid or "")
    return m.group(1) if m else ""


def norm_eid(e: str) -> str:
    return (e or "").strip()


def era_of(y) -> str:
    try:
        y = int(y)
    except (TypeError, ValueError):
        return "UNKNOWN"
    return "PRE_2006" if y <= 2005 else ("2006_2020" if y <= 2020 else "POST_2020")


def load_bench() -> dict:
    bench = json.load(open(BENCH_PATH, encoding="utf-8"))
    b_scopus = [p for p in bench["papers"] if p.get("scopus_eligibility") == "IN_SCOPUS"]
    # openalex_id 文件缺失——从 pool 反查（v1 通道需要）
    pool_path = os.path.join(BASE, "data", "exports", "qgs_candidates_v1.json")
    if os.path.exists(pool_path):
        pool = json.load(open(pool_path, encoding="utf-8"))["candidates"]
        pbd = {}
        for c in pool:
            d = (c.get("doi") or "").strip().lower().replace("https://doi.org/", "")
            if d:
                pbd[d] = c
        for p in b_scopus:
            if not p.get("openalex_id"):
                c = pbd.get((p.get("doi") or "").strip().lower().replace("https://doi.org/", ""))
                if c and c.get("openalex_id"):
                    p["openalex_id"] = c["openalex_id"]
    return {"bench": bench, "b_scopus": b_scopus, "denom": len(b_scopus)}


def load_v1_retrieval() -> set[str]:
    wids = set()
    for f in ("discovery_paper_provenance.json", "discovery_staging.json"):
        p = os.path.join(BASE, "data", "exports", f)
        if not os.path.exists(p):
            continue
        data = json.load(open(p, encoding="utf-8"))
        items = data if isinstance(data, list) else data.get("papers", [])
        for it in items:
            w = norm_wid(it.get("paper_id", "") or it.get("openalex_id", ""))
            if w:
                wids.add(w)
    p = os.path.join(BASE, "data", "exports", "phase2_candidates.json")
    if os.path.exists(p):
        for c in json.load(open(p, encoding="utf-8")).get("candidates", []):
            for sp in (c.get("source_papers") or []):
                w = norm_wid(sp)
                if w:
                    wids.add(w)
    if os.path.exists(KB_PATH):
        con = sqlite3.connect(f"file:{KB_PATH}?mode=ro", uri=True)
        try:
            for (pid,) in con.execute("SELECT paper_id FROM knowledge_records"):
                w = norm_wid(pid)
                if w:
                    wids.add(w)
        finally:
            con.close()
    return wids


def load_depth_records(path: str) -> dict:
    data = json.load(open(path, encoding="utf-8"))
    return data["records"]


def family_budget_retained(records: dict, d: int, K: int,
                           clean_only: bool = False) -> set[str]:
    """曲线 B：per family 按 rank 顺序取前 K 唯一（预算截断重放）。"""
    fam_pool: dict[str, list[dict]] = {}
    for qid, recs in records.items():
        if not recs:
            continue
        if clean_only and recs[0].get("leakage"):
            continue
        fid = recs[0]["family_id"]
        for r in recs:
            if r["rank"] <= d:
                fam_pool.setdefault(fid, []).append(r)
    retained = set()
    for fid, rs in fam_pool.items():
        seen = set()
        n = 0
        for r in sorted(rs, key=lambda x: x["rank"]):
            eid = norm_eid(r["eid"])
            if not eid or eid in seen:
                continue
            seen.add(eid)
            n += 1
            if n > K:
                break
        retained |= seen
    return retained


def raw_union(records: dict, d: int, clean_only: bool = False) -> set[str]:
    """曲线 A：∪_q Top_d(q)（无 budget）。"""
    out = set()
    for qid, recs in records.items():
        if not recs:
            continue
        if clean_only and recs[0].get("leakage"):
            continue
        for r in recs:
            if r["rank"] <= d:
                eid = norm_eid(r["eid"])
                if eid:
                    out.add(eid)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--depth-file", default=RUNS_DEPTH_PATH)
    ap.add_argument("--K", type=int, default=200, help="family budget（冻结 K_f=200）")
    ap.add_argument("--max-depth", type=int, default=1000,
                    help="run 的导出深度（曲线算到 min(max_depth, DEPTH_GRID 上限)）")
    args = ap.parse_args()

    registry = json.load(open(REGISTRY_PATH, encoding="utf-8"))
    families = registry["families"]
    q_by_fam = {f["family_id"]: f["generated_queries"] for f in families}
    ac = anchor_concentration(q_by_fam)
    fc = family_coverage(families, {f["family_id"] for f in families})

    bench = load_bench()
    b_scopus = bench["b_scopus"]
    denom = bench["denom"]
    bench_eids = {norm_eid(p.get("scopus_eid")) for p in b_scopus}
    idx_by_eid = {norm_eid(p.get("scopus_eid")): p for p in b_scopus}
    v1_footprint = load_v1_retrieval()
    bench_wids = {norm_wid(p.get("openalex_id", "")) for p in b_scopus}
    v1_hit = v1_footprint & bench_wids

    records = load_depth_records(args.depth_file)
    n_records = sum(len(v) for v in records.values())
    print("=" * 74)
    print("v2.0.1 Depth-only Retrieval Diagnosis（用户 2026-08-28 口径）")
    print("=" * 74)

    # ── A. QUERY DIVERSITY ──
    print("\n[A] QUERY DIVERSITY")
    print(f"  AC  = {ac['ac']:.4f}  (v1≈1.0, 目标<0.4"
          + (" ✅" if ac['ac'] < 0.4 else " ⚠️") + ")")
    print(f"  FC  = {fc}")
    # MUC / MFC 拆分（修复 2026-08-28：两者混叫）
    owner: dict[str, list[str]] = {}
    for qid, recs in records.items():
        if not recs:
            continue
        fid = recs[0]["family_id"]
        for r in recs:
            if r["rank"] <= args.max_depth:
                owner.setdefault(norm_eid(r["eid"]), []).append(fid)
    muc: dict[str, int] = {}
    for pid, fams in owner.items():
        if len(fams) == 1:
            muc[fams[0]] = muc.get(fams[0], 0) + 1
    ret_n = Counter(fid for fams in owner.values() for fid in set(fams))
    print(f"  MUC = family 独有 paper 数；median={sorted(muc.values())[len(muc)//2] if muc else 0}, "
          f"独有=0 的 family: {sum(1 for f in families if muc.get(f['family_id'], 0)==0)}/{len(families)}")
    mfc_vals = [muc.get(f["family_id"], 0) / max(ret_n.get(f["family_id"], 0), 1)
                for f in families]
    print(f"  MFC = MUC/|R_f|；median={sorted(mfc_vals)[len(mfc_vals)//2]:.3f}")

    # ── B. RETRIEVAL REACH（metadata 已补）──
    all_recs = [r for recs in records.values() for r in recs if r["rank"] <= args.max_depth]
    uniq_eid = {norm_eid(r["eid"]) for r in all_recs}
    venues = {r["venue"] for r in all_recs if r.get("venue")}
    years = [r["year"] for r in all_recs if r.get("year") and str(r["year"]).isdigit()]
    years_i = [int(y) for y in years]
    print("\n[B] RETRIEVAL REACH")
    print(f"  unique papers = {len(uniq_eid)}（v1 footprint = {len(v1_footprint)} W id）")
    print(f"  unique venues = {len(venues)}")
    print(f"  year range    = {min(years_i)}–{max(years_i)}" if years_i else "  year range = N/A")
    print(f"  PRE_2006 retrieved = {sum(1 for y in years_i if y <= 2005)}")
    print(f"  new vs v1: EID↔W 映射未建（下次 run 存双 id 后可算）")

    # ── C. RECALL CURVES ──
    print("\n[C] RECALL CURVES（B_scopus=134，EID exact）")
    grid = [d for d in DEPTH_GRID if d <= args.max_depth] + \
        ([args.max_depth] if args.max_depth > 1000 else [])
    grid = sorted(set(grid))
    print(f"  {'depth':>7} | {'RR_raw':>8} {'Δvs100':>8} | {'RR_ret(K=200)':>13} {'Δvs100':>8} | "
          f"{'CLEAN-only_raw':>14}")
    rr_raw_100 = rr_ret_100 = None
    for d in grid:
        raw_hits = raw_union(records, d) & bench_eids
        ret_hits = family_budget_retained(records, d, args.K) & bench_eids
        clean_hits = raw_union(records, d, clean_only=True) & bench_eids
        rr_r = len(raw_hits) / denom
        rr_t = len(ret_hits) / denom
        if d == 100:
            rr_raw_100, rr_ret_100 = rr_r, rr_t
        d_r = f"+{(rr_r - rr_raw_100)*100:+.1f}pp" if rr_raw_100 else ""
        d_t = f"+{(rr_t - rr_ret_100)*100:+.1f}pp" if rr_ret_100 else ""
        print(f"  {d:>7} | {len(raw_hits):>3}/{denom}={rr_r*100:5.1f}% {d_r:>8} | "
              f"{len(ret_hits):>3}/{denom}={rr_t*100:5.1f}% {d_t:>8} | "
              f"{len(clean_hits)}/{denom}={len(clean_hits)/denom*100:5.1f}%")
    # 判据（用户冻结）
    if rr_raw_100:
        d500 = max([x for x in grid if x >= 500] or [grid[-1]])
        raw500 = len(raw_union(records, d500) & bench_eids) / denom
        delta = (raw500 - rr_raw_100) * 100
        if delta < 3:
            verdict = "ΔRR<3pp → depth 不是主要瓶颈（剩余 gap 是 query coverage）"
        elif delta < 10:
            verdict = "ΔRR 3-10pp → depth 有影响，但不是主导"
        else:
            verdict = "ΔRR≥10pp → 明确存在严重 export-depth bottleneck"
        print(f"\n  判据（100→{d500}）: ΔRR_raw = {delta:+.1f}pp → {verdict}")
    # CLEAN vs FULL 汇总
    full100 = len(raw_union(records, 100) & bench_eids) / denom
    clean100 = len(raw_union(records, 100, clean_only=True) & bench_eids) / denom
    print(f"  100 深度: FULL={full100*100:.1f}% vs CLEAN-only={clean100*100:.1f}%"
          f"（差 = QGS-learned 术语贡献）")
    # PRE_2006
    pre_denom = sum(1 for p in b_scopus if era_of(p.get("year")) == "PRE_2006")
    pre_hits = set()
    for d in grid:
        raw_hits = raw_union(records, d) & bench_eids
        pre = sum(1 for e in raw_hits if era_of(idx_by_eid[e].get("year")) == "PRE_2006")
        if d == grid[-1]:
            pre_hits = raw_hits
    print(f"  RR_PRE2006^retrieval（max depth {grid[-1]}）= {pre} / {pre_denom} = "
          f"{pre / pre_denom * 100:.1f}%（v1 最大盲区穿透检验）")

    # ── D. QGS PROVENANCE + FAILURE TAXONOMY ──
    print("\n[D] QGS PROVENANCE（134 篇逐篇失败分类）")
    # 每篇：best_rank（所有 query 中最小的 rank）、是否 retained
    best_rank: dict[str, dict] = {}     # eid -> {rank, query, family}
    for qid, recs in records.items():
        if not recs:
            continue
        fid = recs[0]["family_id"]
        for r in recs:
            eid = norm_eid(r["eid"])
            if not eid:
                continue
            cur = best_rank.get(eid)
            if cur is None or r["rank"] < cur["rank"]:
                best_rank[eid] = {"rank": r["rank"], "query": qid, "family": fid}
    ret_full = family_budget_retained(records, args.max_depth, args.K)
    tax = Counter()
    rows = []
    for p in b_scopus:
        eid = norm_eid(p.get("scopus_eid"))
        br = best_rank.get(eid)
        year = p.get("year")
        if br is None:
            # 2026-08-28 用户定稿：depth 覆盖到 max_depth 仍未见 → 可能是
            # query 方向缺失，也可能是 rank>max_depth 未观测（F3a）——
            # 统一叫 UNVERIFIED，不妄断方向缺失
            cls = "UNVERIFIED_BEYOND_DEPTH_OR_QUERY_GAP"
            tax[cls] += 1
        elif eid in ret_full:
            cls = "RETRIEVED_OK"
            tax[cls] += 1
        else:
            cls = "F3b_FAMILY_BUDGET_GAP"
            tax[cls] += 1
        rows.append({"idx": p["idx"], "title": p["title"][:60], "year": year,
                     "era": era_of(year), "eid": eid, "best_rank": br["rank"] if br else None,
                     "best_query": br["query"] if br else None,
                     "best_family": br["family"] if br else None,
                     "raw_exported": br is not None,
                     "family_retained": eid in ret_full,
                     "failure_class": cls})
    print(f"  分类统计（max depth={args.max_depth}, K={args.K}）:")
    for c in sorted(tax, key=lambda x: -tax[x]):
        print(f"    {c:<28} {tax[c]:>4}")
    print("  注：F1/F2 区分需看 query family 是否覆盖该方向；rank>max_depth 的"
          "论文本轮无法观测（F3a 需更深导出）")
    out = {"created_at": "2026-08-28", "depth": args.max_depth, "K": args.K,
           "failure_taxonomy": dict(tax), "papers": rows}
    with open(os.path.join(BASE, "data", "exports", "qgs_provenance_table.json"),
              "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n✓ provenance table 已写: data/exports/qgs_provenance_table.json")


if __name__ == "__main__":
    main()
