#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/r04_failure_modes.py — 26 篇 miss 的 failure-mode 汇总（2026-08-31 用户定）。

不新增模块、不重算检索——只读 r04_miss_found_similarity.json（cross_audit_miss_similarity
完整版产物），对每篇 miss 的 top1 nearest found 做层间断裂统计，回答：
  "到底哪种 层之间的断裂 最常出现？"

每 miss 取 top1 found 的四层标签（PROPERTY/STRUCTURE/FORMULATION/APPLICATION，
HIGH/MED/LOW/N/A）+ citation 距离（1-hop/2-hop/disconnected）：
  1. 关联层模式：HIGH|MED 的层集合组合分布（如 {PROPERTY, APPLICATION} → PROP+APP）
  2. 断裂对统计：6 对层，一侧强（HIGH|MED）另一侧弱（LOW|N/A）的 miss 数
  3. citation 状态：connected（1/2-hop）vs disconnected
  4. 结论：最主要断裂 → 指导对现有 query generator 的最小修改方向

输出：
  data/exports/terminology/r04_miss_failure_modes.json

用法：
  python tools/r04_failure_modes.py [--in <similarity.json>] [--out <path>]
"""
import argparse
import datetime
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T = os.path.join(BASE, "data", "exports", "terminology")
DEFAULT_IN = os.path.join(T, "r04_miss_found_similarity.json")
DEFAULT_OUT = os.path.join(T, "r04_miss_failure_modes.json")

LAYERS = ["PROPERTY", "STRUCTURE", "FORMULATION", "APPLICATION"]
PAIRS = [("PROPERTY", "STRUCTURE"), ("PROPERTY", "FORMULATION"),
         ("PROPERTY", "APPLICATION"), ("STRUCTURE", "FORMULATION"),
         ("STRUCTURE", "APPLICATION"), ("FORMULATION", "APPLICATION")]

STRONG = {"HIGH", "MED"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="fin", default=DEFAULT_IN)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    data = json.load(open(args.fin, encoding="utf-8"))
    results = data["results"]

    # ── 1. 关联层模式 ──
    pattern_cnt = {}
    per_miss = []
    for r in results:
        t1 = r["nearest"][0] if r["nearest"] else {}
        labels = t1.get("sim_labels", {})
        strong_layers = [L for L in LAYERS if labels.get(L) in STRONG]
        pattern = "+".join(strong_layers) if strong_layers else "ALL_WEAK"
        pattern_cnt[pattern] = pattern_cnt.get(pattern, 0) + 1
        cit = t1.get("citation", "SKIP")
        per_miss.append({
            "miss_paper_id": r["miss_paper_id"],
            "miss_title": r["miss_title"],
            "pattern": pattern,
            "strong_layers": strong_layers,
            "labels": labels,
            "citation": cit,
        })

    # ── 2. 断裂对统计 ──
    break_cnt = {f"{x}↔{y}": 0 for x, y in PAIRS}
    break_detail = {f"{x}↔{y}": [] for x, y in PAIRS}
    for r, pm in zip(results, per_miss):
        labels = pm["labels"]
        for x, y in PAIRS:
            sx, sy = labels.get(x), labels.get(y)
            sx_strong = sx in STRONG
            sy_strong = sy in STRONG
            if sx_strong and not sy_strong:
                break_cnt[f"{x}↔{y}"] += 1
                break_detail[f"{x}↔{y}"].append(
                    {"miss": r["miss_paper_id"], "title": pm["miss_title"][:60],
                     f"{x}": sx, f"{y}": sy})
            elif sy_strong and not sx_strong:
                break_cnt[f"{x}↔{y}"] += 1
                break_detail[f"{x}↔{y}"].append(
                    {"miss": r["miss_paper_id"], "title": pm["miss_title"][:60],
                     f"{x}": sx, f"{y}": sy})

    # ── 3. citation 状态 ──
    from collections import Counter
    cit_cnt = Counter(pm["citation"] for pm in per_miss)
    n_connected = sum(1 for pm in per_miss if pm["citation"] in ("1-hop", "2-hop"))
    n_disconnected = sum(1 for pm in per_miss if pm["citation"] == "disconnected")

    # ── 4. 结论：最主要断裂 ──
    ranked = sorted(break_cnt.items(), key=lambda kv: -kv[1])
    top_break = ranked[0] if ranked else None
    n = len(results)
    conclusion = (
        f"主要断裂 = {top_break[0]}（{top_break[1]}/{n}）——"
        f"修复方向：现有 TERM/query generation 的该层组合能力不足"
        if top_break and top_break[1] > 0 else "无显著层间断裂")

    print("=" * 72)
    print("R04 26 miss failure-mode 汇总（top1 nearest found）")
    print("=" * 72)
    print("\n=== 关联层模式分布（HIGH|MED 层集合）===")
    for p, c in sorted(pattern_cnt.items(), key=lambda kv: -kv[1]):
        print(f"  {p:<28} {c:>2}/{n}")
    print(f"\n=== 断裂对统计（一侧强 / 另一侧弱）===")
    for pair, c in ranked:
        print(f"  {pair:<28} {c:>2}/{n}")
    print(f"\n=== citation 状态 ===")
    print(f"  connected(1/2-hop): {n_connected}/{n} | disconnected: {n_disconnected}/{n}")
    print(f"\n=== 结论 ===")
    print(f"  {conclusion}")

    out = {
        "version": "r04_miss_failure_modes_v1",
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "source": os.path.relpath(args.fin, BASE),
        "n_misses": n,
        "pattern_distribution": dict(sorted(pattern_cnt.items(), key=lambda kv: -kv[1])),
        "break_pairs": dict(ranked),
        "break_details": break_detail,
        "citation": dict(cit_cnt),
        "citation_connected": n_connected,
        "citation_disconnected": n_disconnected,
        "conclusion": conclusion,
        "per_miss": per_miss,
        "note": "断裂对 = 该对层一侧 HIGH|MED 另一侧 LOW|N/A；ALL_WEAK = 四层均无强关联；"
                "诊断特征非系统 ontology",
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n[OK] {args.out}")


if __name__ == "__main__":
    main()
