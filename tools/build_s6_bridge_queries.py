#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/build_s6_bridge_queries.py — S6 Semantic Bridge deterministic assembler（通用版，P1-A2）

P1-A2 改造：冻结常量与 specs 已数据化为 topic 决策资产（topics/<id>/bridge_spec.json），
本脚本只做编排——按 topic.yaml `bridge` 节生成 actions：
  - mode=frozen_asset : pc001 冻结 specs 决策资产 → 编译（17 actions 逐字段复现 v1.0）
  - mode=synthesize   : thermo 等新主题——termbank roles 自动合成 candidates（VERIFIED 待 S6）

纪律（写死，用户 2026-09-04 22:51 延续）：
  1. 词源纪律：query 内容词必须来自 termbank（frozen→VERIFIED；synth→CANDIDATE 词集），
     生成器在 compile 前做全量断言；禁止手工塞词。
  2. LLM 不输出 Boolean：纯 deterministic（词源已在 termbank 层冻结/候选）。
  3. provenance 全记录（family/domain/strategy/terms/term_source/context_source/
     contains_shrinkage_anchor/exploratory），供 pilot 后比较。
  4. 禁领域特判：spec 来源/词表全部走 topic 资产与生成器，代码不含任何主题业务常量。

输入：topics/<id>/termbank.json + topic.yaml bridge 节（+ bridge_spec.json 若 frozen）
输出：resolve_output(topic, legacy exports 原址, bridge_queries.json)
      —— pc001(legacy) 保持写 data/exports/terminology/s6_bridge_queries.json 语义
         （v1.0 复跑行为等价）；新主题 → topics/<id>/runs/bridge_queries.json
"""
import argparse
import datetime
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from search_engine.s6_bridge import bridge_actions, load_bridge_asset  # noqa: E402
from search_engine.termbank import load_termbank  # noqa: E402
from search_engine.topic_config import (  # noqa: E402
    DEFAULT_TOPIC, load_topic, resolve_output,
)

# pc001 v1.0 冻结产物原址（历史证据，legacy 路由写回）
LEGACY_OUT = os.path.join(BASE, "data", "exports", "terminology",
                          "s6_bridge_queries.json")


def main():
    ap = argparse.ArgumentParser(description="S6 semantic bridge actions（topic 资产驱动）")
    ap.add_argument("--topic", default=None)
    ap.add_argument("--out", default=None,
                    help="覆盖输出路径（默认 resolve_output：legacy→exports 原址 / 新→runs/）")
    args = ap.parse_args()
    topic = args.topic or DEFAULT_TOPIC

    t = load_topic(topic)
    asset = load_bridge_asset(topic)
    mode = asset.get("mode", "frozen_asset")

    # 词源纪律断言：spec 内每个内容词 ∈ 该主题 termbank 词集
    tb = load_termbank(topic)
    allowed = tb.term_set()
    allowed = allowed | set(asset.get("query_form_map", {}).keys())  # 已登记派生
    n_verified = len(tb.verified_entries)
    bad = []
    for sp in asset.get("specs", []):
        for tt in sp.get("obs", []) + sp.get("mech", []) + sp.get("left_terms", []):
            # QUERY_FORM_MAP 派生以 map key 登记（旧纪律：map key ∈ VERIFIED）
            if tt not in allowed:
                bad.append((tt, sp.get("strategy")))
    if bad:
        raise SystemExit(f"[词源违规] {topic} {len(bad)} 个词不在 termbank: "
                         f"{sorted(set(b[0] for b in bad))[:10]}")

    actions = bridge_actions(topic)
    fam_counts = {}
    for a in actions:
        fam_counts[a["family"]] = fam_counts.get(a["family"], 0) + 1
    anchor_free = sum(1 for a in actions if not a["contains_shrinkage_anchor"])
    expl = [a["action_id"] for a in actions if a["exploratory"]]

    out_path = args.out or resolve_output(
        topic, LEGACY_OUT, "bridge_queries.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    out = {
        "version": asset.get("version") or "S6_BRIDGE_QUERIES_SYNTH",
        "topic_id": topic,
        "bridge_mode": mode,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "frozen_refs": asset.get("frozen_ref") or asset.get("synth_note"),
        "word_source_discipline": (
            f"query 内容词 = topic termbank（{topic}: {len(tb.entries)} 词/"
            f"{n_verified} VERIFIED）∪ 已登记派生（query_form_map）"
            f"{' ∪ 冻结 process/domain context' if asset.get('domain_context') else ''}"),
        "role_correction": asset.get("role_correction"),
        "query_form_derivation": asset.get("query_form_map"),
        "family_counts": fam_counts,
        "total_actions": len(actions),
        "anchor_free_actions": anchor_free,
        "exploratory_actions": expl,
        "actions": actions,
    }
    json.dump(out, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[counts] topic={topic} mode={mode} total={len(actions)} "
          f"by_family={fam_counts} anchor_free={anchor_free}")
    print(f"[out] {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
