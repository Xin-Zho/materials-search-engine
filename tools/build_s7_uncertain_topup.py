#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/build_s7_uncertain_topup.py — UNCERTAIN 簇补抽（2026-09-07）

C-008/014/018 抽样 R+U=1/20，326 篇处置悬置。补抽每簇 +20（n=40）收紧估计：
  R+U≥3（≥7.5%≈S6 基准）→ KEEP；≤2 → FAIL（n=40 下 2/40=5% 显著低于基准）
补抽避开已抽 480 keys；abstract 从 Scopus 缓存 doi 桥恢复（同 P0 路线）。
产物：
  s7_community_qa_corpus_uncertain.json —— 60 篇补抽 corpus（blind，同 rubric）
用法：
  .venv\\Scripts\\python.exe tools\\build_s7_uncertain_topup.py
  # 然后跑 QA（labels 单独文件）→ analyze --topup 合并
"""
import datetime
import json
import os
import random
import sqlite3
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from search_engine.identity import scopus_cache_key  # noqa: E402

T = os.path.join(BASE, "data", "exports", "terminology")
CACHE_DB = os.path.join(BASE, "data", "cache", "scopus_cache.db")
MEM = os.path.join(T, "s7_community_memory.json")
CORPUS = os.path.join(T, "s7_community_qa_corpus.json")
RECORDS = os.path.join(T, "s7_execute_query_records.json")
OUT = os.path.join(T, "s7_community_qa_corpus_uncertain.json")
UNCERTAIN_CLUSTERS = ["C-008", "C-014", "C-018"]
TOPUP = 20
SEED_OFFSET = 101   # 与主抽样 seed=7 错开


def main():
    mem = json.load(open(MEM, encoding="utf-8"))
    corpus = json.load(open(CORPUS, encoding="utf-8"))
    already = {p["key"] for p in corpus["papers"]}
    rec = json.load(open(RECORDS, encoding="utf-8"))

    # key -> (title, doi)
    meta = {}
    for w in rec["records_by_group"].values():
        for r in w.get("rows", []):
            k = r.get("key")
            if k and k not in meta:
                meta[k] = {"title": r.get("title") or "", "doi": r.get("doi")}

    # doi -> abstract（cache 桥）
    conn = sqlite3.connect(CACHE_DB)
    items = []
    n_abs = 0
    for c in mem["clusters"]:
        if c["cluster_id"] not in UNCERTAIN_CLUSTERS:
            continue
        pool = [k for k in c["papers"] if k not in already]
        rng = random.Random(SEED_OFFSET)
        picked = rng.sample(pool, min(TOPUP, len(pool)))
        for k in picked:
            m = meta.get(k, {})
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
            items.append({"key": k, "title": m.get("title", ""),
                          "abstract": ab, "cluster_id": c["cluster_id"]})
    conn.close()

    now = datetime.datetime.now().isoformat(timespec="seconds")
    out = {"role": "S7 UNCERTAIN 簇补抽 corpus（blind）",
           "built_at": now, "rubric_version": "S6_QA_RUBRIC_V1",
           "n": len(items), "n_with_abstract": n_abs,
           "clusters": UNCERTAIN_CLUSTERS,
           "topup_per_cluster": TOPUP,
           "note": "blind：cluster 标注仅存于此容器 key→cluster 映射供聚合，"
                   "不进 QA prompt；每项 papers 仅 {key,title,abstract}",
           "papers": [{"key": p["key"], "title": p["title"],
                       "abstract": p["abstract"]} for p in items],
           "key2cluster": {p["key"]: p["cluster_id"] for p in items},
           }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"[OK] {OUT}")
    print(f"  补抽 {len(items)} 篇（abs {n_abs}），每簇 {TOPUP}，"
          f"已避开主抽样 {len(already)} keys")
    print("→ QA: run_s6_qa.py --mode run "
          "--set s7_community_qa_corpus_uncertain.json "
          "--llm-out s7_community_qa_labels_uncertain.json")


if __name__ == "__main__":
    main()
