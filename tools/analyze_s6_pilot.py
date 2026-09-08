#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/analyze_s6_pilot.py — S6 pilot 分层分析（2026-09-04 用户检查点 2）。

用户固定输出四组（判定 S6 主假设——anchor reversal 是否有效，而非仅看个别强 query）：
  1. per-query : new_unique(vs S5) / new_relevant / relevant_yield / total_hits
  2. family    : A/B/C/D 分层汇总
  3. anchor    : anchor-free vs shrinkage-anchor 汇总（S6 最关键内部结果）
  4. overlap   : query 两两 canonical result overlap（post-search 语义重复检测；
                 ≠ token Jaccard——文本不同也可能搜同一批论文）

relevance labels（可选）：
  - QA 环节产出（candidate QA，用户在 analyze 前跑）→ 本工具读入后计算 new_relevant。
  - label 文件格式：{ "<canonical_key>": "RELEVANT"|"UNCERTAIN"|"IRRELEVANT", ... }
    （全局 keyed；一篇论文只判一次，不因命中 query 而异——防循环归因）
  - 无 label 时 relevant 列输出 n/a，其余指标照常。

⚠️ QA 判定口径铁律（写死提醒，不由本工具执行）：
  relevant 判定禁用 shrinkage/shrink/contraction 核心词表（S4/S5 PROBLEM 词）。
  S6 测的正是"无 shrinkage 字面但实质研究聚合后果"的社区——用 shrinkage 词表判 relevant
  会把 anchor-free query 系统性打零分，实验自证失败。须用 domain rubric
  （frame: 光固化聚合收缩/应力及其可观察后果，dental/SLA/optics/packaging 等应用语境）。

用法：
  python tools/analyze_s6_pilot.py                                  # 无 label（只报 new_unique 层）
  python tools/analyze_s6_pilot.py --labels <qa_out.json>           # 带 relevance labels
输出：data/exports/terminology/s6_pilot_analysis.json（+ 控制台表）
"""
import argparse
import json
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T = os.path.join(BASE, "data", "exports", "terminology")
CONFIG = os.path.join(T, "s6_bridge_queries.json")
RECORDS = os.path.join(T, "s6_pilot_query_records.json")
DEFAULT_OUT = os.path.join(T, "s6_pilot_analysis.json")


def jaccard(a: set, b: set) -> float:
    u = a | b
    return len(a & b) / len(u) if u else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=CONFIG)
    ap.add_argument("--records", default=RECORDS)
    ap.add_argument("--labels", default=None,
                    help="QA relevance label 文件：{key: RELEVANT|UNCERTAIN|IRRELEVANT}")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--overlap-threshold", type=float, default=0.30,
                    help="打印 pairwise overlap >= 阈值的 pair")
    args = ap.parse_args()

    cfg = json.load(open(args.config, encoding="utf-8"))
    prov = {a["action_id"]: a for a in cfg["actions"]}
    rec = json.load(open(args.records, encoding="utf-8"))
    assert rec["round"] == "S6_PILOT" and rec["status"] == "DEVELOPMENT_PILOT"
    rbq = rec["records_by_query"]

    labels = None
    if args.labels:
        labels = json.load(open(args.labels, encoding="utf-8"))
        # 兼容两种容器：顶层 dict 或 {"labels": {...}}
        if "labels" in labels and isinstance(labels["labels"], dict):
            labels = labels["labels"]
        n_lab = sum(1 for v in labels.values()
                    if v in ("RELEVANT", "UNCERTAIN", "IRRELEVANT"))
        n_rel = sum(1 for v in labels.values() if v == "RELEVANT")
        print(f"[labels] {n_lab} 篇已判（RELEVANT {n_rel}）")

    rows = []
    for aid, r in rbq.items():
        p = prov[aid]
        keys = {row["key"] for row in r["rows"] if row["key"]}
        rel = unc = None
        if labels is not None:
            rel = sum(1 for k in keys if labels.get(k) == "RELEVANT")
            unc = sum(1 for k in keys if labels.get(k) == "UNCERTAIN")
        rows.append({
            "action_id": aid, "family": p["family"], "domain": p["domain"],
            "strategy": p["strategy"],
            "contains_shrinkage_anchor": p["contains_shrinkage_anchor"],
            "exploratory": p.get("exploratory", False),
            "query_string": p["query_string"],
            "total_hits": r.get("total_hits"),
            "unique_returned": r.get("unique_returned"),
            "new_vs_S5": r.get("new_vs_S5"),
            "new_relevant": rel, "new_uncertain": unc,
            "depth_saturated": r.get("depth_saturated"),
            "error": p.get("error"),
        })

    # ── 1. per-query 表 ──
    print("\n=== 1. per-query ===")
    hdr = f"{'id':<10}{'fam':<4}{'anc':<6}{'exp':<4}{'hits':>7}{'uniq':>6}{'newS5':>7}{'rel':>5}{'yield':>7}  query"
    print(hdr)
    for r in sorted(rows, key=lambda x: (x["family"], x["action_id"])):
        anc = "anchor" if r["contains_shrinkage_anchor"] else "free"
        exp = "EXP" if r["exploratory"] else ""
        y = f"{r['new_relevant'] / r['new_vs_S5']:>6.1%}" if (
            labels is not None and r["new_vs_S5"]) else "   n/a"
        rel_s = str(r["new_relevant"]) if r["new_relevant"] is not None else "  -"
        print(f"{r['action_id']:<10}{r['family']:<4}{anc:<6}{exp:<4}"
              f"{r['total_hits'] or 0:>7}{r['unique_returned'] or 0:>6}"
              f"{r['new_vs_S5'] or 0:>7}{rel_s:>5}{y:>7}  {r['query_string'][:44]}")

    # ── 2/3. family & anchor 分层 ──
    def agg(sub):
        d = {
            "n": len(sub),
            "total_hits": sum(r["total_hits"] or 0 for r in sub),
            "new_vs_S5": sum(r["new_vs_S5"] or 0 for r in sub),
        }
        if labels is not None:
            d["new_relevant"] = sum(r["new_relevant"] or 0 for r in sub)
            d["relevant_yield"] = round(d["new_relevant"] / d["new_vs_S5"], 4) \
                if d["new_vs_S5"] else None
        return d

    fam_agg = {}
    for fam in "ABCD":
        sub = [r for r in rows if r["family"] == fam]
        fam_agg[fam] = agg(sub)
    core = [r for r in rows if not r["exploratory"]]      # 主测试（exploratory 排除）
    expl = [r for r in rows if r["exploratory"]]
    anchor_free = [r for r in core if not r["contains_shrinkage_anchor"]]
    anchor_ed = [r for r in core if r["contains_shrinkage_anchor"]]

    print("\n=== 2. family 分层（A/B/C/D）===")
    print(f"{'fam':<6}{'n':>3}{'hits':>9}{'newS5':>8}{'rel':>6}{'yield':>8}")
    for fam in "ABCD":
        a = fam_agg[fam]
        rel_s = str(a.get("new_relevant")) if "new_relevant" in a else "-"
        y = f"{a['relevant_yield']:.1%}" if a.get("relevant_yield") is not None else "n/a"
        print(f"{fam:<6}{a['n']:>3}{a['total_hits']:>9}{a['new_vs_S5']:>8}{rel_s:>6}{y:>8}")

    print("\n=== 3. anchor-free vs shrinkage-anchor（主测试，exploratory 已排除）===")
    for name, sub in (("anchor-free", anchor_free), ("shrinkage-anchor", anchor_ed)):
        a = agg(sub)
        rel_s = str(a.get("new_relevant")) if "new_relevant" in a else "-"
        y = f"{a['relevant_yield']:.1%}" if a.get("relevant_yield") is not None else "n/a"
        print(f"  {name:<18} n={a['n']:>2} | Σnew_S5={a['new_vs_S5']:>5} "
              f"| Σrel={rel_s:>4} | overall_yield={y}")
    if labels is not None and (anchor_free or anchor_ed):
        a, b = agg(anchor_free), agg(anchor_ed)
        if a["new_vs_S5"] and b["new_vs_S5"]:
            dy = (a["relevant_yield"] or 0) - (b["relevant_yield"] or 0)
            print(f"  → Δyield (free − anchor) = {dy:+.1%}  ← S6 主结论判据（须配 R06 验证）")
    print(f"  exploratory 3 条不参与 family 归因（{', '.join(r['action_id'] for r in expl)}）")

    # ── 4. pairwise canonical overlap ──
    key_sets = {r["action_id"]: {row["key"] for row in rbq[r["action_id"]]["rows"]
                                 if row["key"]} for r in rows}
    print(f"\n=== 4. pairwise canonical overlap（Jaccard on retrieved keys，阈值 "
          f">{args.overlap_threshold:.0%}）===")
    pairs = []
    ids = sorted(key_sets)
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            ov = jaccard(key_sets[ids[i]], key_sets[ids[j]])
            if ov >= args.overlap_threshold:
                pairs.append((ids[i], ids[j], ov))
    pairs.sort(key=lambda x: -x[2])
    if pairs:
        for a, b, ov in pairs:
            print(f"  {a} × {b}: Jaccard={ov:.1%}")
    else:
        print("  （无 pair 超过阈值——17 条 query 检索结果彼此低重叠）")
    print(f"  pairs 超阈值: {len(pairs)} / {len(ids) * (len(ids) - 1) // 2}")

    # ── 落盘 ──
    out = {
        "version": "s6_pilot_analysis_v1",
        "round": "S6_PILOT",
        "status": "DEVELOPMENT_ANALYSIS",
        "per_query": rows,
        "family_agg": fam_agg,
        "anchor_agg": {
            "anchor_free": agg(anchor_free),
            "shrinkage_anchor": agg(anchor_ed),
            "core_note": "主测试 = 全部非 exploratory 条；exploratory 不参与 family/anchor 归因",
            "exploratory_actions": [r["action_id"] for r in expl],
        },
        "overlap": {
            "metric": "Jaccard on canonical retrieved keys（post-search 语义重复，非 token）",
            "threshold": args.overlap_threshold,
            "pairs_over_threshold": [{"q1": a, "q2": b, "jaccard": round(ov, 4)}
                                     for a, b, ov in pairs],
        },
        "qa_discipline": "relevant 判定须用 domain rubric（禁 shrinkage 词表——防 anchor-free "
                         "系统性打零分）；label 文件为全局 keyed 单判",
    }
    json.dump(out, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n[OK] analysis: {args.out}")


if __name__ == "__main__":
    main()
