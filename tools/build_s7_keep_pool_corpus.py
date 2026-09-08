#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/build_s7_keep_pool_corpus.py — KEEP 池论文级 QA 语料（2026-09-07）

簇级裁决 KEEP 6 簇 750 篇 → 论文级盲评（精筛）。已判 120（簇抽样）跳过，
待判 630 生成 corpus。KEEP 池 R 论文 = S7 execute 的真相关产出（进候选）。
输出：s7_keep_pool_qa_corpus.json（blind {key,title,abstract}，同 RUBRIC_V1）
"""
import datetime
import json
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T = os.path.join(BASE, "data", "exports", "terminology")
POOL = os.path.join(T, "s7_keep_pool.json")
LABELS = [
    os.path.join(T, "s7_community_qa_labels.json"),
    os.path.join(T, "s7_community_qa_labels_uncertain.json"),
]
OUT = os.path.join(T, "s7_keep_pool_qa_corpus.json")


def main():
    pool = json.load(open(POOL, encoding="utf-8"))
    judged = {}
    for f in LABELS:
        d = json.load(open(f, encoding="utf-8"))
        if "labels" in d and isinstance(d["labels"], dict):
            d = d["labels"]
        judged.update(d)
    items = []
    for p in pool["papers"]:
        if p["key"] in judged:
            continue
        items.append({"key": p["key"], "title": p["title"],
                      "abstract": p.get("abstract") or ""})
    n_abs = sum(1 for it in items if it["abstract"].strip())
    now = datetime.datetime.now().isoformat(timespec="seconds")
    out = {"role": "S7 KEEP 池论文级 QA（blind，簇级裁决后精筛）",
           "built_at": now, "rubric_version": "S6_QA_RUBRIC_V1",
           "n": len(items), "n_with_abstract": n_abs,
           "note": "blind：key/title/abstract only",
           "papers": items}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"[OK] {OUT}")
    print(f"  待判 {len(items)} 篇（abs {n_abs}），已跳过已判 {len(judged)} keys")
    print("→ QA: run_s6_qa.py --mode run --set s7_keep_pool_qa_corpus.json "
          "--llm-out s7_keep_pool_qa_labels.json")


if __name__ == "__main__":
    main()
