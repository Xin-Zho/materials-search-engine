#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/analyze_s7_community_qa.py — 簇级 QA 聚合 → KEEP/FAIL/UNCERTAIN 决策表（2026-09-07）

P0 产物（480 篇 blind QA）回来后：按簇聚合 R/U/I 密度 → 簇级决策候选。
语义：簇 = S7 execute 打开的候选论文社区；簇级 KEEP = 整簇论文进候选池
（relevant 密度高，值得后续处理），FAIL = 密度低（EX 检索混入的非目标文献）。

统计口径（dev data，非正式审计——正式 recall 走 R06 独立 Audit）：
  R_rate = R / n_sampled；R+U_rate = (R+U)/n_sampled；Wilson 95% 区间
  簇相关数估计 = rate × cluster_size（外推，仅供回灌参考，不作结论）
决策阈值（--keep-n 语义：n_sampled 里 R+U ≥ keep_n → KEEP）：
  默认 keep_n=2（S6 全量 R+U 密度 8.8% ≈ 抽 20 期望 1.8 之上限）、
  fail 默认 R+U=0；1 篇落 UNCERTAIN（须补抽或簇级人工复核）
用法：
  .venv\\Scripts\\python.exe tools\\analyze_s7_community_qa.py
  .venv\\Scripts\\python.exe tools\\analyze_s7_community_qa.py --keep-n 3
输出：stdout 决策表 + data/exports/terminology/s7_community_qa_aggregate.json
"""
import argparse
import json
import math
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T = os.path.join(BASE, "data", "exports", "terminology")
MEM = os.path.join(T, "s7_community_memory.json")
LABELS = os.path.join(T, "s7_community_qa_labels.json")
OUT = os.path.join(T, "s7_community_qa_aggregate.json")
TOPUP_CORPUS = os.path.join(T, "s7_community_qa_corpus_uncertain.json")
TOPUP_LABELS = os.path.join(T, "s7_community_qa_labels_uncertain.json")


def wilson(k, n, z=1.96):
    """Wilson score 区间（k/n 比例的单侧下界与双侧区间）。"""
    if n == 0:
        return 0.0, 0.0, 0.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return centre - half, centre + half, p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-n", type=int, default=2,
                    help="n_sampled 里 R+U ≥ keep_n → 簇 KEEP")
    ap.add_argument("--topup", action="store_true",
                    help="并入 UNCERTAIN 补抽语料+labels（n→40 for C-008/014/018）")
    args = ap.parse_args()

    mem = json.load(open(MEM, encoding="utf-8"))
    labels = json.load(open(LABELS, encoding="utf-8"))
    if "labels" in labels and isinstance(labels["labels"], dict):
        labels = labels["labels"]
    valid = {"RELEVANT", "UNCERTAIN", "IRRELEVANT"}

    # sample keys：主抽样（community memory 冻结的 sample_by_cluster）
    sample_map = {cid: list(v) for cid, v in mem["sample_by_cluster"].items()}
    if args.topup and os.path.exists(TOPUP_LABELS):
        tc = json.load(open(TOPUP_CORPUS, encoding="utf-8"))
        tl = json.load(open(TOPUP_LABELS, encoding="utf-8"))
        if "labels" in tl and isinstance(tl["labels"], dict):
            tl = tl["labels"]
        k2c = tc.get("key2cluster", {})
        for p in tc["papers"]:
            cid = k2c.get(p["key"])
            if cid:
                sample_map.setdefault(cid, []).append(p["key"])
            if p["key"] in tl and tl[p["key"]] in valid:
                labels[p["key"]] = tl[p["key"]]
        print(f"[topup] 并入 {len(tc['papers'])} 篇补抽 label")
    clusters = {c["cluster_id"]: c for c in mem["clusters"]}

    print("=" * 100)
    print("S7 community QA 聚合（簇级 KEEP/FAIL/UNCERTAIN）")
    print("=" * 100)
    total = {"RELEVANT": 0, "UNCERTAIN": 0, "IRRELEVANT": 0, "NO_LABEL": 0}
    rows = []
    for cid in sorted(sample_map):
        keys = sample_map[cid]
        c = clusters[cid]
        cnt = {v: 0 for v in valid}
        cnt["NO_LABEL"] = 0
        for k in keys:
            lab = labels.get(k)
            if lab in valid:
                cnt[lab] += 1
            else:
                cnt["NO_LABEL"] += 1
        for v in total:
            total[v] += cnt[v]
        n = len(keys)
        r, u = cnt["RELEVANT"], cnt["UNCERTAIN"]
        rpu = r + u
        ru_lo, ru_hi, ru_rate = wilson(rpu, n)
        r_lo, _, r_rate = wilson(r, n)
        est_rel = r_rate * c["size"]
        # 决策
        if rpu >= args.keep_n:
            decision = "KEEP"
        elif rpu == 0:
            decision = "FAIL"
        else:
            decision = "UNCERTAIN"
        rows.append({
            "cluster_id": cid, "size": c["size"],
            "n_sampled": n, "n_abstract": c["n_with_abstract"],
            "R": r, "U": u, "I": cnt["IRRELEVANT"],
            "no_label": cnt["NO_LABEL"],
            "R_rate": round(r_rate, 3), "R_plus_U_rate": round(ru_rate, 3),
            "R_plus_U_lo": round(ru_lo, 3), "R_plus_U_hi": round(ru_hi, 3),
            "est_relevant": round(est_rel, 1),
            "decision": decision,
            "top_words": c["top_words"][:8],
            "domain_src": c["domain_src"],
        })

    # 汇总
    tot_sampled = sum(r["n_sampled"] for r in rows)
    tot_r = sum(r["R"] for r in rows)
    tot_rpu = sum(r["R"] + r["U"] for r in rows)
    keep_ids = [r["cluster_id"] for r in rows if r["decision"] == "KEEP"]
    unc_ids = [r["cluster_id"] for r in rows if r["decision"] == "UNCERTAIN"]
    fail_ids = [r["cluster_id"] for r in rows if r["decision"] == "FAIL"]
    n_keep_papers = sum(clusters[cid]["size"] for cid in keep_ids)
    n_unc_papers = sum(clusters[cid]["size"] for cid in unc_ids)

    agg = {
        "role": "S7 community QA aggregate (dev data, 非正式审计)",
        "rubric": "S6_QA_RUBRIC_V1", "keep_n": args.keep_n,
        "total_sampled": tot_sampled,
        "total_labels": {k: v for k, v in total.items()},
        "R_rate_overall": round(tot_r / tot_sampled, 4) if tot_sampled else None,
        "R_plus_U_rate_overall": round(tot_rpu / tot_sampled, 4)
        if tot_sampled else None,
        "clusters": rows,
        "decision_summary": {
            "KEEP": {"n_clusters": len(keep_ids), "ids": keep_ids,
                     "n_papers": n_keep_papers},
            "UNCERTAIN": {"n_clusters": len(unc_ids), "ids": unc_ids,
                          "n_papers": n_unc_papers},
            "FAIL": {"n_clusters": len(fail_ids), "ids": fail_ids,
                     "n_papers": sum(clusters[cid]["size"]
                                     for cid in fail_ids)},
        },
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(agg, f, ensure_ascii=False, indent=1)

    print(f"\n总体（抽样 {tot_sampled}）: R {tot_r} | U {total['UNCERTAIN']} | "
          f"I {total['IRRELEVANT']} | no_label {total['NO_LABEL']}")
    print(f"  R_rate={agg['R_rate_overall']:.1%} | "
          f"R+U_rate={agg['R_plus_U_rate_overall']:.1%} "
          f"（S6 全量参考 8.8%）")
    print(f"\n{'簇':<8}{'size':>5}{'n':>4} {'R':>3}{'U':>3}{'I':>4} "
          f"{'R+U率':>7} {'95%下限':>8} {'估计R':>7}  决策    簇主题")
    for r in sorted(rows, key=lambda x: -(x["R"] + x["U"]) / x["n_sampled"]):
        tag = {"KEEP": "✅", "UNCERTAIN": "❓", "FAIL": "❌"}[r["decision"]]
        print(f"{r['cluster_id']:<8}{r['size']:>5}{r['n_sampled']:>4} "
              f"{r['R']:>3}{r['U']:>3}{r['I']:>4} "
              f"{r['R_plus_U_rate']:>7.1%}{r['R_plus_U_lo']:>8.1%} "
              f"{r['est_relevant']:>7.1f}  {tag}      "
              f"{' '.join(r['top_words'][:4])}")
    d = agg["decision_summary"]
    print(f"\n决策（keep_n={args.keep_n}）:")
    print(f"  KEEP      {d['KEEP']['n_clusters']} 簇 / {d['KEEP']['n_papers']} 篇"
          f"  {d['KEEP']['ids']}")
    print(f"  UNCERTAIN {d['UNCERTAIN']['n_clusters']} 簇 / "
          f"{d['UNCERTAIN']['n_papers']} 篇  {d['UNCERTAIN']['ids']}")
    print(f"  FAIL      {d['FAIL']['n_clusters']} 簇 / {d['FAIL']['n_papers']} 篇")
    print(f"\n[OK] aggregate: {OUT}")
    print("→ 阈值裁决后：KEEP 簇论文进候选池；decision 回灌 relation memory"
          "（EX community 层）")


if __name__ == "__main__":
    main()
