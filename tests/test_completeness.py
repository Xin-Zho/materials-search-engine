"""Phase 3 P0 测试：universe / sampler / recall_bound 数学 invariant（用户定 2026-08-27）。

用户要求的 invariant：
- m 越大 → M_upper 越大 → Recall_lower 越低
- n 越大且 m=0 → Recall_lower 越高
- confidence 99% > 95% → 上界更保守 → Recall_lower 更低
- 同 seed 抽样可复现（相同 universe + seed + size → 完全相同样本）
- universe hash 稳定（同参同 hash，内容变则变）
- m=0 也不能说 100%（p_upper > 0）
- UniverseSnapshot 冻结后不可变（剩余池由 found_relevant 推导）
"""

import json

import pytest

from search_engine.completeness.universe import (
    UniverseSnapshot, freeze_universe, universe_hash, load_snapshots,
)
from search_engine.completeness.sampler import (
    SamplingManifest, draw_sample, load_manifests,
)
from search_engine.completeness.recall_bound import (
    hypergeom_cdf_le, missed_relevant_upper_bound, recall_lower_bound,
)


# ── recall_bound：数学 invariant ──

def test_hypergeom_cdf_matches_brute_force():
    """浮点递推 vs 精确组合数（math.comb）在小 N 上逐点一致。"""
    from math import comb
    N, M, n = 50, 20, 10
    exact = sum(comb(M, k) * comb(N - M, n - k) / comb(N, n) for k in range(0, 5))
    approx = hypergeom_cdf_le(4, N, M, n)
    assert abs(exact - approx) < 1e-9


def test_m_upper_increases_with_m():
    """m 越大 → 剩余池漏检上界越大 → recall 下界越低。"""
    F, N, n = 900, 9100, 500
    bounds = [recall_lower_bound(F, N, n, m) for m in (0, 1, 5, 20)]
    M_uppers = [b["M_upper"] for b in bounds]
    assert M_uppers == sorted(M_uppers), "M_upper 必须随 m 单调不减"
    recalls = [b["recall_lower"] for b in bounds]
    assert recalls == sorted(recalls, reverse=True), "Recall_lower 必须随 m 单调不增"


def test_larger_sample_tighter_bound_when_zero_misses():
    """n 越大且 m=0 → 上界更紧（M_upper 更小）→ Recall_lower 更高。"""
    F, N = 900, 9100
    b500 = recall_lower_bound(F, N, 500, 0)
    b2000 = recall_lower_bound(F, N, 2000, 0)
    assert b2000["M_upper"] < b500["M_upper"]
    assert b2000["recall_lower"] > b500["recall_lower"]


def test_higher_confidence_more_conservative():
    """99% > 95% → 更保守（M_upper 更大）→ Recall_lower 更低。"""
    F, N, n, m = 900, 9100, 500, 1
    b95 = recall_lower_bound(F, N, n, m, confidence_level=0.95)
    b99 = recall_lower_bound(F, N, n, m, confidence_level=0.99)
    assert b99["M_upper"] > b95["M_upper"]
    assert b99["recall_lower"] < b95["recall_lower"]


def test_zero_misses_not_hundred_percent():
    """m=0 不能推出 100% recall——p_upper > 0，Recall_lower < 1。"""
    b = recall_lower_bound(F=900, N=9100, n=500, m=0)
    assert b["p_upper"] > 0
    assert b["recall_lower"] < 1.0


def test_user_worked_example_order_of_magnitude():
    """用户示例：N=5000, n=300, m=1 → p_upper 量级 ~0.018，recall ~90%。"""
    b = recall_lower_bound(F=900, N=5000, n=300, m=1)
    assert 0.005 < b["p_upper"] < 0.05      # 超几何精确值（0.0152）比二项近似紧
    assert 0.85 < b["recall_lower"] < 0.97


def test_full_census_exact():
    """n == N（全池检查）→ M_upper = m，无统计不确定性。"""
    assert missed_relevant_upper_bound(N=9100, n=9100, m=3) == 3


# ── universe：冻结快照 ──

def test_universe_hash_stable_and_sensitive():
    ids = ["a", "b", "c"]
    h1 = universe_hash(ids, "kb_v1")
    h2 = universe_hash(["c", "a", "b"], "kb_v1")   # 顺序无关
    assert h1 == h2
    assert universe_hash(ids, "kb_v2") != h1        # 版本变 → hash 变
    assert universe_hash(ids + ["d"], "kb_v1") != h1  # 论文变 → hash 变


def test_freeze_universe_requires_found_relevant():
    with pytest.raises(ValueError, match="found_relevant"):
        freeze_universe("pc_001", ["a", "b", "c"])


def test_remaining_pool_derived_from_breakdown():
    snap = freeze_universe(
        "pc_001",
        ["a", "b", "c", "d", "e"],
        kb_version="kb1",
        source_breakdown={"found_relevant": ["a", "b"]})
    assert snap.total_count == 5
    assert snap.found_relevant_count() == 2
    assert snap.remaining_pool() == ["c", "d", "e"]
    assert snap.remaining_pool_size() == 3


def test_snapshot_idempotent_save(tmp_path):
    snap = freeze_universe("pc_001", ["a", "b"], kb_version="kb1",
                           source_breakdown={"found_relevant": ["a"]})
    p = str(tmp_path / "universes.json")
    from search_engine.completeness.universe import save_snapshot
    save_snapshot(snap, p)
    save_snapshot(snap, p)                     # 同 universe_id 幂等
    snaps = load_snapshots(p)
    assert len(snaps) == 1


# ── sampler：可复现抽样 ──

def test_same_seed_same_sample():
    pool = [f"p{i}" for i in range(1000)]
    s1 = draw_sample(pool, 300, seed=42)
    s2 = draw_sample(pool, 300, seed=42)
    assert s1.sampled_paper_ids == s2.sampled_paper_ids
    assert s1.remaining_population_size == 1000


def test_different_seed_different_sample():
    pool = [f"p{i}" for i in range(1000)]
    s1 = draw_sample(pool, 300, seed=42)
    s2 = draw_sample(pool, 300, seed=7)
    assert s1.sampled_paper_ids != s2.sampled_paper_ids


def test_sample_without_replacement():
    pool = [f"p{i}" for i in range(100)]
    s = draw_sample(pool, 50, seed=1)
    assert len(s.sampled_paper_ids) == 50
    assert len(set(s.sampled_paper_ids)) == 50   # 不放回


def test_sample_size_capped_at_pool():
    pool = [f"p{i}" for i in range(10)]
    s = draw_sample(pool, 500, seed=1)
    assert s.sample_size == 10                   # 全池（完全检查）


def test_manifest_persist_roundtrip(tmp_path):
    pool = [f"p{i}" for i in range(100)]
    s = draw_sample(pool, 30, seed=5, audit_id="a1", universe_id="u1")
    p = str(tmp_path / "manifests.json")
    from search_engine.completeness.sampler import save_manifest
    save_manifest(s, p)
    ms = load_manifests(p)
    assert len(ms) == 1
    assert ms[0]["audit_id"] == "a1"
    assert ms[0]["sampled_paper_ids"] == s.sampled_paper_ids
