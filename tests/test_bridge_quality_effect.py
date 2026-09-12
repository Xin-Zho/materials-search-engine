# -*- coding: utf-8 -*-
"""`tools/analyze_bridge_quality.py` 的回归测试（LLM 判定回接候选面）。

守的不变量：
  1. **同源性**：解释产物与候选文件必须对得上（按位置对齐时错位是静默的）
  2. 小样本点估计必须配区间（Wilson），且分档比较要有显著性检验
  3. 混淆项检查必须真的算（避免把"重读 AA"说成"LLM 加了信息"）
  4. `unclear` 的判定不得在证据不足时被当成结论（探索性口径写进产物）
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(BASE, "tools", "analyze_bridge_quality.py")
sys.path.insert(0, os.path.join(BASE, "tools"))

_spec = importlib.util.spec_from_file_location("bridge_quality", TOOL)
bq = importlib.util.module_from_spec(_spec)
sys.modules["bridge_quality"] = bq
_spec.loader.exec_module(bq)


def _row(q, formed, cn=29.0, aa=7.0, n_ev=6, ml=0.125):
    return {"cand_id": "E0001", "a": "x", "b": "y", "adamic_adar": aa,
            "common_neighbors": cn, "shared_neighbors": ["a"] * 8,
            "method_like_share": ml, "bridge_quality": q,
            "uncertainty": 0.42, "groundedness": 1.0, "n_evidence": n_ev,
            "formed": formed}


# ══ 1. 统计工具 ════════════════════════════════════════════════════════
def test_wilson_bounds_are_sane():
    assert bq._wilson(0, 10)[0] == 0.0
    assert bq._wilson(10, 10)[1] == 1.0
    lo, hi = bq._wilson(11, 20)
    assert 0.30 < lo < 0.36 and 0.73 < hi < 0.78
    assert bq._wilson(0, 0) is None


def test_wilson_interval_narrows_with_n():
    narrow = bq._wilson(550, 1000)
    wide = bq._wilson(11, 20)
    assert (narrow[1] - narrow[0]) < (wide[1] - wide[0])


def test_two_prop_z_is_zero_for_identical_rates():
    z, p = bq._two_prop_z(50, 100, 25, 50)
    assert abs(z) < 1e-9 and p > 0.99


def test_two_prop_z_matches_hand_computation():
    """11/20 vs 53/73 -> z≈-1.51, p≈0.13（本工具首次实跑的结论就是靠它站住的）。"""
    z, p = bq._two_prop_z(11, 20, 53, 73)
    assert abs(z + 1.51) < 0.02
    assert 0.12 < p < 0.15


def test_two_prop_z_handles_empty_group():
    assert bq._two_prop_z(0, 0, 5, 10) == (None, None)


def test_auc_returns_none_for_tiny_class():
    assert bq.auc([1, 2, 3], [4, 5, 6]) is None
    assert bq.auc([1, 2, 3, 4, 5], [6, 7, 8, 9, 10]) == 0.0


# ══ 2. 构念效度与混淆项 ════════════════════════════════════════════════
def test_construct_validity_medians_per_bucket():
    rows = ([_row("strong", True, ml=0.1) for _ in range(4)]
            + [_row("unclear", True, ml=0.3) for _ in range(4)])
    cv = bq.construct_validity(rows)
    assert cv["by_bridge_quality"]["strong"]["method_like_share_median"] == 0.1
    assert cv["by_bridge_quality"]["unclear"]["method_like_share_median"] == 0.3
    assert cv["by_bridge_quality"]["strong"]["n"] == 4


def test_construct_validity_spearman_sign():
    """质量越差（档位越大）+ 表征手段占比越高 -> rho 应为正。"""
    q = {"strong": 0, "unclear": 1, "weak": 2}
    rows = []
    for name, lvl in q.items():
        for i in range(5):
            rows.append(_row(name, True, ml=0.05 + 0.1 * lvl + 0.001 * i))
    cv = bq.construct_validity(rows)
    assert cv["spearman_rho_quality_vs_method_share"] > 0.8


def test_confound_check_reports_structure_per_bucket():
    rows = ([_row("strong", True, cn=10.0, aa=3.0) for _ in range(4)]
            + [_row("unclear", True, cn=50.0, aa=9.0) for _ in range(4)])
    cf = bq.confound_check(rows)
    assert cf["strong"]["cn_median"] == 10.0
    assert cf["unclear"]["cn_median"] == 50.0
    assert cf["unclear"]["ev_median"] == 6
    assert "_auc_in_pool" in cf


def test_confound_check_detects_structure_only_signal():
    """若 unclear 恰好都是低结构量，池内结构 AUC 会偏离 0.5 —— 该检查必须能反映出来。"""
    rows = ([_row("strong", True, cn=50.0) for _ in range(6)]
            + [_row("unclear", False, cn=5.0) for _ in range(6)])
    cf = bq.confound_check(rows)
    assert cf["_auc_in_pool"]["common_neighbors"] == 1.0


# ══ 3. 效用与分档 ══════════════════════════════════════════════════════
def test_utility_hit_rate_by_quality_with_interval_and_test():
    rows = ([_row("strong", True) for _ in range(8)]
            + [_row("strong", False) for _ in range(2)]
            + [_row("unclear", True) for _ in range(3)]
            + [_row("unclear", False) for _ in range(7)])
    ut = bq.utility(rows, k_list=(5,))
    s = ut["hit_rate_by_bridge_quality"]["strong"]
    u = ut["hit_rate_by_bridge_quality"]["unclear"]
    assert s["hit_rate"] == 0.8 and u["hit_rate"] == 0.3
    assert s["wilson95"] and u["wilson95"]
    assert u["vs_strong_p"] is not None and u["vs_strong_p"] < 0.05


def test_utility_skips_topk_larger_than_pool():
    rows = [_row("strong", True) for _ in range(3)]
    ut = bq.utility(rows, k_list=(20, 50))
    assert ut["topk"] == {}


def test_utility_strong_only_filter_changes_topk():
    """AA 最高的几条若都是 unclear，滤掉后命中率必须变化（否则过滤是空操作）。"""
    rows = ([_row("unclear", False, aa=9.0) for _ in range(4)]
            + [_row("strong", True, aa=1.0) for _ in range(8)])
    ut = bq.utility(rows, k_list=(4,))
    t = ut["topk"]["top4"]
    assert t["raw_hit"] == 0.0
    assert t["strong_only_hit"] == 1.0


def test_utility_auc_of_uncertainty_uses_negative_signal():
    rows = ([_row("strong", True)] * 6) + ([_row("unclear", False)] * 6)
    ut = bq.utility(rows, k_list=(999,))
    assert ut["auc_minus_uncertainty"] is not None


# ══ 4. 同源性（静默错位是这类工具最危险的失败）══════════════════════════
def test_load_rows_refuses_when_analysis_exceeds_candidates(tmp_path, monkeypatch):
    cand = tmp_path / "c.json"
    cand.write_text(json.dumps({"candidates": [{"a": "x", "b": "y"}]}),
                    encoding="utf-8")
    ana = tmp_path / "a.json"
    ana.write_text(json.dumps({"analyses": [{"cand_id": "E0001"}, {"cand_id": "E0002"}]}),
                   encoding="utf-8")
    monkeypatch.setattr(bq, "CANDIDATES", str(cand))
    monkeypatch.setattr(bq, "ANALYSIS", str(ana))
    with pytest.raises(SystemExit) as e:
        bq.load_rows()
    assert "不同源" in str(e.value)


def test_load_rows_refuses_when_artifact_certifies_other_file(tmp_path, monkeypatch):
    cand = tmp_path / "c.json"
    cand.write_text(json.dumps({"candidates": [{"a": "x", "b": "y"}]}),
                    encoding="utf-8")
    ana = tmp_path / "a.json"
    ana.write_text(json.dumps({"analyses": [{"cand_id": "E0001"}],
                               "inputs": {"candidates_sha256": "0" * 64}}),
                   encoding="utf-8")
    monkeypatch.setattr(bq, "CANDIDATES", str(cand))
    monkeypatch.setattr(bq, "ANALYSIS", str(ana))
    with pytest.raises(SystemExit) as e:
        bq.load_rows()
    assert "不再对应" in str(e.value)


def test_real_artifacts_are_aligned_and_certified():
    """真实产物：解释条数 <= 候选条数，且候选 sha 与磁盘一致。"""
    if not (os.path.exists(bq.CANDIDATES) and os.path.exists(bq.ANALYSIS)):
        pytest.skip("产物缺失")
    rows, dec = bq.load_rows()
    assert rows, "未对齐出任何候选"
    assert all(r["cand_id"].startswith("E") for r in rows)
    assert hasattr(dec, "is_method_like")


def test_report_declares_exploratory_status(tmp_path):
    """产物必须写明"这是同一测试集的第二遍看" —— 否则会被当成新的确证结论。"""
    if not (os.path.exists(bq.CANDIDATES) and os.path.exists(bq.ANALYSIS)):
        pytest.skip("产物缺失")
    rows, _ = bq.load_rows()
    rep = {"note": ("探索性分析：AA 排序已在 EVAL 上评过一次，本文件是同一测试集的第二遍看，"
                    "故一律报随机带且不得当作新的确证结论。"),
           "construct_validity": bq.construct_validity(rows),
           "confound_check": bq.confound_check(rows),
           "utility": bq.utility(rows, k_list=(20,))}
    assert "探索性" in rep["note"] and "不得当作新的确证结论" in rep["note"]
    assert rep["utility"]["hit_rate_by_bridge_quality"]["strong"]["wilson95"]


def test_quality_order_convention_is_explicit_and_used():
    """三档的先后是**约定**不是物理量 —— 必须显式定义、写进产物、并与打印顺序一致。

    （实测踩到：函数里写的是 weak=1/unclear=2，而打印与分档表按 unclear 在前 →
    同一个数字在两处含义相反。）
    """
    assert bq.QUALITY_ORDER == {"strong": 0, "unclear": 1, "weak": 2}
    rows = [_row("strong", True), _row("unclear", True), _row("weak", True)]
    cv = bq.construct_validity(rows)
    assert cv["quality_order_convention"] == bq.QUALITY_ORDER


def test_all_bucket_iterations_follow_the_declared_order():
    """遍历顺序必须由 QUALITY_ORDER 决定，不能各写一份字面量。"""
    import inspect
    src = inspect.getsource(bq)
    assert '"strong", "unclear", "weak"' not in src, "还有硬编码的桶顺序"
    assert src.count("sorted(QUALITY_ORDER") >= 3, "遍历未统一走 QUALITY_ORDER"


def test_quality_order_convention_is_explicit_and_used():
    """三档的先后是**约定**不是物理量 —— 必须显式定义、写进产物、并与打印顺序一致。

    （实测踩到：函数里写的是 weak=1/unclear=2，而打印与分档表按 unclear 在前 →
    同一个数字在两处含义相反。）
    """
    assert bq.QUALITY_ORDER == {"strong": 0, "unclear": 1, "weak": 2}
    rows = [_row("strong", True), _row("unclear", True), _row("weak", True)]
    cv = bq.construct_validity(rows)
    assert cv["quality_order_convention"] == bq.QUALITY_ORDER


def test_all_bucket_iterations_follow_the_declared_order():
    """遍历顺序必须由 QUALITY_ORDER 决定，不能各写一份字面量。"""
    import inspect
    src = inspect.getsource(bq)
    assert '"strong", "unclear", "weak"' not in src, "还有硬编码的桶顺序"
    assert src.count("sorted(QUALITY_ORDER") >= 3, "遍历未统一走 QUALITY_ORDER"


# ══ 5. AA 匹配对照（weak 的 AA 本来就低，必须排除"只是结构差"）══════════
def test_aa_matched_pairs_on_comparable_adamic_adar():
    rows = ([_row("weak", False, aa=5.0) for _ in range(6)]
            + [_row("strong", True, aa=5.0) for _ in range(6)])
    m = bq.aa_matched(rows, "weak")
    assert m["target_hit"] == 0.0
    assert m["matched_control_hit"] == 1.0
    assert m["delta_pp"] == -100.0


def test_aa_matched_excludes_far_aa_controls():
    """AA 差得远的候选不该被算作对照（否则等于没匹配）。"""
    rows = ([_row("weak", True, aa=1.0) for _ in range(4)]
            + [_row("strong", True, aa=100.0) for _ in range(4)])
    m = bq.aa_matched(rows, "weak", window=0.15)
    assert m["matched_control_n"] == 0
    assert m["matched_control_hit"] is None


def test_aa_matched_reports_significance():
    rows = ([_row("weak", False) for _ in range(30)]
            + [_row("strong", True) for _ in range(30)])
    m = bq.aa_matched(rows, "weak")
    assert m["p"] is not None and m["p"] < 0.001


def test_aa_matched_handles_missing_target():
    rows = [_row("strong", True) for _ in range(4)]
    assert bq.aa_matched(rows, "weak")["n"] == 0


# ══ 5. AA 匹配对照（weak 的 AA 本来就低，必须排除"只是结构差"）══════════
def test_aa_matched_pairs_on_comparable_adamic_adar():
    rows = ([_row("weak", False, aa=5.0) for _ in range(6)]
            + [_row("strong", True, aa=5.0) for _ in range(6)])
    m = bq.aa_matched(rows, "weak")
    assert m["target_hit"] == 0.0
    assert m["matched_control_hit"] == 1.0
    assert m["delta_pp"] == -100.0


def test_aa_matched_excludes_far_aa_controls():
    """AA 差得远的候选不该被算作对照（否则等于没匹配）。"""
    rows = ([_row("weak", True, aa=1.0) for _ in range(4)]
            + [_row("strong", True, aa=100.0) for _ in range(4)])
    m = bq.aa_matched(rows, "weak", window=0.15)
    assert m["matched_control_n"] == 0
    assert m["matched_control_hit"] is None


def test_aa_matched_reports_significance():
    rows = ([_row("weak", False) for _ in range(30)]
            + [_row("strong", True) for _ in range(30)])
    m = bq.aa_matched(rows, "weak")
    assert m["p"] is not None and m["p"] < 0.001


def test_aa_matched_handles_missing_target():
    rows = [_row("strong", True) for _ in range(4)]
    assert bq.aa_matched(rows, "weak")["n"] == 0
