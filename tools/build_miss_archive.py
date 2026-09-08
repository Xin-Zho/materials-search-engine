#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/build_miss_archive.py — S1→S4 各轮 audit miss 统一归档（2026-08-31 用户定）。

整合四轮 miss 成单一文件，供 Step 7 Cross-Frame Generalization Diagnosis 使用：
  R01（S0 baseline, audit pc_001::20260829043656, m=95）
  R02（S2 阶段, s3_residual_misses.json, 70）——S3 修复输入
  R03（S3 阶段, s4_residual_misses.json, 38 raw/37 canonical）——S4 修复输入
  R04（S4 阶段, r04_misses.json, 26 confirmed FALSE）——Step 7 输入

统一条目：{paper_id(WID), title, year, doi}
每轮记录：audit_id / search_version / definition / n / source_file。

只读，不重算。输出：
  data/exports/terminology/miss_archive_s1_s4.json

用法：
  python tools/build_miss_archive.py [--out <path>]
"""
import argparse
import datetime
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T = os.path.join(BASE, "data", "exports", "terminology")
EX = os.path.join(BASE, "data", "exports")
DEFAULT_OUT = os.path.join(T, "miss_archive_s1_s4.json")

SRC = [
    {
        "round": "R01",
        "search_version": "S0 baseline（Audit R01）",
        "audit_id": "pc_001::20260829043656",
        "definition": "R01 RELEVANT ∧ Seen_S0=FALSE（m=95，Recall_LCB=0.0698 PROVISIONAL）",
        "file": os.path.join(EX, "audit_miss_diagnostics.json"),
        "list_key": "misses",
        "fields": {"paper_id": "paper_id", "title": "title", "year": "year",
                   "doi": ("evidence", "identity_evidence", "doi")},
        "note": "Terminology Repair（S1）输入；95 miss → TermFamily(113) → S1_FINAL=19",
    },
    {
        "round": "R02",
        "search_version": "S2（depth=1000）",
        "audit_id": "pc_001::20260829130129",
        "definition": "R02 RELEVANT ∧ Seen_S2=FALSE（residual，S3 修复输入）",
        "file": os.path.join(T, "s3_residual_misses.json"),
        "list_key": "misses",
        "fields": {"paper_id": "wid", "title": "title", "year": "year", "doi": "doi"},
        "note": "R02 70 篇 residual → S3 citation-first + query repair 输入",
    },
    {
        "round": "R03",
        "search_version": "S3（citation 16 seeds + query 7）",
        "audit_id": "pc_001::20260830011713",
        "definition": "R03 RELEVANT ∧ Seen_S3=FALSE（38 raw / 37 canonical，S4 修复输入）",
        "file": os.path.join(T, "s4_residual_misses.json"),
        "list_key": "misses",
        "fields": {"paper_id": "wid", "title": "title", "year": "year", "doi": "doi"},
        "note": "S4 diagnosis 基础（37 canonical；W7110794929=.s001 补充材料记录 duplicate）",
    },
    {
        "round": "R04",
        "search_version": "S4（diverse citation 64 + quality-gated query 18, seen=19194）",
        "audit_id": "pc_001::20260830155226",
        "definition": "R04 RELEVANT ∧ Seen_S4=FALSE（confirmed FALSE，宽 frame bae4cd5a5dc6）",
        "file": os.path.join(T, "r04_misses.json"),
        "list_key": "misses",
        "fields": {"paper_id": "paper_id", "title": "title", "year": "year", "doi": "doi"},
        "note": "Step 7 Cross-Frame Generalization Diagnosis 输入（26 篇全 s3_seen=FALSE）",
    },
]


def _get(entry, path):
    """按字段路径取值（支持嵌套 tuple）。"""
    if isinstance(path, tuple):
        cur = entry
        for p in path:
            cur = cur.get(p) if isinstance(cur, dict) else None
            if cur is None:
                return None
        return cur
    return entry.get(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    rounds = []
    total = 0
    print("=" * 70)
    print("S1→S4 miss archive 构建")
    print("=" * 70)
    for s in SRC:
        data = json.load(open(s["file"], encoding="utf-8"))
        entries = data[s["list_key"]]
        miss = []
        for e in entries:
            pid = _get(e, s["fields"]["paper_id"])
            title = _get(e, s["fields"]["title"]) or ""
            year = _get(e, s["fields"]["year"])
            doi = _get(e, s["fields"]["doi"]) or ""
            miss.append({"paper_id": pid, "title": title,
                         "year": year, "doi": doi})
        rounds.append({
            "round": s["round"],
            "search_version": s["search_version"],
            "audit_id": s["audit_id"],
            "definition": s["definition"],
            "source_file": os.path.relpath(s["file"], BASE),
            "n": len(miss),
            "note": s["note"],
            "misses": miss,
        })
        total += len(miss)
        n_doi = sum(1 for m in miss if m["doi"])
        n_year = sum(1 for m in miss if m["year"])
        print(f"  {s['round']}: n={len(miss):>3} | doi={n_doi:>3} | year={n_year:>3}")

    out = {
        "version": "miss_archive_s1_s4_v1",
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "purpose": "S1→S4 各轮 audit miss 统一归档——Step 7 Cross-Frame Generalization "
                   "Diagnosis（R03 37 vs R04 26 分布对比）输入；每轮定义独立，不可跨轮混算",
        "n_rounds": len(rounds),
        "total_misses": total,
        "rounds": rounds,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n[OK] {args.out} | total={total}")
    for r in rounds:
        print(f"  {r['round']}: {r['n']} misses → {r['source_file']}")


if __name__ == "__main__":
    main()
