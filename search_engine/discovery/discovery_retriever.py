"""Phase 2.1b Discovery Retriever（用户定 2026-08-26，P2.2）。

执行"由新知识产生的新 query"，证明它们能带回**以前没见过的论文**。

边界（用户定，锁死）：
    **Expander 找回来的论文不能直接写进"已确认知识 KB"。**
    先进入 discovery staging（candidate universe），再走 P2.3 的
    relevance → extraction → edge 流程——否则"因为系统自己生成了 query，
    所以搜回来的论文天然相关"会污染 KB。

QueryExecution 状态机（用户 invariant ①）：
    PENDING → RUNNING → SUCCEEDED / FAILED
    **只有 SUCCEEDED 才算执行过**；FAILED 可以重试。

统计（用户 invariant ③）：
    retrieved_total / existing_papers / new_unique_papers——真正有意义的是
    new_unique（否则 bulk-fill query 找回大量原 KB 已有论文，看似 retrieval 强，
    但没扩大搜索空间）。

relevance（用户 invariant ④）：
    新论文进 staging 时 relevance_status = "STAGED"（**不冒充 RELEVANT**）；
    三态 RELEVANT/UNCERTAIN/IRRELEVANT 由 P2.3 精筛（recall-first：
    RELEVANT+UNCERTAIN 保留，只丢 IRRELEVANT）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import asyncio
import inspect
import json
import os
from datetime import datetime, timezone

from .paper_provenance import (
    DiscoveryPaperProvenance, load_provenance, save_provenance, add_provenance,
)
from .query_registry import load_registry, save_registry

STAGING_PATH = "data/exports/discovery_staging.json"
POOL_PATH = "data/exports/phase2_candidates.json"


@dataclass
class QueryExecution:
    """一次 query 执行（用户定结构）。status: PENDING / RUNNING / SUCCEEDED / FAILED。"""

    query_id: str
    query_text: str
    status: str = "PENDING"
    retrieved_count: int = 0
    existing_count: int = 0
    new_unique_count: int = 0
    error: str | None = None
    executed_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "QueryExecution":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _query_id_from_text(query_text: str) -> str:
    """与 query_registry._query_id 同源（md5(query_text)[:12]）——join 键一致。"""
    from .query_registry import _query_id
    return _query_id(query_text)


def build_existing_universe(pool_path: str = POOL_PATH,
                            staging_path: str = STAGING_PATH,
                            provenance_path: str | None = None,
                            kb_edges: list | None = None) -> set[str]:
    """已确认知识宇宙 = KB edges 论文 ∪ 候选池 source_papers/supporting ∪ staging/provenance。

    P2.2 判定"新论文"的基线：不在 universe 里的才是真正新增。
    """
    ids: set[str] = set()
    for e in kb_edges or []:
        pid = getattr(e, "paper_id", None) or (e.get("paper_id") if isinstance(e, dict) else None)
        if pid:
            ids.add(pid)
    # 候选池（scanner 早已消费的论文）
    if os.path.exists(pool_path):
        try:
            with open(pool_path, encoding="utf-8") as f:
                for c in json.load(f).get("candidates", []):
                    ids.update(c.get("source_papers", []) or [])
                    v = (c.get("provenance") or {}).get("verification") or {}
                    ids.update(v.get("supporting_papers", []) or [])
        except Exception:
            pass
    # 已 staging / 已有 provenance
    if os.path.exists(staging_path):
        try:
            with open(staging_path, encoding="utf-8") as f:
                for p in json.load(f).get("papers", []):
                    if p.get("paper_id"):
                        ids.add(p["paper_id"])
        except Exception:
            pass
    if provenance_path:
        ids.update(_pids(load_provenance(provenance_path)))
    return ids


def _pids(records: list[dict]) -> set[str]:
    return {r.get("paper_id", "") for r in records if r.get("paper_id")}


def load_staging(path: str = STAGING_PATH) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("papers", [])
    except Exception:
        return []


def save_staging(papers: list[dict], path: str = STAGING_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"phase": "2.1b-staging", "papers": papers},
                  f, ensure_ascii=False, indent=2)


def _paper_attr(paper, name: str, default=""):
    """Paper 对象 / dict 容错取值。"""
    if isinstance(paper, dict):
        return paper.get(name, default)
    return getattr(paper, name, default)


def add_staging_paper(staging: list[dict], paper, query_record: dict,
                      provenance_records: list[dict]) -> dict | None:
    """新论文进 staging（**relevance_status=STAGED，不冒充 RELEVANT**）。

    provenance many-to-many：同 paper 被多个 query 找到 → 各自追加 provenance 记录，
    staging 的 query_ids/promoted_nodes 累积（不覆盖）。
    **存 title/abstract**（P2.3 extractor 需要完整 Paper 字段；entry 已存在时补全
    title/abstract——幂等重跑/多 query 命中时数据完整）。
    """
    paper_id = _paper_attr(paper, "paper_id")
    if not paper_id:
        return None
    title = _paper_attr(paper, "title", "")
    abstract = _paper_attr(paper, "abstract", "")
    entry = next((p for p in staging if p.get("paper_id") == paper_id), None)
    if entry is None:
        entry = {"paper_id": paper_id, "title": title, "abstract": abstract,
                 "relevance_status": "STAGED",
                 "query_ids": [], "promoted_nodes": []}
        staging.append(entry)
    else:
        if title and not entry.get("title"):
            entry["title"] = title
        if abstract and not entry.get("abstract"):
            entry["abstract"] = abstract
    if query_record.get("query_id") not in entry["query_ids"]:
        entry["query_ids"].append(query_record.get("query_id", ""))
    node = query_record.get("source_node", "")
    if node and node not in entry["promoted_nodes"]:
        entry["promoted_nodes"].append(node)
    return entry


async def execute_query(record: dict, search_fn, existing_ids: set[str],
                        provenance_records: list[dict], staging: list[dict],
                        limit: int = 20,
                        existing_retrieved: set[str] | None = None,
                        retrieved_all: set[str] | None = None) -> QueryExecution:
    """执行一条 query（注入 search_fn，测试可 mock）。

    search_fn(query_text) -> list[Paper] 或 coroutine（async backend 如
    OpenAlexBackend.search_relevance——await 它；同一事件循环内复用 httpx client）。
    只有 SUCCEEDED 才算执行过（invariant ①）。
    existing_retrieved：跨 query 累计"被找回但已在 universe"的论文 id（unique）。
    retrieved_all：跨 query 累计本轮所有返回论文 id（含 existing + new——
    P2.2 accounting 数学一致统计用，用户 invariant ③）。
    """
    qid = record.get("query_id") or _query_id_from_text(record.get("query_text", ""))
    exec_ = QueryExecution(query_id=qid, query_text=record.get("query_text", ""),
                           status="RUNNING")
    try:
        result = search_fn(record.get("query_text", ""), limit=limit)
        if inspect.iscoroutine(result):
            result = await result
        papers = result
    except Exception as e:                       # noqa: BLE001
        exec_.status = "FAILED"
        exec_.error = str(e)
        exec_.executed_at = _now()
        return exec_

    exec_.status = "SUCCEEDED"
    exec_.retrieved_count = len(papers)
    exec_.executed_at = _now()
    for p in papers:
        pid = _paper_attr(p, "paper_id")
        if not pid:
            continue
        if retrieved_all is not None:
            retrieved_all.add(pid)
        if pid in existing_ids:
            exec_.existing_count += 1
            if existing_retrieved is not None:
                existing_retrieved.add(pid)
            continue
        exec_.new_unique_count += 1
        add_staging_paper(staging, p, record, provenance_records)
        add_provenance(provenance_records, DiscoveryPaperProvenance(
            paper_id=pid,
            promoted_node=record.get("source_node", ""),
            promotion_id=record.get("origin_promotion", ""),
            origin_round=record.get("origin_round"),
            query_id=qid,
            query_text=record.get("query_text", ""),
            query_family=record.get("query_family", "NODE"),
        ))
    return exec_


async def execute_pending(registry: list[dict], search_fn, existing_ids: set[str],
                          provenance_records: list[dict], staging: list[dict],
                          limit: int = 20) -> tuple[list[QueryExecution], set[str], set[str]]:
    """执行全部未成功过的 query（PENDING + FAILED 可重试；SUCCEEDED 跳过）。

    返回 (executions, existing_retrieved, retrieved_all)：
      existing_retrieved —— 本轮被找回且已在 universe_before 的论文 id（unique）
      retrieved_all      —— 本轮所有返回论文 id（unique，含 existing + new）
    调用方用 retrieved_all 与 existing_retrieved 做数学一致统计（invariant：
    new_unique = retrieved_all - existing_retrieved，且两者互斥并集=retrieved_all）。
    """
    executions = []
    existing_retrieved: set[str] = set()
    retrieved_all: set[str] = set()
    for record in registry:
        if record.get("status") == "SUCCEEDED":
            continue
        exec_ = await execute_query(record, search_fn, existing_ids,
                                    provenance_records, staging, limit=limit,
                                    existing_retrieved=existing_retrieved,
                                    retrieved_all=retrieved_all)
        executions.append(exec_)
        # 写回 registry（PENDING/FAILED → RUNNING 结果；status/counts/error/executed_at）
        record["status"] = exec_.status
        record["retrieved_count"] = exec_.retrieved_count
        record["existing_count"] = exec_.existing_count
        record["new_unique_count"] = exec_.new_unique_count
        record["error"] = exec_.error
        record["executed_at"] = exec_.executed_at
    executions.sort(key=lambda e: e.executed_at or "")
    return executions, existing_retrieved, retrieved_all


def summarize(executions: list[QueryExecution], registry: list[dict],
              staging: list[dict], provenance: list[dict] | None = None,
              existing_retrieved: set[str] | None = None,
              retrieved_all: set[str] | None = None,
              succeeded_before: int = 0, pending_before: int = 0) -> dict:
    """P2.2 报告（用户 invariant：数学一致 + before-run/this-run 状态分开）。

    数学一致（硬断言）：
        new_unique = retrieved_all - existing_retrieved
        existing_retrieved ∪ new_unique = retrieved_all（互斥并集）
    **staging 不参与 unique 统计**——它是执行后产物，用 staging 重建 universe
    会把新论文自己算成 existing（已踩坑）。
    报告口径：
        unique_papers = 本轮 retrieved unique（含 existing + new）
        existing_papers = 被找回且已在 universe_before（unique）
        new_unique_papers = retrieved - existing（真正新增）
    状态口径：succeeded_before_run / pending_before_run（执行前快照）与
    executed_this_run / succeeded_after_run / failed_this_run 分开——否则看不出
    "以前搜过"还是"这轮刚搜"。
    """
    succeeded = [e for e in executions if e.status == "SUCCEEDED"]
    failed = [e for e in executions if e.status == "FAILED"]
    provs = provenance if provenance is not None else []
    ex = existing_retrieved or set()
    ra = retrieved_all or set()
    new_unique = ra - ex
    # 数学一致（用户 invariant ①：existing/new 都是本轮 retrieved 的子集且并集=unique）
    assert len(ex) <= len(ra), "existing_retrieved 超过 retrieved"
    assert len(new_unique) <= len(ra), "new_unique 超过 retrieved"
    assert len(ex) + len(new_unique) == len(ra), \
        f"数学不一致: existing({len(ex)}) + new({len(new_unique)}) != unique({len(ra)})"
    return {
        "registered_total": len(registry),
        "succeeded_before_run": succeeded_before,
        "pending_before_run": pending_before,
        "executed_this_run": len(succeeded),
        "succeeded_after_run": sum(1 for r in registry
                                   if r.get("status") == "SUCCEEDED"),
        "failed_this_run": len(failed),
        "raw_hits": sum(e.retrieved_count for e in succeeded),
        "unique_papers": len(ra),
        "existing_papers": len(ex),
        "new_unique_papers": len(new_unique),
        "by_family": _count_by_family(provs),
    }


def _count_by_family(provenance: list[dict]) -> dict[str, int]:
    """By query family：provenance 记录按 query_family 统计（新论文来源）。"""
    counts: dict[str, int] = {}
    for r in provenance:
        f = r.get("query_family", "NODE")
        counts[f] = counts.get(f, 0) + 1
    return counts
