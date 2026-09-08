"""P1-A2: S6 semantic-bridge 统一入口（topic 资产 → spec → actions）。

按 topic.yaml `bridge` 节生成 S6 bridge actions：
  - mode=frozen_asset : 读 topic 决策资产（bridge_spec.json，pc001 冻结 specs）→ 编译
  - mode=synthesize   : termbank roles 自动合成 specs → 编译（candidates，VERIFIED 待 S6）

所有 S6/S7 消费方（pilot / term layers / execute 的 domain context）统一从这里
拿 spec/actions/domain_context，禁止各自 import 主题常量。
"""
from __future__ import annotations

import json
import os

from .bridge_compiler import compile_spec_asset
from .bridge_synth import build_synth_spec_asset, synthesize_specs
from .termbank import load_termbank
from .topic_config import load_topic


def load_bridge_asset(topic_id: str) -> dict:
    """按 topic.yaml bridge 节生成/加载 spec 资产（dict）。不落盘。"""
    t = load_topic(topic_id)
    br = t.raw.get("bridge") or {}
    mode = br.get("mode", "frozen_asset")
    if mode == "frozen_asset":
        rel = br.get("asset") or "bridge_spec.json"
        p = os.path.join(t.dir, rel)
        if not os.path.exists(p):
            raise FileNotFoundError(
                f"[{topic_id}] bridge.mode=frozen_asset 但缺资产 {p}")
        return json.load(open(p, encoding="utf-8"))
    if mode == "synthesize":
        tb = load_termbank(topic_id)
        caps = {k: v for k, v in br.items()
                if k.startswith("cap_") and isinstance(v, int)}
        return build_synth_spec_asset(topic_id, tb, anchor=t.anchor or "", **caps)
    raise ValueError(f"[{topic_id}] bridge.mode 未知: {mode}")


def bridge_actions(topic_id: str, *, term_source_label: str | None = None,
                   prefix: str = "S6") -> list[dict]:
    """topic → S6 bridge actions（frozen 或 synth 统一入口）。"""
    asset = load_bridge_asset(topic_id)
    if term_source_label is None:
        # 资产自带词源标签（frozen 复现旧值；synth 标 CANDIDATE）→ 零歧义
        term_source_label = asset.get("term_source_label") or (
            f"{asset.get('version', '?')} "
            f"(CANDIDATE-{len(load_termbank(topic_id).entries)}词)")
    return compile_spec_asset(asset, term_source_label, prefix=prefix)


def domain_context(topic_id: str) -> dict:
    """topic 的 domain context 映射（S7 execute 编译与 S6 共用）。"""
    return (load_bridge_asset(topic_id).get("domain_context") or {})


def process_words(topic_id: str) -> list[str]:
    a = load_bridge_asset(topic_id)
    return a.get("process_words") or []


def process_clause(topic_id: str):
    return load_bridge_asset(topic_id).get("process_clause")
