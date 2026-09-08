# -*- coding: utf-8 -*-
"""P1-B1 回归测试：deterministic action 裁剪（135 → 36）+ query safety gate。

护栏：
  1. cut 决策资产存在且 version 匹配 P1B1_ROUND1_CUT_V2（用户终裁 36 条）。
  2. 工具输出 36（C26/A4/D6），全部 action_id ∈ 源 135。
  3. 保留 query 互异；removed 均有可追溯 cut_reason。
  4. gate 资产 specificity 覆盖 36 全部 A-term；gated 产物 21 discovery / 15 precision，
     precision query 必含 umbrella、discovery query 与原始逐字节一致。
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CUT_ASSET = os.path.join(BASE, "topics", "thermochromic_materials",
                         "p1b1_round1_cut.json")
BRIDGE = os.path.join(BASE, "topics", "thermochromic_materials", "runs",
                      "bridge_queries.json")
RUNS = os.path.join(BASE, "topics", "thermochromic_materials", "runs")


def _load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _run_cut():
    """调用工具（写 runs/ 产物），返回输出路径。"""
    subprocess.run([sys.executable,
                    os.path.join(BASE, "tools", "cut_bridge_actions.py"),
                    "--asset", CUT_ASSET],
                   check=True, capture_output=True, timeout=120)
    return os.path.join(RUNS, "p1b1_round1_actions.json"), \
        os.path.join(RUNS, "p1b1_round1_cut_report.json")


def test_cut_asset_schema_self_consistent():
    asset = _load(CUT_ASSET)
    assert asset["version"] == "P1B1_ROUND1_CUT_V2"
    all_members = []
    reps = set()
    for cl in asset["clusters"]:
        all_members.extend(cl["members"])
        if cl["representative"]:
            reps.add(cl["representative"])
            assert cl["representative"] in cl["members"], \
                f"rep {cl['representative']} 不在 members 中"
    assert len(all_members) == len(set(all_members)), "簇 members 跨簇重复"
    assert len(reps) == sum(1 for c in asset["clusters"] if c["representative"]), "rep 重复"
    lex_kept = {lk["term"] for lk in asset["lex_keeps"]}
    lex_dropped = {lk["term"] for lk in asset["lex_dropped"]}
    assert not (lex_kept & lex_dropped), "lex_keeps 与 lex_dropped 重叠"
    # 机制类别平衡（用户终裁）：每个簇都有代表（无整簇延后）
    assert all(c["representative"] for c in asset["clusters"]), "存在零代表簇（机制不平衡）"


def test_cut_output_counts_and_sourcing():
    acts_path, rep_path = _run_cut()
    out = _load(acts_path)
    rep = _load(rep_path)
    assert out["n"] == 36
    assert out["family_counts"] == {"C": 26, "A": 4, "D": 6}
    src_ids = {a["action_id"] for a in _load(BRIDGE)["actions"]}
    assert len(out["actions"]) == 36
    assert all(a["action_id"] in src_ids for a in out["actions"]), \
        "保留 action 不在源 135 中"
    qs = [a["query_string"] for a in out["actions"]]
    assert len(set(qs)) == 36, "保留 query 重复"
    assert rep["n_keep"] + rep["n_removed"] == 135
    assert rep["removed_by_reason"] == {
        "b_capped": 38, "lex_dropped": 9, "cluster_dup": 52}
    reasons = {r["cut_reason"] for r in rep["removed"]}
    assert reasons <= {"b_capped", "lex_dropped", "cluster_dup"}, \
        f"未知 cut_reason: {reasons}"


def _run_gate():
    subprocess.run([sys.executable,
                    os.path.join(BASE, "tools", "apply_query_gate.py"),
                    "--gate", os.path.join(BASE, "topics", "thermochromic_materials",
                                           "query_gate.json")],
                   check=True, capture_output=True, timeout=120)
    return os.path.join(RUNS, "p1b1_round1_gated_actions.json")


def test_gate_asset_covers_all_36_terms():
    gate = _load(os.path.join(BASE, "topics", "thermochromic_materials",
                              "query_gate.json"))
    assert gate["version"] == "P1B1_QUERY_GATE_V1"
    spec_terms = {s["term"] for s in gate["specificity"]}
    classes = {s["class"] for s in gate["specificity"]}
    assert classes == {"specific", "generic"}
    # 36 actions 的 A-term 全集必须全被标注（default_class 只做防御，不应被触发）
    keep = _load(os.path.join(RUNS, "p1b1_round1_actions.json"))
    a_terms = {a["terms"][0] for a in keep["actions"]}
    assert a_terms <= spec_terms, f"未标注 A-term: {a_terms - spec_terms}"


def test_gated_output_channels_and_query_integrity():
    gated_path = _run_gate()
    g = _load(gated_path)
    assert g["n"] == 36
    assert g["gate_class_counts"] == {"discovery": 21, "precision": 15}
    umb = g["umbrella"]
    for a in g["actions"]:
        assert a["gate_class"] in ("discovery", "precision")
        assert "original_query_string" in a
        if a["gate_class"] == "precision":
            assert a["query_string"].startswith("TITLE-ABS-KEY((") or \
                a["query_string"].count("(") >= 3
            assert any(t in a["query_string"] for t in ("thermochrom*", "thermal color change")), \
                f"precision query 缺 umbrella: {a['action_id']}"
            assert a["query_string"] != a["original_query_string"]
        else:
            assert a["query_string"] == a["original_query_string"], \
                f"discovery query 被改动: {a['action_id']}"
    # 具体抽查：lc_phase（generic）加 umbrella；spin crossover（specific）不加
    by_aid = {a["action_id"]: a for a in g["actions"]}
    assert by_aid["S6-C-73"]["gate_class"] == "precision"
    assert "liquid-crystal phase transition" in by_aid["S6-C-73"]["query_string"]
    assert by_aid["S6-C-57"]["gate_class"] == "discovery"
    assert by_aid["S6-C-57"]["query_string"] == by_aid["S6-C-57"]["original_query_string"]
