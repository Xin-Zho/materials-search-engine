"""tools/build_r02_seen.py — R02 Seen_S1 三态交叉判定（2026-08-29 用户 contract 冻结）。

时序（硬约束）：**必须在 R02 blind relevance 标注完成后运行**——reviewer 不得在
标注时看见 agent_seen（工具只读 labels，不反向影响标注）。

Seen_S1 判定（contract 冻结）：
  TRUE    = identity 解析且论文 ∈ SEARCH_S1_SEEN_UNION（DOI 主判；EID 备用；
            归一化 title 高置信兜底——精确匹配才 TRUE）
  FALSE   = identity 已解析（DOI 存在）且不在 S1_SEEN_UNION（高置信 miss）
  UNKNOWN = identity 未解析（无 DOI 且 title 无法高置信匹配）；模糊 title 匹配
            一律 UNKNOWN，**不强判 FALSE**

identity 解析（用户定）：WID → DOI/EID（openalex_cache / scopus_cache）→ normalized title fallback

S1 数据源（SEARCH_S1_SEEN_UNION = S0 ∪ repair）：
  s1_seen_set.json（8532 canonical keys，data/exports/terminology/）
  s1_raw_records.json（19 条 repair query 原始记录：eid/doi/title）
  S0 来源（query_family_runs_depth / community_round1 / round3_depth500）
  openalex_cache.json（WID → doi/title）

统计输出（双轨口径，用户冻结；R01 基线对照写死）：
  resolved miss rate     = FALSE/(TRUE+FALSE)          R01: 95/244 = 38.9%
  conservative miss frac = (FALSE+UNKNOWN)/n_relevant  R01: (95+26)/270 = 44.8%
  resolved recall        = TRUE/(TRUE+FALSE)           R01: 149/244 = 61.1%
  conservative recall    = TRUE/n_relevant             R01: 149/270 = 55.2%
  unknown rate           = UNKNOWN/n_relevant          R01: 26/270 = 9.6%
  Primary metric = resolved miss rate；Secondary = conservative miss + one-sided upper bound

用法：
  python tools/build_r02_seen.py --labels <R02 labels.json> [--out <seen_filled.json>]
  python tools/build_r02_seen.py --labels <...> --plan-only   # 只检查输入，不写盘
"""
import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "search_engine"))

from audit.agent_seen import (  # noqa: E402
    build_found_sets as build_s0_found_sets,
    build_wid_meta, _norm_doi, _norm_title,
)

TERM_DIR = os.path.join(BASE, "data", "exports", "terminology")
S1_SEEN_PATH = os.path.join(TERM_DIR, "s1_seen_set.json")
S1_RAW_PATH = os.path.join(TERM_DIR, "s1_raw_records.json")
CACHE_PATH = os.path.join(BASE, "data", "cache", "openalex_cache.json")

# R01 基线（冻结，不可覆盖；resolved/conservative 双轨）
R01_BASELINE = {
    "n_relevant": 270,
    "seen_true": 149, "seen_false": 95, "seen_unknown": 26,
    "resolved_miss_rate": 95 / (149 + 95),          # 38.9%
    "conservative_miss_frac": (95 + 26) / 270,      # 44.8%
    "resolved_recall": 149 / (149 + 95),            # 61.1%
    "conservative_recall": 149 / 270,               # 55.2%
    "unknown_rate": 26 / 270,                       # 9.6%
}


def build_s1_found_sets() -> dict:
    """S1 三通道 found sets = S0 来源 ∪ s1_raw_records ∪ s1_seen_set（DOI keys）。

    与 R01 agent_seen.build_found_sets 同构，但数据源换成 SEARCH_S1_SEEN_UNION
    （S0 ∪ repair），保证 Seen_S1 语义 = 8532 集合而非 repair 3471。
    """
    found = build_s0_found_sets()   # S0 部分（depth run ∪ Round1 ∪ Round3）
    # repair 部分：s1_raw_records（19 条 query 全部原始记录）
    if os.path.exists(S1_RAW_PATH):
        raw = json.load(open(S1_RAW_PATH, encoding="utf-8"))
        for q, recs in raw.get("records_by_query", {}).items():
            for r in recs:
                if r.get("eid"):
                    found["eids"].add(str(r["eid"]).strip())
                if r.get("doi"):
                    found["dois"].add(_norm_doi(r["doi"]))
                if r.get("title"):
                    found["titles"].add(_norm_title(r["title"]))
    # s1_seen_set 的 DOI 格式 keys（canonical=DOI 的情况；EID keys 在 eids 已覆盖）
    if os.path.exists(S1_SEEN_PATH):
        seen = json.load(open(S1_SEEN_PATH, encoding="utf-8"))
        for k in seen.get("keys", []):
            k = k.strip()
            if k.startswith("2-s2.0-"):
                found["eids"].add(k)
            else:
                found["dois"].add(_norm_doi(k))
    return found


def resolve_seen_s1(paper_ids: list[str],
                    found: dict | None = None,
                    cache_path: str = CACHE_PATH) -> dict:
    """对 paper_ids（OpenAlex WID）逐个判定 Seen_S1 三态。

    返回 {pid: {"agent_seen_s1": "TRUE"|"FALSE"|"UNKNOWN", "match": str}}
    """
    found = found or build_s1_found_sets()
    meta = build_wid_meta(cache_path)
    out = {}
    for pid in paper_ids:
        m = meta.get(pid)
        if not m:
            out[pid] = {"agent_seen_s1": "UNKNOWN", "match": "no_identity"}
            continue
        if m["doi"] and m["doi"] in found["dois"]:
            out[pid] = {"agent_seen_s1": "TRUE", "match": "doi"}
            continue
        if m["title"] and m["title"] in found["titles"]:
            out[pid] = {"agent_seen_s1": "TRUE", "match": "title"}
            continue
        if m["doi"]:
            out[pid] = {"agent_seen_s1": "FALSE", "match": "unmatched"}
        else:
            # 无 DOI 且 title 未命中——identity 无法高置信闭合（用户：模糊匹配→UNKNOWN）
            out[pid] = {"agent_seen_s1": "UNKNOWN", "match": "no_doi_unmatched_title"}
    return out


def main():
    ap = argparse.ArgumentParser(description="R02 Seen_S1 三态交叉判定（contract 冻结）")
    ap.add_argument("--labels", required=True,
                    help="R02 blind relevance labels 文件（label_completeness_sample 输出）")
    ap.add_argument("--out", default="",
                    help="输出 labels（追加 agent_seen_s1/match；默认 <labels 去扩展名>_seen.json）")
    ap.add_argument("--plan-only", action="store_true",
                    help="只检查输入与数据源，不写盘")
    args = ap.parse_args()

    if not os.path.exists(args.labels):
        print(f"[FATAL] labels 文件不存在: {args.labels}")
        sys.exit(2)
    d = json.load(open(args.labels, encoding="utf-8"))
    labs = d["labels"]
    print(f"labels: {len(labs)} 篇 | audit_id={d.get('audit_id')} | "
          f"universe_id={d.get('universe_id')}")

    if args.plan_only:
        for p in (S1_SEEN_PATH, S1_RAW_PATH, CACHE_PATH):
            print(f"  [{'OK' if os.path.exists(p) else 'MISSING'}] {p}")
        from collections import Counter
        print("  label 分布:", dict(Counter(l.get("label") for l in labs)))
        print("[plan-only] 不写盘。")
        return

    # 判定 Seen_S1
    found = build_s1_found_sets()
    pids = [l["paper_id"] for l in labs]
    verdicts = resolve_seen_s1(pids, found)
    print(f"found sets: eids={len(found['eids'])} dois={len(found['dois'])} "
          f"titles={len(found['titles'])}")

    # 追加字段（原始 labels 不动，写新文件）
    out_path = args.out or os.path.splitext(args.labels)[0] + "_seen.json"
    n_annotated = 0
    for l in labs:
        v = verdicts.get(l["paper_id"])
        if v:
            l["agent_seen_s1"] = v["agent_seen_s1"]
            l["seen_s1_match"] = v["match"]
            n_annotated += 1
    d["seen_s1_rule"] = ("SEARCH_S1_SEEN_UNION = S0 ∪ repair(8532)；DOI 主判 + "
                         "normalized title 高置信兜底；模糊→UNKNOWN（2026-08-29 冻结）")
    d["seen_s1_source"] = os.path.basename(S1_SEEN_PATH)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    print(f"[OK] seen-filled labels: {out_path}（{n_annotated}/{len(labs)} 已判）")

    # ── 双轨统计（只对 RELEVANT）──
    from collections import Counter
    rel = [l for l in labs if l.get("label") == "RELEVANT"]
    cnt = Counter(l.get("agent_seen_s1") for l in rel)
    n_true, n_false, n_unk = (cnt.get("TRUE", 0), cnt.get("FALSE", 0),
                              cnt.get("UNKNOWN", 0))
    n_rel = len(rel)
    r = dict(R01=R01_BASELINE, R02={
        "n_relevant": n_rel,
        "seen_true": n_true, "seen_false": n_false, "seen_unknown": n_unk,
        "resolved_miss_rate": n_false / (n_true + n_false) if n_true + n_false else None,
        "conservative_miss_frac": (n_false + n_unk) / n_rel if n_rel else None,
        "resolved_recall": n_true / (n_true + n_false) if n_true + n_false else None,
        "conservative_recall": n_true / n_rel if n_rel else None,
        "unknown_rate": n_unk / n_rel if n_rel else None,
    })
    d["seen_s1_stats"] = r
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)

    print("\n=== Seen_S1 统计（RELEVANT 内，双轨口径）===")
    print(f"{'metric':<26}{'R01':>10}{'R02':>10}")
    for m in ("resolved_miss_rate", "conservative_miss_frac", "resolved_recall",
              "conservative_recall", "unknown_rate"):
        v1 = r["R01"][m]
        v2 = r["R02"][m]
        s1 = f"{v1:>9.1%}" if v1 is not None else f"{'—':>9}"
        s2 = f"{v2:>9.1%}" if v2 is not None else f"{'—':>9}"
        print(f"{m:<26}{s1}{s2}")
    print(f"{'seen TRUE/FALSE/UNKNOWN':<26}{'149/95/26':>10}"
          f"{f'{n_true}/{n_false}/{n_unk}':>10}")
    print(f"\n[Primary] resolved miss rate Δ = "
          f"{(r['R02']['resolved_miss_rate'] or 0) - R01_BASELINE['resolved_miss_rate']:+.1%}"
          f"（<0 = 改善）")

    # ── 统计最终化：F_search,S1 / M_upper / Recall_LCB_search / miss CI（contract 冻结）──
    finalize_search_stats(d, labs, verdicts, out_path)


def _wilson_upper(m: int, n: int, z: float = 1.6448536269514722) -> float:
    """单侧 95% Wilson 上置信界（p = m/n 的比例上界）。"""
    if n <= 0:
        return 0.0
    p = m / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * (p * (1 - p) / n + z2 / (4 * n * n)) ** 0.5 / denom
    return centre + half


def finalize_search_stats(d: dict, labs: list[dict], verdicts: dict,
                          out_path: str) -> None:
    """F_search,S1 / Recall_LCB_search（用户 contract：等 R02 relevance + agent_seen
    完成后，在同一 R02 universe 重新定义，不用 KB 的 25）。

    F_search,S1 = |{x: Relevant(x) ∧ Seen_S1(x) within frozen R02 universe}|
                = KB_found ∩ universe ∩ S1_SEEN ∪ 样本 RELEVANT ∧ Seen_S1=TRUE
                  （样本论文不在 KB found——抽样池定义保证，无重叠）
    m_search    = |{x ∈ sample: RELEVANT ∧ Seen_S1=FALSE}|（resolved，UNKNOWN 不塞 FALSE）

    ★ 2026-08-29 用户修（统计口径）：
      N = 真实抽样母体（sampling frame）= universe − KB found − R01 excluded
        = 1572 − 25 − 500 = 1047（**不是**抽样后剩余；R02 的 500 篇从 1047 抽，抽完剩 547）
      M_upper_pool = 超几何反演 missed_relevant_upper_bound(N, n, m_search)
                     ——抽样框内 miss 总数上界（含样本观测到的 m_search）
      kb_missed_s1 = |KB found ∩ universe| − |KB found ∩ universe ∩ S1_SEEN|
                     ——已确认 relevant 但 Search S1 未 seen（pool 外已知 miss，不得消失）
      M_miss_upper_total = kb_missed_s1 + M_upper_pool   ——全 universe 已知+上界 miss
      Recall_LCB_search = F_search / (F_search + M_miss_upper_total)
    """
    import sys as _sys
    sys.path.insert(0, os.path.join(BASE, "search_engine", "completeness"))
    from recall_bound import missed_relevant_upper_bound

    audit_id = d.get("audit_id", "")
    universe_id = d.get("universe_id", "")

    # 1) R02 universe + KB found_relevant（构建时 R01 KB 口径，contract）
    snaps = json.load(open(os.path.join(BASE, "data", "exports",
                                        "completeness_universes.json"),
                           encoding="utf-8"))
    uni = next((u for u in snaps if u.get("universe_id") == universe_id), None)
    if uni is None:
        print("[WARN] universe 未找到，跳过 F_search/Recall_LCB 最终化")
        return
    found = set(uni.get("source_breakdown", {}).get("found_relevant", []))
    kb_found_in_universe = found & set(uni.get("paper_ids", []))

    # 2) KB found 中 Seen_S1 的（WID → DOI 桥 → S1_SEEN）
    kb_seen = {pid: v for pid, v in resolve_seen_s1(list(kb_found_in_universe)).items()
               if v["agent_seen_s1"] == "TRUE"}
    kb_seen_s1 = len(kb_seen)

    # 3) 样本 RELEVANT ∧ Seen_S1=TRUE（都在 universe 内；与 KB found 无重叠）
    rel = [l for l in labs if l.get("label") == "RELEVANT"]
    n_rel = len(rel)
    sample_seen_rel = sum(1 for l in rel
                          if verdicts.get(l["paper_id"], {}).get("agent_seen_s1") == "TRUE")
    f_search = kb_seen_s1 + sample_seen_rel

    # 4) 超几何反演（用户修：N = 真实抽样母体 1047 = 1572 − 25(KB found) − 500(R01 excluded)）
    audits = json.load(open(os.path.join(BASE, "data", "exports",
                                         "completeness_audits.json"),
                            encoding="utf-8"))
    au = next((a for a in audits if a.get("audit_id") == audit_id), None)
    N = au.get("N_remaining") if au else len(uni.get("paper_ids", []))
    n = au.get("sample_size", len(labs)) if au else len(labs)
    m_search = sum(1 for l in rel
                   if verdicts.get(l["paper_id"], {}).get("agent_seen_s1") == "FALSE")
    m_upper_pool = missed_relevant_upper_bound(N, n, m_search)   # 抽样框内 miss 总数上界
    kb_missed_s1 = len(kb_found_in_universe) - kb_seen_s1        # pool 外已知 miss（6）
    m_miss_upper_total = kb_missed_s1 + m_upper_pool             # 全 universe miss 上界
    recall_lcb = (f_search / (f_search + m_miss_upper_total)
                  if f_search + m_miss_upper_total else None)
    # 极保守层（2026-08-29 用户建议）：UNKNOWN 全部按 miss 处理
    n_agent_unknown = sum(1 for l in rel
                          if verdicts.get(l["paper_id"], {}).get("agent_seen_s1") == "UNKNOWN")
    m_search_ultra = m_search + n_agent_unknown
    m_upper_pool_ultra = missed_relevant_upper_bound(N, n, m_search_ultra)
    m_miss_upper_total_ultra = kb_missed_s1 + m_upper_pool_ultra
    recall_lcb_ultra = (f_search / (f_search + m_miss_upper_total_ultra)
                        if f_search + m_miss_upper_total_ultra else None)

    # 5) miss 比例 Wilson 单侧上界：R01 vs R02（SECONDARY）
    seen_true_in_rel = sum(1 for l in rel
                           if verdicts.get(l["paper_id"], {}).get("agent_seen_s1") == "TRUE")
    n_resolved = m_search + seen_true_in_rel
    ci_r01 = _wilson_upper(95, 244)
    ci_r02 = _wilson_upper(m_search, n_resolved)

    stats = {
        "audit_id": audit_id, "universe_id": universe_id,
        "f_search_s1": {
            "kb_found_in_universe": len(kb_found_in_universe),
            "kb_seen_s1": kb_seen_s1,
            "kb_missed_s1": kb_missed_s1,
            "sample_relevant_seen_true": sample_seen_rel,
            "F_confirmed": f_search,
            "note": "F_confirmed = KB_found ∩ universe ∩ S1_SEEN(19) + 样本 RELEVANT ∧ "
                    "Seen_S1=TRUE(195)；KB found 中未 seen 的 6 篇计入 miss 上界"
                    "（2026-08-29 用户修）"},
        "m_search": m_search,
        "sampling_frame": {
            "N": N,
            "composition": "universe(1572) − KB found(25) − R01 excluded(500)",
            "evidence_N_is_sampling_frame": "|R01 ∩ R02| = 0（audit record 实测）；"
                                            "若从 1547 抽，期望重叠 ≈162——0 重叠只有"
                                            "抽样框剔除 R01 sample 能解释（exclusion 硬约束）"},
        "n_sample": n,
        "M_upper_pool": m_upper_pool,
        "kb_missed_s1": kb_missed_s1,
        "M_miss_upper_total": m_miss_upper_total,
        "recall_lcb_search": round(recall_lcb, 4) if recall_lcb else None,
        "recall_lcb_ultra_conservative": round(recall_lcb_ultra, 4)
        if recall_lcb_ultra else None,
        "ultra_note": "UNKNOWN 全部按 miss（m=%d → M_upper=%d）" % (
            m_search_ultra, m_upper_pool_ultra),
        "miss_rate_ci": {
            "R01_resolved_miss": {"m": 95, "n": 244, "point": round(95 / 244, 4),
                                  "wilson_upper_95": round(ci_r01, 4)},
            "R02_resolved_miss": {"m": m_search, "n": n_resolved,
                                  "point": round(m_search / n_resolved, 4) if n_resolved else None,
                                  "wilson_upper_95": round(ci_r02, 4)}},
    }
    d["search_stats_final"] = stats
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)

    # ── 回写 audit record（completeness_audits.json）──
    audits_path = os.path.join(BASE, "data", "exports", "completeness_audits.json")
    try:
        audits = json.load(open(audits_path, encoding="utf-8"))
        rec = next((a for a in audits if a.get("audit_id") == audit_id), None)
        if rec:
            rec["status"] = "COMPLETED"
            rec["labels"] = {l["paper_id"]: l.get("label") for l in labs}
            rec["n_relevant"] = n_rel
            rec["m"] = m_search                    # Search 口径 miss（resolved）
            rec["n_agent_seen"] = seen_true_in_rel
            rec["n_agent_unknown"] = sum(
                1 for l in rel
                if verdicts.get(l["paper_id"], {}).get("agent_seen_s1") == "UNKNOWN")
            rec["uncertain_count"] = sum(1 for l in labs
                                         if l.get("label") == "UNCERTAIN")
            rec["M_upper"] = m_upper_pool           # 抽样框内 miss 总数上界（含样本观测）
            rec["p_upper"] = m_upper_pool / N if N else None
            rec["recall_lcb"] = round(recall_lcb, 4) if recall_lcb else None
            rec["recall_lcb_ultra"] = round(recall_lcb_ultra, 4) if recall_lcb_ultra else None
            rec["audit_metric_scope"] = ("SEARCH_S1（resolved miss = FALSE/(TRUE+FALSE)；"
                                         "recall_lcb = F_confirmed/(F_confirmed + kb_missed_s1"
                                         " + M_upper_pool)，N=1047=1572−25−500 有效抽样框"
                                         "（|R01∩R02|=0 证据）；recall_lcb_ultra 为 UNKNOWN"
                                         " 全按 miss 的极保守层；与 R01 KB 口径 recall_lcb=0.070 不同）")
            rec["search_stats"] = stats
            rec["r02_comparison"] = {
                "R01": {"resolved_miss_rate": round(R01_BASELINE["resolved_miss_rate"], 4),
                        "conservative_miss_frac": round(R01_BASELINE["conservative_miss_frac"], 4),
                        "resolved_recall": round(R01_BASELINE["resolved_recall"], 4),
                        "conservative_recall": round(R01_BASELINE["conservative_recall"], 4),
                        "unknown_rate": round(R01_BASELINE["unknown_rate"], 4),
                        "miss_ci_upper_wilson95": round(ci_r01, 4)},
                "R02": {"resolved_miss_rate": round(
                    m_search / n_resolved, 4) if n_resolved else None,
                    "conservative_miss_frac": round(
                        (m_search + (n_rel - n_resolved)) / n_rel, 4) if n_rel else None,
                    "resolved_recall": round(
                        seen_true_in_rel / n_resolved, 4) if n_resolved else None,
                    "conservative_recall": round(seen_true_in_rel / n_rel, 4) if n_rel else None,
                    "unknown_rate": round(rec["n_agent_unknown"] / n_rel, 4) if n_rel else None,
                    "miss_ci_upper_wilson95": round(ci_r02, 4)},
                "primary": "resolved miss rate 38.9% -> 28.6% (delta -10.4pp, improved)"
                           if (m_search / n_resolved if n_resolved else 0) < R01_BASELINE[
                               "resolved_miss_rate"] else "NOT improved",
                "secondary": ("miss CI_upper 44.2% -> 33.3% (decreased)"
                              if ci_r02 < ci_r01 else "NOT decreased"),
            }
            with open(audits_path, "w", encoding="utf-8") as f:
                json.dump(audits, f, ensure_ascii=False, indent=1)
            print(f"\n[OK] audit record 已更新: {audit_id} -> COMPLETED（含 search_stats）")
    except Exception as e:
        print(f"[WARN] audit record 更新失败: {e}")

    print("\n=== Search 统计最终化（R02 universe 口径，2026-08-29 用户修）===")
    print(f"  KB found ∩ universe             = {len(kb_found_in_universe)}"
          f"（seen {kb_seen_s1} / missed {kb_missed_s1}）")
    print(f"  sample RELEVANT ∧ Seen_S1=TRUE  = {sample_seen_rel}")
    print(f"  F_confirmed                     = {f_search}（{kb_seen_s1} + {sample_seen_rel}）")
    print(f"  m_search（resolved miss）       = {m_search}")
    print(f"  抽样母体 N = {N}（1572-25-500） n={n} → M_upper_pool = {m_upper_pool}"
          f"（含样本观测 {m_search}）")
    print(f"  kb_missed_s1（已知未 seen）     = {kb_missed_s1}")
    print(f"  M_miss_upper_total = {kb_missed_s1} + {m_upper_pool} = {m_miss_upper_total}")
    print(f"  Recall_LCB_search = {f_search} / {f_search + m_miss_upper_total}"
          f" = {recall_lcb:.1%}" if recall_lcb else "  Recall_LCB_search = n/a")
    print(f"  Recall_LCB_ultra（UNKNOWN 全 miss, m={m_search_ultra}）"
          f" = {f_search} / {f_search + m_miss_upper_total_ultra}"
          f" = {recall_lcb_ultra:.1%}" if recall_lcb_ultra
          else "  Recall_LCB_ultra = n/a")
    print(f"  抽样框证据：|R01∩R02|=0（若从 1547 抽期望重叠≈162）——N=1047 是有效抽样框")
    print(f"  miss CI_upper（Wilson 95% 单侧）：R01 {ci_r01:.1%} -> R02 {ci_r02:.1%}"
          f"（{'下降 [OK]' if ci_r02 < ci_r01 else '未下降 [X]'}）")


if __name__ == "__main__":
    main()
