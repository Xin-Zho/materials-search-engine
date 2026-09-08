#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
analyze_s3_pilot_overlap.py — S3 双通道 pilot 实际 recovery overlap + citation greedy set cover

输入:
  --query-pilot     data/exports/terminology/s3_query_pilot_results.json
  --citation-pilot  data/exports/terminology/s3_citation_pilot_results.json
  --misses          data/exports/terminology/s3_residual_misses.json
  --out             输出 JSON 路径

输出:
  1. QUERY(v2) vs CITATION 实际 recovery overlap 四格:
       query_only / citation_only / both / union / remaining（含标题明细）
  2. CITATION actual cost-aware greedy set cover（用户定 Score）:
       Score(a|S) = |Recovered(a) - Recovered(S)| / (1 + log(1 + NewCandidates_a))
       NewCandidates = new_vs_S2（真实新候选数，非估计；fallback estimated_cost）
     → 从 45 个 seed 选最小高效子集（对比 45 全量成本 2283）
  3. citation 未覆盖的 residual 中 QUERY v2 还能补几篇（增量贡献，判断 query 是否值得留）

原则（用户冻结 2026-08-30）:
  - 只看 actual recovered_miss_ids，不看理论 covered_miss_ids
  - 45 个 citation seed 全有效但高度重复 → 必须 set cover，不能全加
  - QUERY 和 CITATION 同池竞争由 S3 构建时统一处理；本工具先给 citation 主通道的最小子集
"""
import argparse
import json
import math
import os
import sys

# ──────────────────────────────────────────────────────────────
# 1. 读取
# ──────────────────────────────────────────────────────────────
def load(path: str, what: str) -> dict:
    if not os.path.exists(path):
        print(f"[FATAL] {what} 不存在: {path}")
        sys.exit(2)
    return json.load(open(path, encoding="utf-8"))


def action_recovered(a: dict) -> set:
    return {str(w) for w in (a.get("recovered_miss_ids") or [])}


def action_cost(a: dict) -> int:
    """真实新候选数优先；citation 无 new_vs_S2 时 fallback estimated_cost。"""
    v = a.get("new_vs_S2")
    if v is None:
        v = a.get("estimated_cost")
    return int(v or 0)


def greedy_set_cover(actions: list[dict], targets: set,
                     init_covered: set | None = None
                     ) -> tuple[list, list[set], set, int]:
    """cost-aware greedy set cover。Score = |ΔRecovered| / (1+log(1+cost))。

    targets      = 需要覆盖的目标集合（纯新增目标）
    init_covered = 已覆盖集合初始值（第二通道的真增量计算：delta 扣除已覆盖）
    返回 (selected, deltas, added, total_cost)：
      selected  = 按选择顺序的 action 列表
      deltas[i] = 第 i 个 action 在该轮的真增量覆盖集合（扣 init_covered 与先前选择）
      added     = 全部新增覆盖（不含 init_covered）
    """
    covered: set = set(init_covered or ())
    added: set = set()
    selected: list = []
    deltas: list[set] = []
    total_cost = 0
    n_iters = 0
    while added < targets and n_iters < 500:
        n_iters += 1
        best, best_score, best_delta = None, -1e18, set()
        for a in actions:
            delta = action_recovered(a) - covered
            if not delta:
                continue
            cost = action_cost(a)
            score = len(delta) / (1.0 + math.log1p(max(cost, 0)))
            if score > best_score:
                best, best_score, best_delta = a, score, delta
        if best is None:
            break
        selected.append(best)
        deltas.append(best_delta)
        covered |= best_delta
        added |= best_delta
        total_cost += action_cost(best)
    return selected, deltas, added, total_cost


def _safeprint(s: str, n: int = 90) -> str:
    """终端 ASCII 安全截断（GBK 终端遇非 ASCII 字符崩溃——已知问题）。"""
    return (s or "").encode("ascii", "replace").decode("ascii")[:n]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query-pilot",
                    default="data/exports/terminology/s3_query_pilot_results.json")
    ap.add_argument("--citation-pilot",
                    default="data/exports/terminology/s3_citation_pilot_results.json")
    ap.add_argument("--misses",
                    default="data/exports/terminology/s3_residual_misses.json")
    ap.add_argument("--out", default="data/exports/terminology/s3_pilot_overlap.json")
    args = ap.parse_args()

    qp = load(args.query_pilot, "query pilot")
    cp = load(args.citation_pilot, "citation pilot")
    rm = load(args.misses, "residual misses")

    q_acts = qp["actions"]
    c_acts = cp["actions"]
    misses = rm["misses"]
    all_wids = {m["wid"] for m in misses}
    if len(all_wids) != 70:
        print(f"[WARN] residual misses 集合 size={len(all_wids)} != 70")
    title_of = {m["wid"]: (m.get("oa_title") or m.get("title") or "") for m in misses}

    # ──────────────────────────────────────────────────────────
    # 2. overlap 四格
    # ──────────────────────────────────────────────────────────
    q_rec = set().union(*[action_recovered(a) for a in q_acts]) if q_acts else set()
    c_rec = set().union(*[action_recovered(a) for a in c_acts]) if c_acts else set()
    both = q_rec & c_rec
    q_only = q_rec - c_rec
    c_only = c_rec - q_rec
    union = q_rec | c_rec
    remaining = all_wids - union

    print("=" * 78)
    print("S3 pilot 实际 recovery overlap（QUERY v2 vs CITATION）")
    print("=" * 78)
    print(f"{'metric':<16}{'count':>6}   {'pct of 70':>10}")
    print(f"{'query_recovered':<16}{len(q_rec):>6}   {len(q_rec)/70:>9.1%}")
    print(f"{'citation_recovered':<16}{len(c_rec):>6}   {len(c_rec)/70:>9.1%}")
    print(f"{'both':<16}{len(both):>6}   {len(both)/70:>9.1%}")
    print(f"{'query_only':<16}{len(q_only):>6}   {len(q_only)/70:>9.1%}")
    print(f"{'citation_only':<16}{len(c_only):>6}   {len(c_only)/70:>9.1%}")
    print(f"{'union_recovered':<16}{len(union):>6}   {len(union)/70:>9.1%}")
    print(f"{'remaining':<16}{len(remaining):>6}   {len(remaining)/70:>9.1%}")

    if remaining:
        print("\nremaining miss（双通道都没追回）：")
        for wid in sorted(remaining):
            print(f"  {wid} | {_safeprint(title_of.get(wid, ''))}")

    # ──────────────────────────────────────────────────────────
    # 3. CITATION greedy set cover
    # ──────────────────────────────────────────────────────────
    c_targets = c_rec  # citation 能覆盖的理论目标（24）
    sel_c, deltas_c, cov_c, cost_c = greedy_set_cover(c_acts, c_targets)
    print("\n" + "=" * 78)
    print(f"CITATION greedy set cover（Score = |ΔRecovered|/(1+log(1+new))，"
          f"目标={len(c_targets)} miss）")
    print("=" * 78)
    print(f"selected seeds = {len(sel_c)} / {len(c_acts)} | "
          f"covered = {len(cov_c & c_targets)} / {len(c_targets)} | "
          f"total_new_candidates = {cost_c}（全量 45 seed = 2283）")
    print(f"{'seed':<10}{'rec':>4}{'new':>6}{'delta':>6}   action")
    for a, dl in zip(sel_c, deltas_c):
        print(f"{a['action_id']:<10}{a.get('residual_recovered', 0):>4}"
              f"{action_cost(a):>6}{len(dl):>6}   "
              f"{str(a.get('source') or a.get('seed_wid'))[:60]}")
    not_cov = c_targets - cov_c
    if not_cov:
        print(f"\n[WARN] citation set cover 未覆盖 {len(not_cov)} 篇（应为 0）")
        for wid in sorted(not_cov):
            print(f"  {wid} | {_safeprint(title_of.get(wid, ''), 80)}")

    # ──────────────────────────────────────────────────────────
    # 4. citation 未覆盖 → query 增量贡献（真增量：covered 预置 citation 已覆盖集）
    # ──────────────────────────────────────────────────────────
    c_uncovered = all_wids - cov_c          # citation 最优子集后的剩余
    q_extra = q_rec & c_uncovered           # query 能额外补的
    q_sel, deltas_q, q_cov, q_cost = greedy_set_cover(
        [a for a in q_acts if action_recovered(a) & c_uncovered], q_extra,
        init_covered=cov_c)
    print("\n" + "=" * 78)
    print("QUERY v2 对 citation 子集后的增量贡献（真增量，扣 citation 已覆盖）")
    print("=" * 78)
    print(f"citation 子集后剩余 residual = {len(c_uncovered)}（含 {len(q_extra)} 篇 query 可补）")
    print(f"query 增量 selected = {len(q_sel)} actions | 补回 {len(q_cov)} 篇 | "
          f"new_candidates = {q_cost}")
    for a, dl in zip(q_sel, deltas_q):
        print(f"  {a['action_id']:<10} rec={a.get('residual_recovered', 0):>2} "
              f"new={action_cost(a):>4} delta={len(dl)} | "
              f"{a.get('query_string', '')[:70]}")

    # ──────────────────────────────────────────────────────────
    # 5. 落盘
    # ──────────────────────────────────────────────────────────
    out = {
        "version": "s3_pilot_overlap_v1",
        "frozen_at": "2026-08-30",
        "development_source": "AUDIT_R02",
        "pilot_sources": {
            "query": args.query_pilot, "citation": args.citation_pilot,
            "misses": args.misses,
        },
        "residual_total": len(all_wids),
        "overlap": {
            "query_recovered": len(q_rec), "citation_recovered": len(c_rec),
            "both": len(both), "query_only": len(q_only),
            "citation_only": len(c_only), "union_recovered": len(union),
            "remaining": len(remaining),
            "remaining_wids": sorted(remaining),
        },
        "citation_set_cover": {
            "selected_count": len(sel_c), "total_actions": len(c_acts),
            "covered": len(cov_c & c_targets), "target": len(c_targets),
            "total_new_candidates": cost_c,
            "selected": [{"action_id": a["action_id"], "seed_wid": a.get("seed_wid"),
                          "source": a.get("source"), "direction": a.get("direction"),
                          "new_vs_S2": action_cost(a),
                          "residual_recovered": a.get("residual_recovered", 0),
                          "delta_covered": len(dl),
                          "recovered_miss_ids": sorted(dl)}
                         for a, dl in zip(sel_c, deltas_c)],
        },
        "query_incremental_after_citation": {
            "citation_subset_remaining": len(c_uncovered),
            "query_can_cover": len(q_extra),
            "query_selected_count": len(q_sel),
            "query_covered": len(q_cov),
            "query_new_candidates": q_cost,
            "selected": [{"action_id": a["action_id"], "query_string": a.get("query_string"),
                          "new_vs_S2": action_cost(a),
                          "residual_recovered": a.get("residual_recovered", 0),
                          "delta_covered": len(dl),
                          "recovered_miss_ids": sorted(dl)}
                         for a, dl in zip(q_sel, deltas_q)],
        },
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n[OK] written: {args.out}")


if __name__ == "__main__":
    main()
