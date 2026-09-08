#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/build_s7_relation_candidates.py — S7 relation proposals（deterministic，P1-A2）

从 topic 的 S6 bridge spec（frozen 或 synth）确定性提取 relation 候选
（A=mech/left_terms 词，B=observable 词），产出 S7 planner / QA novelty gate
的输入结构（schema 对齐 pc001 s7_relation_candidates_v2.json）。

无 LLM：rationale 为确定性模板、plausibility/novelty_gate 留 pending——
真实裁决在 P1-B 由 QA 链（run_s7_relation_qa）填充。模板类左锚 spec
（left=process/shrink_lex/ctx，无词面左锚）不产生词对——跳过。

用法：
  .venv\\Scripts\\python.exe tools/build_s7_relation_candidates.py --topic <id>
输出：
  pc001(legacy) → runs/relation_candidates.deterministic.json（只读对照）
  新主题        → topics/<id>/runs/relation_candidates.json
"""
import argparse
import json
import os
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from search_engine.s6_bridge import load_bridge_asset  # noqa: E402
from search_engine.topic_config import DEFAULT_TOPIC, load_topic  # noqa: E402


def build(topic_id: str) -> dict:
    t = load_topic(topic_id)
    asset = load_bridge_asset(topic_id)
    term_src = asset.get("term_source_label") or "TERMBANK"
    has_verified = "VERIFIED" in term_src.upper()

    candidates = []
    seen = set()
    i = 0
    for sp in asset.get("specs", []):
        a_terms = sp.get("mech") or sp.get("left_terms") or []
        b_terms = sp.get("obs") or []
        if not a_terms or not b_terms:
            continue          # 模板左锚（process/shrink/ctx）无词对
        for a_term in a_terms:
            for b_term in b_terms:
                key = (a_term.lower().rstrip("*"), b_term.lower().rstrip("*"))
                if key in seen:
                    continue
                seen.add(key)
                i += 1
                candidates.append({
                    "concept_A": a_term,
                    "concept_B": b_term,
                    "relation_type": sp.get("strategy"),
                    "domain": sp.get("domain"),
                    "rationale": (f"[deterministic] synth/frozen spec family {sp.get('family')} "
                                  f"({sp.get('strategy')}); exploratory="
                                  f"{sp.get('exploratory', False)}"),
                    "why_S5_failed": None,
                    "plausibility": None,      # 待 QA 盲评回填
                    "concept_A_source": term_src,
                    "concept_B_source": term_src,
                    "recall_layer": "R_main" if has_verified else "R_discovery",
                    "novelty_gate": {"verdict": "PENDING_QA",
                                     "exploration_level": None,
                                     "reason": "deterministic 候选：待 S7 relation QA 盲评"},
                    "candidate_id": f"RC{i:02d}",
                })
    return {
        "version": "s7_relation_candidates_deterministic_v1",
        "topic_id": topic_id,
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "status": "DETERMINISTIC_PENDING_QA",
        "contract": ("deterministic 词对候选（无 LLM）；QA novelty gate 与 plausibility "
                     "在 P1-B 由 run_s7_relation_qa 盲评回填；novelty LOW 留作负样本"),
        "n": len(candidates),
        "candidates": candidates,
        "rejected": [],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    topic = args.topic or DEFAULT_TOPIC

    out = build(topic)
    t = load_topic(topic)
    if args.out:
        out_path = args.out
    elif topic == "photopolymerization_shrinkage":
        out_path = os.path.join(t.runs_dir, "relation_candidates.deterministic.json")
    else:
        out_path = os.path.join(t.runs_dir, "relation_candidates.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    json.dump(out, open(out_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"[ok] topic={topic} n={out['n']} -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
