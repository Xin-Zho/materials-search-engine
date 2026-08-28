"""Phase 3 P1 测试：goldset / saturation / capture 三个 diagnostic（用户定 2026-08-27）。

核心原则锁死：
- 三个 diagnostic **都不拥有停止权**——saturation.py 源码不得含 STATISTICAL_STOP
- goldset 分层分开输出，无总加权分；missing 精确列出
- capture overlap=0 不崩 → INSUFFICIENT_DATA；dependent 源必须带 warning
"""

import os

import pytest

from search_engine.completeness.goldset import (
    goldset_report, normalize_doi, load_gold_set, ROLE_TO_LAYER, LAYER_ORDER,
)
from search_engine.completeness.saturation import (
    saturation_report, SaturationReport,
)
from search_engine.completeness.capture import (
    chapman_diagnostic, chao_diagnostic, INDEPENDENT_CHANNEL_TYPES,
)


# ── goldset：分层分开，无总加权分，missing 精确 ──

GOLD = [
    {"doi": "10.1016/A001", "title": "Foundational paper one", "role": "奠基",
     "must_hit": True},
    {"doi": "10.1016/A002", "title": "Foundational paper two", "role": "奠基",
     "must_hit": True},
    {"doi": "10.1016/B001", "title": "Representative paper one", "role": "代表",
     "must_hit": False},
    {"doi": "10.1016/C001", "title": "Frontier review", "role": "综述",
     "must_hit": True},
    {"doi": "10.1016/D001", "title": "3D print frontier", "role": "3D打印代表",
     "must_hit": False},
]


def test_goldset_layers_separate_no_overall_score():
    """分层分开：foundational 3/4 与 representative 5/5 必须各自呈现，不能合并。"""
    known = {"10.1016/b001"}   # 只有 representative 找到
    r = goldset_report(GOLD, known)
    assert "overall" not in str(r).lower() or "score" not in str(r).lower()
    assert r["layers"]["foundational"]["total"] == 2
    assert r["layers"]["foundational"]["found"] == 0     # 奠基全漏
    assert r["layers"]["representative"]["found"] == 1
    assert r["layers"]["frontier"]["total"] == 2
    # 无总加权分：layers 是 dict 不是单值
    assert isinstance(r["layers"], dict)


def test_goldset_must_hit_and_missing_listed():
    r = goldset_report(GOLD, {"10.1016/a001", "10.1016/a002", "10.1016/c001"})
    assert r["must_hit"]["total"] == 3
    assert r["must_hit"]["found"] == 3
    assert r["must_hit"]["recall"] == 1.0
    missing = r["missing_papers"]
    # missing 输出保留原文 DOI，用 normalize_doi 比较（大小写无关）
    assert any(normalize_doi(m["doi"]) == "10.1016/b001"
               and m["layer"] == "representative" for m in missing)
    assert any(normalize_doi(m["doi"]) == "10.1016/d001"
               and m["layer"] == "frontier" for m in missing)
    # missing 字段精确（DOI/title/role/layer/must_hit）
    m0 = missing[0]
    assert {"doi", "title", "role", "layer", "must_hit"} <= set(m0.keys())


def test_doi_normalization():
    assert normalize_doi("10.1016/S0300-5712(96)00063-2") == "10.1016/s0300-5712(96)00063-2"
    assert normalize_doi("https://doi.org/10.1016/s0300-5712(96)00063-2") == \
        "10.1016/s0300-5712(96)00063-2"
    assert normalize_doi("HTTPS://DOI.ORG/10.X") == "10.x"


def test_goldset_title_fallback():
    """gold 无 DOI 或 KB 无 DOI → title 兜底匹配。"""
    gold = [{"doi": "", "title": "Factors involved in the development of stress",
             "role": "奠基", "must_hit": True}]
    r = goldset_report(gold, set(),
                       known_by_title={"Factors involved in the development of stress": "W1"})
    assert r["layers"]["foundational"]["found"] == 1


def test_goldset_empty_insufficient():
    r = goldset_report([], set())
    assert r["status"] == "INSUFFICIENT_DATA"


def test_role_mapping():
    assert ROLE_TO_LAYER["奠基"] == "foundational"
    assert ROLE_TO_LAYER["代表"] == "representative"
    assert ROLE_TO_LAYER["综述"] == "frontier"
    assert ROLE_TO_LAYER["3D打印代表"] == "frontier"


def test_goldset_against_real_file():
    """真实 benchmarks_v1.json：15 篇、分层结构完整、无加权分。"""
    gold = load_gold_set()
    assert len(gold) == 15
    r = goldset_report(gold, set())   # 无任何已知 → 全部 missing
    assert sum(l["total"] for l in r["layers"].values()) == 15
    assert r["must_hit"]["total"] == 10


# ── saturation：边际趋势，无停止权 ──

def test_zero_gain_streak_example():
    """用户示例：10, 5, 2, 0, 0, 0 → zero_gain_streak = 3。"""
    rounds = [{"round_id": i + 1, "new_unique_papers": v}
              for i, v in enumerate([10, 5, 2, 0, 0, 0])]
    r = saturation_report(rounds)
    assert r.zero_gain_streak == 3
    assert r.status == "AVAILABLE"


def test_relative_gain_formula():
    """RelativeGain_r = NewPapers_r / CumulativePapers_{r-1}。"""
    rounds = [{"round_id": 1, "new_unique_papers": 35},
              {"round_id": 2, "new_unique_papers": 12},
              {"round_id": 3, "new_unique_papers": 4}]
    r = saturation_report(rounds)
    assert r.rounds[0].relative_gain is None            # 首轮无分母
    assert r.rounds[1].relative_gain == round(12 / 35, 4)
    assert r.rounds[2].relative_gain == round(4 / 47, 4)
    assert r.rounds[2].cumulative_unique_papers == 51


def test_retrieval_vs_relevant_saturation_separated():
    """Round5: new=50 relevant=0 ≠ Round5: new=0——两类 streak 分开记录。"""
    rounds = [
        {"round_id": i + 1, "new_unique_papers": v,
         "new_relevant_papers": rv}
        for i, (v, rv) in enumerate([(50, 5), (50, 2), (50, 0), (50, 0)])
    ]
    rep = saturation_report(rounds)
    assert rep.zero_gain_streak == 0        # retrieval 未饱和
    assert rep.relevant_zero_gain_streak == 2   # relevant 连续 2 轮 0
    # 对照：new_unique=0 的序列
    rounds2 = [{"round_id": i + 1, "new_unique_papers": v}
               for i, v in enumerate([50, 50, 0, 0])]
    assert saturation_report(rounds2).zero_gain_streak == 2


def test_saturation_insufficient_rounds():
    r = saturation_report([{"round_id": 1, "new_unique_papers": 10}])
    assert r.status == "INSUFFICIENT_DATA"


def test_saturation_source_has_no_stop():
    """源码锁死：saturation.py 不含 STATISTICAL_STOP——diagnostic 无停止权。"""
    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "search_engine", "completeness", "saturation.py")
    src = open(path, encoding="utf-8").read()
    assert "STATISTICAL_STOP" not in src
    assert "stop" not in src.lower() or "zero_gain_streak" in src  # 只有趋势字段


# ── capture：克制，不硬算，不崩 ──

def test_chapman_formula():
    """Chapman 修正手算：n1=100, n2=80, overlap=20 → (101*81)/21-1 = 388.6。"""
    d = chapman_diagnostic(100, 80, 20,
                           source_types=["LEXICAL_SEARCH", "BACKWARD_CITATION"])
    assert d.status == "AVAILABLE"
    assert d.N_hat == round((101 * 81 / 21) - 1, 1)


def test_chapman_zero_overlap_no_crash():
    """A=100 B=80 overlap=0 → 不崩，INSUFFICIENT_DATA。"""
    d = chapman_diagnostic(100, 80, 0)
    assert d.status == "INSUFFICIENT_DATA"
    assert d.N_hat is None
    assert "overlap=0" in d.reason


def test_chapman_dependent_sources_warning():
    """源被标记 dependent → 必须带 ASSUMPTION_WARNING，状态 INVALID_ASSUMPTION。"""
    d = chapman_diagnostic(100, 80, 20, dependent=True)   # 默认 dependent
    assert d.status == "INVALID_ASSUMPTION"
    assert "dependent" in d.assumption_warning


def test_chao_query_family_rejected():
    """NODE/RELATION 不是独立通道 → NOT_ENOUGH_INDEPENDENT_CHANNELS，不硬算。"""
    d = chao_diagnostic([["a"], ["b"]], source_types=["NODE", "RELATION"])
    assert d.status == "NOT_ENOUGH_INDEPENDENT_CHANNELS"
    assert d.N_hat is None


def test_independent_channel_types():
    assert INDEPENDENT_CHANNEL_TYPES == {
        "LEXICAL_SEARCH", "SEMANTIC_SEARCH", "FORWARD_CITATION",
        "BACKWARD_CITATION", "REVIEW_REFERENCE"}
