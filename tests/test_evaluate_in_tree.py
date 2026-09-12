"""`tools/evaluate_in_tree.py` 的回归测试（树内评估）。

守四件事：
  1) 标签窗口的两个期间必须**互不重叠**（标签自己不能拿重叠的窗口比份额）
  2) 必须保留一个与特征 late 窗口(2016-2020)对齐的"严格零重叠"变体 ——
     否则"增速预测增速"会共享同一批计数，AUC 会被系统性偏置
  3) AUC 的三个边界（完全可分 / 完全反向 / 全并列）不能被算错
  4) 特征表必须同时含**变化型**与**规模型**对照量 —— 少了规模对照，
     "增长类特征正向"这个结论就变成不可比的
"""
from __future__ import annotations

import collections
import importlib.util
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(BASE, "tools", "evaluate_in_tree.py")
sys.path.insert(0, os.path.join(BASE, "tools"))

_spec = importlib.util.spec_from_file_location("eval_in_tree", TOOL)
vit = importlib.util.module_from_spec(_spec)
sys.modules["eval_in_tree"] = vit
_spec.loader.exec_module(vit)


def test_label_windows_have_disjoint_periods():
    for tag, (t0, t1), (e0, e1) in vit.LABEL_WINDOWS:
        assert t1 < e0, f"{tag}: 标签的两个期间重叠了 ({t0}-{t1} vs {e0}-{e1})"
        assert t0 <= t1 and e0 <= e1


def test_label_windows_keep_a_zero_overlap_variant():
    """必须有一个窗口的分母 = 特征的 late 窗口(2016-2020)，便于剔除窗口耦合。"""
    spans = [(t0, t1) for _, (t0, t1), _ in vit.LABEL_WINDOWS]
    assert (2016, 2020) in spans, f"缺少与特征 late 窗口对齐的标签窗口: {spans}"


def test_label_windows_are_at_least_three_so_robustness_is_checkable():
    assert len(vit.LABEL_WINDOWS) >= 3


def test_auc_perfect_inverted_and_tied():
    names = list(range(6))
    label = {n: n >= 3 for n in names}
    perfect = vit.auc_against_label(lambda n: n, names, lambda n: label[n])
    assert perfect["auc"] == 1.0 and perfect["n_pos"] == 3 and perfect["n_neg"] == 3
    inverted = vit.auc_against_label(lambda n: -n, names, lambda n: label[n])
    assert inverted["auc"] == 0.0
    tied = vit.auc_against_label(lambda n: 7, names, lambda n: label[n])
    assert tied["auc"] == 0.5


def test_auc_returns_none_when_one_class_missing():
    names = list(range(4))
    r = vit.auc_against_label(lambda n: n, names, lambda n: True)
    assert r["auc"] is None and r["n_neg"] == 0
    r = vit.auc_against_label(lambda n: n, names, lambda n: False)
    assert r["auc"] is None and r["n_pos"] == 0


def test_auc_p_is_small_for_large_perfect_separation():
    names = list(range(60))
    label = {n: n >= 30 for n in names}
    r = vit.auc_against_label(lambda n: n, names, lambda n: label[n])
    assert r["auc"] == 1.0 and r["p"] < 1e-6


def test_median_pct_is_below_half_for_inverted_score():
    """反向打分时，正例的百分位中位数必须 < 0.5 —— 这是"方向反"的量化口径。"""
    names = list(range(40))
    label = {n: n < 20 for n in names}
    r = vit.auc_against_label(lambda n: n, names, lambda n: label[n])
    assert r["auc"] == 0.0 and r["median_pct"] < 0.5


def test_value_of_prefers_scores_dict_then_row_field():
    row = {"scores": {"bridge": 0.7}, "degree": 4}
    assert vit.value_of(row, "bridge") == 0.7
    assert vit.value_of(row, "degree") == 4
    assert vit.value_of(row, "missing") is None


def test_feature_names_include_change_type_and_scale_baselines():
    rows = [{"scores": {"bridge": 0.1}, "growth": 0.2}]
    feats = vit.feature_names(rows)
    for must in ("growth", "new_edge_share", "support", "degree"):
        assert must in feats, f"特征表缺少对照量 {must}"


def test_coverage_ladder_starts_at_one_node_and_is_monotone():
    """支撑度阶梯必须是单调不增序列，且 support>=1 等于全部节点数。"""
    ladder = sorted(vit.SUPPORT_LADDER)
    assert ladder == list(vit.SUPPORT_LADDER), "支撑度阶梯未按升序书写"
    assert ladder[0] == 1


def test_internal_windows_are_disjoint_ascending_and_pre_cutoff():
    """内部门槛的三个窗口必须：升序、两两不重叠、且全部落在 cutoff(2020) 之前。"""
    ws = [(k, lo, hi) for k, (lo, hi) in vit.INTERNAL_WINDOWS]
    assert len(ws) == 3, "内部门槛需要 A/B/C 三个窗口"
    for i in range(len(ws) - 1):
        assert ws[i][2] < ws[i + 1][1], f"{ws[i]} 与 {ws[i+1]} 重叠或乱序"
    for _k, _lo, hi in ws:
        assert hi <= 2020, f"内部窗口 {_k} 越过了 cutoff，会引入未来信息"


def test_internal_feature_and_label_windows_do_not_overlap():
    """特征用 A->B、标签用 B->C：特征区间 [A,B] 与标签的被比较区间 [B,C] 必须
    只在 B 处相切，不能共享整段 —— 否则"增速预测增速"。"""
    (_, a), (_, b), (_, c) = vit.INTERNAL_WINDOWS
    assert b[1] == vit.INTERNAL_WINDOWS[1][1][1]
    assert a[1] < b[0] and b[1] < c[0], "A/B/C 必须首尾相接而不相交"


def test_gate_threshold_requires_positive_discrimination():
    """门槛必须是"正向分辨力"，不能设成 0.5 以下（否则反向特征也会被放行）。"""
    assert vit.GATE_AUC > 0.5
    assert 0 < vit.GATE_P <= 0.05


def test_auc_against_label_accepts_record_accessors():
    """门槛表用的是"记录 + 取值函数"的调用形态，必须能直接吃 dict 记录。"""
    recs = [{"v": i, "label": i >= 5} for i in range(10)]
    r = vit.auc_against_label(lambda d: d["v"], recs, lambda d: d["label"])
    assert r["auc"] == 1.0 and r["n_pos"] == 5 and r["n_neg"] == 5


# ══ 抽取族内部门槛（功效警告必须能作废判定）══════════════════════════════
def _nodes(spec):
    """spec: {name: {year: count}} -> (nodes, meta) 的 build_tree 形态。

    meta 必须一起给：抽取族份额的**分母是窗口论文数**（不是概念自己的总数）。
    用后者会造出构造性假相关（某概念全部出现于 B 期 ⇒ share_C≈0 ⇒ 标签必为负
    ⇒ `scale_B` 与标签机械反相关，AUC 假性掉到 0.13）。
    """
    nodes, meta = {}, {}
    years = collections.Counter()
    for name, ys in spec.items():
        nodes[name] = {"type": "direction", "support": sum(ys.values()),
                       "years": collections.Counter(ys),
                       "topics": collections.Counter({"t": 1}),
                       "first_seen": min(ys)}
        for y, c in ys.items():
            years[y] += c
    # meta 造出与窗口规模相称的论文量（每概念计数之和即窗口论文数下限）
    for y, c in years.items():
        for i in range(c):
            meta[f"p{y}_{i}"] = {"year": y}
    return nodes, meta


def test_extraction_sweep_refuses_verdict_when_underpowered():
    """每窗口计数 ~1 次时必须**拒绝给判定**。

    这是实测踩到的真实陷阱：计数为 1 时份额被拉普拉斯平滑的常数项主导，
    `new_entrant` / `support` 会因构造性原因拿到 0.60 的 AUC ——
    把它当成"通过门槛"比不给结论更有害。
    """
    spec = {}
    for i in range(40):
        spec[f"c{i}"] = {2012: 1, 2015: 1, 2017: 1, 2019: 1}   # 每窗口恰好 1 次
    nodes, meta = _nodes(spec)
    rep = vit.internal_extraction_sweep(nodes, min_support=3, meta=meta)
    assert rep["median_count_per_window"] == {"A": 1.0, "B": 1.0, "C": 1.0, "D": 1.0}
    assert rep["verdict"] == "UNDERPOWERED（不判定）"
    assert rep["features_passing_gate"] == [], "功效不足时不得给出通过名单"
    assert rep["power_warning"]


def test_extraction_sweep_powered_case_does_report_a_verdict():
    """计数充足时必须给出 PASS / NO_FEATURE_PASSES，而不是永远不判定。"""
    spec = {}
    for i in range(20):
        spec[f"up{i}"] = {2012: 4, 2015: 8, 2017: 16, 2019: 16}
        spec[f"down{i}"] = {2012: 4, 2015: 8, 2017: 16, 2019: 4}
    nodes, meta = _nodes(spec)
    rep = vit.internal_extraction_sweep(nodes, min_support=3, meta=meta)
    assert min(rep["median_count_per_window"].values()) >= 3
    assert rep["power_warning"] is None
    assert rep["verdict"] in ("PASS", "NO_FEATURE_PASSES")
    assert rep["n_evaluated"] == 40


def test_extraction_sweep_skips_nodes_below_min_support():
    nodes, meta = _nodes({"big": {2012: 4, 2015: 8, 2017: 16, 2019: 16},
                          "small": {2012: 1, 2015: 1, 2017: 1, 2019: 1}})
    rep = vit.internal_extraction_sweep(nodes, min_support=10, meta=meta)
    assert rep["n_evaluated"] == 1


def test_extraction_sweep_requires_window_doc_counts():
    """不给 meta（窗口论文数未知）时不得产出任何行 —— 分母缺失会退回错误的归一。"""
    nodes, _meta = _nodes({"c": {2012: 4, 2015: 8, 2017: 16, 2019: 16}})
    rep = vit.internal_extraction_sweep(nodes, min_support=3, meta=None)
    assert rep["n_evaluated"] == 0


def test_label_is_invariant_to_feature_window_counts():
    """**核心守卫**：标签不得读取特征窗口（A/B）的任何计数。

    做法不是断言某个数值区间（合成数据太容易造出完美分离，AUC=1.0 也可能是真的），
    而是断言**结构不变量**：把 A/B 窗口的计数整体放大 5 倍，标签集合必须一字不变。
    旧设计（标签 = rate_C > rate_B）会让一半标签翻转，本条测试即失败。
    """
    base = {f"c{i}": {2012: 2, 2015: 3, 2017: 5, 2019: 6} for i in range(20)}
    base.update({f"d{i}": {2012: 4, 2015: 2, 2017: 3, 2019: 1} for i in range(20)})
    nodes_a, meta_a = _nodes(base)
    bumped = {k: {y: (v * 5 if y <= 2016 else v) for y, v in ys.items()}
              for k, ys in base.items()}
    nodes_b, meta_b = _nodes(bumped)
    la = vit.internal_extraction_sweep(nodes_a, min_support=3, meta=meta_a)
    lb = vit.internal_extraction_sweep(nodes_b, min_support=3, meta=meta_b)
    assert la["n_evaluated"] == lb["n_evaluated"] == 40
    assert la["n_pos"] == lb["n_pos"], (
        "放大特征窗口计数后正例数变了 -> 标签读了特征窗口（共享项伪影）")


def test_extraction_sweep_reports_gate_constants():
    """判定必须引用与词面族同一套门槛常数（不许两套阈值）。"""
    spec = {f"c{i}": {2012: 4, 2015: 8, 2017: 16, 2019: 16} for i in range(10)}
    nodes, meta = _nodes(spec)
    rep = vit.internal_extraction_sweep(nodes, min_support=3, meta=meta)
    for f in ("growth_AB", "accel_AB", "new_entrant", "recency",
              "scale_B", "support"):
        assert f in rep["per_feature"], f"抽取族门槛缺少特征 {f}"
    assert vit.GATE_AUC > 0.5 and vit.GATE_P <= 0.05



# ══ 共享项守卫（今天用 AUC=0.014 演示过的一类致命伪影）════════════════
def test_disjoint_windows_have_no_overlap():
    """四个窗口必须两两不相交、且升序 —— 否则特征与标签会共享同一批计数。"""
    ws = [(k, lo, hi) for k, (lo, hi) in vit.DISJOINT_WINDOWS]
    assert len(ws) == 4
    for i in range(len(ws) - 1):
        assert ws[i][2] < ws[i + 1][1], f"{ws[i]} 与 {ws[i+1]} 重叠"


def test_label_uses_only_cd_and_features_only_ab():
    """核心不变量：**特征不得与标签共享窗口**。

    旧设计 `feature = rate_B/rate_A` 对 `label = rate_C > rate_B` 共享 rate_B，
    实测退化成近似恒等式：AUC = **0.014**（p=0）。那不是"均值回归"，是数学恒等式。
    故窗口必须切成四段：特征只用 A/B，标签只用 C/D。
    """
    ks = [k for k, _ in vit.DISJOINT_WINDOWS]
    assert ks == ["A", "B", "C", "D"]
    feat_ks, label_ks = ks[:2], ks[2:]
    assert not set(feat_ks) & set(label_ks), "特征窗口与标签窗口重叠"
    # 标签的两个窗口自身也不得与特征窗口相邻共享年份
    d = dict(vit.DISJOINT_WINDOWS)
    assert d["B"][1] < d["C"][0], "B 与 C 之间有年份重叠"


# ══ 溯源：评估输出必须自带可核对的输入链 ══════════════════════════════
def _load_verify_module():
    import importlib.util as iu
    p = os.path.join(BASE, "tools", "verify_artifacts.py")
    sp = iu.spec_from_file_location("verify_artifacts_for_eit", p)
    m = iu.module_from_spec(sp)
    sp.loader.exec_module(m)
    return m


def test_provenance_block_covers_concepts_db_and_tool(tmp_path):
    concepts = tmp_path / "c.jsonl"
    concepts.write_bytes(b'{' + b'"x":1}' + b'\n')

    class _D:
        DB_PATH = os.path.join(BASE, "datasets", "photopolymerization_v1",
                               "benchmark.yaml")

    blk = vit.provenance_block(_D, str(concepts))
    for k in ("concepts_file", "concepts_sha256", "db_file", "db_sha256",
              "tool_file", "tool_sha256"):
        assert k in blk, "溯源块缺少 %s" % k
    assert len(blk["concepts_sha256"]) == 64
    assert blk["tool_file"].endswith("evaluate_in_tree.py")


def test_provenance_block_survives_cross_drive_paths(tmp_path):
    """跨盘符不能让 relpath 抛错（本仓库实测过 15 处这个坑）。"""
    concepts = tmp_path / "c.jsonl"
    concepts.write_bytes(b"x\n")

    class _D:
        DB_PATH = os.path.join(BASE, "datasets", "photopolymerization_v1",
                               "benchmark.yaml")

    blk = vit.provenance_block(_D, str(concepts))
    assert isinstance(blk["concepts_file"], str) and blk["concepts_file"]


def test_provenance_block_output_passes_repo_verifier(tmp_path):
    """把溯源块塞进产物，交给仓库级核对器 —— 必须判 OK（闭环）。"""
    va = _load_verify_module()
    concepts = tmp_path / "c.jsonl"
    concepts.write_bytes(b"y\n")

    class _D:
        DB_PATH = os.path.join(BASE, "datasets", "photopolymerization_v1",
                               "benchmark.yaml")

    art = tmp_path / "report.json"
    art.write_text(json.dumps({"inputs": vit.provenance_block(_D, str(concepts))},
                              ensure_ascii=False), encoding="utf-8", newline="")
    res = va.audit_artifact(str(art))
    assert res["status"] == va.OK, res["checks"]
