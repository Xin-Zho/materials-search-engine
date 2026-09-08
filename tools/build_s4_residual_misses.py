#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/build_s4_residual_misses.py — S4 预备：R03 38 个 residual miss 清单（2026-08-30 用户定）。

定义（用户冻结）：R03 RELEVANT ∧ Seen_S3 = FALSE（S3 全链路 still 漏掉的 confirmed miss）。
R03: T/F/U = 189/38/20 → residual 应为 38 篇。

R02 hard tail 35 仅作辅助历史证据，不作为 S4 主要优化目标（用户定）。

输出：data/exports/terminology/s4_residual_misses.json
  [{wid, doi, title, year, abstract, seen_s3, oa_title, oa_year} × 38]

用法：
  python tools/build_s4_residual_misses.py --labels <R03 filled.json> [--out <path>]
"""
import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "tools"))

from build_r03_seen import build_s3_found_sets  # noqa: E402
from build_r02_seen import resolve_seen_s1 as _resolve_seen  # noqa: E402

DEFAULT_OUT = os.path.join(BASE, "data", "exports", "terminology",
                           "s4_residual_misses.json")
OPENALEX_CACHE = os.path.join(BASE, "data", "cache", "openalex_cache.json")
DEFAULT_LABELS = os.path.join(BASE, "data", "exports", "completeness_labels",
                              "pc_001__20260830011713_filled.json")


def load_oa_titles() -> dict:
    """{wid: {title, year}}（OpenAlex 补全 R03 labels 可能缺的 title/abstract）。"""
    meta = {}
    if not os.path.exists(OPENALEX_CACHE):
        return meta
    cache = json.load(open(OPENALEX_CACHE, encoding="utf-8"))
    for q, resp in cache.items():
        for w in resp.get("results", []):
            wid = (w.get("id") or "").replace("https://openalex.org/", "")
            if wid and wid not in meta:
                meta[wid] = {"title": w.get("title"),
                             "year": w.get("publication_year")}
    return meta


def main():
    ap = argparse.ArgumentParser(description="S4 预备：R03 residual miss 清单")
    ap.add_argument("--labels", default=DEFAULT_LABELS,
                    help="R03 filled labels 路径（默认项目；若未 copy 用 Downloads 路径）")
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    if not os.path.exists(args.labels):
        print(f"[FATAL] 缺 {args.labels}")
        sys.exit(2)
    d = json.load(open(args.labels, encoding="utf-8"))
    labs = d["labels"]
    rel = [l for l in labs if l.get("label") == "RELEVANT"]

    found_s3 = build_s3_found_sets()
    v3 = _resolve_seen([l["paper_id"] for l in rel], found_s3)
    oa = load_oa_titles()

    residual = []
    n_false = n_unk = 0
    for l in rel:
        s3 = v3.get(l["paper_id"], {}).get("agent_seen_s1", "UNKNOWN")
        if s3 == "FALSE":
            n_false += 1
        elif s3 == "UNKNOWN":
            n_unk += 1
        if s3 == "FALSE":
            om = oa.get(l["paper_id"], {})
            residual.append({
                "wid": l["paper_id"], "doi": l.get("doi"),
                "title": l.get("title"), "year": l.get("year"),
                "abstract": l.get("abstract"),
                "seen_s3": s3,
                "oa_title": om.get("title"), "oa_year": om.get("year"),
            })
    residual.sort(key=lambda x: x["wid"])

    n = len(rel)
    print(f"R03 RELEVANT = {n} | Seen_S3 FALSE = {n_false} | UNKNOWN = {n_unk}")
    print(f"residual = {len(residual)}（预期 38）")

    out = {
        "version": "s4_residual_misses_v1",
        "frozen_at": "2026-08-30",
        "development_source": "AUDIT_R03（R03 已转 development data）",
        "definition": "R03 RELEVANT ∧ Seen_S3 = FALSE（S3 全链路 confirmed miss）",
        "seen_baseline": "S3_SEEN_SET 13430（canonical 三通道）",
        "r03_t_f_u": {"seen_true": sum(1 for l in rel if v3.get(l["paper_id"], {}).get(
            "agent_seen_s1") == "TRUE"),
            "seen_false": n_false, "seen_unknown": n_unk},
        "count": len(residual),
        "note": "R02 hard tail 35 仅作辅助历史证据，不作为 S4 主要优化目标（用户定 2026-08-30）",
        "misses": residual,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print("\n前 10 篇 residual miss：")
    for r in residual[:10]:
        t = (r.get("oa_title") or r.get("title") or "")
        print(f"  {r['wid']} | {t[:78]}")
    print(f"\n[OK] written: {args.out}")


if __name__ == "__main__":
    main()
