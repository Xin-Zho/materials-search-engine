"""Phase 2.1c Trajectory Recorder（用户定 2026-08-26）。

把散落在 round / query / paper / edge / candidate 里的 provenance 串成一条完整轨迹：

    State（当前知识状态）
      → Action（选哪个 promoted node + 生成/执行哪条 query）
      → Outcome（retrieved → NEW paper → relevant → edge → candidate）
      → Downstream（后续轮次的 validated / promoted，后期补写）
      → Cost（api_calls / llm_calls）

**只记录事实，不定义 RL reward**（用户定：太早定死奖励公式容易被错误 KPI 带偏）。
轨迹数据够了以后（几十~几百条）再进 Phase 2.5 bandit/RL——分析先行：
哪类 query 最易找到 NEW paper？哪类最易产 NEW candidate？cost 与 yield 怎么权衡？

粒度：**每 query 一条 trajectory**（trajectory_id = f"{origin_round}::{query_id}"，
幂等 upsert——P2.2 执行后记基础 outcome，P2.3 后补 relevant/edges/candidates，
后续轮次补 downstream）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
import json
import os
from datetime import datetime, timezone

TRAJECTORIES_PATH = "data/exports/discovery_trajectories.json"


@dataclass
class DiscoveryTrajectory:
    """一条 state → action → outcome → downstream → cost 轨迹（用户 schema）。

    outcome 各字段按 query 聚合：new_unique_papers 来自 provenance（many-to-many）、
    new_relevant_papers 来自 staging 三态、new_edges/new_candidates 来自 discovery
    edges（按 query_id 关联）。downstream 由后续轮次补写。
    """

    trajectory_id: str
    origin_round: int | None = None

    state: dict = field(default_factory=dict)       # ontology_version / kb_version / candidate_pool_size
    action: dict = field(default_factory=dict)      # promoted_node / query_id / query_family / query_text
    outcome: dict = field(default_factory=dict)     # retrieved_total / new_unique_papers /
                                                    # new_relevant_papers / new_edges /
                                                    # new_candidates / new_candidate_not_seen_before
    downstream: dict = field(default_factory=dict)  # validated_candidates / promoted_candidates（后期补写）
    cost: dict = field(default_factory=dict)        # api_calls / llm_calls
    created_at: str = field(default_factory=lambda:
                            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    updated_at: str = field(default_factory=lambda:
                            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "DiscoveryTrajectory":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_trajectories(path: str = TRAJECTORIES_PATH) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else data.get("trajectories", [])
    except Exception:
        return []


def save_trajectories(records: list[dict], path: str = TRAJECTORIES_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def upsert_trajectory(records: list[dict], t: DiscoveryTrajectory,
                      path: str | None = None) -> bool:
    """按 trajectory_id 幂等：存在则合并更新（outcome/downstream 逐轮补写），
    不存在则追加。返回 True=新增，False=更新。
    """
    t.updated_at = _now()
    for i, r in enumerate(records):
        if r.get("trajectory_id") == t.trajectory_id:
            # 合并：已有字段保留（如 downstream 已由后续轮写），新字段覆盖
            merged = {**r, **t.to_dict()}
            for key in ("state", "action", "outcome", "downstream", "cost"):
                if key in r and key in t.to_dict():
                    merged[key] = {**r[key], **t.to_dict()[key]}
            records[i] = merged
            if path is not None:
                save_trajectories(records, path)
            return False
    records.append(t.to_dict())
    if path is not None:
        save_trajectories(records, path)
    return True


# ── outcome 聚合（从现有 provenance/staging/edges 计算，只记事实）──

def outcome_from_query(query_id: str, registry: list[dict],
                       provenance: list[dict], staging: list[dict],
                       edges: list[dict], pool: list[dict] | None = None) -> dict:
    """单条 query 的 outcome（事实聚合，不判 reward）。

    - retrieved_total：registry 记录
    - new_unique_papers：provenance 中该 query 的论文数
    - new_relevant_papers：staging 中该 query 论文的 RELEVANT 数
    - new_edges：edges 中 discovery_provenance.query_id 匹配数
    - new_candidates：这些 edges 引出的候选名（raw_mechanism/raw_route 去重）
    - new_candidate_not_seen_before：其中不在历史池 raw_name 的（旧候选重新发现不算）
    """
    rec = next((r for r in registry if r.get("query_id") == query_id), {})
    new_papers = sorted({r["paper_id"] for r in provenance
                         if r.get("query_id") == query_id})
    relevant = [p for p in staging
                if p.get("paper_id") in new_papers
                and p.get("relevance_status") == "RELEVANT"]
    q_edges = [e for e in edges
               if (e.get("discovery_provenance") or {}).get("query_id") == query_id]
    cand_names = sorted({e.get("raw_mechanism") or e.get("raw_route", "")
                         for e in q_edges} - {""})
    known = {c.get("raw_name", "").lower() for c in (pool or [])}
    unseen = [n for n in cand_names if n.lower() not in known]
    return {
        "retrieved_total": rec.get("retrieved_count", 0),
        "new_unique_papers": len(new_papers),
        "new_relevant_papers": len(relevant),
        "new_edges": len(q_edges),
        "new_candidates": cand_names,
        "new_candidate_not_seen_before": unseen,
    }


def record_from_query(query_record: dict, round_id: int | None,
                      registry: list[dict], provenance: list[dict],
                      staging: list[dict], edges: list[dict],
                      pool: list[dict] | None = None,
                      ontology_version: str = "", kb_version: str = "",
                      api_calls: int = 0, llm_calls: int = 0,
                      path: str | None = None) -> DiscoveryTrajectory:
    """从 query 记录聚合一条 trajectory 并 upsert（幂等）。"""
    qid = query_record.get("query_id") or ""
    outcome = outcome_from_query(qid, registry, provenance, staging, edges, pool)
    t = DiscoveryTrajectory(
        trajectory_id=f"{round_id}::{qid}" if round_id is not None else qid,
        origin_round=round_id,
        state={
            "ontology_version": ontology_version,
            "kb_version": kb_version,
            "candidate_pool_size": len(pool or []),
        },
        action={
            "promoted_node": query_record.get("source_node", ""),
            "query_id": qid,
            "query_family": query_record.get("query_family", ""),
            "query_text": query_record.get("query_text", ""),
        },
        outcome=outcome,
        cost={"api_calls": api_calls, "llm_calls": llm_calls},
    )
    if path is not None:
        records = load_trajectories(path)
        upsert_trajectory(records, t, path=path)
    return t


def update_downstream(trajectory_id: str, validated: list[str] | None = None,
                      promoted: list[str] | None = None,
                      path: str = TRAJECTORIES_PATH) -> bool:
    """后续轮次补写 downstream（validated/promoted candidates），按 id 更新。"""
    records = load_trajectories(path)
    for r in records:
        if r.get("trajectory_id") == trajectory_id:
            r.setdefault("downstream", {})
            if validated is not None:
                r["downstream"]["validated_candidates"] = \
                    sorted(set(r["downstream"].get("validated_candidates", [])) | set(validated))
            if promoted is not None:
                r["downstream"]["promoted_candidates"] = \
                    sorted(set(r["downstream"].get("promoted_candidates", [])) | set(promoted))
            r["updated_at"] = _now()
            save_trajectories(records, path)
            return True
    return False


# ── 分析（用户问题：哪类 query 收益高？cost 与 yield 怎么权衡？）──

def analyze_trajectories(records: list[dict] | None = None,
                         path: str = TRAJECTORIES_PATH) -> dict:
    """by_family 收益分析（只汇总事实，不下 RL 结论）。

    返回每 query_family 的：count / 平均 retrieved / 平均 new_unique_papers /
    平均 new_relevant_papers / 平均 new_edges / 平均 new_candidates /
    new_candidate_not_seen_before 总数 / 总 api_calls。
    """
    recs = records if records is not None else load_trajectories(path)
    by_family: dict[str, list[dict]] = {}
    for r in recs:
        fam = (r.get("action") or {}).get("query_family", "?")
        by_family.setdefault(fam, []).append(r)
    result: dict[str, dict] = {}
    for fam, items in sorted(by_family.items()):
        n = len(items)
        avg = lambda k: round(sum(_g(i, "outcome", k) for i in items) / n, 2)
        avg_len = lambda k: round(
            sum(len(_g(i, "outcome", k) or []) for i in items) / n, 2)
        result[fam] = {
            "count": n,
            "avg_retrieved": avg("retrieved_total"),
            "avg_new_unique_papers": avg("new_unique_papers"),
            "avg_new_relevant_papers": avg("new_relevant_papers"),
            "avg_new_edges": avg("new_edges"),
            "avg_new_candidates": avg_len("new_candidates"),
            "new_candidate_not_seen_before": sum(
                len(_g(i, "outcome", "new_candidate_not_seen_before") or [])
                for i in items),
            "total_api_calls": sum(_g(i, "cost", "api_calls") for i in items),
        }
    result["_total"] = {"trajectories": len(recs)}
    return result


def _g(record: dict, section: str, key: str):
    return (record.get(section) or {}).get(key, 0)
