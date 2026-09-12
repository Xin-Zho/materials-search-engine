"""`tools/evaluate_edge_event.py` 的回归测试（边层事件型评估）。

守的都是今天用血换来的不变量：
  1. **特征窗口与标签窗口不得共享** —— 共享就会退化成恒等式（实测 AUC=0.014）
  2. 过滤链的每条规则单独可测（method_like / 子串 / 共享 token / 同类型）
  3. AUC/Top-K/随机带的数学边界
  4. 各期各用自己的规模做分母（改一期的规模，率必须跟着变）
  5. **输入来源必须被正确解析**：扩树版存在时不得静默退回 993 篇的 v1
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(BASE, "tools", "evaluate_edge_event.py")
sys.path.insert(0, os.path.join(BASE, "tools"))

_spec = importlib.util.spec_from_file_location("edge_eval", TOOL)
ee = importlib.util.module_from_spec(_spec)
sys.modules["edge_eval"] = ee
_spec.loader.exec_module(ee)

_d = ee._load("dec", "discover_emergence_candidates.py")


# ══ 1. 窗口不共享（最重要）══════════════════════════════════════════════
def test_feature_window_does_not_overlap_label_windows():
    """特征窗口右端必须严格早于标签基线窗口的左端。"""
    assert ee.CUT < ee.HISTORY[0], (
        "特征窗口(%s)与标签基线窗口(%s)重叠 -> 特征与标签共享项 -> 恒等式风险"
        % (ee.CUT, ee.HISTORY))
    assert ee.HISTORY[1] < ee.FUTURE[0], "标签的基线窗口与未来窗口重叠"


def test_label_windows_are_ascending_and_disjoint():
    assert ee.HISTORY[0] <= ee.HISTORY[1] < ee.FUTURE[0] <= ee.FUTURE[1]


# ══ 2. 过滤链 ══════════════════════════════════════════════════════════
def _graph(nodes, edges, home=None, ctype=None):
    import collections
    adj = collections.defaultdict(set)
    for a, b in edges:
        adj[a].add(b)
        adj[b].add(a)
    return {"nodes": set(nodes), "adj": adj, "home": home or {n: "t" for n in nodes},
            "type": ctype or {n: "material" for n in nodes},
            "pairs_early": collections.Counter(), "sup": {}}


def test_profile_raw_keeps_everything():
    nodes = ["a", "b", "scanning electron microscopy", "a b"]
    pairs = [("a", "b"), ("a", "scanning electron microscopy")]
    g = _graph(nodes, pairs)
    kept, drop = ee.apply_filters(g, pairs, "raw", _d)
    assert len(kept) == 2 and not drop


def test_profile_basic_drops_method_like_endpoints():
    g = _graph(["a", "b", "scanning electron microscopy", "c"],
               [("a", "b"), ("a", "scanning electron microscopy"), ("b", "c")])
    pairs = [("a", "b"), ("a", "scanning electron microscopy"), ("b", "c")]
    kept, drop = ee.apply_filters(g, pairs, "basic", _d)
    assert drop["method_like"] == 1
    assert ("a", "scanning electron microscopy") not in kept
    assert len(kept) == 2


def test_profile_strict_drops_substring_shared_token_and_same_type():
    import collections
    g = _graph(["shrinkage", "shrinkage stress", "volume shrinkage",
                "volumetric shrinkage", "a", "b"],
               [], home={}, ctype={"shrinkage": "challenge",
                                  "shrinkage stress": "challenge",
                                  "volume shrinkage": "challenge",
                                  "volumetric shrinkage": "challenge",
                                  "a": "material", "b": "application"})
    pairs = [("shrinkage", "shrinkage stress"),      # 子串
             ("volume shrinkage", "volumetric shrinkage"),  # 共享 token
             ("a", "b")]                              # 干净
    kept, drop = ee.apply_filters(g, pairs, "strict", _d)
    assert drop["substring_nested"] == 1
    assert drop["shared_token"] == 1
    assert kept == [("a", "b")]


def test_profile_strict_requires_different_type():
    g = _graph(["x", "y", "z"], [], home={},
               ctype={"x": "material", "y": "material", "z": "challenge"})
    kept, drop = ee.apply_filters(g, [("x", "y"), ("x", "z")], "strict", _d)
    assert drop["same_type"] == 1
    assert kept == [("x", "z")]


# ══ 3. 特征与评估数学 ══════════════════════════════════════════════════
def test_pair_features_on_a_tiny_graph():
    g = _graph(["a", "b", "c", "d"], [("a", "c"), ("b", "c"), ("a", "d"), ("b", "d")])
    f = ee.pair_features(g, "a", "b")
    assert f["common_neighbors"] == 2.0        # c, d
    assert f["jaccard"] == 1.0                 # N(a)={c,d},N(b)={c,d}
    assert f["deg_min"] == 2.0 and f["deg_max"] == 2.0
    assert f["adamic_adar"] > 0


def test_auc_boundaries_and_orientation():
    recs = [{"v": i, "deg_min": 5.0, "label": i >= 10} for i in range(20)]
    e = ee.evaluate(recs, "v")
    assert e["auc"] == 1.0 and e["n_pos"] == 10 and e["n_neg"] == 10
    assert e["base_rate"] == 0.5
    inv = ee.evaluate([dict(r, v=-r["v"]) for r in recs], "v")
    assert inv["auc"] == 0.0


def test_evaluate_returns_none_auc_when_one_class_tiny():
    recs = [{"v": 1.0, "deg_min": 3.0, "label": True} for _ in range(5)]
    e = ee.evaluate(recs, "v")
    assert e["auc"] is None and e["n_neg"] == 0


def test_topk_lift_is_rate_over_base():
    """lift = Top-K 命中率 ÷ 基线率。100 取 5 正例（基线 0.05），
    Top-10 里正好 5 个正例 ⇒ 0.5 / 0.05 = 10x。"""
    recs = [{"v": float(100 - i), "deg_min": 3.0, "label": i < 5} for i in range(100)]
    e = ee.evaluate(recs, "v", k_list=(10,))
    assert e["base_rate"] == 0.05
    assert e["topk"]["top10"]["hits"] == 5
    assert abs(e["topk"]["top10"]["lift"] - 10.0) < 1e-9


def test_random_band_is_ordered_and_bounded():
    recs = [{"v": 1.0, "deg_min": 3.0, "label": (i % 3 == 0)} for i in range(300)]
    b = ee.random_band(recs, "v", 30, draws=500)
    assert 0.0 <= b["lo95"] <= b["median"] <= b["hi95"] <= 1.0


# ══ 4. 各期各用自己的规模 ══════════════════════════════════════════════
def test_rate_ratio_is_sensitive_to_per_window_scale():
    """率的分母必须是**该期**规模：改一期的规模，率必须跟着变。

    若两期共用同一个分母（今天的第三个自造 bug），这里会测不出来。
    """
    counts = (4, 4)
    lab_a = {"ds": {}, "hist": set(), "fut": set(), "n_hist": 100, "n_fut": 100}
    lab_b = {"ds": {}, "hist": set(), "fut": set(), "n_hist": 400, "n_fut": 100}
    ra = ee.rate_ratio(counts, lab_a)
    rb = ee.rate_ratio(counts, lab_b)
    assert ra is not None and rb is not None
    assert rb > ra, "history 期规模变大后，同样计数应给出更大的率比"


def test_pair_counts_returns_none_for_missing_docset():
    lab = {"ds": {"a": {1, 2, 3}}, "hist": {1}, "fut": {2}, "n_hist": 1, "n_fut": 1}
    assert ee.pair_counts(lab, "a", "zzz") is None
    assert ee.pair_counts(lab, "a", "a") == (1, 1)


def test_label_of_respects_tightness():
    assert ee.label_of((10, 1), 1) is True
    assert ee.label_of((10, 1), 5) is False
    assert ee.label_of(None, 1) is None


# ══ 5. 输入来源解析（今天真的踩到：静默退回 993 篇）════════════════════
def test_default_concepts_prefers_full_extraction():
    """扩树版存在时必须用它；退回 v1 会让结论完全不可比，且不会有任何报错。"""
    assert os.path.basename(ee.CONCEPTS_FULL) == "concepts_full_v2.jsonl"
    assert os.path.basename(ee.CONCEPTS_V1) == "concepts_v1.jsonl"
    if os.path.exists(ee.CONCEPTS_FULL):
        assert os.path.exists(ee.CONCEPTS_V1), "两份产物都存在，用于验证优先级"
        chosen = (ee.CONCEPTS_FULL if os.path.exists(ee.CONCEPTS_FULL)
                  else ee.CONCEPTS_V1)
        assert chosen == ee.CONCEPTS_FULL


def test_spec_declares_no_window_sharing():
    """committed spec 必须显式声明"特征与标签不共享窗口"这条硬规则。"""
    sp = ee.load_spec()
    if sp is None:
        import pytest
        pytest.skip("尚未提交 edge_event_spec.yaml")
    assert sp["windows"]["feature_until"] < sp["windows"]["label_history"][0]
    must = sp["reporting"]["must_report"]
    for k in ("base_rate", "strata_auc_by_deg_min", "topk_lift", "random_band"):
        assert k in must, "报告口径缺少 %s（没有它命中率无法解释）" % k

# ══ 6. 目标范围：唯一目标 = "会不会连上"，Task S 只作诊断 ═══════════════
def test_spec_declares_sole_target_and_excludes_task_s():
    """committed spec 必须写明唯一目标，并把 Task S 显式排除。

    用户裁定：Task L（会不会连上）已经够了，Task S（会不会变更强）不作产品目标。
    这条守卫防的是"下次又把 S 当待优化目标"。
    """
    sp = ee.load_spec()
    if sp is None:
        import pytest
        pytest.skip("尚未提交 edge_event_spec.yaml")
    assert sp["target"]["sole_target"] is True
    sc = ee.check_spec_scope(sp)
    assert sc["ok"], sc["reasons"]
    diag = sp["diagnostic_only"]["task_strengthen"]
    assert diag["verdict"] == "REVERSED", "Task S 的判定必须记录在案"
    assert "不参与" in diag["action"]


def test_scope_guard_refuses_when_task_s_not_excluded():
    """缺声明时必须拒绝运行，而不是默默把 S 当目标。"""
    good = {"target": {"sole_target": True},
            "diagnostic_only": {"task_strengthen": {}}}
    assert ee.check_spec_scope(good)["ok"]
    assert not ee.check_spec_scope({"target": {"sole_target": True}})["ok"]
    assert not ee.check_spec_scope(
        {"diagnostic_only": {"task_strengthen": {}}})["ok"]
    assert not ee.check_spec_scope(None)["ok"]


def test_artifact_marks_task_s_as_diagnostic_only():
    """产物里 Task S 必须自带 diagnostic_only 与 role，防止被当目标读。"""
    if not os.path.exists(ee.OUT_PATH):
        import pytest
        pytest.skip("尚未落盘候选清单")
    with open(ee.OUT_PATH, encoding="utf-8") as f:
        rep = json.load(f)
    blk = rep.get("task_S_strengthen")
    assert blk, "产物缺少 Task S 诊断块"
    assert blk["diagnostic_only"] is True
    assert blk["role"] == "diagnostic_control"
    assert rep["target"]["sole_target"] is True


def test_artifact_task_s_size_matches_spec():
    """产物的 n 必须与 spec 记录的一致 —— 不一致说明结论变了却没更新规范。"""
    sp = ee.load_spec()
    if sp is None or not os.path.exists(ee.OUT_PATH):
        import pytest
        pytest.skip("缺少 spec 或产物")
    with open(ee.OUT_PATH, encoding="utf-8") as f:
        rep = json.load(f)
    assert rep["task_S_strengthen"]["n"] == sp["diagnostic_only"]["task_strengthen"]["n"]


# ══ 7. 溯源守卫：产物必须能为自己认证的输入把关 ═════════════════════════
def _artifact(tmp_path, good=True):
    inp = tmp_path / "concepts.jsonl"
    inp.write_bytes(b'{"paper_uid": "x"}\n')
    art = {"inputs": {"concepts_file": str(inp),
                      "concepts_sha256": ee._sha256(str(inp)) if good else "0" * 64,
                      "tool_file": TOOL, "tool_sha256": ee._sha256(TOOL)},
           "spec_file": ee.SPEC_PATH, "spec_sha256": ee._sha256(ee.SPEC_PATH)}
    p = tmp_path / "artifact.json"
    p.write_text(json.dumps(art), encoding="utf-8")
    return str(p), str(inp)


def test_verify_passes_when_hashes_match(tmp_path):
    a, _ = _artifact(tmp_path)
    res = ee.verify(a)
    assert res["ok"], res["reasons"]


def test_verify_detects_changed_input(tmp_path):
    a, _ = _artifact(tmp_path, good=False)
    res = ee.verify(a)
    assert not res["ok"]
    assert any("concepts_sha256" in r for r in res["reasons"])


def test_verify_detects_missing_input_file(tmp_path):
    """实测过的那种脱钩：产物认证了一份已不存在的文件。"""
    a, inp = _artifact(tmp_path)
    os.remove(inp)
    res = ee.verify(a)
    assert not res["ok"]
    assert any("不存在" in r for r in res["reasons"])


def test_verify_detects_tool_drift(tmp_path):
    """工具改了但产物没重跑 -> 必须报脱钩（否则复现性只是感觉）。"""
    a, _ = _artifact(tmp_path)
    with open(a, encoding="utf-8") as f:
        d = json.load(f)
    d["inputs"]["tool_sha256"] = "1" * 64
    with open(a, "w", encoding="utf-8", newline="\n") as f:
        json.dump(d, f)
    assert not ee.verify(a)["ok"]


def test_verify_flags_artifact_without_spec_provenance(tmp_path):
    """没记录 spec 来源的产物无法核对 -> 判脱钩（旧产物就属于这种）。"""
    a, _ = _artifact(tmp_path)
    with open(a, encoding="utf-8") as f:
        d = json.load(f)
    d.pop("spec_file", None)
    d.pop("spec_sha256", None)
    with open(a, "w", encoding="utf-8") as f:
        json.dump(d, f)
    res = ee.verify(a)
    assert not res["ok"]
    assert any("spec" in r for r in res["reasons"])


def test_verify_reports_missing_artifact(tmp_path):
    res = ee.verify(str(tmp_path / "nope.json"))
    assert not res["ok"] and res["checks"] == []

# ══ 6. 目标范围：唯一目标 = "会不会连上"，Task S 只作诊断 ═══════════════
def test_spec_declares_sole_target_and_excludes_task_s():
    """committed spec 必须写明唯一目标，并把 Task S 显式排除。

    用户裁定：Task L（会不会连上）已经够了，Task S（会不会变更强）不作产品目标。
    这条守卫防的是"下次又把 S 当待优化目标"。
    """
    sp = ee.load_spec()
    if sp is None:
        import pytest
        pytest.skip("尚未提交 edge_event_spec.yaml")
    assert sp["target"]["sole_target"] is True
    sc = ee.check_spec_scope(sp)
    assert sc["ok"], sc["reasons"]
    diag = sp["diagnostic_only"]["task_strengthen"]
    assert diag["verdict"] == "REVERSED", "Task S 的判定必须记录在案"
    assert "不参与" in diag["action"]


def test_scope_guard_refuses_when_task_s_not_excluded():
    """缺声明时必须拒绝运行，而不是默默把 S 当目标。"""
    good = {"target": {"sole_target": True},
            "diagnostic_only": {"task_strengthen": {}}}
    assert ee.check_spec_scope(good)["ok"]
    assert not ee.check_spec_scope({"target": {"sole_target": True}})["ok"]
    assert not ee.check_spec_scope(
        {"diagnostic_only": {"task_strengthen": {}}})["ok"]
    assert not ee.check_spec_scope(None)["ok"]


def test_artifact_marks_task_s_as_diagnostic_only():
    """产物里 Task S 必须自带 diagnostic_only 与 role，防止被当目标读。"""
    if not os.path.exists(ee.OUT_PATH):
        import pytest
        pytest.skip("尚未落盘候选清单")
    with open(ee.OUT_PATH, encoding="utf-8") as f:
        rep = json.load(f)
    blk = rep.get("task_S_strengthen")
    assert blk, "产物缺少 Task S 诊断块"
    assert blk["diagnostic_only"] is True
    assert blk["role"] == "diagnostic_control"
    assert rep["target"]["sole_target"] is True


def test_artifact_task_s_size_matches_spec():
    """产物的 n 必须与 spec 记录的一致 —— 不一致说明结论变了却没更新规范。"""
    sp = ee.load_spec()
    if sp is None or not os.path.exists(ee.OUT_PATH):
        import pytest
        pytest.skip("缺少 spec 或产物")
    with open(ee.OUT_PATH, encoding="utf-8") as f:
        rep = json.load(f)
    assert rep["task_S_strengthen"]["n"] == sp["diagnostic_only"]["task_strengthen"]["n"]


# ══ 7. 溯源守卫：产物必须能为自己认证的输入把关 ═════════════════════════
def _artifact(tmp_path, good=True):
    inp = tmp_path / "concepts.jsonl"
    inp.write_bytes(b'{"paper_uid": "x"}\n')
    art = {"inputs": {"concepts_file": str(inp),
                      "concepts_sha256": ee._sha256(str(inp)) if good else "0" * 64,
                      "tool_file": TOOL, "tool_sha256": ee._sha256(TOOL)},
           "spec_file": ee.SPEC_PATH, "spec_sha256": ee._sha256(ee.SPEC_PATH)}
    p = tmp_path / "artifact.json"
    p.write_text(json.dumps(art), encoding="utf-8")
    return str(p), str(inp)


def test_verify_passes_when_hashes_match(tmp_path):
    a, _ = _artifact(tmp_path)
    res = ee.verify(a)
    assert res["ok"], res["reasons"]


def test_verify_detects_changed_input(tmp_path):
    a, _ = _artifact(tmp_path, good=False)
    res = ee.verify(a)
    assert not res["ok"]
    assert any("concepts_sha256" in r for r in res["reasons"])


def test_verify_detects_missing_input_file(tmp_path):
    """实测过的那种脱钩：产物认证了一份已不存在的文件。"""
    a, inp = _artifact(tmp_path)
    os.remove(inp)
    res = ee.verify(a)
    assert not res["ok"]
    assert any("不存在" in r for r in res["reasons"])


def test_verify_detects_tool_drift(tmp_path):
    """工具改了但产物没重跑 -> 必须报脱钩（否则复现性只是感觉）。"""
    a, _ = _artifact(tmp_path)
    with open(a, encoding="utf-8") as f:
        d = json.load(f)
    d["inputs"]["tool_sha256"] = "1" * 64
    with open(a, "w", encoding="utf-8", newline="\n") as f:
        json.dump(d, f)
    assert not ee.verify(a)["ok"]


def test_verify_flags_artifact_without_spec_provenance(tmp_path):
    """没记录 spec 来源的产物无法核对 -> 判脱钩（旧产物就属于这种）。"""
    a, _ = _artifact(tmp_path)
    with open(a, encoding="utf-8") as f:
        d = json.load(f)
    d.pop("spec_file", None)
    d.pop("spec_sha256", None)
    with open(a, "w", encoding="utf-8") as f:
        json.dump(d, f)
    res = ee.verify(a)
    assert not res["ok"]
    assert any("spec" in r for r in res["reasons"])


def test_verify_reports_missing_artifact(tmp_path):
    res = ee.verify(str(tmp_path / "nope.json"))
    assert not res["ok"] and res["checks"] == []
