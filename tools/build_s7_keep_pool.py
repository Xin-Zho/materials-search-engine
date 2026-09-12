#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/build_s7_keep_pool.py — KEEP 池物化（2026-09-07）

簇级裁决冻结（KEEP 6 簇 750 篇）→ 候选池清单。把簇聚合层落到论文粒度：
  每篇 key/title/abstract(doi 桥恢复)/doi/ex_sources/cluster/EX 贡献源。
KEEP 池语义 = S7 execute 打开的 frame 内社区论文（簇级 QA R+U 密度支撑），
下一处置（候选入池/relation 验证/精筛）由上层流程决定——本工具只物化。
输出：
  s7_keep_pool.json  —— 750 篇候选（含 abstract 可直供 QA/精筛）
  keep 池 by EX 源统计（记录 retrieval gain 依据）
"""
import datetime
import json
import os
import sqlite3
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from search_engine.identity import scopus_cache_key  # noqa: E402

T = os.path.join(BASE, "data", "exports", "terminology")
CACHE_DB = os.path.join(BASE, "data", "cache", "scopus_cache.db")
MEM = os.path.join(T, "s7_community_memory.json")
VERDICT = os.path.join(T, "s7_community_verdict.json")
RECORDS = os.path.join(T, "s7_execute_query_records.json")
OUT = os.path.join(T, "s7_keep_pool.json")


def main():
    mem = json.load(open(MEM, encoding="utf-8"))
    verdict = json.load(open(VERDICT, encoding="utf-8"))
    rec = json.load(open(RECORDS, encoding="utf-8"))

    keep_ids = {cid for cid, v in verdict["clusters"].items()
                if v["decision"] == "KEEP"}
    clusters = {c["cluster_id"]: c for c in mem["clusters"]}

    # key -> title/doi/ex_sources（跨 EX 组）
    meta = {}
    for gid, w in rec["records_by_group"].items():
        for r in w.get("rows", []):
            k = r.get("key")
            if not k:
                continue
            m = meta.setdefault(k, {"title": "", "doi": None, "ex": []})
            if r.get("title") and not m["title"]:
                m["title"] = r["title"]
            m["doi"] = m["doi"] or r.get("doi")
            if gid not in m["ex"]:
                m["ex"].append(gid)

    # doi 桥恢复 abstract
    conn = sqlite3.connect(CACHE_DB)
    pool = []
    n_abs = 0
    for cid in sorted(keep_ids):
        c = clusters[cid]
        for k in sorted(c["papers"]):
            m = meta.get(k, {"title": "", "doi": None, "ex": []})
            ab = ""
            doi = (m.get("doi") or "").strip().lower()
            if doi:
                row = conn.execute(
                    "SELECT normalized_json FROM papers WHERE paper_id=?",
                    (scopus_cache_key(doi),)).fetchone()
                if row:
                    try:
                        ab = (json.loads(row[0]).get("abstract") or "").strip()
                    except Exception:
                        ab = ""
            if ab:
                n_abs += 1
            pool.append({"key": k, "title": m["title"], "abstract": ab,
                         "doi": m["doi"], "cluster_id": cid,
                         "ex_sources": m["ex"]})
    conn.close()

    by_ex = {}
    for p in pool:
        for g in p["ex_sources"]:
            by_ex[g] = by_ex.get(g, 0) + 1
    now = datetime.datetime.now().isoformat(timespec="seconds")
    out = {
        "role": "S7 KEEP 池候选物化（簇级裁决 KEEP 6 簇）",
        "built_at": now,
        "verdict_source": os.path.basename(VERDICT),
        "n_papers": len(pool), "n_with_abstract": n_abs,
        "clusters": sorted(keep_ids),
        "by_cluster": {cid: clusters[cid]["size"] for cid in sorted(keep_ids)},
        "by_ex_source": dict(sorted(by_ex.items(), key=lambda x: -x[1])),
        "papers": pool,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"[OK] keep pool: {OUT}")
    print(f"  papers={len(pool)} | with_abstract={n_abs} "
          f"({n_abs / len(pool):.1%})")
    print(f"  clusters: {sorted(keep_ids)}")
    print(f"  by EX source: {dict(sorted(by_ex.items(), key=lambda x: -x[1]))}")


if __name__ == "__main__":
    main()
