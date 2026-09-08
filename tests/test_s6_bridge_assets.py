# -*- coding: utf-8 -*-
"""P1-A2 回归测试：topic 资产驱动的 S6/S7 配置生成。

护栏：
  1. pc001 frozen bridge spec → 编译 actions 与 exports 冻结产物逐字段 byte 一致
     （v1.0 行为等价；specs/常量已从代码资产化，禁回归到代码常量）。
  2. thermo synthesize → 生成 actions，词源全部 ∈ termbank（禁越源塞词）。
  3. termbank 归一读取对两种 schema（pc001 三段 / 平铺）都产出正确 role/status。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from search_engine.bridge_compiler import compile_spec_asset  # noqa: E402
from search_engine.s6_bridge import bridge_actions  # noqa: E402
from search_engine.termbank import load_termbank  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PC_SPEC = os.path.join(BASE, "topics", "photopolymerization_shrinkage",
                       "bridge_spec.json")
PC_FROZEN = os.path.join(BASE, "data", "exports", "terminology",
                         "s6_bridge_queries.json")


def _load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def test_pc001_bridge_asset_reproduces_frozen_actions():
    asset = _load(PC_SPEC)
    frozen = _load(PC_FROZEN)
    actions = compile_spec_asset(asset, asset.get("term_source_label", ""))
    assert len(actions) == len(frozen["actions"]) == 17
    for new, fz in zip(actions, frozen["actions"]):
        assert all(fz[k] == new.get(k) for k in fz), f"{fz['action_id']} 漂移"


def test_thermo_synth_actions_terms_inside_termbank():
    tb = load_termbank("thermochromic_materials")
    allowed = tb.term_set()
    actions = bridge_actions("thermochromic_materials")
    assert len(actions) > 0
    outside = {t for a in actions for t in a["terms"] if t not in allowed}
    assert not outside, f"词源越界: {outside}"


def test_termbank_normalization_both_schemas():
    # pc001 三段式
    pc = load_termbank("photopolymerization_shrinkage")
    assert pc.schema == "pc001_three_tier"
    assert len(pc.verified_entries) == 40
    assert pc.summary()["by_status"] == {"VERIFIED": 40, "SEMANTIC_CANDIDATE": 16}
    # thermo 平铺式
    th = load_termbank("thermochromic_materials")
    assert th.schema == "flat"
    assert th.summary()["n"] == 60
    assert th.by_role["mechanism"] and th.by_role["observable"]
    # 泛锚过滤语义：thermo lexical general 域 = 主题泛锚（不作体系锚）
    from search_engine.bridge_synth import _split_roles
    _, _, anchors, _, _, _ = _split_roles(th)
    assert all(a.domain != "general" for a in anchors)


def test_thermo_term_layers_pending_without_gate():
    import importlib.util
    path = os.path.join(BASE, "tools", "build_s7_term_layers.py")
    spec = importlib.util.spec_from_file_location("tl", path)
    tl = importlib.util.module_from_spec(spec)
    sys.modules["tl"] = tl
    spec.loader.exec_module(tl)
    out = tl.build("thermochromic_materials")
    assert out["counts"]["gate_present"] is False
    assert out["counts"]["pending_candidate"] == 60
    assert "R_pending_candidate" in out["layers"]
