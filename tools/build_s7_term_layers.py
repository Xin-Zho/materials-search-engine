#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/build_s7_term_layers.py — S7 relation generator v2 三层词源构建
（2026-09-07 用户终裁：QA v2 schema 冻结；relation generator 转向 v2）。

问题：S6 后 VERIFIED 40 词的 relation 组合空间与「S6 去重」后几乎耗尽，
v1 生成的 20 条 → QA v2 判 novelty LOW 18（VALID≠RUN：已知关系、低探索价值）。
v1 根因 = 输入只有 VERIFIED 词 + S6 已探索组合，LLM 只能在「已有论文关系回声」里打转。

用户裁决（2026-09-07 词源边界）：
  输入分三层，SEMANTIC 不再整体禁入，而是按归因分级 + 检索报告分层：
  ┌──────────────────────────────────────────────────────────────────────┐
  │ 层             │ 来源                        │ 归因门        │ recall │
  │ MAIN           │ VERIFIED_EXACT 40           │ VERIFIED_EXACT│ R_main │
  │ NORMALIZED     │ SEMANTIC 16 中 gate 归因 ∈  │ 词真实存在于  │ R_main │
  │                │ {NORMALIZED_FALSE_NEGATIVE, │ corpus/审计宇宙│        │
  │                │  SOURCE_MISMATCH}           │ (corpus_hit 或│        │
  │                │  → 条件升级                 │  evidence)    │        │
  │ DISCOVERY      │ SEMANTIC 16 中 gate 归因 ∈  │ 探索层，不进  │R_discov│
  │                │ {SEMANTIC_ONLY, CORPUS_ABSENT} 正式 recall │  ery   │
  └──────────────────────────────────────────────────────────────────────┘
  门 = 检索/报告分层，不设在生成层：SEMANTIC 词可作 term_B 提议，
      但 R_main 只算 VERIFIED+VERIFIED_NORMALIZED，R_discovery 单独报。

输入：
  - s6_term_bank.json（VERIFIED 40 + SEMANTIC 16 原表）
  - s6_semantic_gate_diagnosis.json（16 词 gate 归因；2026-09-07 归位并修复
    cure shrinkage 误标 SOURCE_MISMATCH→CORPUS_ABSENT，逐词与 aggregate 一致 3/5/3/5）
输出：
  - s7_term_layers.json（三层词源；每词带 term/role/domain/source_status/recall_layer）
  - 被升级的 8 词带 attribution + corpus_hit 佐证（证据纪律）
用法：
  python tools/build_s7_term_layers.py
"""
import json
import os
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T = os.path.join(BASE, "data", "exports", "terminology")
OUT = os.path.join(T, "s7_term_layers.json")

# 条件升级门：词在 corpus/审计宇宙真实存在，仅 gate 匹配粒度/LLM 引用问题
UPGRADE_ATTRIBUTIONS = {"NORMALIZED_FALSE_NEGATIVE", "SOURCE_MISMATCH"}
# 探索层：corpus 缺席或纯语义归纳 —— 不进正式 recall，R_discovery 单独报
DISCOVERY_ATTRIBUTIONS = {"SEMANTIC_ONLY", "CORPUS_ABSENT"}


def load(name):
    return json.load(open(os.path.join(T, name), encoding="utf-8"))


def build():
    tb = load("s6_term_bank.json")
    gd = load("s6_semantic_gate_diagnosis.json")

    verified = tb["terms_verified_exact"]
    semantic = tb["terms_semantic_candidates"]
    sem_by_term = {e["term"]: e for e in semantic}

    # gate 诊断逐词归因（已归位修复，与 aggregate 一致）
    gd_by_term = {t["term"]: t for t in gd["terms"]}
    assert len(gd_by_term) == len(semantic) == 16, "gate 诊断与 term_bank SEMANTIC 数量不一致"

    main_terms, norm_terms, disc_terms = [], [], []
    for e in verified:
        main_terms.append({
            "term": e["term"], "role": e["semantic_role"], "domain": e["domain"],
            "source_status": "VERIFIED", "recall_layer": "R_main",
            "evidence_paper_id": e.get("evidence_paper_id"),
        })

    for e in semantic:
        t = e["term"]
        g = gd_by_term.get(t)
        if not g:
            raise SystemExit(f"[err] gate 诊断缺 {t}")
        attr = g["attribution"]
        base = {"term": t, "role": e["semantic_role"], "domain": e["domain"],
                "evidence_paper_id": e.get("evidence_paper_id"),
                "corpus_hit": g.get("corpus_hit"),
                "attribution": attr,
                "gate_note": g.get("note", "")}
        if attr in UPGRADE_ATTRIBUTIONS:
            # 条件升级 → 词真实存在于 corpus/审计宇宙，进 R_main
            base.update({"source_status": "VERIFIED_NORMALIZED",
                         "recall_layer": "R_main",
                         "upgrade_reason": ("gate 归因∈升级门：" + attr +
                                            "；词真实存在，仅匹配粒度/引用问题")})
            norm_terms.append(base)
        elif attr in DISCOVERY_ATTRIBUTIONS:
            base.update({"source_status": "SEMANTIC_CANDIDATE",
                         "recall_layer": "R_discovery",
                         "discovery_note": ("gate 归因∈探索层：" + attr +
                                            "；不进 R_main，R_discovery 单报")})
            disc_terms.append(base)
        else:
            raise SystemExit(f"[err] 未知 attribution {attr} for {t}")

    # 分层统计（须与 gate aggregate 一致）
    from collections import Counter
    dist = Counter(gd_by_term[t]["attribution"] for t in gd_by_term)
    counts = {
        "verified": len(main_terms),          # 40
        "normalized": len(norm_terms),        # 8 = NORMALIZED_FN 3 + SOURCE_MISMATCH 5
        "discovery": len(disc_terms),         # 8 = SEMANTIC_ONLY 3 + CORPUS_ABSENT 5
        "gate_distribution": dict(dist),      # {NORMALIZED_FN:3, SOURCE_MISMATCH:5, SEMANTIC_ONLY:3, CORPUS_ABSENT:5}
        "r_main_vocab": len(main_terms) + len(norm_terms),   # 48（正式 recall 词源）
    }
    assert counts["verified"] == 40 and counts["normalized"] == 8 and counts["discovery"] == 8
    assert counts["gate_distribution"]["NORMALIZED_FALSE_NEGATIVE"] == 3
    assert counts["gate_distribution"]["SOURCE_MISMATCH"] == 5
    assert counts["gate_distribution"]["SEMANTIC_ONLY"] == 3
    assert counts["gate_distribution"]["CORPUS_ABSENT"] == 5

    out = {
        "version": "s7_term_layers_v1",
        "created_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "status": "FROZEN_INPUT_FOR_S7_V2",
        "contract": (
            "正式 recall（R_main）词源 = VERIFIED ∪ VERIFIED_NORMALIZED(48)；"
            "Discovery（R_discovery）= SEMANTIC_CANDIDATE(8) 单独报告；"
            "SEMANTIC 词不污染 R_main 统计"),
        "counts": counts,
        "layers": {
            "R_main_verified": main_terms,
            "R_main_normalized": norm_terms,
            "R_discovery": disc_terms,
        },
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"[ok] {OUT}")
    print(f"  R_main: VERIFIED {counts['verified']} + NORMALIZED {counts['normalized']}"
          f" = {counts['r_main_vocab']} 词")
    print(f"  R_discovery: {counts['discovery']} 词（探索层单报）")
    print("  升级 8 词（VERIFIED_NORMALIZED，进 R_main）:")
    for t in norm_terms:
        print(f"    {t['term']:32s} {t['role']:10s} {t['domain']:8s} "
              f"{t['attribution']:26s} corpus_hit={t.get('corpus_hit')}")
    print("  Discovery 8 词（SEMANTIC_CANDIDATE，R_discovery）:")
    for t in disc_terms:
        print(f"    {t['term']:32s} {t['role']:10s} {t['domain']:8s} {t['attribution']}")


if __name__ == "__main__":
    build()
