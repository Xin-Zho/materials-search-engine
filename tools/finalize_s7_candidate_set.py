#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/finalize_s7_candidate_set.py — KEEP 池论文级 QA 合并 → 候选集（2026-09-07）

KEEP 池 750 篇（簇级裁决 KEEP 6 簇）全量论文级盲评完成：
  抽样 120（簇 QA）+ 全量补判 630 = 750 labels。
R/U/I 论文级归属 → R1 口径候选池 = RELEVANT ∪ UNCERTAIN（staging 未排除语义）。
KEEP 池 R 密度 vs 全量 2806 密度 = 簇级筛选的富集倍数（S7.3 有效性实证）。
输出：s7_candidate_set.json（750 篇 key/title/label + EX 贡献 + 统计）
"""
import argparse
import datetime
import json
import os
import sys
from collections import Counter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from search_engine.topic_config import (  # noqa: E402
    DEFAULT_TOPIC, is_legacy_topic, resolve_output,
)

T = os.path.join(BASE, "data", "exports", "terminology")
# P0-2: pc001 阶段链产物原址（非 legacy 主题拒绝——该链是 pc001 实验记录，
# 通用候选池合并将由 topic_papers 查询替代，见 P1）
POOL = os.path.join(T, "s7_keep_pool.json")
LABEL_FILES = [
    ("sample", os.path.join(T, "s7_community_qa_labels.json")),
    ("topup", os.path.join(T, "s7_community_qa_labels_uncertain.json")),
    ("full", os.path.join(T, "s7_keep_pool_qa_labels.json")),
]
OUT = os.path.join(T, "s7_candidate_set.json")


def main():
    ap = argparse.ArgumentParser(description="KEEP 池论文级 QA 合并 → 候选集")
    ap.add_argument("--topic", default=None,
                    help="topic_id（默认 v1.0 legacy 主题；非 legacy 禁止——输入耦合 pc001 阶段产物）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只统计不写盘（防误覆盖冻结候选集）")
    ap.add_argument("--out", default=None, help="输出路径覆盖")
    args = ap.parse_args()
    topic = args.topic or DEFAULT_TOPIC
    if not is_legacy_topic(topic):
        raise SystemExit(
            f"[topic] {topic}: 本脚本输入耦合 pc001 阶段 QA 产物（s7_keep_pool + 三份 "
            f"qa_labels）；非 legacy 主题不可用——通用候选池读取 topic_papers 列入 P1。")
    out_path = args.out or OUT

    pool = json.load(open(POOL, encoding="utf-8"))
    labels = {}
    src = {}
    for tag, f in LABEL_FILES:
        d = json.load(open(f, encoding="utf-8"))
        if "labels" in d and isinstance(d["labels"], dict):
            d = d["labels"]
        for k, v in d.items():
            if v in ("RELEVANT", "UNCERTAIN", "IRRELEVANT"):
                labels[k] = v
                src.setdefault(k, tag)
    pool_papers = {p["key"]: p for p in pool["papers"]}
    missing = [k for k in pool_papers if k not in labels]
    assert not missing, f"KEEP 池仍有未判: {missing[:5]}"

    papers_out = []
    for k in sorted(pool_papers):
        p = pool_papers[k]
        papers_out.append({
            "key": k, "title": p["title"], "label": labels[k],
            "label_source": src.get(k), "cluster_id": p["cluster_id"],
            "ex_sources": p["ex_sources"],
        })
    cnt = Counter(p["label"] for p in papers_out)   # 池内 750（勿混入非池 labels）
    cand_r1 = [p for p in papers_out if p["label"] in ("RELEVANT", "UNCERTAIN")]
    cand_r = [p for p in papers_out if p["label"] == "RELEVANT"]
    by_ex_r = Counter()
    for p in cand_r:
        for g in p["ex_sources"]:
            by_ex_r[g] += 1
    now = datetime.datetime.now().isoformat(timespec="seconds")
    out = {
        "role": "S7 candidate set — KEEP 池论文级 QA 终裁",
        "built_at": now,
        "rubric": "S6_QA_RUBRIC_V1",
        "contract": "论文级盲评；R1 口径候选=RELEVANT∪UNCERTAIN；"
                    "R2 口径=仅 RELEVANT（reporting 分流）",
        "labels": dict(cnt),
        "n": len(papers_out),
        "R_rate": round(cnt["RELEVANT"] / len(papers_out), 4),
        "R_plus_U_rate": round((cnt["RELEVANT"] + cnt["UNCERTAIN"])
                               / len(papers_out), 4),
        "enrichment_vs_all": {   # 簇级筛选富集（vs 全量 2806 抽样 R+U 7.4%）
            "keep_pool_R_plus_U": round((cnt["RELEVANT"] + cnt["UNCERTAIN"])
                                        / len(papers_out), 4),
            "all_new_R_plus_U": 0.074,
            "fold": round((cnt["RELEVANT"] + cnt["UNCERTAIN"]) / len(papers_out)
                          / 0.074, 2),
        },
        "candidate_R1": {"n": len(cand_r1), "keys": [p["key"] for p in cand_r1]},
        "candidate_R": {"n": len(cand_r), "keys": [p["key"] for p in cand_r]},
        "by_ex_relevant": dict(sorted(by_ex_r.items(), key=lambda x: -x[1])),
        "by_cluster": dict(sorted(Counter(p["cluster_id"]
                                          for p in papers_out).items())),
        "papers": papers_out,
    }
    if args.dry_run:
        print("[dry-run] 未写盘；统计见上。")
        return
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"[OK] candidate set: {out_path}")
    print(f"  n={len(papers_out)}  labels={dict(cnt)}")
    print(f"  R_rate={out['R_rate']:.1%} | R+U={out['R_plus_U_rate']:.1%}"
          f" | 富集 {out['enrichment_vs_all']['fold']}x vs 全量 2806")
    print(f"  candidate R1 (R∪U) = {len(cand_r1)} 篇 | R = {len(cand_r)} 篇")
    print(f"  R by EX: {out['by_ex_relevant']}")


if __name__ == "__main__":
    main()
