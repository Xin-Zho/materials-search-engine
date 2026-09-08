#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/build_s7_term_layers.py — S7 relation generator v2 三层词源构建（通用版，P1-A2）

P1-A2 改造：输入 termbank 从 topic 资产读（schema 归一），gate diagnosis 按主题
路由——pc001 有 s6_semantic_gate_diagnosis.json（16 词 gate 归因）走完整三层；
新主题尚无 gate（SEMANTIC 词未过 QA）→ 生成 pending 层（全部词暂标
R_discovery pending，待 S6 词源三层 + gate 后升级）。禁止主题特判。

三层词源契约（用户 2026-09-07 终裁，不变）：
  MAIN 层      = VERIFIED_EXACT（正式 recall 词源 R_main）
  NORMALIZED   = SEMANTIC 中 gate 归因 ∈ {NORMALIZED_FALSE_NEGATIVE, SOURCE_MISMATCH}
                → 条件升级进 R_main（词真实存在于 corpus/审计宇宙）
  DISCOVERY    = SEMANTIC 中 gate 归因 ∈ {SEMANTIC_ONLY, CORPUS_ABSENT}
                → 探索层，R_discovery 单独报告（不污染 R_main）

用法：
  .venv\\Scripts\\python.exe tools/build_s7_term_layers.py --topic <id> [--out PATH]
输出：
  legacy(pc001) → data/exports/terminology/s7_term_layers.json（原址，行为等价）
  新主题        → topics/<id>/runs/term_layers.json
"""
import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
T = os.path.join(BASE, "data", "exports", "terminology")

from search_engine.termbank import load_termbank  # noqa: E402
from search_engine.topic_config import (  # noqa: E402
    DEFAULT_TOPIC, resolve_input, resolve_output,
)

LEGACY_GATE = os.path.join(T, "s6_semantic_gate_diagnosis.json")
LEGACY_OUT = os.path.join(T, "s7_term_layers.json")

# 条件升级门 / 探索层（用户 2026-09-07 冻结）
UPGRADE_ATTRIBUTIONS = {"NORMALIZED_FALSE_NEGATIVE", "SOURCE_MISMATCH"}
DISCOVERY_ATTRIBUTIONS = {"SEMANTIC_ONLY", "CORPUS_ABSENT"}


def build(topic_id: str) -> dict:
    tb = load_termbank(topic_id)
    verified = [e for e in tb.entries if e.status == "VERIFIED"]
    semantic = [e for e in tb.entries if e.status == "SEMANTIC_CANDIDATE"]
    # 平铺 schema（CANDIDATE verdict）：termbank 尚未词源验证 → pending 层
    flat_candidates = [e for e in tb.entries if e.status == "CANDIDATE"]

    has_verified = bool(verified)
    has_gate = False
    gate_path = resolve_input(topic_id, LEGACY_GATE, "semantic_gate_diagnosis.json")
    gate = {}
    if os.path.exists(gate_path):
        gd = json.load(open(gate_path, encoding="utf-8"))
        gate = {t["term"]: t for t in gd.get("terms", [])}
        has_gate = bool(gate)

    main_terms, norm_terms, disc_terms, pending_terms = [], [], [], []

    for e in verified:
        main_terms.append({
            "term": e.term, "role": e.role, "domain": e.domain,
            "source_status": "VERIFIED", "recall_layer": "R_main",
            "evidence_paper_id": e.evidence_paper_id,
        })

    if has_gate:
        # 完整三层：SEMANTIC × gate 归因（pc001 冻结语义）
        for e in semantic:
            g = gate.get(e.term)
            if not g:
                raise SystemExit(f"[err] gate 诊断缺 {e.term}（termbank SEMANTIC）")
            attr = g["attribution"]
            base = {"term": e.term, "role": e.role, "domain": e.domain,
                    "evidence_paper_id": e.evidence_paper_id,
                    "corpus_hit": g.get("corpus_hit"), "attribution": attr,
                    "gate_note": g.get("note", "")}
            if attr in UPGRADE_ATTRIBUTIONS:
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
                raise SystemExit(f"[err] 未知 attribution {attr} for {e.term}")
    elif semantic:
        # 有 SEMANTIC 词但无 gate：pending（不臆断归因）
        for e in semantic:
            disc_terms.append({
                "term": e.term, "role": e.role, "domain": e.domain,
                "evidence_paper_id": e.evidence_paper_id,
                "source_status": "SEMANTIC_CANDIDATE",
                "recall_layer": "R_discovery",
                "pending": "gate diagnosis 缺失——归因待 S6 gate QA 后重算",
            })

    # 平铺 CANDIDATE schema（thermo 现状：全词待词源三层验证）
    for e in flat_candidates:
        pending_terms.append({
            "term": e.term, "role": e.role, "domain": e.domain,
            "source_status": "CANDIDATE",
            "recall_layer": "R_discovery",
            "pending": "VERIFIED 词源三层验证未跑（S6）；暂不进入 R_main",
        })

    dist = Counter(g["attribution"] for g in gate.values()) if gate else Counter()
    counts = {
        "verified": len(main_terms),
        "normalized": len(norm_terms),
        "discovery": len(disc_terms),
        "gate_distribution": dict(dist),   # 旧 schema 兼容（无 gate 主题为空）
        "pending_candidate": len(pending_terms),
        "r_main_vocab": len(main_terms) + len(norm_terms),
        "gate_present": has_gate,
        "schema": tb.schema,
    }
    if has_verified and has_gate:
        # pc001 冻结契约（防静默漂移）
        assert counts["verified"] == 40 and counts["normalized"] == 8 and counts["discovery"] == 8, counts

    layers = {"R_main_verified": main_terms,
              "R_main_normalized": norm_terms,
              "R_discovery": disc_terms}
    if pending_terms:
        layers["R_pending_candidate"] = pending_terms

    return {
        "version": "s7_term_layers_v1",
        "topic_id": topic_id,
        "created_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "status": ("FROZEN_INPUT_FOR_S7_V2" if (has_verified and has_gate)
                   else "PENDING_VERIFICATION"),
        "contract": (
            "正式 recall（R_main）词源 = VERIFIED ∪ VERIFIED_NORMALIZED；"
            "Discovery（R_discovery）单独报告；CANDIDATE 待 S6 词源三层后升级"),
        "counts": counts,
        "layers": layers,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    topic = args.topic or DEFAULT_TOPIC

    out = build(topic)
    out_path = args.out or resolve_output(topic, LEGACY_OUT, "term_layers.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    c = out["counts"]
    print(f"[ok] {out_path}")
    print(f"  topic={topic} schema={c['schema']} gate={c['gate_present']} "
          f"R_main(V+N)={c['r_main_vocab']} discovery={c['discovery']} "
          f"pending={c['pending_candidate']}")


if __name__ == "__main__":
    main()
