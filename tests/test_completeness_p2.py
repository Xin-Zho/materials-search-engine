"""Phase 3 P2 测试：audit 生命周期 + report 两区 + 六条 Exit Criteria（用户定 2026-08-27）。

Exit Criteria：
① 同 universe/hash/seed/sample size → sampled IDs 完全一致
② label 未完整 → 不允许输出正式 Recall_LCB
③ COMPLETE labels → m 可精确重建
④ M_upper / Recall_LCB → 与 P0 数学函数逐位一致
⑤ Diagnostic 再漂亮 → 不能改变 STATISTICAL_STOP（集成测试，极端构造）
⑥ audit replay → report 全量可复现
"""

import json

import pytest

from search_engine.completeness.audit import (
    create_audit, load_labels, replay, find_audit, AWAITING_LABELS,
    INCOMPLETE_LABELS, COMPLETED, save_audit, AuditRecord,
)
from search_engine.completeness.report import build_report
from search_engine.completeness.recall_bound import recall_lower_bound


def _synthetic_universe(F=900, U=9100, total=None):
    """合成 universe：F 篇 found relevant + U 篇 remaining（total = F+U）。"""
    total = total if total is not None else F + U
    found = [f"W{i}" for i in range(F)]
    remaining = [f"W{F + i}" for i in range(U)]
    return {"paper_ids": found + remaining, "found_relevant": found,
            "kb_version": "test-v1", "search_run_ids": ["r1"]}


def _make_labels(audit, missed_ids: set[str] | None = None,
                 reviewer: str = "test-auditor") -> dict:
    """构造完整 Auditor 标签（RELEVANT = 漏检集，其余 IRRELEVANT）。"""
    missed = missed_ids or set()
    labels = []
    for pid in audit.sampled_paper_ids:
        label = "RELEVANT" if pid in missed else "IRRELEVANT"
        labels.append({"paper_id": pid, "label": label,
                       "reviewer": reviewer, "reason": "review"})
    return {"audit_id": audit.audit_id, "universe_id": audit.universe_id,
            "labels": labels}


# ── ① sampled IDs 可复现 ──

def _paths(tmp_path):
    return dict(audits_path=str(tmp_path / "audits.json"),
                labels_dir=str(tmp_path),
                snapshots_path=str(tmp_path / "universes.json"),
                manifests_path=str(tmp_path / "manifests.json"))


def test_create_audit_deterministic(tmp_path):
    p = _paths(tmp_path)
    a1 = create_audit("pc_001", lambda: _synthetic_universe(), sample_size=500,
                      seed=42, **p)
    a2 = create_audit("pc_001", lambda: _synthetic_universe(), sample_size=500,
                      seed=42, **p)
    assert a1.sampled_paper_ids == a2.sampled_paper_ids
    assert a1.universe_hash == a2.universe_hash
    assert a1.F == 900 and a1.N_remaining == 9100
    assert a1.status == AWAITING_LABELS
    assert a1.m is None                      # 创建时系统不知道 m


# ── ② label 未完整 → 无 Recall_LCB ──

def test_incomplete_labels_no_recall(tmp_path):
    p = _paths(tmp_path)
    a = create_audit("pc_001", lambda: _synthetic_universe(), sample_size=50,
                     seed=1, **p)
    labels = _make_labels(a)
    labels["labels"] = labels["labels"][:-1]        # 少一篇
    a = load_labels(a, labels, audits_path=p["audits_path"])
    assert a.status == INCOMPLETE_LABELS
    assert a.recall_lcb is None                     # 宁可不出数
    assert a.statistical_stop is None
    assert "无标签" in a.diagnostic_warnings[0]

    # UNRESOLVED 也不给数
    a2 = find_audit(a.audit_id, p["audits_path"])
    full = _make_labels(a2)
    full["labels"][0]["label"] = "UNRESOLVED"
    a2 = load_labels(a2, full, audits_path=p["audits_path"])
    assert a2.status == INCOMPLETE_LABELS
    assert a2.recall_lcb is None


# ── ③ COMPLETE → m 精确重建 ──

def test_completed_m_rebuilt(tmp_path):
    p = _paths(tmp_path)
    a = create_audit("pc_001", lambda: _synthetic_universe(), sample_size=500,
                     seed=7, **p)
    missed = set(a.sampled_paper_ids[10:13])        # 精确 3 篇漏检
    a = load_labels(a, _make_labels(a, missed), audits_path=p["audits_path"])
    assert a.status == COMPLETED
    assert a.m == 3


# ── ④ M_upper / Recall_LCB 与 P0 逐位一致 ──

def test_statistics_match_p0(tmp_path):
    p = _paths(tmp_path)
    a = create_audit("pc_001", lambda: _synthetic_universe(), sample_size=500,
                     seed=7, **p)
    missed = set(a.sampled_paper_ids[10:13])
    a = load_labels(a, _make_labels(a, missed), audits_path=p["audits_path"])
    p0 = recall_lower_bound(F=a.F, N=a.N_remaining, n=a.sample_size,
                            m=a.m, confidence_level=a.confidence_level)
    assert a.M_upper == p0["M_upper"]
    assert abs(a.recall_lcb - p0["recall_lower"]) < 1e-9
    # statistical_stop 唯一公式：COMPLETED and recall_LCB >= target
    assert a.statistical_stop == (a.recall_lcb >= a.target_recall)


# ── ⑤ 集成：Diagnostic 不能改变 STOP（极端构造）──

def _audit_with_stats(F, N, n, m, target, status=COMPLETED):
    r = recall_lower_bound(F=F, N=N, n=n, m=m)
    a = AuditRecord(audit_id=f"t-{F}-{m}", topic_id="pc_001",
                    F=F, N_remaining=N, sample_size=n, m=m,
                    confidence_level=0.95, target_recall=target,
                    status=status, recall_lcb=r["recall_lower"],
                    M_upper=r["M_upper"], p_upper=r["p_upper"],
                    statistical_stop=r["recall_lower"] >= target)
    return a


def test_diagnostics_cannot_flip_stop():
    """极端 1：Gold 100% + saturation 20 轮 zero + capture 99.999%
    + Recall_LCB 91% < target 95% → STOP 必须 False。"""
    a = _audit_with_stats(F=900, N=9100, n=500, m=10, target=0.95)   # LCB ≈ 低
    assert a.statistical_stop is False
    shiny_diag = {
        "goldset": {"status": "AVAILABLE",
                    "layers": {L: {"found": 10, "total": 10, "recall": 1.0}
                               for L in ("foundational", "representative", "frontier")},
                    "must_hit": {"found": 10, "total": 10, "recall": 1.0},
                    "missing_papers": []},
        "saturation": {"status": "AVAILABLE", "zero_gain_streak": 20,
                       "rounds": []},
        "capture": {"status": "AVAILABLE", "N_hat": 901.0},
    }
    report = build_report(a, shiny_diag)
    assert "STATISTICAL_STOP:  NO" in report
    # Diagnostic 物理上不影响 stop：无论 A 区多漂亮，B 区 STOP 保持 False
    assert "A. Diagnostic Evidence" in report
    assert "B. Independent Statistical Audit" in report


def test_stop_yes_despite_poor_diagnostics():
    """极端 2：Gold 50% + saturation 持续新增 + capture INVALID
    + Recall_LCB 97% ≥ 95% → STOP 必须 True（但带 DIAGNOSTIC_WARNINGS）。"""
    a = _audit_with_stats(F=900, N=10000, n=2000, m=0, target=0.95)  # LCB 高
    assert a.statistical_stop is True
    a.diagnostic_warnings = ["Gold Foundational recall 50%",
                             "Saturation: 仍在持续新增",
                             "Capture: INVALID_ASSUMPTION"]
    poor_diag = {
        "goldset": {"status": "AVAILABLE",
                    "layers": {"foundational": {"found": 2, "total": 4, "recall": 0.5}},
                    "must_hit": {"found": 5, "total": 10, "recall": 0.5},
                    "missing_papers": [{"doi": "x", "title": "y"}]},
        "saturation": {"status": "AVAILABLE", "zero_gain_streak": 0,
                       "rounds": [{"round_id": 1, "new_unique_papers": 10}]},
        "capture": {"status": "INVALID_ASSUMPTION",
                    "assumption_warning": "dependent channels"},
    }
    report = build_report(a, poor_diag)
    assert "STATISTICAL_STOP:  YES" in report
    assert "DIAGNOSTIC_WARNINGS" in report          # 提示但不改 STOP
    assert "WARNING: Diagnostic evidence may reveal known gaps" in report


def test_report_has_predefined_universe_caveat(tmp_path):
    p = _paths(tmp_path)
    a = create_audit("pc_001", lambda: _synthetic_universe(), sample_size=50,
                     seed=1, **p)
    a = load_labels(a, _make_labels(a), audits_path=p["audits_path"])
    report = build_report(a, {})
    assert "Within the predefined literature universe" in report


# ── ⑥ replay 全量可复现 ──

def test_replay_reproducible(tmp_path):
    p = _paths(tmp_path)
    a = create_audit("pc_001", lambda: _synthetic_universe(), sample_size=200,
                     seed=99, **p)
    a = load_labels(a, _make_labels(a, set(a.sampled_paper_ids[:2])),
                    audits_path=p["audits_path"])
    first = (a.universe_hash, list(a.sampled_paper_ids), a.m,
             a.M_upper, a.recall_lcb, a.statistical_stop)
    replayed = replay(a.audit_id, p["audits_path"])
    assert replayed is not None
    second = (replayed.universe_hash, list(replayed.sampled_paper_ids),
              replayed.m, replayed.M_upper, replayed.recall_lcb,
              replayed.statistical_stop)
    assert first == second


# ── 报告输出含 status 三态 ──

def test_report_awaiting_labels_status(tmp_path):
    p = _paths(tmp_path)
    a = create_audit("pc_001", lambda: _synthetic_universe(), sample_size=20,
                     seed=3, **p)
    report = build_report(a, {})
    assert AWAITING_LABELS in report
    assert "不输出正式 Recall_LCB" in report
