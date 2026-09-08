#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/build_r05_seen.py — R05 独立审计：paired S4/S5 双 seen join（2026-09-01 用户拍板）。

S5 已正式冻结（canonical seen=20417，+1223/+6.37% vs S4=19194）。
R05 目的：同一 fresh sample 上同时 join S4_SEEN 与 S5_SEEN，回答 cross-layer query
mechanism 是否真正泛化：
  ΔRecall_paired = Recall(S5|R05) − Recall(S4|R05)

重点记录 4 个量（用户定）：
  1. paired gain（Δ resolved / Δ conservative）
  2. RescueRate = (S5 newly seen relevant) / (S4 resolved misses) = (T5−T4)/(T4+F4)
  3. candidate cost：+1223 / +6.37%
  4. gain per candidate cost（engineering efficiency，非正式统计指标）

Seen source：
  S4 口径 = build_s4_found_sets()（S4 canonical 三通道）
  S5 口径 = build_s5_found_sets()（S4 全量 + s5_query_records 三通道）
  判定复用 resolve_seen_s1（DOI 主判 + normalized title 高置信兜底；模糊→UNKNOWN）

LCB：口径 A（N=audit.N_remaining 抽样前排除后池）

输出：
  r05_seen.json（paired stats + RescueRate + candidate cost + 历史 R04 上下文）
  completeness_audits.json 回写 r05_paired_comparison

用法：
  python tools/build_r05_seen.py --labels <R05 filled.json> [--out <path>]
"""
import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tools"))
sys.path.insert(0, os.path.join(BASE, "search_engine", "completeness"))

from build_r02_seen import resolve_seen_s1, _norm_doi, _norm_title  # noqa: E402
from build_r04_seen import build_s4_found_sets  # noqa: E402
from recall_bound import missed_relevant_upper_bound  # noqa: E402

T = os.path.join(BASE, "data", "exports", "terminology")
Q_REC = os.path.join(T, "s5_query_records.json")
UNIVERSES = os.path.join(BASE, "data", "exports", "completeness_universes.json")
AUDITS = os.path.join(BASE, "data", "exports", "completeness_audits.json")
DEFAULT_OUT = os.path.join(BASE, "data", "exports", "completeness_labels",
                           "r05_seen.json")

R04_AUDIT_ID = "pc_001::20260830155226"


def build_s5_found_sets() -> dict:
    """S5 三通道 found sets = S4 全量 + s5_query_records（canonical 口径）。"""
    found = build_s4_found_sets()
    rbq = json.load(open(Q_REC, encoding="utf-8"))["records_by_query"]
    for qs, rows in rbq.items():
        for r in rows:
            if r.get("eid"):
                found["eids"].add(str(r["eid"]).strip())
            if r.get("doi"):
                found["dois"].add(_norm_doi(r["doi"]))
            if r.get("title"):
                found["titles"].add(_norm_title(r["title"]))
    return found


def _wilson_upper(m: int, n: int, z: float = 1.6448536269514722) -> float:
    if n <= 0:
        return 0.0
    p = m / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * (p * (1 - p) / n + z2 / (4 * n * n)) ** 0.5 / denom
    return centre + half


def _stats(rel_ids, found, kb_in_uni, N_pool, n):
    verdicts = resolve_seen_s1(rel_ids, found)
    n_true = sum(1 for pid in rel_ids
                 if verdicts.get(pid, {}).get("agent_seen_s1") == "TRUE")
    n_false = sum(1 for pid in rel_ids
                  if verdicts.get(pid, {}).get("agent_seen_s1") == "FALSE")
    n_unk = sum(1 for pid in rel_ids
                if verdicts.get(pid, {}).get("agent_seen_s1") == "UNKNOWN")
    n_rel = len(rel_ids)
    n_resolved = n_true + n_false
    resolved_recall = n_true / n_resolved if n_resolved else None
    conservative_recall = n_true / n_rel if n_rel else None
    resolved_miss = n_false / n_resolved if n_resolved else None
    conservative_miss = (n_false + n_unk) / n_rel if n_rel else None
    unknown_rate = n_unk / n_rel if n_rel else None

    kb_verdicts = resolve_seen_s1(list(kb_in_uni), found)
    kb_seen = sum(1 for w in kb_in_uni
                  if kb_verdicts.get(w, {}).get("agent_seen_s1") == "TRUE")
    kb_missed = len(kb_in_uni) - kb_seen
    f_search = kb_seen + n_true
    m_upper_res = missed_relevant_upper_bound(N_pool, n, n_false)
    m_upper_ultra = missed_relevant_upper_bound(N_pool, n, n_false + n_unk)
    lcb_res = f_search / (f_search + kb_missed + m_upper_res) \
        if f_search + kb_missed + m_upper_res else None
    lcb_ultra = f_search / (f_search + kb_missed + m_upper_ultra) \
        if f_search + kb_missed + m_upper_ultra else None
    ci_upper = _wilson_upper(n_false, n_resolved)
    return {
        "t_f_u": [n_true, n_false, n_unk], "n_relevant": n_rel,
        "resolved_recall": round(resolved_recall, 4) if resolved_recall else None,
        "conservative_recall": round(conservative_recall, 4)
        if conservative_recall else None,
        "resolved_miss": round(resolved_miss, 4) if resolved_miss else None,
        "conservative_miss": round(conservative_miss, 4)
        if conservative_miss else None,
        "unknown_rate": round(unknown_rate, 4) if unknown_rate else None,
        "miss_ci_upper_wilson95": round(ci_upper, 4),
        "kb_seen": kb_seen, "kb_missed": kb_missed, "f_search": f_search,
        "m_upper_resolved": m_upper_res, "m_upper_ultra": m_upper_ultra,
        "recall_lcb_resolved": round(lcb_res, 4) if lcb_res else None,
        "recall_lcb_ultra": round(lcb_ultra, 4) if lcb_ultra else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", required=True, help="R05 filled labels 路径")
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    labs = json.load(open(args.labels, encoding="utf-8"))["labels"]
    audit_id = json.load(open(args.labels, encoding="utf-8")).get("audit_id", "")
    rel_ids = list(dict.fromkeys(l["paper_id"]
                                 for l in labs if l.get("label") == "RELEVANT"))

    audits = json.load(open(AUDITS, encoding="utf-8"))
    au = next((a for a in audits if a.get("audit_id") == audit_id), None)
    if au is None:
        print("[FATAL] audit record 未找到", audit_id)
        sys.exit(2)
    universe_id = au["universe_id"]
    snaps = json.load(open(UNIVERSES, encoding="utf-8"))
    uni = next((u for u in snaps if u.get("universe_id") == universe_id), None)
    universe_total = len(uni.get("paper_ids", [])) if uni else None
    known_relevant = set(uni.get("source_breakdown", {}).get("found_relevant", [])) \
        if uni else set()
    n = au.get("sample_size", len(labs))
    N_pool = au.get("N_remaining")
    assert N_pool is not None, "audit record 缺 N_remaining"
    kb_in_uni = known_relevant & set(uni.get("paper_ids", []))

    # ── paired：同一 sample 双 seen join ──
    found_s4 = build_s4_found_sets()
    found_s5 = build_s5_found_sets()
    st_s4 = _stats(rel_ids, found_s4, kb_in_uni, N_pool, n)
    st_s5 = _stats(rel_ids, found_s5, kb_in_uni, N_pool, n)

    # ── 4 个重点量 ──
    t4, f4, u4 = st_s4["t_f_u"]
    t5, f5, u5 = st_s5["t_f_u"]
    rescue_rate = ((t5 - t4) / (t4 + f4)) if (t4 + f4) else None
    delta_resolved = (st_s5["resolved_recall"] or 0) - (st_s4["resolved_recall"] or 0)
    delta_conservative = (st_s5["conservative_recall"] or 0) - \
        (st_s4["conservative_recall"] or 0)
    candidate_cost = {"s4": 19194, "s5": 20417, "delta": 1223,
                      "growth": round(1223 / 19194, 4)}
    gain_per_cost = (delta_resolved / 0.0637) if candidate_cost["growth"] else None

    # ── 历史上下文：R04/S4（宽 frame 首个 paired，仅参考）──
    r04 = next((a for a in audits if a.get("audit_id") == R04_AUDIT_ID), None)
    hist = None
    if r04 and r04.get("r04_paired_comparison"):
        p4 = r04["r04_paired_comparison"]
        hist = {"R04_S4_resolved": p4.get("R04_S4", {}).get("resolved_recall"),
                "R04_S3_resolved": p4.get("R04_S3", {}).get("resolved_recall"),
                "note": "历史上下文（宽 frame）；R05 主对比 = S4 vs S5 同 sample"}

    print("=" * 78)
    print(f"R05 paired S4/S5 audit（audit={audit_id}，宽 frame {universe_id}）")
    print("=" * 78)
    print(f"RELEVANT={len(rel_ids)} | N_pool={N_pool} | n={n} | "
          f"universe={universe_total} | KB∩universe={len(kb_in_uni)}")
    print(f"\n{'metric':<26}{'R05/S4(19194)':>16}{'R05/S5(20417)':>16}")
    for m, k in (("resolved_recall", "resolved_recall"),
                 ("conservative_recall", "conservative_recall"),
                 ("resolved_miss", "resolved_miss"),
                 ("conservative_miss", "conservative_miss"),
                 ("unknown_rate", "unknown_rate"),
                 ("Recall_LCB", "recall_lcb_resolved")):
        v4, v5 = st_s4[k], st_s5[k]
        s4 = f"{v4:>15.1%}" if isinstance(v4, (int, float)) else f"{'—':>15}"
        s5 = f"{v5:>15.1%}" if isinstance(v5, (int, float)) else f"{'—':>15}"
        print(f"{m:<26}{s4}{s5}")
    print(f"{'T/F/U':<26}{str(st_s4['t_f_u']):>16}{str(st_s5['t_f_u']):>16}")

    print("\n=== 重点量（用户定 4 个）===")
    print(f"  ΔRecall_paired (resolved) = {delta_resolved:+.1%}")
    print(f"  ΔRecall_paired (conservative) = {delta_conservative:+.1%}")
    print(f"  RescueRate = (T5−T4)/(T4+F4) = {t5-t4}/{t4+f4} = "
          f"{rescue_rate:.1%}" if rescue_rate is not None else "  RescueRate = n/a")
    print(f"  candidate cost = +1223 / +{candidate_cost['growth']:.2%}")
    print(f"  gain per cost = {gain_per_cost:+.3f} pp per 1% candidate growth"
          if gain_per_cost is not None else "  gain per cost = n/a")
    if hist:
        print(f"\n=== 历史上下文（R04 宽 frame，仅参考）===")
        print(f"  R04/S4={hist['R04_S4_resolved']:.1%} | R04/S3={hist['R04_S3_resolved']:.1%}")

    out = {
        "version": "r05_paired_stats_v1",
        "frozen_at": "2026-09-01",
        "audit_id": audit_id, "universe_id": universe_id,
        "paired": {"same_sample": True, "R05_S4": st_s4, "R05_S5": st_s5,
                   "delta_resolved_recall": round(delta_resolved, 4),
                   "delta_conservative_recall": round(delta_conservative, 4),
                   "rescue_rate": round(rescue_rate, 4) if rescue_rate is not None else None,
                   "candidate_cost": candidate_cost,
                   "gain_per_candidate_cost": round(gain_per_cost, 4)
                   if gain_per_cost is not None else None},
        "historical_R04_S4": hist,
        "sampling_pool_contract": {
            "universe_total": universe_total,
            "known_relevant_excluded": len(known_relevant),
            "excluded_historical_samples": len(au.get("excluded_papers", [])),
            "sampling_pool_size_before_sample": N_pool,
            "sample_n": n, "hypergeom_population_N": N_pool,
        },
        "interpretation": "R05 paired：同一 fresh sample 上 Recall(S5|R05) vs Recall(S4|R05)；"
                          "Δ 干净归因 cross-layer query mechanism（S5 只 +6.37% candidates）；"
                          "S4→S5 对比 S3→S4（+42.9% candidates / +1.8pp）",
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    try:
        audits = json.load(open(AUDITS, encoding="utf-8"))
        rec = next((a for a in audits if a.get("audit_id") == audit_id), None)
        if rec:
            rec["status"] = "COMPLETED"
            rec["labels"] = {l["paper_id"]: l.get("label") for l in labs}
            rec["n_relevant"] = len(rel_ids)
            rec["m"] = f5
            rec["n_agent_seen"] = t5
            rec["n_agent_unknown"] = u5
            rec["uncertain_count"] = sum(1 for l in labs
                                         if l.get("label") == "UNCERTAIN")
            rec["recall_lcb"] = st_s5["recall_lcb_resolved"]
            rec["search_stats"] = out
            rec["r05_paired_comparison"] = {
                "same_sample": True, "R05_S4": st_s4, "R05_S5": st_s5,
                "delta_resolved_recall": round(delta_resolved, 4),
                "rescue_rate": round(rescue_rate, 4) if rescue_rate is not None else None,
                "candidate_cost": candidate_cost,
                "gain_per_candidate_cost": round(gain_per_cost, 4)
                if gain_per_cost is not None else None,
                "note": "主指标 = paired Δ(S5−S4)，同一 fresh sample；R04 仅历史上下文",
            }
            with open(AUDITS, "w", encoding="utf-8") as f:
                json.dump(audits, f, ensure_ascii=False, indent=1)
            print(f"[OK] audit record 已更新: {audit_id} -> COMPLETED（r05_paired_comparison）")
    except Exception as e:
        print(f"[WARN] audit record 更新失败: {e}")

    print(f"\n[OK] written: {args.out}")


if __name__ == "__main__":
    main()
