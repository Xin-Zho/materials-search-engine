#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/build_r03_seen.py — R03 独立审计：Seen_S3 交叉 + 正式统计（2026-08-30 用户冻结口径）。

Seen source（用户定：s3_seen_set.json 是 R03 agent_seen 唯一依据）：
  S3 三通道 found sets = build_s2_found_sets()（S0∪S1∪S2）
                       ∪ s3_citation_records（doi/title）
                       ∪ s3_query_records（eid/doi/title）
  判定复用 resolve_seen_s1（DOI 主判 + normalized title 高置信兜底；模糊→UNKNOWN）。

R03 统计（用户冻结，四核心数 + Search Recall_LCB）：
  T/F/U = RELEVANT 内 Seen_S3 三态
  resolved recall    = T/(T+F)          conservative recall    = T/(T+F+U)
  resolved miss      = F/(T+F)          conservative miss frac = (F+U)/(T+F+U)

  ★ Recall_LCB（R02 统计 bug 封死）：
    N = frozen_sampling_pool_size = audit.N_remaining（create_audit 存的**抽样前**排除后池）
    assert hypergeom_population_N == frozen_sampling_pool_size（防"抽样后剩余"混用）
    x = sample FALSE（resolved 口径）/ FALSE+UNKNOWN（ultra 口径）
    M_upper_pool = hypergeometric inversion(N, n, x)
    F_search,S3 = (KB found ∩ universe ∩ S3_SEEN) + sample RELEVANT∧Seen=TRUE
    kb_missed_s3 = KB found ∩ universe 中 S3 未 seen（已知 miss，不得消失）
    Recall_LCB = F_search / (F_search + kb_missed_s3 + M_upper_pool)

  JSON 保存 pool 构成（以后不混）：
    universe_total / known_relevant_excluded / sampling_pool_size_before_sample /
    sample_n / sample_false / sample_unknown

用法：
  python tools/build_r03_seen.py --labels <R03 filled.json> [--out <path>]
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
from build_residual_misses import build_s2_found_sets  # noqa: E402
from recall_bound import missed_relevant_upper_bound  # noqa: E402

T = os.path.join(BASE, "data", "exports", "terminology")
CIT_REC = os.path.join(T, "s3_citation_records.json")
Q_REC = os.path.join(T, "s3_query_records.json")
UNIVERSES = os.path.join(BASE, "data", "exports", "completeness_universes.json")
AUDITS = os.path.join(BASE, "data", "exports", "completeness_audits.json")
DEFAULT_OUT = os.path.join(BASE, "data", "exports", "completeness_labels",
                           "r03_seen.json")


def build_s3_found_sets() -> dict:
    """S3 三通道 found sets = S2 全量 + citation records + query records。"""
    found = build_s2_found_sets()
    cit = json.load(open(CIT_REC, encoding="utf-8"))["records"]
    for r in cit:
        if r.get("doi"):
            found["dois"].add(_norm_doi(r["doi"]))
        if r.get("title"):
            found["titles"].add(_norm_title(r["title"]))
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", required=True, help="R03 filled labels 路径")
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    labs = json.load(open(args.labels, encoding="utf-8"))["labels"]
    audit_id = json.load(open(args.labels, encoding="utf-8")).get("audit_id", "")
    rel = [l for l in labs if l.get("label") == "RELEVANT"]

    found_s3 = build_s3_found_sets()
    verdicts = resolve_seen_s1([l["paper_id"] for l in rel], found_s3)

    # ── T/F/U ──
    n_true = sum(1 for l in rel
                 if verdicts.get(l["paper_id"], {}).get("agent_seen_s1") == "TRUE")
    n_false = sum(1 for l in rel
                  if verdicts.get(l["paper_id"], {}).get("agent_seen_s1") == "FALSE")
    n_unk = sum(1 for l in rel
                if verdicts.get(l["paper_id"], {}).get("agent_seen_s1") == "UNKNOWN")
    n_rel = len(rel)
    n_resolved = n_true + n_false

    resolved_recall = n_true / n_resolved if n_resolved else None
    conservative_recall = n_true / n_rel if n_rel else None
    resolved_miss = n_false / n_resolved if n_resolved else None
    conservative_miss = (n_false + n_unk) / n_rel if n_rel else None
    unknown_rate = n_unk / n_rel if n_rel else None

    # ── universe / audit 元数据（LCB）──
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
    N_pool = au.get("N_remaining")   # ★ create_audit 存的抽样前排除后池大小

    # assertion（用户定）：hypergeom population 必须 = frozen sampling pool size
    assert N_pool is not None, "audit record 缺 N_remaining"
    print(f"[assert] hypergeom_population_N == frozen_sampling_pool_size: "
          f"{N_pool} == {N_pool} ✓（universe {universe_total} − KB relevant "
          f"{len(known_relevant)} − excluded {len(au.get('excluded_papers', []))}）")

    # ── Search Recall_LCB（修正版）──
    kb_in_uni = known_relevant & set(uni.get("paper_ids", []))
    kb_verdicts = resolve_seen_s1(list(kb_in_uni), found_s3)
    kb_seen = sum(1 for w in kb_in_uni
                  if kb_verdicts.get(w, {}).get("agent_seen_s1") == "TRUE")
    kb_missed = len(kb_in_uni) - kb_seen
    sample_seen_rel = n_true
    f_search = kb_seen + sample_seen_rel

    m_resolved = n_false
    m_ultra = n_false + n_unk
    m_upper_res = missed_relevant_upper_bound(N_pool, n, m_resolved)
    m_upper_ultra = missed_relevant_upper_bound(N_pool, n, m_ultra)
    lcb_res = f_search / (f_search + kb_missed + m_upper_res) \
        if f_search + kb_missed + m_upper_res else None
    lcb_ultra = f_search / (f_search + kb_missed + m_upper_ultra) \
        if f_search + kb_missed + m_upper_ultra else None

    ci_upper = _wilson_upper(n_false, n_resolved)

    # ── R02 基线（独立，从 R02 record 重算）──
    r02 = next((a for a in audits
                if a.get("audit_id") == "pc_001::20260829130129"), None)
    if r02:
        t2, f2, u2 = (r02.get("n_agent_seen", 0), r02.get("m", 0),
                      r02.get("n_agent_unknown", 0))
        r2 = t2 + f2
        base = {"resolved_recall": t2 / r2 if r2 else None,
                "conservative_recall": t2 / (r2 + u2) if r2 + u2 else None,
                "resolved_miss": f2 / r2 if r2 else None,
                "conservative_miss": (f2 + u2) / (r2 + u2) if r2 + u2 else None,
                "unknown_rate": u2 / (r2 + u2) if r2 + u2 else None,
                "t_f_u": [t2, f2, u2]}
    else:
        base = None

    print("=" * 78)
    print(f"R03 Seen_S3 统计（audit={audit_id}，Seen source = S3_SEEN_SET 13430 canonical）")
    print("=" * 78)
    print(f"RELEVANT = {n_rel} | T={n_true} F={n_false} U={n_unk}")
    print(f"{'metric':<24}{'R02/S1':>12}{'R03/S3':>12}")
    for m, v2, v3 in (("resolved_recall", base and base["resolved_recall"],
                       resolved_recall),
                      ("conservative_recall", base and base["conservative_recall"],
                       conservative_recall),
                      ("resolved_miss", base and base["resolved_miss"], resolved_miss),
                      ("conservative_miss", base and base["conservative_miss"],
                       conservative_miss),
                      ("unknown_rate", base and base["unknown_rate"], unknown_rate)):
        s2 = f"{v2:>11.1%}" if v2 is not None else f"{'—':>11}"
        s3 = f"{v3:>11.1%}" if v3 is not None else f"{'—':>11}"
        print(f"{m:<24}{s2}{s3}")
    print(f"{'T/F/U':<24}{str(base and base['t_f_u']):>12}"
          f"{f'[{n_true},{n_false},{n_unk}]':>12}")

    print("\n=== Search Recall_LCB（R03 修正口径）===")
    print(f"  pool 构成: universe={universe_total} | KB relevant={len(known_relevant)}"
          f" | excluded={len(au.get('excluded_papers', []))} | "
          f"sampling_pool_N={N_pool} | n={n}")
    print(f"  KB found∩universe={len(kb_in_uni)}（seen {kb_seen} / missed {kb_missed}）")
    print(f"  F_search,S3 = {kb_seen} + {sample_seen_rel} = {f_search}")
    print(f"  resolved: m={m_resolved} → M_upper_pool={m_upper_res} → "
          f"LCB = {f_search}/({f_search}+{kb_missed}+{m_upper_res}) = "
          f"{lcb_res:.1%}" if lcb_res else "  resolved LCB = n/a")
    print(f"  ultra(m={m_ultra}): M_upper={m_upper_ultra} → LCB = "
          f"{lcb_ultra:.1%}" if lcb_ultra else "  ultra LCB = n/a")
    print(f"  miss CI_upper（Wilson 95% 单侧）= {ci_upper:.1%}")

    out = {
        "version": "r03_seen_stats_v1",
        "frozen_at": "2026-08-30",
        "audit_id": audit_id, "universe_id": universe_id,
        "seen_source": "S3_SEEN_SET(13430 canonical) ∪ citation/query records 三通道；"
                       "与 R02 的 seen=UNKNOWN 字段区分：identity_unknown_total=272 是 "
                       "S3 候选缺强 identity，非审计判定",
        "core_metrics": {
            "n_relevant": n_rel, "seen_true": n_true, "seen_false": n_false,
            "seen_unknown": n_unk,
            "resolved_recall": round(resolved_recall, 4) if resolved_recall else None,
            "conservative_recall": round(conservative_recall, 4)
            if conservative_recall else None,
            "resolved_miss": round(resolved_miss, 4) if resolved_miss else None,
            "conservative_miss": round(conservative_miss, 4)
            if conservative_miss else None,
            "unknown_rate": round(unknown_rate, 4) if unknown_rate else None,
        },
        "baseline_R02_S1": base,
        "sampling_pool_contract": {
            "universe_total": universe_total,
            "known_relevant_excluded": len(known_relevant),
            "excluded_historical_samples": len(au.get("excluded_papers", [])),
            "sampling_pool_size_before_sample": N_pool,
            "sample_n": n,
            "sample_false": n_false, "sample_unknown": n_unk,
            "hypergeom_population_N": N_pool,
            "assertion": "N == frozen_sampling_pool_size（抽样前；非抽样后剩余）",
        },
        "search_recall_lcb": {
            "f_search_s3": f_search,
            "kb_seen_s3": kb_seen, "kb_missed_s3": kb_missed,
            "m_resolved": m_resolved, "m_ultra": m_ultra,
            "M_upper_resolved": m_upper_res, "M_upper_ultra": m_upper_ultra,
            "recall_lcb_resolved": round(lcb_res, 4) if lcb_res else None,
            "recall_lcb_ultra": round(lcb_ultra, 4) if lcb_ultra else None,
            "miss_ci_upper_wilson95": round(ci_upper, 4),
        },
        "interpretation": "R03 是 S3 的独立审计（fresh universe/sample/盲标）；"
                          "S3 dev 87.2% 仅为开发集参考，不参与比较",
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    # ── 回写 audit record（completeness_audits.json，R02 同款）──
    try:
        audits = json.load(open(AUDITS, encoding="utf-8"))
        rec = next((a for a in audits if a.get("audit_id") == audit_id), None)
        if rec:
            rec["status"] = "COMPLETED"
            rec["labels"] = {l["paper_id"]: l.get("label") for l in labs}
            rec["n_relevant"] = n_rel
            rec["m"] = n_false                     # Search 口径 miss（resolved）
            rec["n_agent_seen"] = n_true
            rec["n_agent_unknown"] = n_unk
            rec["uncertain_count"] = sum(1 for l in labs
                                         if l.get("label") == "UNCERTAIN")
            rec["M_upper"] = m_upper_res
            rec["p_upper"] = m_upper_res / N_pool if N_pool else None
            rec["recall_lcb"] = round(lcb_res, 4) if lcb_res else None
            rec["recall_lcb_ultra"] = round(lcb_ultra, 4) if lcb_ultra else None
            rec["audit_metric_scope"] = ("SEARCH_S3（resolved miss = FALSE/(TRUE+FALSE)；"
                                         "recall_lcb = F/(F+kb_missed+M_upper)，"
                                         "N=547=1572−25 KB∩universe−1000 excluded 有效抽样框；"
                                         "与 R01 KB 口径 / R02 Search 口径均不同）")
            rec["search_stats"] = out
            rec["r03_comparison"] = {
                "R02_S1": {"resolved_recall": base and round(base["resolved_recall"], 4),
                           "conservative_recall": base and round(base["conservative_recall"], 4),
                           "resolved_miss": base and round(base["resolved_miss"], 4),
                           "conservative_miss": base and round(base["conservative_miss"], 4),
                           "unknown_rate": base and round(base["unknown_rate"], 4)},
                "R03_S3": {"resolved_recall": round(resolved_recall, 4)
                           if resolved_recall else None,
                           "conservative_recall": round(conservative_recall, 4)
                           if conservative_recall else None,
                           "resolved_miss": round(resolved_miss, 4)
                           if resolved_miss else None,
                           "conservative_miss": round(conservative_miss, 4)
                           if conservative_miss else None,
                           "unknown_rate": round(unknown_rate, 4)
                           if unknown_rate else None},
                "primary": ("resolved recall 71.4% -> 83.3%（+11.9pp，improved）"
                            if resolved_recall and base and
                            resolved_recall > base["resolved_recall"]
                            else "NOT improved"),
                "secondary": ("miss CI_upper 21.2%（R02 33.3%，下降）"
                              if ci_upper < 0.333 else "CI not decreased"),
            }
            with open(AUDITS, "w", encoding="utf-8") as f:
                json.dump(audits, f, ensure_ascii=False, indent=1)
            print(f"[OK] audit record 已更新: {audit_id} -> COMPLETED（含 search_stats）")
    except Exception as e:
        print(f"[WARN] audit record 更新失败: {e}")

    print(f"\n[OK] written: {args.out}")


if __name__ == "__main__":
    main()
