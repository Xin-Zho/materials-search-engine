#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/build_r04_seen.py — R04 独立审计：paired S3/S4 双 seen join（2026-08-30 用户拍板）。

R04 协议补丁（用户 2026-08-30 23:50 拍板）：
  - frame 升级到宽定义（bae4cd5a5dc6, n=5890）——窄 frame（241a7a93def7, n=1572）被
    R01+R02+R03 历史样本吃光（N_remaining=47 < 500）。
  - **paired comparison**：R04 的同一批 fresh sample 上同时 join S3_SEEN_SET(13430) 和
    S4_SEEN_SET(19194)——frame 变化不混叠，Δ(S3→S4) 是干净的同池配对增量。
  - R03/S3=83.3%（窄 frame）保留为历史轨迹上下文，**不**与 R04/S4 直接连成纯性能轨迹。

Seen source：
  S3 口径 = build_s3_found_sets()（S0∪S1∪S2∪S3 全量三通道）
  S4 口径 = build_s4_found_sets()（S3 全量 + S4 citation/query records，canonical）
  判定复用 resolve_seen_s1（DOI 主判 + normalized title 高置信兜底；模糊→UNKNOWN）
  ——与 R03 完全同口径；S4 identity 已 reconcile（overlap 0→478, seen=19194）

统计（每口径独立）：
  T/F/U = RELEVANT 内 Seen 三态
  resolved recall = T/(T+F)  conservative recall = T/(T+F+U)  resolved miss = F/(T+F)

  ★ Recall_LCB（口径 A，R03 封死）：
    N = frozen_sampling_pool_size = audit.N_remaining（抽样前排除后池）
    x = sample FALSE（resolved）/ FALSE+UNKNOWN（ultra）
    M_upper_pool = hypergeometric inversion(N, n, x)
    F_search = (KB found ∩ universe ∩ Seen) + sample RELEVANT∧Seen=TRUE
    kb_missed = KB found ∩ universe 中 Seen 未命中（已知 miss 不得消失）
    Recall_LCB = F_search / (F_search + kb_missed + M_upper_pool)

输出：
  r04_seen.json（paired stats：core_metrics_s3 / core_metrics_s4 / paired_delta /
                 historical_R03_S3 / sampling_pool_contract / search_recall_lcb_s3/s4）

用法：
  python tools/build_r04_seen.py --labels <R04 filled.json> [--out <path>]
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
from build_r03_seen import build_s3_found_sets  # noqa: E402
from recall_bound import missed_relevant_upper_bound  # noqa: E402

T = os.path.join(BASE, "data", "exports", "terminology")
CIT_REC = os.path.join(T, "s4_citation_records.json")
Q_REC = os.path.join(T, "s4_query_records.json")
UNIVERSES = os.path.join(BASE, "data", "exports", "completeness_universes.json")
AUDITS = os.path.join(BASE, "data", "exports", "completeness_audits.json")
DEFAULT_OUT = os.path.join(BASE, "data", "exports", "completeness_labels",
                           "r04_seen.json")

# 历史轨迹（窄 frame，仅上下文）：R03/S3
R03_AUDIT_ID = "pc_001::20260830011713"


def build_s4_found_sets() -> dict:
    """S4 三通道 found sets = S3 全量 + S4 citation/query records（canonical 口径）。"""
    found = build_s3_found_sets()
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


def _stats(rel_ids, found, kb_in_uni, N_pool, n):
    """单口径统计：T/F/U + 四核心数 + LCB（口径 A）。"""
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
        "kb_seen": kb_seen, "kb_missed": kb_missed,
        "f_search": f_search,
        "m_upper_resolved": m_upper_res, "m_upper_ultra": m_upper_ultra,
        "recall_lcb_resolved": round(lcb_res, 4) if lcb_res else None,
        "recall_lcb_ultra": round(lcb_ultra, 4) if lcb_ultra else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", required=True, help="R04 filled labels 路径")
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    labs = json.load(open(args.labels, encoding="utf-8"))["labels"]
    audit_id = json.load(open(args.labels, encoding="utf-8")).get("audit_id", "")
    rel = [l["paper_id"] for l in labs if l.get("label") == "RELEVANT"]
    rel_ids = list(dict.fromkeys(rel))

    # ── universe / audit 元数据（口径 A）──
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
    N_pool = au.get("N_remaining")   # ★ 抽样前排除后池（口径 A）
    assert N_pool is not None, "audit record 缺 N_remaining"
    kb_in_uni = known_relevant & set(uni.get("paper_ids", []))

    # ── paired：同一 sample 双 seen join ──
    found_s3 = build_s3_found_sets()
    found_s4 = build_s4_found_sets()
    st_s3 = _stats(rel_ids, found_s3, kb_in_uni, N_pool, n)   # R04 sample × S3 seen
    st_s4 = _stats(rel_ids, found_s4, kb_in_uni, N_pool, n)   # R04 sample × S4 seen

    # ── 历史轨迹（窄 frame，仅上下文）──
    r03 = next((a for a in audits if a.get("audit_id") == R03_AUDIT_ID), None)
    hist = None
    if r03:
        t3, f3, u3 = (r03.get("n_agent_seen", 0), r03.get("m", 0),
                      r03.get("n_agent_unknown", 0))
        r3 = t3 + f3
        hist = {"resolved_recall": round(t3 / r3, 4) if r3 else None,
                "conservative_recall": round(t3 / (r3 + u3), 4) if r3 + u3 else None,
                "recall_lcb_resolved": r03.get("recall_lcb"),
                "t_f_u": [t3, f3, u3],
                "frame": "narrow(241a7a93def7, n=1572)",
                "note": "历史轨迹上下文，frame 与 R04 宽 frame 不同——"
                        "不与 R04/S4 直接连成纯性能轨迹"}

    # ── 输出 ──
    print("=" * 78)
    print(f"R04 paired S3/S4 audit（audit={audit_id}，宽 frame {universe_id}）")
    print("=" * 78)
    print(f"RELEVANT={len(rel_ids)} | N_pool={N_pool} | n={n} | "
          f"universe={universe_total} | KB∩universe={len(kb_in_uni)}")
    print(f"\n{'metric':<26}{'R04/S3(seen13430)':>18}{'R04/S4(seen19194)':>18}")
    for m, k in (("resolved_recall", "resolved_recall"),
                 ("conservative_recall", "conservative_recall"),
                 ("resolved_miss", "resolved_miss"),
                 ("conservative_miss", "conservative_miss"),
                 ("unknown_rate", "unknown_rate"),
                 ("Recall_LCB", "recall_lcb_resolved")):
        v3, v4 = st_s3[k], st_s4[k]
        s3 = f"{v3:>17.1%}" if isinstance(v3, (int, float)) else f"{'—':>17}"
        s4 = f"{v4:>17.1%}" if isinstance(v4, (int, float)) else f"{'—':>17}"
        print(f"{m:<26}{s3}{s4}")
    print(f"{'T/F/U':<26}{str(st_s3['t_f_u']):>18}{str(st_s4['t_f_u']):>18}")
    print(f"{'miss CI_upper':<26}{st_s3['miss_ci_upper_wilson95']:>17.1%}"
          f"{st_s4['miss_ci_upper_wilson95']:>18.1%}")

    paired_delta = None
    if st_s3["resolved_recall"] is not None and st_s4["resolved_recall"] is not None:
        paired_delta = {
            "d_resolved_recall": round(st_s4["resolved_recall"] - st_s3["resolved_recall"], 4),
            "d_conservative_recall": round(
                (st_s4["conservative_recall"] or 0) - (st_s3["conservative_recall"] or 0), 4),
            "d_unknown_rate": round(
                (st_s4["unknown_rate"] or 0) - (st_s3["unknown_rate"] or 0), 4),
            "note": "同一 R04 fresh sample 上 S4−S3 的配对增量（同 frame 同 sample，"
                    "干净隔离 S4 新增机制效果）",
        }
        print(f"\n=== paired delta (S4−S3, 同 sample 同 frame) ===")
        for k, v in paired_delta.items():
            if k == "note":
                continue
            print(f"  {k:<28}{v:+.1%}")

    if hist:
        print(f"\n=== 历史轨迹（窄 frame，仅上下文）===")
        print(f"  R03/S3: resolved={hist['resolved_recall']:.1%} "
              f"LCB={hist['recall_lcb_resolved']:.1%} {hist['frame']}")

    out = {
        "version": "r04_paired_stats_v1",
        "frozen_at": "2026-08-30",
        "audit_id": audit_id, "universe_id": universe_id,
        "protocol_patch": "R04 frame 升级宽定义(bae4cd5a5dc6, n=5890) + paired S3/S4 "
                          "同 sample 双 seen join——frame 变化不混叠，Δ 为同池配对增量；"
                          "R03/S3 窄 frame 历史保留为上下文，不连成纯性能轨迹",
        "sampling_pool_contract": {
            "universe_total": universe_total,
            "known_relevant_excluded": len(known_relevant),
            "excluded_historical_samples": len(au.get("excluded_papers", [])),
            "sampling_pool_size_before_sample": N_pool,
            "sample_n": n,
            "hypergeom_population_N": N_pool,
            "assertion": "N == frozen_sampling_pool_size（抽样前；口径 A）",
        },
        "paired": {
            "same_sample": True, "same_frame": True,
            "R04_S3": st_s3, "R04_S4": st_s4,
            "delta": paired_delta,
        },
        "historical_R03_S3": hist,
        "candidate_cost": {"S3_seen": 13430, "S4_seen": 19194,
                           "delta": 5764, "growth_rate": round(5764 / 13430, 4)},
        "interpretation": "R04 paired：主对比 = 同一 fresh 宽-frame sample 上 "
                          "Recall(S4 seen) vs Recall(S3 seen)；Δ 干净归因 S4 新增机制；"
                          "R03/S3=83.3%（窄 frame）仅为历史上下文",
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    # ── 回写 audit record ──
    try:
        audits = json.load(open(AUDITS, encoding="utf-8"))
        rec = next((a for a in audits if a.get("audit_id") == audit_id), None)
        if rec:
            rec["status"] = "COMPLETED"
            rec["labels"] = {l["paper_id"]: l.get("label") for l in labs}
            rec["n_relevant"] = len(rel_ids)
            rec["m"] = st_s4["t_f_u"][1]              # S4 口径 resolved miss
            rec["n_agent_seen"] = st_s4["t_f_u"][0]
            rec["n_agent_unknown"] = st_s4["t_f_u"][2]
            rec["uncertain_count"] = sum(1 for l in labs
                                         if l.get("label") == "UNCERTAIN")
            rec["M_upper"] = st_s4["m_upper_resolved"]
            rec["recall_lcb"] = st_s4["recall_lcb_resolved"]
            rec["audit_metric_scope"] = ("SEARCH_S4 paired（同一 fresh 宽-frame sample 上 "
                                         "S3/S4 双 seen join；N=抽样前排除后池，口径 A）")
            rec["search_stats"] = out
            rec["r04_paired_comparison"] = {
                "same_sample": True,
                "R04_S3": st_s3, "R04_S4": st_s4, "delta": paired_delta,
                "historical_R03_S3_narrow_frame": hist,
                "candidate_cost": out["candidate_cost"],
                "note": "主指标 = paired Δ(S4−S3)；R03/S3 窄 frame 仅上下文",
            }
            with open(AUDITS, "w", encoding="utf-8") as f:
                json.dump(audits, f, ensure_ascii=False, indent=1)
            print(f"[OK] audit record 已更新: {audit_id} -> COMPLETED（r04_paired_comparison）")
    except Exception as e:
        print(f"[WARN] audit record 更新失败: {e}")

    print(f"\n[OK] written: {args.out}")


if __name__ == "__main__":
    main()
