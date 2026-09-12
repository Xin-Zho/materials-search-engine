"""P4-2 Emergence Score 回归测试（纯合成数据，不调用 LLM、不读真数据集）。

盯住三件容易出错的事：
  1. **反泄漏**：输入含 EVAL 或 >cutoff 的论文必须**拒绝运行**（不是警告）
  2. **采样归一化**：数据集是分层抽样、不按年份等比例。一个在前后两期
     "出现频率相同"的概念，growth 必须 ≈ 1 —— 若直接比计数就会算出假增长
  3. **被证伪的启发式不得复活**：`dir_ratio` 曾把 `photoinitiator`/
     `vinylcyclopropane` 这类**真概念**当噪声排掉，却没排掉 SEM。
     它的结论是「与'是否表征手段'正交」，故**不得**再作为门槛。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(BASE, "tools")
for p in (BASE, TOOLS):
    if p not in sys.path:
        sys.path.insert(0, p)


def _load():
    spec = importlib.util.spec_from_file_location(
        "predict_emergence_p4_2",
        os.path.join(TOOLS, "predict_emergence_p4_2.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


p2 = _load()


def _row(uid, year, concepts, relations=()):
    return {"paper_uid": uid, "status": "OK", "year": year,
            "concepts": [{"type": t, "name": n, "evidence": None}
                         for t, n in concepts],
            "relations": [{"source": s, "relation": r, "target": t}
                          for s, r, t in relations]}


def _meta(pairs, split="TRAIN"):
    return {uid: {"year": y, "primary_topic": "T", "split": split}
            for uid, y in pairs}


# ══ 1. 反泄漏：必须拒绝，不是警告 ═══════════════════════════════════════
def test_refuses_eval_side_input():
    """EVAL 侧只用于对答案；一旦进入指标计算，整个实验失效。"""
    meta = _meta([("p1", 2015)], split="EVAL")
    with pytest.raises(SystemExit) as e:
        p2.assert_no_leakage(meta, [_row("p1", 2015, [("direction", "x y")])])
    assert "EVAL" in str(e.value)


def test_refuses_future_year_input():
    meta = _meta([("p1", 2023)])
    with pytest.raises(SystemExit) as e:
        p2.assert_no_leakage(meta, [_row("p1", 2023, [("direction", "x y")])])
    assert "2023" in str(e.value) or "refused" in str(e.value)


def test_accepts_clean_train_input():
    meta = _meta([("p1", 2015), ("p2", 2018)])
    guard = p2.assert_no_leakage(
        meta, [_row("p1", 2015, [("direction", "x y")]),
               _row("p2", 2018, [("direction", "x y")])])
    assert guard["max_year"] == 2015 or guard["max_year"] == 2018
    assert guard["split_distribution"] == {"TRAIN": 2}


# ══ 2. 期归一化：不许把采样偏斜算成增长 ════════════════════════════════
def test_growth_normalizes_by_period_size():
    """构造：early 期 2 篇、late 期 8 篇；某概念在**两个期的出现率都相同**。

    若直接比计数（df_late/df_early = 4/1 = 4）会得到"增长 4 倍"的假信号；
    按期份额归一化后必须 ≈ 1。
    """
    rows = []
    meta = {}
    # early: 2 篇，其中 1 篇提到 c
    for i in range(2):
        uid = f"e{i}"
        rows.append(_row(uid, 2012, [("direction", "flat concept")] if i == 0 else []))
        meta[uid] = {"year": 2012, "primary_topic": "T", "split": "TRAIN"}
    # late: 8 篇，其中 4 篇提到 c（同样 50%）
    for i in range(8):
        uid = f"l{i}"
        rows.append(_row(uid, 2018, [("direction", "flat concept")] if i < 4 else []))
        meta[uid] = {"year": 2018, "primary_topic": "T", "split": "TRAIN"}

    metrics, block = p2.compute_metrics(rows, meta)
    m = metrics["flat concept"]
    assert m["df_early"] == 1 and m["df_late"] == 4
    # 直接比计数会是 4.0；归一化后必须接近 1
    assert 0.8 <= m["growth"] <= 1.25, f"期归一化失败：growth={m['growth']}"
    assert block["papers_period"]["early"] == 2
    assert block["papers_period"]["late"] == 8


def test_growth_detects_real_increase():
    rows, meta = [], {}
    for i in range(10):
        uid = f"e{i}"
        rows.append(_row(uid, 2012, [("direction", "rising")] if i < 1 else []))
        meta[uid] = {"year": 2012, "primary_topic": "T", "split": "TRAIN"}
    for i in range(10):
        uid = f"l{i}"
        rows.append(_row(uid, 2018, [("direction", "rising")] if i < 9 else []))
        meta[uid] = {"year": 2018, "primary_topic": "T", "split": "TRAIN"}
    m = p2.compute_metrics(rows, meta)[0]["rising"]
    assert m["growth"] > 5, f"真增长没被检出：{m['growth']}"


def test_period_block_states_why_share_not_count():
    rows = [_row("p1", 2015, [("direction", "x y")])]
    meta = _meta([("p1", 2015)])
    block = p2.compute_metrics(rows, meta)[1]
    assert "why_share_not_count" in block["period_note"]
    assert block["period_note"]["laplace"] == p2.LAPLACE


# ══ 3. 百分位排名 ═══════════════════════════════════════════════════════
def test_pct_rank_handles_ties_and_ordering():
    r = p2._pct_rank({"a": 1.0, "b": 2.0, "c": 3.0})
    assert r["c"] == 1.0 and r["a"] == 0.0 and r["b"] == 0.5
    # 并列取平均秩（顺序无关）
    t = p2._pct_rank({"a": 5.0, "b": 5.0, "c": 1.0})
    assert t["a"] == t["b"]
    assert t["c"] == 0.0
    assert p2._pct_rank({"only": 1.0}) == {"only": 0.5}


def test_pct_rank_lower_is_better_flag():
    r = p2._pct_rank({"a": 1.0, "b": 3.0}, higher_is_better=False)
    assert r["a"] == 1.0 and r["b"] == 0.0


# ══ 4. 类型过滤 + 分类型排名 ═══════════════════════════════════════════
def _metrics_for_types():
    rows, meta = [], {}
    spec = [
        ("p1", 2012, "direction", "thiol-ene chemistry"),
        ("p2", 2018, "direction", "thiol-ene chemistry"),
        ("p3", 2018, "direction", "thiol-ene chemistry"),
        ("p4", 2012, "challenge", "shrinkage stress"),
        ("p5", 2018, "challenge", "shrinkage stress"),
        ("p6", 2018, "fabrication_method", "scanning electron microscopy"),
        ("p7", 2012, "fabrication_method", "digital light processing"),
        ("p8", 2018, "fabrication_method", "digital light processing"),
        ("p9", 2018, "material", "photoinitiator"),
    ]
    for uid, y, t, n in spec:
        rows.append(_row(uid, y, [(t, n)]))
        meta[uid] = {"year": y, "primary_topic": "T", "split": "TRAIN"}
    return p2.compute_metrics(rows, meta)[0]


def test_score_filters_by_prediction_types():
    metrics = _metrics_for_types()
    rows = p2.score(metrics, 1, prediction_types=("direction", "challenge"))
    names = {r["concept"] for r in rows}
    assert "thiol-ene chemistry" in names and "shrinkage stress" in names
    assert "scanning electron microscopy" not in names
    assert "photoinitiator" not in names


def test_empty_prediction_types_keeps_everything():
    metrics = _metrics_for_types()
    rows = p2.score(metrics, 1, prediction_types=())
    assert len(rows) == len(metrics)


def test_rank_in_type_is_independent_per_type():
    """跨类型不可比 -> 每个 type 内部各自排名。"""
    metrics = _metrics_for_types()
    rows = p2.score(metrics, 1, prediction_types=("direction", "challenge"))
    by_t = {}
    for r in rows:
        by_t.setdefault(r["type"], []).append(r)
    for t, rs in by_t.items():
        assert sorted(r["rank_in_type"] for r in rs) == list(range(1, len(rs) + 1))


def test_emergence_score_is_weighted_rank_sum():
    metrics = _metrics_for_types()
    rows = p2.score(metrics, 1, prediction_types=("direction", "challenge"))
    r = rows[0]
    expect = (p2.WEIGHTS["growth"] * r["rank_growth"]
              + p2.WEIGHTS["acceleration"] * r["rank_acceleration"]
              + p2.WEIGHTS["connectivity"] * r["rank_connectivity"]
              + p2.WEIGHTS["cross_domain"] * r["rank_cross_domain"]
              + p2.WEIGHTS["novelty"] * r["rank_novelty"])
    assert abs(r["emergence_score"] - expect) < 1e-6


def test_min_support_filters_low_count_concepts():
    metrics = _metrics_for_types()
    assert p2.score(metrics, 99, prediction_types=()) == []


# ══ 5. 被证伪的启发式不得复活（回归）═══════════════════════════════════
def test_dir_ratio_must_not_be_a_gate():
    """`dir_ratio` 实测与「是否表征手段」**正交**：

    它挡掉了 photoinitiator(0.12) / vinylcyclopropane(0.00) / epoxy resin(0.15)
    这些**真概念**，却没挡掉 scanning electron microscopy。
    本测试断言：即使 dir_ratio 很低，只要类型符合，仍必须进入预测榜。
    """
    rows, meta = [], {}
    # photoinitiator 全以 part_of/requires 出现 -> dir_ratio = 0
    pairs = [("p1", 2012), ("p2", 2018), ("p3", 2018)]
    for i, (uid, y) in enumerate(pairs):
        rows.append(_row(uid, y,
                         [("material", "photoinitiator"),
                          ("mechanism", "photopolymerization")],
                         [("photopolymerization", "requires", "photoinitiator")]))
        meta[uid] = {"year": y, "primary_topic": "T", "split": "TRAIN"}
    metrics = p2.compute_metrics(rows, meta)[0]
    assert metrics["photoinitiator"]["dir_ratio"] == 0.0
    assert metrics["photoinitiator"]["directional"] is False
    # 关键：type 符合时必须留下（dir_ratio 只作诊断）
    kept = p2.score(metrics, 1, prediction_types=("material",))
    assert [r["concept"] for r in kept] == ["photoinitiator"]


def test_dir_ratio_is_recorded_for_audit():
    metrics = _metrics_for_types()
    assert "dir_ratio" in metrics["thiol-ene chemistry"]
    assert "rel_problem_solving" in metrics["thiol-ene chemistry"]
    assert "rel_total" in metrics["thiol-ene chemistry"]


# ══ 6. 冻结产物的自洽性 ════════════════════════════════════════════════
def test_frozen_json_matches_config_if_present():
    """若已冻结，产物必须自带 leakage_guard 与输入哈希（先冻结、后验证）。"""
    path = BASE + "/datasets/photopolymerization_v1/emergence_scores_p4_2.json"
    if not os.path.exists(path):
        pytest.skip("尚未冻结预测")
    d = json.load(open(path, encoding="utf-8"))
    assert d["leakage_guard"]["max_year"] <= p2.CUTOFF_YEAR
    assert "EVAL" not in d["leakage_guard"]["split_distribution"]
    assert len(d["inputs"]["concepts_sha256"]) == 64
    assert d["config"]["no_citation_features"] is True
    assert d["config"]["rejected_heuristic"]["verdict"] == "REJECTED"
    assert d["config"]["rank_within_type"] is True
    assert len(d["top_k"]) == d["config"]["top_k"]


def test_no_citation_columns_are_read():
    """反泄漏第 2 条：不得读 `citation_count` 快照（含预测窗口引用）。

    ⚠️ 必须用 AST 而不是文本搜索：字符串 `citation_count` 会出现在
    **解释它被禁用**的注释与 docstring 里 —— 文本搜索会把"说明"当成"违规"，
    逼着加豁免（本项目在 static guard 上踩过同一个坑）。
    """
    import ast
    path = os.path.join(TOOLS, "predict_emergence_p4_2.py")
    tree = ast.parse(open(path, encoding="utf-8").read())

    doc_nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)) and node.body:
            first = node.body[0]
            if (isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                doc_nodes.add(id(first.value))
    offenders = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in doc_nodes and "citation_count" in node.value):
            offenders.append(node.value[:80])
        if isinstance(node, ast.Name) and node.id == "citation_count":
            offenders.append("Name:citation_count")
        if isinstance(node, ast.Attribute) and node.attr == "citation_count":
            offenders.append("Attr:citation_count")
    assert not offenders, f"代码里出现 citation_count（快照值禁入特征）：{offenders}"
