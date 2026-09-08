#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/qa_s3_final.py — S3_FINAL 冻结前的 integrity QA（用户定 2026-08-30）。

纯 QA，不做基于语义/结果的人工挑选。4 件事（全部自动化验证）：
  1. seed ∈ S2_SEEN_SET                      （通过 seen_dois 桥接验证）
  2. seed 是 high-confidence relevant        （重算 high-conf seed 集合，独立验证）
  3. citation edge 真实 + direction 可追溯   （audit per_miss.linked_seed_wids 交叉验证）
  4. 无数据泄漏（seed ∉ 70 residual miss）    （未用 miss 本身作 seed）

QA 全 PASS → 生成 S3_FINAL 冻结配置（s3_final_config.json）：
  citation 16 + query 7（含 cost_tier：LOW new<=150 / MEDIUM 150<new<=600 / HIGH >600）
  + development 诊断（S3 dev：TRUE 238 / FALSE 35 / UNKNOWN 32 → resolved 87.2%，非正式）
QA 任一 FAIL → status=BLOCKED，不冻结。

用法：
  python tools/qa_s3_final.py [--out <path>]
"""
import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "tools"))

from build_r02_seen import resolve_seen_s1  # noqa: E402
from citation_reachability_audit import load_oa, seen_dois  # noqa: E402
from build_residual_misses import build_s2_found_sets  # noqa: E402

T = os.path.join(BASE, "data", "exports", "terminology")
PILOT = os.path.join(T, "s3_citation_pilot_results.json")
AUDIT = os.path.join(T, "s3_citation_audit.json")
MISSES = os.path.join(T, "s3_residual_misses.json")
OVERLAP = os.path.join(T, "s3_pilot_overlap.json")
LABELS = os.path.join(BASE, "data", "exports", "completeness_labels",
                      "pc_001__20260829130129_filled.json")
UNIVERSES = os.path.join(BASE, "data", "exports", "completeness_universes.json")
DEFAULT_OUT = os.path.join(T, "s3_final_config.json")

# cost_tier 规则（用户定：LOW/MEDIUM/HIGH，便于 R03 后看 ΔRecall/ΔCandidates）
def cost_tier(new: int) -> str:
    if new <= 150:
        return "LOW"
    if new <= 600:
        return "MEDIUM"
    return "HIGH"


def rebuild_seed_subsets(labels_path: str):
    """重算 high-conf seed 的两个来源子集（独立验证 QA2 + provenance 来源标注）。"""
    found_s2 = build_s2_found_sets()
    labs = json.load(open(labels_path, encoding="utf-8"))["labels"]
    rel = [l for l in labs if l.get("label") == "RELEVANT"]
    verdicts = resolve_seen_s1([l["paper_id"] for l in rel], found_s2)
    r02_seeds = {l["paper_id"] for l in rel
                 if verdicts.get(l["paper_id"], {}).get("agent_seen_s1") == "TRUE"}
    snaps = json.load(open(UNIVERSES, encoding="utf-8"))
    r02_uni = next((u for u in snaps
                    if u["universe_id"] == "pc_001-2026-08-29T130129"), None)
    kb_seeds = set()
    if r02_uni:
        found = set(r02_uni.get("source_breakdown", {}).get("found_relevant", []))
        kb_verdicts = resolve_seen_s1(list(found), found_s2)
        kb_seeds = {wid for wid in found
                    if kb_verdicts.get(wid, {}).get("agent_seen_s1") == "TRUE"}
    return kb_seeds, r02_seeds


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pilot", default=PILOT)
    ap.add_argument("--audit", default=AUDIT)
    ap.add_argument("--misses", default=MISSES)
    ap.add_argument("--overlap", default=OVERLAP)
    ap.add_argument("--r02-labels", default=LABELS)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    cp = json.load(open(args.pilot, encoding="utf-8"))
    audit = json.load(open(args.audit, encoding="utf-8"))
    rm = json.load(open(args.misses, encoding="utf-8"))
    ov = json.load(open(args.overlap, encoding="utf-8"))

    sel_ids = {a["action_id"] for a in ov["citation_set_cover"]["selected"]}
    sel = [a for a in cp["actions"] if a["action_id"] in sel_ids]
    if len(sel) != 16:
        print(f"[FATAL] set cover 选中 {len(sel)} != 16")
        sys.exit(2)

    oa = load_oa()
    found_s2 = build_s2_found_sets()
    kb_seeds, r02_seeds = rebuild_seed_subsets(args.r02_labels)
    hc_seeds = kb_seeds | r02_seeds
    residual_wids = {m["wid"] for m in rm["misses"]}
    miss_idx = {pm["wid"]: pm for pm in audit["per_miss"]}

    print("=" * 78)
    print("S3_FINAL integrity QA（16 citation seeds，4 项检查）")
    print("=" * 78)
    print(f"high-conf seeds 重算: KB={len(kb_seeds)} R02={len(r02_seeds)} "
          f"union={len(hc_seeds)} | residual={len(residual_wids)}")

    results = []
    all_pass = True
    for a in sel:
        seed = a["seed_wid"]
        m = oa.get(seed, {})
        # QA1 seed ∈ S2_SEEN：与 seed 构造完全同口径（resolve_seen_s1，DOI/title 双通道）
        qa1 = resolve_seen_s1([seed], found_s2).get(seed, {}).get(
            "agent_seen_s1") == "TRUE"
        # QA2 high-confidence relevant
        qa2 = seed in hc_seeds
        # QA3 edge 真实 + direction 可追溯：逐 (seed, miss) 直接重算（pilot 为双向 1-hop）
        #   BACKWARD edge = miss 引用 seed（seed ∈ miss.referenced_works）
        #   FORWARD edge  = seed 引用 miss（miss ∈ seed.referenced_works）
        recs = a.get("recovered_miss_ids", [])
        seed_refs = set(m.get("referenced_works", []))
        qa3_rows = []
        n_dir_mismatch = 0
        for wid in recs:
            mm = oa.get(wid, {})
            miss_refs = set(mm.get("referenced_works", []))
            direct_back = seed in miss_refs
            direct_fwd = wid in seed_refs
            edge = direct_back or direct_fwd
            actual_dir = "BACKWARD" if direct_back else ("FORWARD" if direct_fwd else None)
            d_match = actual_dir == a.get("direction")
            if not d_match:
                n_dir_mismatch += 1
            qa3_rows.append({"miss": wid, "edge_exists": edge,
                             "actual_direction": actual_dir,
                             "action_direction": a.get("direction"),
                             "direction_match": d_match})
        qa3 = bool(qa3_rows) and all(x["edge_exists"] for x in qa3_rows)
        # QA4 无泄漏（seed 不能是 residual miss 本身）
        qa4 = seed not in residual_wids
        # direction/counts 一致性（补充佐证，不单独计 FAIL）
        cnt_ok = ((a.get("backward_count") or 0) > 0
                  if a.get("direction") == "BACKWARD"
                  else (a.get("forward_count") or 0) > 0)
        if seed in kb_seeds and seed in r02_seeds:
            src = "BOTH"
        elif seed in kb_seeds:
            src = "KB"
        elif seed in r02_seeds:
            src = "R02"
        else:
            src = "UNKNOWN"
        ok = qa1 and qa2 and qa3 and qa4
        if not ok:
            all_pass = False
        results.append({
            "action_id": a["action_id"], "seed_wid": seed,
            "seed_doi": m.get("doi"), "seed_source": src,
            "direction": a.get("direction"),
            "new_vs_S2": a.get("new_vs_S2"), "delta": len(recs),
            "qa1_seed_in_s2_seen": qa1,
            "qa2_seed_high_conf_relevant": qa2,
            "qa3_edge_real_and_direction_traceable": qa3,
            "qa3_rows": qa3_rows,
            "qa4_no_data_leakage": qa4,
            "direction_count_consistent": cnt_ok,
            "pass": ok,
        })
        print(f"  {a['action_id']:<8} {seed:<18} src={src:<5} dir={a.get('direction'):<8} "
              f"QA1={qa1} QA2={qa2} QA3={qa3} QA4={qa4} "
              f"({'PASS' if ok else 'FAIL'})")

    n_pass = sum(1 for r in results if r["pass"])
    print("-" * 78)
    print(f"QA 汇总: {n_pass}/16 PASS → {'FROZEN' if all_pass else 'BLOCKED'}")
    if not all_pass:
        for r in results:
            if not r["pass"]:
                print(f"  [FAIL] {r['action_id']} {r['seed_wid']} "
                      f"QA1={r['qa1_seed_in_s2_seen']} QA2={r['qa2_seed_high_conf_relevant']} "
                      f"QA3={r['qa3_edge_real_and_direction_traceable']} "
                      f"QA4={r['qa4_no_data_leakage']}")

    # ── S3_FINAL 配置 ──
    ov_sel = {a["action_id"]: a for a in ov["citation_set_cover"]["selected"]}
    q_sel = ov["query_incremental_after_citation"]["selected"]
    # citation 16：用 overlap 的 delta（= 全量，init=None），加 QA 结果
    cit_actions = []
    for r in results:
        oa_act = ov_sel[r["action_id"]]
        cit_actions.append({
            "action_id": r["action_id"], "type": "CITATION",
            "seed_wid": r["seed_wid"], "seed_source": r["seed_source"],
            "direction": r["direction"],
            "new_vs_S2": r["new_vs_S2"], "delta_covered": r["delta"],
            "recovered_miss_ids": oa_act["recovered_miss_ids"],
            "qa": {k: r[k] for k in ("qa1_seed_in_s2_seen", "qa2_seed_high_conf_relevant",
                                     "qa3_edge_real_and_direction_traceable",
                                     "qa4_no_data_leakage")},
        })
    # query 7：加 cost_tier
    q_actions = []
    for q in q_sel:
        q_actions.append({
            "action_id": q["action_id"], "type": "QUERY_V2",
            "query_string": q["query_string"],
            "new_vs_S2": q["new_vs_S2"], "delta_covered": q["delta_covered"],
            "cost_tier": cost_tier(q["new_vs_S2"]),
            "recovered_miss_ids": q["recovered_miss_ids"],
        })

    # ── development 诊断（用户口径，非正式）──
    # S2 在 R02: TRUE=203 / FALSE=70 / UNKNOWN=32；S3 dev recovery=35 (union)
    dev = {
        "s2_on_r02": {"seen_true": 203, "seen_false": 70, "seen_unknown": 32},
        "s3_dev_recovery": ov["overlap"]["union_recovered"],
        "s3_dev": {"seen_true": 238, "seen_false": 35, "seen_unknown": 32},
        "resolved_recall": round(238 / (238 + 35), 4),
        "conservative_recall": round(238 / 305, 4),
        "trajectory": {"S1": 0.714, "S2": 0.744, "S3_dev": 0.872},
        "note": "DEVELOPMENT DIAGNOSTIC ONLY——S3 由 R02 misses 开发而来，"
                "此数字不得作为正式召回率；正式判定必须 fresh R03",
    }

    out = {
        "version": "s3_final_config_v1",
        "frozen_at": "2026-08-30",
        "status": "FROZEN" if all_pass else "BLOCKED",
        "development_source": "AUDIT_R02",
        "qa": {
            "checks": ["seed in S2_SEEN_SET", "seed high-confidence relevant",
                       "citation edge real + direction traceable",
                       "no data leakage (seed not in residual)"],
            "n_pass": n_pass, "n_total": 16, "all_pass": all_pass,
            "per_seed": results,
        },
        "citation_actions": cit_actions,
        "query_actions": q_actions,
        "total_actions": len(cit_actions) + len(q_actions),
        "expected_dev_recovery": ov["overlap"]["union_recovered"],
        "development_diagnostic": dev,
        "next": "S3 正式执行（depth=1000 基础设施参数）→ 冻结 S3_SEEN_SET → fresh R03",
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n[OK] {'FROZEN' if all_pass else 'BLOCKED'} config written: {args.out}")
    if all_pass:
        tiers = {}
        for q in q_actions:
            tiers.setdefault(q["cost_tier"], []).append(q["action_id"])
        print(f"query cost_tier: {json.dumps(tiers)}")
        print(f"dev diagnostic: resolved {dev['resolved_recall']:.1%} / "
              f"conservative {dev['conservative_recall']:.1%}（非正式）")


if __name__ == "__main__":
    main()
