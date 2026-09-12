# -*- coding: utf-8 -*-
"""`tools/build_edge_shortlist.py` 的回归测试（产品过滤层）。

守的不变量：
  1. **同源性**：解释产物必须认证同一份候选文件（条数 + sha + cand_id 位置）
  2. **产品列不得含评估期字段**（eval/joint_fut/rate_ratio 出现即拒绝）
  3. **窗口口径**：`not_analyzed` 必须显式给出，且不得与"被剔除"混为一谈
  4. **截断后评估块同源**：`--top-n` 截断后分子分母都要按截断后的名单算
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(BASE, "tools", "build_edge_shortlist.py")
sys.path.insert(0, os.path.join(BASE, "tools"))

_spec = importlib.util.spec_from_file_location("edge_shortlist", TOOL)
sl = importlib.util.module_from_spec(_spec)
sys.modules["edge_shortlist"] = sl
_spec.loader.exec_module(sl)


def _cand(a, b, aa, fut=7, **kw):
    r = {"a": a, "b": b, "type_a": "material", "type_b": "application",
         "home_a": "t1", "home_b": "t2", "adamic_adar": aa, "common_neighbors": 3.0,
         "joint_fut": fut}
    r.update(kw)
    return r


def _ana(items, sha=None):
    rows = []
    for i, q in enumerate(items, 1):
        rows.append({"cand_id": "E%04d" % i,
                     "analysis": {"bridge_quality": q, "uncertainty": 0.42,
                                  "shared_structure": "共享 composite",
                                  "falsifiable_checks": ["2016 后若 X 则支持"]}})
    out = {"analyses": rows}
    if sha:
        out["inputs"] = {"candidates_sha256": sha}
    return out


def _write(tmp_path, payload, name):
    p = tmp_path / name
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(p)


# ══ 1. 同源性 ═══════════════════════════════════════════════════════════
def test_load_inputs_accepts_consistent_pair(tmp_path):
    c = _write(tmp_path, {"candidates": [_cand("x", "y", 9.0)]}, "c.json")
    a = _write(tmp_path, _ana(["strong"]), "a.json")
    cand, cands, rows, sha = sl.load_inputs(c, a)
    assert len(cands) == 1 and len(rows) == 1 and len(sha) == 64


def test_load_inputs_refuses_more_analyses_than_candidates(tmp_path):
    c = _write(tmp_path, {"candidates": [_cand("x", "y", 9.0)]}, "c.json")
    a = _write(tmp_path, _ana(["strong", "weak"]), "a.json")
    with pytest.raises(SystemExit) as e:
        sl.load_inputs(c, a)
    assert "不同源" in str(e.value)


def test_load_inputs_refuses_sha_mismatch(tmp_path):
    c = _write(tmp_path, {"candidates": [_cand("x", "y", 9.0)]}, "c.json")
    a = _write(tmp_path, _ana(["strong"], sha="0" * 64), "a.json")
    with pytest.raises(SystemExit) as e:
        sl.load_inputs(c, a)
    assert "已变" in str(e.value)


def test_load_inputs_refuses_position_id_mismatch(tmp_path):
    c = _write(tmp_path, {"candidates": [_cand("x", "y", 9.0)]}, "c.json")
    payload = _ana(["strong"])
    payload["analyses"][0]["cand_id"] = "E0007"          # 位置对不上
    a = _write(tmp_path, payload, "a.json")
    with pytest.raises(SystemExit) as e:
        sl.load_inputs(c, a)
    assert "位置不一致" in str(e.value)


# ══ 2. 产品列纯净度 ═════════════════════════════════════════════════════
def test_product_columns_exclude_eval_only_fields():
    cands = [_cand("x", "y", 9.0, joint_fut=7)]
    rows = _ana(["strong"])["analyses"]
    rep = sl.build({}, cands, rows)
    for item in rep["shortlist"]:
        assert set(item) == set(sl.PRODUCT_FIELDS)
        for k in item:
            assert not any(t in k.lower() for t in sl.EVAL_ONLY_RE), k


def test_build_refuses_when_product_spec_pollutes_columns(monkeypatch):
    """把评估期字段塞进产品列规格 -> 必须拒绝（而不是安静地写进产物）。"""
    monkeypatch.setattr(sl, "PRODUCT_FIELDS",
                        sl.PRODUCT_FIELDS + ("eval_formed",))
    with pytest.raises(SystemExit) as e:
        sl.build({}, [_cand("x", "y", 9.0)], _ana(["strong"])["analyses"])
    assert "评估期字段" in str(e.value)


def test_excluded_rows_do_not_appear_in_shortlist():
    cands = [_cand("keep", "y", 9.0), _cand("drop", "z", 8.0)]
    rows = _ana(["strong", "weak"])["analyses"]
    rep = sl.build({}, cands, rows)
    names = [it["a"] for it in rep["shortlist"]]
    assert names == ["keep"]
    assert [x["a"] for x in rep["excluded"]] == ["drop"]


# ══ 3. 排序与窗口口径 ═══════════════════════════════════════════════════
def test_shortlist_is_aa_descending_and_ranked_from_one():
    cands = [_cand("c", "d", 5.0), _cand("a", "b", 9.0), _cand("e", "f", 7.0)]
    rows = _ana(["strong"] * 3)["analyses"]
    rep = sl.build({}, cands, rows)
    assert [it["adamic_adar"] for it in rep["shortlist"]] == [9.0, 7.0, 5.0]
    assert [it["rank"] for it in rep["shortlist"]] == [1, 2, 3]


def test_window_accounts_for_unanalyzed_candidates():
    """未分析 != 不合格，必须单独计数并在产物里写明。"""
    cands = [_cand("a", "b", 9.0)] + [_cand("u%d" % i, "v%d" % i, 1.0) for i in range(4)]
    rows = _ana(["strong"])["analyses"]
    rep = sl.build({}, cands, rows)
    assert rep["windowing"]["analyzed_window"] == 1
    assert rep["windowing"]["n_candidates_total"] == 5
    assert rep["windowing"]["not_analyzed"] == 4
    assert "未评估" in rep["windowing"]["note"]


def test_top_n_truncates_and_eval_block_stays_consistent():
    """截断后评估块必须按截断后的名单算（否则命中率会 >100%）。"""
    cands = [_cand("a", "b", 9.0, joint_fut=7), _cand("c", "d", 8.0, joint_fut=0),
             _cand("e", "f", 7.0, joint_fut=0)]
    rows = _ana(["strong"] * 3)["analyses"]
    rep = sl.build({}, cands, rows, top_n=1)
    assert len(rep["shortlist"]) == 1
    ev = rep["evaluation"]["eval_formed"]
    assert ev["shortlist_n"] == 1
    assert ev["shortlist_hit_rate"] == 1.0        # 唯一一条是正例
    assert 0.0 <= ev["window_base_rate"] <= 1.0


def test_keep_can_include_unclear():
    cands = [_cand("a", "b", 9.0), _cand("c", "d", 8.0)]
    rows = _ana(["strong", "unclear"])["analyses"]
    rep = sl.build({}, cands, rows, keep=("strong", "unclear"))
    assert len(rep["shortlist"]) == 2 and rep["counts"]["excluded_by_bridge"] == 0


# ══ 4. CSV 与真实产物 ═══════════════════════════════════════════════════
def test_write_csv_has_header_and_escapes_quotes(tmp_path):
    p = tmp_path / "s.csv"
    sl.write_csv(str(p), [{"rank": 1, "a": 'he said "hi"', "b": "x"}])
    txt = p.read_text(encoding="utf-8")
    assert txt.splitlines()[0].split(",")[:2] == ["rank", "a"]
    assert '""hi""' in txt


def test_real_artifacts_build_and_carry_provenance():
    if not (os.path.exists(sl.CANDIDATES) and os.path.exists(sl.ANALYSIS)):
        pytest.skip("产物缺失")
    cand, cands, rows, sha = sl.load_inputs()
    rep = sl.build(cand, cands, rows)
    assert rep["counts"]["shortlist"] > 0
    assert rep["windowing"]["not_analyzed"] == len(cands) - len(rows)
    assert rep["evaluation"]["eval_formed"]["note"].find("不得") >= 0
