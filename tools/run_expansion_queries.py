"""Phase 2.1b expansion query retrieval CLI（用户定 2026-08-26，P2.2）。

用法:
    # 只读：列出该 node 待执行 query（PENDING/FAILED），不发请求
    python tools/run_expansion_queries.py --node "bulk-fill composite formulation" --plan-only

    # 正式：执行未成功 query → OpenAlex search_relevance → staging + provenance 写回
    python tools/run_expansion_queries.py --node "bulk-fill composite formulation"

边界（用户定，锁死）：新论文只进 discovery staging（relevance_status=STAGED），
**不直接写进已确认知识 KB**——P2.3 才走 relevance → extraction → edge → scanner。
"""

import argparse
import json
import os
import sys

from search_engine.discovery.query_registry import load_registry, save_registry, REGISTRY_PATH
from search_engine.discovery.paper_provenance import (
    load_provenance, save_provenance, PROVENANCE_PATH,
)
from search_engine.discovery.discovery_retriever import (
    execute_pending, build_existing_universe, load_staging, save_staging,
    STAGING_PATH,
)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


POOL_PATH = "data/exports/phase2_candidates.json"


def _registry_for_node(node: str) -> list[dict]:
    registry = load_registry()
    return [r for r in registry if r.get("source_node") == node]


def _load_pool() -> list[dict]:
    """候选池（trajectory unseen 判定基线用）。"""
    if not os.path.exists(POOL_PATH):
        return []
    with open(POOL_PATH, encoding="utf-8") as f:
        return json.load(f).get("candidates", [])


def _search_backend(mailto: str | None = None):
    """真实检索：OpenAlexBackend.search_relevance（**async 方法**，直接传）。

    execute_pending 是 async——同一事件循环内 await 每个 query，
    httpx AsyncClient 跨 query 复用，不会跨循环炸。
    mailto：OpenAlex polite pool（大幅提高日配额，推荐传邮箱）。
    """
    from search_engine.backends.openalex import OpenAlexBackend
    return OpenAlexBackend(mailto=mailto).search_relevance


def _print_plan(records: list[dict]) -> None:
    pending = [r for r in records if r.get("status") != "SUCCEEDED"]
    print("=" * 60)
    print("Expansion Queries plan-only（真正只读，不发请求）")
    print("=" * 60)
    print(f"registered: {len(records)}   already done: {len(records) - len(pending)}"
          f"   pending/retryable: {len(pending)}")
    for r in pending:
        st = r.get("status", "PENDING")
        err = f"  (last error: {r.get('error')})" if r.get("error") else ""
        print(f"  [{st:<8}] [{r.get('query_family', '?'):<8}] {r.get('query_text')}{err}")
    print("[plan-only] 未发请求。正式执行去掉 --plan-only。")


def _print_report(summary: dict) -> None:
    print("=" * 50)
    print("Knowledge Expansion Retrieval")
    print("=" * 50)
    print("Queries（before-run / this-run 分开，用户 invariant）:")
    print(f"  registered total     {summary['registered_total']}")
    print(f"  succeeded before run {summary['succeeded_before_run']}")
    print(f"  pending before run   {summary['pending_before_run']}")
    print(f"  executed this run    {summary['executed_this_run']}")
    print(f"  succeeded after run  {summary['succeeded_after_run']}")
    print(f"  failed this run      {summary['failed_this_run']}")
    print()
    print("Retrieval（数学一致：unique = existing + new）:")
    print(f"  raw hits             {summary['raw_hits']}")
    print(f"  unique papers        {summary['unique_papers']}")
    print(f"  already in universe  {summary['existing_papers']}")
    print(f"  NEW unique papers    {summary['new_unique_papers']}")
    print()
    print("By query family:")
    for f in ("NODE", "RELATION", "MECHANISM", "ADJACENT"):
        print(f"  {f:<10} new papers = {summary['by_family'].get(f, 0)}")


def main():
    ap = argparse.ArgumentParser(description="Phase 2.1b expansion query retrieval（P2.2）")
    ap.add_argument("--node", required=True, help="promoted node（registry 的 source_node）")
    ap.add_argument("--plan-only", action="store_true", help="只读：列出待执行 query，不发请求")
    ap.add_argument("--limit", type=int, default=20, help="每 query 取多少（默认 20）")
    ap.add_argument("--mailto", default=os.environ.get("OPENALEX_MAILTO", ""),
                    help="OpenAlex polite pool 邮箱（提高配额；或用 OPENALEX_MAILTO 环境变量）")
    args = ap.parse_args()

    records = _registry_for_node(args.node)
    if not records:
        print(f"✗ registry 中找不到 source_node={args.node} 的 query"
              f"（先跑 tools/expand_promoted_node.py 生成）")
        return

    if args.plan_only:
        _print_plan(records)
        return

    # 正式执行
    provenance = load_provenance()
    staging = load_staging()
    existing = build_existing_universe(provenance_path=PROVENANCE_PATH)
    search_fn = _search_backend(args.mailto or None)

    print(f"执行 {args.node} 的 expansion queries（{len(records)} 条注册）...")
    import asyncio
    # P2.2 accounting：执行前冻结快照（universe_before + query 状态），
    # 报告区分 before-run / this-run（用户 invariant）
    succeeded_before = sum(1 for r in records if r.get("status") == "SUCCEEDED")
    pending_before = len(records) - succeeded_before
    executions, existing_retrieved, retrieved_all = asyncio.run(execute_pending(
        records, search_fn, existing, provenance, staging, limit=args.limit))

    # 写回：registry（status/counts）+ provenance（many-to-many）+ staging
    save_registry(records, REGISTRY_PATH)
    save_provenance(provenance, PROVENANCE_PATH)
    save_staging(staging, STAGING_PATH)

    from search_engine.discovery.discovery_retriever import summarize
    summary = summarize(executions, records, staging, provenance,
                        existing_retrieved, retrieved_all,
                        succeeded_before=succeeded_before,
                        pending_before=pending_before)
    _print_report(summary)

    # 2.1c trajectory：每条 SUCCEEDED query 记录 state→action→outcome（事实，不定 reward）
    from search_engine.discovery.trajectory import (
        record_from_query, TRAJECTORIES_PATH,
    )
    from search_engine.discovery.round_state import kb_version, ontology_version
    pool_now = _load_pool()   # merge 前池（unseen 判定基线）
    for rec in records:
        if rec.get("status") != "SUCCEEDED":
            continue
        record_from_query(
            rec, rec.get("origin_round"), records, provenance, staging,
            edges=[], pool=pool_now,
            ontology_version=ontology_version(), kb_version=kb_version(),
            api_calls=1, llm_calls=0, path=TRAJECTORIES_PATH)
    print(f"\n✓ trajectory 已记录（{TRAJECTORIES_PATH}）——relevant/edges/candidates "
          f"待 P2.3 补写")

    failed = [e for e in executions if e.status == "FAILED"]
    if failed:
        print("\n失败 query（可重试）:")
        for e in failed:
            print(f"  ✗ {e.query_text}: {e.error}")
    print(f"\n✓ 已写 registry / provenance / staging（新论文 relevance_status=STAGED，"
          f"未进已确认 KB）")


if __name__ == "__main__":
    main()
