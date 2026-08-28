"""Phase 2.1c trajectory 测试（用户定：只记录事实，不定 RL reward）。

覆盖：schema 字段齐全（无 reward 字段）、upsert 幂等合并、outcome 事实聚合
（retrieved / new_unique / relevant / edges / candidates / unseen）、
downstream 后期补写、analyze by_family。
"""

import json

import pytest

from search_engine.discovery.trajectory import (
    DiscoveryTrajectory, load_trajectories, save_trajectories, upsert_trajectory,
    outcome_from_query, record_from_query, update_downstream, analyze_trajectories,
)


def _reg(qid="q1", retrieved=10, family="ADJACENT", text="bulk-fill x") -> list[dict]:
    return [{"query_id": qid, "query_family": family, "query_text": text,
             "source_node": "bulk-fill composite formulation",
             "retrieved_count": retrieved, "status": "SUCCEEDED",
             "origin_round": 2}]


def _prov(paper_id, qid="q1", family="ADJACENT") -> list[dict]:
    return [{"paper_id": paper_id, "query_id": qid, "query_family": family,
             "promoted_node": "bulk-fill composite formulation"}]


def _staging(paper_id, status="RELEVANT", qids=("q1",)) -> list[dict]:
    return [{"paper_id": paper_id, "relevance_status": status,
             "query_ids": list(qids)}]


def _edges(qid="q1", mech="filler surface treatment", paper="W1") -> list[dict]:
    return [{"paper_id": paper, "raw_mechanism": mech, "raw_route": "",
             "discovery_provenance": {"query_id": qid,
                                      "promoted_node": "bulk-fill composite formulation"}}]


def _pool(names=("bulk-fill composite formulation",)) -> list[dict]:
    return [{"raw_name": n} for n in names]


# ── schema：只记事实，无 reward ──

def test_trajectory_schema_fields():
    t = DiscoveryTrajectory(trajectory_id="2::q1", origin_round=2)
    d = t.to_dict()
    assert set(d.keys()) >= {"trajectory_id", "origin_round", "state", "action",
                             "outcome", "downstream", "cost"}
    # 用户定：先只记录事实，不定义 RL reward——schema 无 reward 字段
    assert "reward" not in d and "return" not in d
    for sec in ("state", "action", "outcome", "downstream", "cost"):
        assert isinstance(d[sec], dict)


def test_trajectory_full_fill():
    t = DiscoveryTrajectory(
        trajectory_id="2::q1", origin_round=2,
        state={"ontology_version": "a", "kb_version": "b", "candidate_pool_size": 208},
        action={"promoted_node": "bulk-fill", "query_id": "q1",
                "query_family": "ADJACENT", "query_text": "bulk-fill x"},
        outcome={"retrieved_total": 10, "new_unique_papers": 3,
                 "new_relevant_papers": 2, "new_edges": 1,
                 "new_candidates": ["x"], "new_candidate_not_seen_before": ["x"]},
        downstream={"validated_candidates": [], "promoted_candidates": []},
        cost={"api_calls": 1, "llm_calls": 1})
    assert t.trajectory_id == "2::q1"


# ── upsert 幂等 ──

def test_upsert_idempotent_merge(tmp_path):
    path = tmp_path / "traj.json"
    records = []
    t1 = DiscoveryTrajectory(trajectory_id="2::q1", origin_round=2,
                             outcome={"retrieved_total": 10, "new_unique_papers": 3})
    assert upsert_trajectory(records, t1, path=str(path)) is True   # 新增
    t2 = DiscoveryTrajectory(trajectory_id="2::q1", origin_round=2,
                             outcome={"retrieved_total": 10, "new_unique_papers": 3,
                                      "new_edges": 2},               # P2.3 补写
                             downstream={"validated_candidates": ["x"]})
    assert upsert_trajectory(records, t2, path=str(path)) is False  # 更新
    assert len(records) == 1                                        # 不双写
    assert records[0]["outcome"]["new_edges"] == 2                  # 新字段合并
    assert records[0]["outcome"]["new_unique_papers"] == 3          # 旧字段保留
    assert records[0]["downstream"]["validated_candidates"] == ["x"]
    assert len(load_trajectories(str(path))) == 1


# ── outcome 事实聚合 ──

def test_outcome_from_query_aggregates():
    outcome = outcome_from_query(
        "q1",
        _reg(retrieved=10),
        _prov("W1") + _prov("W2"),
        _staging("W1", "RELEVANT") + _staging("W2", "UNCERTAIN"),
        _edges(qid="q1", mech="filler surface treatment") + _edges(qid="q1", mech="low modulus"),
        _pool())
    assert outcome["retrieved_total"] == 10
    assert outcome["new_unique_papers"] == 2          # W1, W2
    assert outcome["new_relevant_papers"] == 1        # W1 RELEVANT
    assert outcome["new_edges"] == 2
    assert outcome["new_candidates"] == ["filler surface treatment", "low modulus"]
    assert outcome["new_candidate_not_seen_before"] == \
        ["filler surface treatment", "low modulus"]   # 不在历史池


def test_outcome_unseen_excludes_known():
    """新候选名与历史池重复 → 不算 unseen（旧候选重新发现不算）。"""
    outcome = outcome_from_query("q1", _reg(), _prov("W1"),
                                 _staging("W1"), _edges(qid="q1"),
                                 _pool(names=("bulk-fill composite formulation",
                                              "filler surface treatment")))
    assert outcome["new_candidates"] == ["filler surface treatment"]
    assert outcome["new_candidate_not_seen_before"] == []   # 已在池 → 不算新


def test_outcome_filters_by_query_id():
    """只聚合该 query 的 provenance/edges（跨 query 不串）。"""
    outcome = outcome_from_query(
        "q1", _reg(retrieved=5),
        _prov("W1", qid="q1") + _prov("W9", qid="other"),
        _staging("W1"),
        _edges(qid="q1") + _edges(qid="other", mech="other mech"),
        _pool())
    assert outcome["new_unique_papers"] == 1           # 只 W1
    assert outcome["new_edges"] == 1
    assert outcome["new_candidates"] == ["filler surface treatment"]


# ── record_from_query + downstream 补写 ──

def test_record_from_query_upserts(tmp_path):
    path = tmp_path / "traj.json"
    rec = _reg(qid="q1", retrieved=10)[0]
    t = record_from_query(rec, round_id=2, registry=_reg(),
                          provenance=_prov("W1"), staging=_staging("W1"),
                          edges=_edges(), pool=_pool(),
                          ontology_version="ov", kb_version="kv",
                          api_calls=1, llm_calls=0, path=str(path))
    assert t.trajectory_id == "2::q1"
    assert t.state["candidate_pool_size"] == 1
    assert t.action["query_family"] == "ADJACENT"
    assert t.outcome["new_unique_papers"] == 1
    assert t.cost["api_calls"] == 1
    records = load_trajectories(str(path))
    assert len(records) == 1
    # 同 id 再记录 → 更新不双写
    record_from_query(rec, round_id=2, registry=_reg(), provenance=_prov("W1"),
                      staging=_staging("W1"), edges=_edges(), pool=_pool(),
                      path=str(path))
    assert len(load_trajectories(str(path))) == 1


def test_update_downstream_late(tmp_path):
    path = tmp_path / "traj.json"
    records = []
    t = DiscoveryTrajectory(trajectory_id="2::q1", origin_round=2)
    upsert_trajectory(records, t, path=str(path))
    assert update_downstream("2::q1", validated=["inc"], promoted=["bulk-fill"],
                             path=str(path)) is True
    assert update_downstream("2::q1", validated=["inc", "dcb"], path=str(path)) is True
    loaded = load_trajectories(str(path))[0]
    assert loaded["downstream"]["validated_candidates"] == ["dcb", "inc"]  # 累积不覆盖
    assert loaded["downstream"]["promoted_candidates"] == ["bulk-fill"]


# ── analyze by_family ──

def test_analyze_trajectories_by_family():
    recs = [
        DiscoveryTrajectory(trajectory_id="1::n1", origin_round=1,
                            action={"query_family": "NODE"},
                            outcome={"retrieved_total": 10, "new_unique_papers": 1,
                                     "new_relevant_papers": 0, "new_edges": 0,
                                     "new_candidates": [], "new_candidate_not_seen_before": []},
                            cost={"api_calls": 1}).to_dict(),
        DiscoveryTrajectory(trajectory_id="1::a1", origin_round=1,
                            action={"query_family": "ADJACENT"},
                            outcome={"retrieved_total": 8, "new_unique_papers": 4,
                                     "new_relevant_papers": 2, "new_edges": 3,
                                     "new_candidates": ["x"], "new_candidate_not_seen_before": ["x"]},
                            cost={"api_calls": 1}).to_dict(),
        DiscoveryTrajectory(trajectory_id="1::a2", origin_round=1,
                            action={"query_family": "ADJACENT"},
                            outcome={"retrieved_total": 6, "new_unique_papers": 2,
                                     "new_relevant_papers": 1, "new_edges": 1,
                                     "new_candidates": [], "new_candidate_not_seen_before": []},
                            cost={"api_calls": 1}).to_dict(),
    ]
    a = analyze_trajectories(recs)
    assert a["NODE"]["count"] == 1
    assert a["NODE"]["avg_new_unique_papers"] == 1.0
    assert a["ADJACENT"]["count"] == 2
    assert a["ADJACENT"]["avg_new_unique_papers"] == 3.0     # (4+2)/2
    assert a["ADJACENT"]["avg_new_edges"] == 2.0
    assert a["ADJACENT"]["new_candidate_not_seen_before"] == 1
    assert a["_total"]["trajectories"] == 3
