"""Edge 质量审计：每篇论文的 edge 是否形成 route × mechanism → evidence 闭环。

背景（用户诊断）：extractor 抽出 mechanism 但没绑定 route → edge 缺失 →
coverage matrix 不敢关 gap。本工具逐篇检查：
  1. 每篇的 canonical routes / edges（✓）/ 该论文应覆盖但缺失的 checklist 机制（✗）
  2. unbound 机制统计（route=null 的 edge，无法关 gap）
  3. 全局：所有 edge 覆盖的 (route × mechanism) vs CORE_ROUTE_MECHANISMS checklist

纯本地（读 KB + CoverageMatcher，不调 LLM）。重抽后跑，验证"edge 闭环"是否形成。

用法:
    python tools/check_edge_quality.py [--paper-id X] [--verbose]
"""

import argparse
import sys

from search_engine.knowledge_base import KnowledgeBase
from search_engine.route_mechanism_ontology import (
    CoverageMatcher, assign_route, get_mechanisms, compute_gap_coverage,
)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def paper_canonical_routes(rec) -> set[str]:
    routes: set[str] = set()
    for phrase in rec.strategy_routes or []:
        r = assign_route([phrase])
        if r:
            routes.add(r)
    return routes


def main():
    ap = argparse.ArgumentParser(description="Edge 质量审计（route×mechanism→evidence 闭环）")
    ap.add_argument("--paper-id", default="", help="只看指定 paper_id")
    ap.add_argument("--verbose", action="store_true", help="打印 evidence 全文")
    args = ap.parse_args()

    kb = KnowledgeBase()
    records = kb.get_all()
    matcher = CoverageMatcher()

    total_edges = 0
    unbound_edges = 0
    papers_with_edges = 0
    all_edges = []   # 全局汇总用（compute_gap_coverage 统一口径）

    print("=" * 78)
    print("Edge 质量审计")
    print("=" * 78)

    for rec in records:
        if args.paper_id and rec.paper_id != args.paper_id:
            continue
        routes = paper_canonical_routes(rec)
        edges = rec.route_mechanism_edges
        all_edges.extend(edges)
        title = (rec.problem or rec.paper_id)[:70]
        print(f"\n── {rec.paper_id[:45]}  [v{rec.extractor_version}]  {title}")
        print(f"   canonical_routes: {sorted(routes) or '(无归并)'}")

        if not edges:
            print(f"   edges: (无)" + ("  ← 未重抽/无绑定" if rec.extractor_version.startswith("1.") else ""))
            continue

        papers_with_edges += 1
        for e in edges:
            total_edges += 1
            r = e.canonical_route or e.raw_route or ""
            m = e.canonical_mechanism or e.raw_mechanism or ""
            if not r:
                unbound_edges += 1
                print(f"   ✗ unbound : {m}  [conf={e.confidence:.2f}]"
                      + (f"  evidence: {e.evidence[:70]}" if args.verbose and e.evidence else ""))
                continue
            mark = "✓"
            print(f"   {mark} {r} → {m}  [{e.relation_type}, conf={e.confidence:.2f}]"
                  + (f"  evidence: {e.evidence[:70]}" if args.verbose and e.evidence else ""))

        # 该论文的 checklist missing：有 canonical route 但对应机制没被这篇的 edge 覆盖
        missing = []
        for r in sorted(routes):
            for mech in get_mechanisms(r):
                covered = any(
                    matcher.edge_supports_gap(e, r, mech) in ("DIRECT_MODEL", "DIRECT_HUMAN", "INHERITED")
                    for e in edges
                )
                if not covered:
                    missing.append((r, mech))
        if missing:
            print(f"   missing (checklist): " + ", ".join(f"{r}×{m}" for r, m in missing))

    # ── 全局汇总（统一 compute_gap_coverage 口径，与 print_coverage_matrix 一致）──
    gap_cov = compute_gap_coverage(all_edges)
    stats = {"DIRECT_MODEL": 0, "DIRECT_HUMAN": 0, "INHERITED": 0, "OPEN": 0}
    still_open_by_route: dict[str, list[str]] = {}
    for (r, m), info in gap_cov.items():
        status = info["status"]
        stats[status] += 1
        if status == "OPEN":
            still_open_by_route.setdefault(r, []).append(m)

    covered = stats["DIRECT_MODEL"] + stats["DIRECT_HUMAN"] + stats["INHERITED"]
    print("\n" + "=" * 78)
    print("全局 edge 覆盖 vs checklist（与 print_coverage_matrix 同一口径）")
    print("=" * 78)
    print(f"records               : {len(records)}")
    print(f"含 edges 的记录       : {papers_with_edges}")
    print(f"edge 总数             : {total_edges}")
    print(f"unbound edge          : {unbound_edges}  (无法关 gap)")
    print(f"checklist (route×mech): {len(gap_cov)}")
    print(f"DIRECT_MODEL covered  : {stats['DIRECT_MODEL']}")
    print(f"DIRECT_HUMAN covered  : {stats['DIRECT_HUMAN']}   ← 人工核实补的 edge，评估 extractor 时单独统计")
    print(f"INHERITED covered     : {stats['INHERITED']}")
    print(f"TOTAL covered         : {covered}")
    print(f"OPEN                  : {stats['OPEN']}")
    if still_open_by_route:
        print("\n仍 open（按 route）:")
        for r, mechs in sorted(still_open_by_route.items()):
            print(f"   {r:<16} ✗ {', '.join(mechs)}")
    print("\nOPEN 的可能来源（Phase 1.7 语义边界）:")
    print("  1. TRUE_OPEN        — 当前 KB 尚无直接证据 → 才能进入 autonomous gap search")
    print("  2. EXTRACTION_MISS  — evidence 已存在但 extractor 未生成 edge → DIRECT_HUMAN / targeted extraction")
    print("  3. CANONICAL_MISS   — edge 存在但 ontology 未对齐 → 修 normalizer")
    print("OPEN ≠ extractor 一定失败；OPEN ≠ 搜索一定失败。只有完成 semantic audit")
    print("（tools/audit_open_gaps.py）后的 TRUE_OPEN 才能进 loop。")
    kb.close()


if __name__ == "__main__":
    main()
