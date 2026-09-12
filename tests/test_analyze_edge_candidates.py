# -*- coding: utf-8 -*-
"""`tools/analyze_edge_candidates_llm.py` 的回归测试（边层解释层）。

守的是四类不变量：
  1. **未来信息结构性排除**：payload 里不许出现未来标签 / 影响力字段，
     且证据年份必须 <= CUT（不是靠"提醒模型别说"）
  2. **输出 schema 可校验**：字段缺失 / 枚举越界 / uncertainty 写成字符串都要被标出
  3. **接地度**必须可度量，低于 0.5 要报警（解释层的主要失效模式是自说自话）
  4. 协议绑定：prompt / 工具 / 候选文件任一变更，`--live` 必须拒绝运行
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(BASE, "tools", "analyze_edge_candidates_llm.py")
sys.path.insert(0, os.path.join(BASE, "tools"))

_spec = importlib.util.spec_from_file_location("edge_analyst", TOOL)
ana = importlib.util.module_from_spec(_spec)
sys.modules["edge_analyst"] = ana
_spec.loader.exec_module(ana)


# ══ 假上下文：让证据构造与年份断言可以在不载入真语料的情况下受测 ═══════════
class _FakeIdx:
    """docset(name) -> 该概念命中的论文下标集合。"""

    def __init__(self, table):
        self.table = table

    def docset(self, name):
        return self.table.get(name, set())


def _fake_ctx(rows, table):
    nodes = set(table)
    adj = {n: set() for n in nodes}
    for n, docs in table.items():
        for i in docs:
            adj[n].add("bridge")
    adj.setdefault("bridge", set()).update(nodes)
    return {"g": {"adj": adj, "type": {n: "material" for n in nodes},
                  "home": {n: "t" for n in nodes}},
            "idx": _FakeIdx(table), "early": set(range(len(rows))), "text": rows}


def _row(a, b, **kw):
    base = {"a": a, "b": b, "type_a": "material", "type_b": "application",
            "home_a": "t1", "home_b": "t2", "adamic_adar": 9.5,
            "common_neighbors": 3.0, "jaccard": 0.2, "deg_min": 4.0, "deg_max": 9.0}
    base.update(kw)
    return base


# ══ 1. 未来信息结构性排除 ══════════════════════════════════════════════
def test_forbidden_key_regex_catches_future_and_impact_fields():
    for k in ("joint_fut", "eval_formed", "rate_ratio", "eval_lex>=5",
              "citation_count", "cited_by_count", "fwci", "future_window"):
        assert ana.FORBIDDEN_KEY_RE.search(k), "漏掉了 %s" % k
    for k in ("adamic_adar", "common_neighbors", "shared_neighbors",
              "evidence_papers", "deg_min"):
        assert not ana.FORBIDDEN_KEY_RE.search(k), "误伤 %s" % k


def test_payload_omits_future_label_fields():
    """上游候选行里的 future 字段**根本不该进 payload**。"""
    rows = [("u1", 2010, "dental restoration with epoxy resin"),
            ("u2", 2011, "epoxy resin curing"),
            ("u3", 2012, "shrinkage stress in composites")]
    table = {"dental restoration": {0}, "epoxy resin": {0, 1},
             "bridge": {0}, "shrinkage stress": {2}}
    ctx = _fake_ctx(rows, table)
    row = _row("dental restoration", "epoxy resin",
               joint_fut=7, eval_formed=True, rate_ratio=2.5, eval_lex=9)
    body = ana.build_payload(ctx, row, "E0001")
    keys = set(ana._walk_keys(body))
    assert not [k for k in keys if ana.FORBIDDEN_KEY_RE.search(str(k))]
    assert "joint_fut" not in keys and "eval_formed" not in keys


def test_evidence_year_over_cut_is_refused():
    """证据年份越界必须 SystemExit —— 这是硬断言，不是提醒。"""
    rows = [("u1", 2023, "epoxy resin")]          # 未来论文
    table = {"dental restoration": {0}, "epoxy resin": {0}, "bridge": {0}}
    ctx = _fake_ctx(rows, table)
    with pytest.raises(SystemExit) as e:
        ana.build_payload(ctx, _row("dental restoration", "epoxy resin"), "E0001")
    assert "越界" in str(e.value)


def test_shared_neighbors_are_sorted_by_adamic_adar_contribution():
    rows = [("u1", 2010, "text")]
    table = {"a": {0}, "b": {0}, "hub": {0}, "rare": {0}}
    ctx = _fake_ctx(rows, table)
    ctx["g"]["adj"] = {"a": {"hub", "rare"}, "b": {"hub", "rare"},
                       "hub": {"a", "b", "x", "y", "z"}, "rare": {"a", "b"}}
    got = [s["name"] for s in ana.shared_neighbors(ctx["g"], "a", "b")]
    assert set(got) == {"hub", "rare"}
    assert got[0] == "rare", "低度数邻居的 AA 贡献更大，应排在前面"


# ══ 2. 输出校验 + 接地度 ════════════════════════════════════════════════
def _good_obj(**kw):
    o = {"candidate": "dental restoration x epoxy resin",
         "shared_structure": "两端共享 composite 与 gel point",
         "why_they_may_connect": "因为在 openalex:W1（2010）里……",
         "evidence": ["[openalex:W1] 2010: 相关段落"],
         "bridge_quality": "strong",
         "alternative_explanations": ["也可能只是共同出现在综述里"],
         "uncertainty": 0.42,
         "falsifiable_checks": ["2016 年后若出现 X 则支持"]}
    o.update(kw)
    return o


def test_validate_accepts_well_formed_payload():
    clean, issues = ana.validate_payload(_good_obj(), ["openalex:W1"], ["composite"])
    assert issues == [] and clean["uncertainty"] == 0.42
    assert clean["groundedness"] == 1.0


def test_validate_flags_missing_fields_and_enum():
    clean, issues = ana.validate_payload({"candidate": "x"}, [], [])
    assert clean is not None
    for f in ("shared_structure", "why_they_may_connect", "evidence",
              "bridge_quality", "alternative_explanations", "falsifiable_checks",
              "uncertainty"):
        assert any(f in i for i in issues), "未标出缺失字段 %s" % f


def test_validate_rejects_uncertainty_as_string():
    """模型常把 uncertainty 写成字符串 —— 必须标出来，不能静默通过。"""
    _, issues = ana.validate_payload(_good_obj(uncertainty="0.4"), [], [])
    assert any("非数字" in i for i in issues)


def test_validate_rejects_bridge_quality_outside_enum():
    _, issues = ana.validate_payload(_good_obj(bridge_quality="very strong"), [], [])
    assert any("枚举" in i for i in issues)


def test_groundedness_flags_self_talk():
    """evidence 全都不引用给定材料 -> 接地度低 -> 必须报警。"""
    obj = _good_obj(evidence=["我觉得这两个方向关系很大", "总之很有前景"])
    clean, issues = ana.validate_payload(obj, ["openalex:W1"], ["composite"])
    assert clean["groundedness"] == 0.0
    assert any("接地度" in i for i in issues)


def test_groundedness_counts_year_mentions_as_grounded():
    obj = _good_obj(evidence=["该现象在 2012 年前后的文献中被讨论"])
    clean, _ = ana.validate_payload(obj, ["openalex:W1"], ["composite"])
    assert clean["groundedness"] == 1.0


# ══ 3. 候选选择 ════════════════════════════════════════════════════════
def test_select_candidates_is_aa_ordered_and_ids_are_stable():
    rep = {"candidates": [{"a": "x", "b": "y", "adamic_adar": 1.0},
                          {"a": "p", "b": "q", "adamic_adar": 9.0},
                          {"a": "m", "b": "n", "adamic_adar": 5.0}]}
    got = ana.select_candidates(rep, 2)
    assert [r["cand_id"] for r in got] == ["E0001", "E0002"]
    assert [r["adamic_adar"] for r in got] == [9.0, 5.0]


def test_select_candidates_does_not_mutate_input():
    rep = {"candidates": [{"a": "x", "b": "y", "adamic_adar": 1.0}]}
    ana.select_candidates(rep, 1)
    assert "cand_id" not in rep["candidates"][0], "不应污染上游产物"


# ══ 4. prompt 解析与协议绑定 ════════════════════════════════════════════
def test_load_prompt_splits_system_and_user():
    sys_part, tmpl = ana.load_prompt()
    assert "不是预测器" in sys_part
    assert "{cutoff}" in tmpl and "{payload}" in tmpl


def test_load_prompt_requires_bare_fence(tmp_path):
    """围栏带语言标注（```text）会让模板解析失败 —— 这是踩过的坑。"""
    p = tmp_path / "p.md"
    p.write_text("## SYSTEM\nS\n## USER\n```text\n{payload}\n```\n", encoding="utf-8")
    with pytest.raises(ValueError):
        ana.load_prompt(p)


def test_committed_prompt_declares_explicit_schema():
    """prompt 必须给出逐字字段清单，否则模型自造字段名（实测 5/7 无效）。

    字段清单在 SYSTEM 段（schema 属于系统指令），围栏模板在 USER 段 ——
    所以两段都要看，只查模板会漏。
    """
    sys_part, tmpl = ana.load_prompt()
    both = sys_part + tmpl
    for f in ("shared_structure", "why_they_may_connect", "bridge_quality",
              "uncertainty", "falsifiable_checks", "alternative_explanations"):
        assert f in both, "prompt 未声明字段 %s" % f
    assert "不是预测器" in sys_part, "SYSTEM 段必须写明「不是预测器」"
    for v in ana.BRIDGE_QUALITY:
        assert v in both, "prompt 未给出 bridge_quality 枚举值 %s" % v


def test_spec_declares_no_future_labels_in_payload():
    import yaml
    p = ana.SPEC_PATH
    if not p.exists():
        pytest.skip("尚未冻结协议")
    spec = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert spec["policy"]["no_future_labels_in_payload"] is True
    assert spec["policy"]["evidence_year_asserted"] is True
    assert spec["evidence_window"][1] == ana.CUT


def test_assert_spec_fresh_refuses_when_prompt_changed(tmp_path, monkeypatch):
    import yaml
    spec = {"prompt_sha256": "deadbeef", "tool_sha256": ana._sha256(__file__),
            "candidates_sha256": ana._sha256(TOOL)}
    sp = tmp_path / "spec.yaml"
    sp.write_text(yaml.safe_dump(spec), encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        ana.assert_spec_fresh(sp)
    assert "重冻结" in str(e.value)


def test_freeze_spec_records_three_hashes(tmp_path, monkeypatch):
    cand = tmp_path / "c.json"
    cand.write_text(json.dumps({"candidates": []}), encoding="utf-8")
    monkeypatch.setattr(ana, "SPEC_PATH", tmp_path / "spec.yaml")
    spec = ana.freeze_spec(cand)
    for k in ("prompt_sha256", "tool_sha256", "candidates_sha256"):
        assert len(spec[k]) == 64
    assert spec["candidates_sha256"] == ana._sha256(str(cand))


# ══ 5. 片段与产物溯源 ══════════════════════════════════════════════════
def test_snippet_windows_around_the_concept_token():
    text = "x" * 300 + " epoxy resin curing behavior " + "y" * 300
    s = ana._snippet(text, ["epoxy resin"])
    assert "epoxy resin" in s and len(s) < len(text)


def test_snippet_falls_back_to_head_when_token_absent():
    s = ana._snippet("a" * 500, ["nope nope"])
    assert s == "a" * 170


def test_artifact_records_provenance_when_present():
    p = ana.OUT_PATH
    if not p.exists():
        pytest.skip("尚未落盘分析产物")
    d = json.loads(p.read_text(encoding="utf-8"))
    for k in ("candidates_sha256", "concepts_sha256", "spec_sha256", "tool_sha256"):
        assert d["inputs"].get(k), "产物缺少 %s" % k
    assert d["inputs"].get("candidates_sha256") == ana._sha256(ana.CANDIDATES_PATH)
