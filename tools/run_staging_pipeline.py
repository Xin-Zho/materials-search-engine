"""Phase 2.1b P2.3 staging pipeline CLI（用户定 2026-08-26）。

用法:
    # 只读：staging 论文数 + 预计三态分布（不发 extraction/不写任何东西）
    python tools/run_staging_pipeline.py --plan-only

    # 正式：STAGED → relevance 三态 → extractor → discovery edges
    #        → scanner rerun → candidate before/after diff → trace
    python tools/run_staging_pipeline.py

核心指标（invariant ⑤）：new_relevant_papers / new_edges / new_candidates /
new_candidate_not_seen_before。新 edges 只进 discovery knowledge layer
（data/exports/discovery_edges.json），不写已确认 KB。
"""

import argparse
import json
import os
import sys

from search_engine.discovery.query_registry import load_registry
from search_engine.discovery.discovery_retriever import load_staging
from search_engine.discovery.paper_provenance import load_provenance
from search_engine.discovery.staging_pipeline import (
    screen_staging, extract_candidates, edges_to_discovery_layer,
    save_discovery_edges, load_discovery_edges, rerun_scanner, candidate_diff,
    build_trace,
)
from search_engine.discovery.candidate import merge_pool

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

POOL_PATH = "data/exports/phase2_candidates.json"


def _load_pool() -> list[dict]:
    if not os.path.exists(POOL_PATH):
        return []
    with open(POOL_PATH, encoding="utf-8") as f:
        return json.load(f).get("candidates", [])


def _provenance_map() -> dict:
    """paper_id → {promoted_node, query_id, query_text, query_family, origin_round}。"""
    m = {}
    for r in load_provenance():
        m.setdefault(r["paper_id"], {
            "promoted_node": r.get("promoted_node", ""),
            "query_id": r.get("query_id", ""),
            "query_text": r.get("query_text", ""),
            "query_family": r.get("query_family", ""),
            "origin_round": r.get("origin_round"),
        })
    return m


def _real_extractor():
    """真实 extractor（LLM，异步）。测试注入 mock。

    key 来源：DEEPSEEK_API_KEY 环境变量（与 verify_candidate 同源）。
    """
    import asyncio
    import os
    from search_engine.llm import create_backend
    from search_engine.knowledge_extractor import KnowledgeExtractor
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        raise SystemExit("✗ 未设置 DEEPSEEK_API_KEY 环境变量（extractor 需要 LLM key）"
                         "\n  设置后重跑：set DEEPSEEK_API_KEY=sk-... && python tools/run_staging_pipeline.py --extractor llm")
    backend = create_backend(provider="deepseek", api_key=key)
    extractor = KnowledgeExtractor(backend)
    def run(papers):
        return asyncio.run(extractor.extract_many(papers))
    return run


def main():
    ap = argparse.ArgumentParser(description="Phase 2.1b P2.3：STAGED → relevance → edges → candidate diff")
    ap.add_argument("--plan-only", action="store_true", help="只读：三态预计，不 extract 不写盘")
    ap.add_argument("--extractor", choices=("llm", "mock"), default="llm",
                    help="extractor 后端（测试/离线用 mock）")
    args = ap.parse_args()

    staging = load_staging()
    registry = load_registry()
    if not staging:
        print("✗ staging 为空（先跑 tools/run_expansion_queries.py 填充）")
        return

    # invariant ①：STAGED 必须过 relevance 三态
    verdicts = screen_staging(staging, registry)
    print("=" * 56)
    print("Staging Pipeline（P2.3）")
    print("=" * 56)
    print(f"STAGED papers: {len(staging)}")
    print(f"Relevance:  RELEVANT={verdicts.get('RELEVANT', 0)}  "
          f"UNCERTAIN={verdicts.get('UNCERTAIN', 0)}  "
          f"IRRELEVANT={verdicts.get('IRRELEVANT', 0)}")

    if args.plan_only:
        print("[plan-only] 未 extract / 未写任何东西。正式执行去掉 --plan-only。")
        return

    # 写回 staging（relevance_status 三态）
    from search_engine.discovery.discovery_retriever import save_staging
    save_staging(staging)

    # invariant ①（硬断言，fail-fast）：所有 STAGED 必须三态化才能 extractor
    from search_engine.discovery.staging_pipeline import assert_all_screened
    assert_all_screened(staging)
    extractable = sum(1 for p in staging
                      if p.get("relevance_status") in ("RELEVANT", "UNCERTAIN"))
    print(f"Extractable（RELEVANT+UNCERTAIN）: {extractable}（screening 全量完成）")

    # invariant ②：RELEVANT + UNCERTAIN 进 extraction（只丢 IRRELEVANT）
    extract_fn = _real_extractor() if args.extractor == "llm" else None
    if extract_fn is None:
        print("✗ --extractor mock 仅用于测试；正式运行用 --extractor llm（需要 LLM key）")
        return
    records = extract_candidates(staging, extract_fn)
    print(f"Extracted records: {len(records)}（RELEVANT+UNCERTAIN，recall-first）")

    # invariant ③：new edges 带完整 discovery provenance → discovery layer
    edges = edges_to_discovery_layer(records, _provenance_map())
    existing_edges = load_discovery_edges()
    known_keys = {(e["paper_id"], e.get("raw_mechanism"), e.get("raw_route"))
                  for e in existing_edges}
    new_edges = [e for e in edges
                 if (e["paper_id"], e.get("raw_mechanism"), e.get("raw_route"))
                 not in known_keys]
    if new_edges:
        save_discovery_edges(existing_edges + new_edges)

    # invariant ④：scanner rerun + candidate before/after diff（raw + canonical 双口径）
    from search_engine.knowledge_base import KnowledgeBase
    kb = KnowledgeBase()
    pool = _load_pool()          # merge 前池（unseen 判定基线）
    before_ids = {c["candidate_id"] for c in pool}
    scanned, merged = rerun_scanner(_scan, kb, new_edges, pool)
    kb.close()
    after_ids = {c["candidate_id"] for c in merged}
    diff = candidate_diff(before_ids, after_ids)
    # canonical-before/after（用户 invariant：canonical identity 而非 candidate_id）
    from search_engine.discovery.staging_pipeline import (
        canonical_candidates, audit_new_candidates,
    )
    cdiff = canonical_candidates(pool, merged)
    new_cand_objs = [c for c in merged if c["candidate_id"] in diff["new_candidates"]]
    audit = audit_new_candidates(new_cand_objs, load_provenance())

    # 2.1c trajectory：P2.3 后补写 outcome（relevant/edges/candidates 全量聚合）
    from search_engine.discovery.trajectory import (
        record_from_query, TRAJECTORIES_PATH,
    )
    from search_engine.discovery.round_state import kb_version, ontology_version
    from search_engine.discovery.discovery_retriever import load_staging as _ls
    from search_engine.discovery.paper_provenance import load_provenance as _lp
    reg = load_registry()   # 顶部已 import
    prov_all = _lp()
    staging_all = _ls()
    for rec in reg:
        if rec.get("status") != "SUCCEEDED":
            continue
        record_from_query(
            rec, rec.get("origin_round"), reg, prov_all, staging_all,
            edges=existing_edges + new_edges, pool=pool,     # before 池判 unseen
            ontology_version=ontology_version(), kb_version=kb_version(),
            api_calls=1, llm_calls=1, path=TRAJECTORIES_PATH)
    print(f"✓ trajectory outcome 已补写（relevant/edges/candidates）")

    # invariant ⑤：核心指标（canonical 口径为主，Phase 2.1 验收只看 canonical）
    new_relevant = sum(1 for p in staging if p.get("relevance_status") == "RELEVANT")
    metrics = {
        "new_relevant_papers": new_relevant,
        "new_edges": len(new_edges),
        "new_raw_candidates": cdiff["new_raw_candidates"],
        "new_canonical_candidates": cdiff["new_canonical_candidates"],
        "new_canonical_candidate_not_seen_before":
            cdiff["new_canonical_candidate_not_seen_before"],
    }

    print()
    print("Core metrics:")
    print(f"  new_relevant_papers          {metrics['new_relevant_papers']}")
    print(f"  new_edges                    {metrics['new_edges']}")
    print(f"  new_raw_candidates           {len(metrics['new_raw_candidates'])}"
          f"  {metrics['new_raw_candidates'][:8]}")
    print(f"  new_canonical_candidates     {len(metrics['new_canonical_candidates'])}"
          f"  {metrics['new_canonical_candidates'][:8]}")

    # 审计表：56 个 hash ≠ 56 个新概念（用户要求 raw/canonical/type/来源）
    if audit:
        print()
        print("New candidate audit（raw vs canonical）:")
        for row in audit:
            print(f"  {row['raw_name'][:38]:40s} | canon={row['canonical_identity'][:24]:26s}"
                  f" | {row['candidate_type']:<22} | fam={row['query_family']}")

    # trace：完整 join（promotion→query→paper→relevance→edge→candidate）+
    # trace_complete 硬判定（用户 invariant：不能'尽量填'）
    closed_loops = _build_traces(merged, staging, reg, prov_all, new_edges,
                                 new_cand_objs, before_ids)
    print(f"\nsuccessful_closed_loop: {len(closed_loops)}"
          f"{'（INCOMPLETE_TRACE 不计入）' if not closed_loops else ''}")
    for t in closed_loops[:3]:
        print()
        print("Trace（Phase 2.1 核心链）:")
        for k, v in t.items():
            print(f"  {k}: {v}")

    # 写回候选池（scan merge 结果落盘）
    with open(POOL_PATH, "w", encoding="utf-8") as f:
        json.dump({"phase": "phase2_candidates", "schema_version": "2.1",
                   "candidates": merged}, f, ensure_ascii=False, indent=2)
    print("\n✓ 已写 discovery edges / staging relevance / candidate pool"
          "（新 edges 未进已确认 KB）")


def _scan(view):
    """scan_kb 包装（DiscoveryLayerView 兼容）。"""
    from search_engine.discovery.scanner import scan_kb
    return scan_kb(view)


def _build_traces(merged, staging, registry, provenance, edges, new_cand_objs,
                  before_ids) -> list[dict]:
    """每个新 candidate 的完整 trace join（用户 invariant：trace 必须完整）。

    join 路径：candidate.source_papers[0] → provenance（query_id/text/family/
    promoted_node/promotion_id）→ registry（query 完整记录）→ staging（relevance）
    → edges（该 candidate 的 edge）。trace_complete=False 的标记 INCOMPLETE_TRACE
    不计入 successful_closed_loop。
    """
    from search_engine.discovery.staging_pipeline import build_trace
    prov_by_paper: dict[str, list] = {}
    for r in provenance:
        prov_by_paper.setdefault(r.get("paper_id", ""), []).append(r)
    reg_by_qid = {r.get("query_id"): r for r in registry}
    staging_by_paper = {p.get("paper_id"): p for p in staging}
    traces = []
    for c in new_cand_objs:
        paper_id = (c.get("source_papers") or [None])[0]
        provs = prov_by_paper.get(paper_id, [])
        prov = provs[0] if provs else {}
        qid = prov.get("query_id", "")
        qrec = reg_by_qid.get(qid, {})
        paper = staging_by_paper.get(paper_id, {})
        cname = (c.get("raw_name") or "").lower()
        cand_edge = next((e for e in edges
                          if cname and (cname in (e.get("raw_mechanism") or "").lower()
                                        or cname in (e.get("raw_route") or "").lower())),
                         None)
        traces.append(build_trace(
            origin_promotion=prov.get("promoted_node", ""),
            promotion_id=prov.get("promotion_id", ""),
            query_id=qid,
            query_text=qrec.get("query_text", prov.get("query_text", "")),
            query_family=qrec.get("query_family", prov.get("query_family", "")),
            paper_id=paper_id,
            relevance=paper.get("relevance_status", ""),
            edge=cand_edge,
            candidate=c.get("raw_name"),
            seen_before=c.get("candidate_id") in before_ids))
    return traces


if __name__ == "__main__":
    main()
