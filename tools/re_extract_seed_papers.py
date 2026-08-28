"""Phase 1.8 迁移：重新抽取 seed papers → route_mechanism_edges。

核心原则（用户定）：
- 旧 route-mechanism relation **不迁移**（旧数据没有"谁属于谁"的信息，
  自动迁移只会把旧污染写进新库）。旧 paper metadata 可保留，但重新抽。
- extractor 升级到 2.0-edges：输出 route_mechanism_edges（route—mechanism 证据边）。
- 每篇重抽后打印 route/mechanism/evidence edge 表，人工快速检查。

用法（需 DEEPSEEK_API_KEY）:
    python tools/re_extract_seed_papers.py [--limit N] [--paper-id openalex:W...]
                                           [--dump-only] [--mailto you@example.com]

--dump-only: 不调用 LLM，只打印当前 KB 里已有记录的 edges（重抽后复查用）。
"""

import argparse
import asyncio
import os
import sys

from search_engine.llm import DeepSeekBackend
from search_engine.knowledge_base import KnowledgeBase
from search_engine.knowledge_extractor import KnowledgeExtractor
from search_engine.backends import OpenAlexBackend

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def _lookup_spec(paper_id: str) -> tuple[str, str] | None:
    """paper_id → (kind, value)，供 OpenAlex 拉原文。

    兼容三种格式（KB 历史遗留）：
      'openalex:https://openalex.org/W...' → ('openalex_id', 'W...')
      'openalex:W...'                      → ('openalex_id', 'W...')
      '10.xxxx/yyyy'（DOI）                → ('doi', '10.xxxx/yyyy')
    其他（scopus 等）→ None（跳过）
    """
    if paper_id.startswith("openalex:https://openalex.org/"):
        return ("openalex_id", paper_id[len("openalex:https://openalex.org/"):])
    if paper_id.startswith("openalex:"):
        return ("openalex_id", paper_id[len("openalex:"):])
    if paper_id.startswith("10."):
        return ("doi", paper_id)
    return None


def _print_edges(rec, verbose: bool = False):
    """打印一篇 record 的 route/mechanism/evidence edge 表（人工检查）。"""
    print(f"\n  ── {rec.paper_id}  [v{rec.extractor_version}]  {rec.problem[:60]}")
    print(f"     routes      : {rec.strategy_routes}")
    print(f"     mechanisms  : {[m.canonical or m.mechanism for m in rec.physical_mechanisms]}")
    if not rec.route_mechanism_edges:
        print("     edges       : (无 —— 未重抽或 extractor 未产出)")
        return
    for e in rec.route_mechanism_edges:
        route = e.canonical_route or e.raw_route or "(unbound)"
        mech = e.canonical_mechanism or e.raw_mechanism
        line = f"     edge        : {route} → {mech}  [{e.relation_type}, conf={e.confidence:.2f}]"
        print(line)
        if verbose and e.evidence:
            print(f"                   evidence: {e.evidence[:100]}")


async def main():
    ap = argparse.ArgumentParser(description="Phase 1.8 重抽 seed papers → edges")
    ap.add_argument("--limit", type=int, default=0, help="最多重抽 N 篇（0=全部）")
    ap.add_argument("--paper-id", action="append", default=[],
                    help="只重抽指定 paper_id（可多次传，如 --paper-id A --paper-id B）")
    ap.add_argument("--dump-only", action="store_true", help="不调 LLM，只打印现有 edges")
    ap.add_argument("--mailto", default=os.environ.get("OPENALEX_MAILTO", ""),
                    help="OpenAlex polite pool email")
    ap.add_argument("--verbose", action="store_true", help="打印 evidence 全文")
    args = ap.parse_args()

    kb = KnowledgeBase()
    records = kb.get_all()
    if not records:
        print("KB 为空，没有可重抽的 seed papers。")
        kb.close()
        return
    print(f"KB 现有 {len(records)} 条记录")

    if args.dump_only:
        for rec in records:
            if args.paper_id and rec.paper_id != args.paper_id:
                continue
            _print_edges(rec, verbose=args.verbose)
        kb.close()
        return

    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        print("⚠️  未设 DEEPSEEK_API_KEY —— 无法调用 extractor。", file=sys.stderr)
        kb.close()
        return

    # 1. 选目标 papers
    targets = []
    for rec in records:
        if args.paper_id and rec.paper_id not in args.paper_id:
            continue
        spec = _lookup_spec(rec.paper_id)
        if not spec:
            print(f"  ⏭  {rec.paper_id}: 非 openalex/doi id，跳过（无法拉原文）")
            continue
        targets.append((rec.paper_id, spec))
    if args.limit > 0:
        targets = targets[:args.limit]
    if not targets:
        print("没有可重抽的 openalex/doi paper。")
        kb.close()
        return

    llm = DeepSeekBackend(api_key=key)
    extractor = KnowledgeExtractor(llm, extractor_version="2.0-edges")
    mailto = args.mailto or None

    async with OpenAlexBackend(mailto=mailto) as oa:
        for i, (pid, (kind, value)) in enumerate(targets, 1):
            # 2. 按 openalex id / DOI 拉完整 Paper（title + abstract）
            try:
                if kind == "doi":
                    paper = await oa.get_by_doi(value)
                else:
                    papers = await oa._fetch_works_by_ids([value])
                    paper = papers[0] if papers else None
            except Exception as e:
                print(f"  ⚠ [{i}/{len(targets)}] {pid}: 拉取异常 {type(e).__name__}: {e}")
                continue
            if paper is None:
                print(f"  ⚠ [{i}/{len(targets)}] {pid}: OpenAlex 拉不到，跳过")
                continue
            if not paper.abstract:
                print(f"  ⚠ [{i}/{len(targets)}] {pid}: 无 abstract（只有 title），仍重抽")

            # 3. 重抽（2.0-edges）
            rec = await extractor.extract(paper)
            if rec is None:
                print(f"  ✗ [{i}/{len(targets)}] {pid}: 抽取失败")
                continue

            # 4. 保持原 row key（DOI 记录重抽后仍是 DOI 行，不因 get_by_doi 换成 openalex 行）。
            #    identity 分离：canonical_paper_id/doi/openalex_id 已由 extractor 填好，
            #    去重/merge 由 audit_paper_identity 统一按 canonical 处理。
            #    edges 的 paper_id 必须同步为 row key（否则同一论文的 edges 散在不同 id 下）。
            rec.paper_id = pid
            for e in rec.route_mechanism_edges:
                e.paper_id = pid

            # 5. extraction_status：无 abstract = 文本不足，保留记录但标记（edges 可能少/空）
            rec.extraction_status = "insufficient_evidence" if not paper.abstract else "ok"

            # 6. 覆盖入库（INSERT OR REPLACE，含 edges 表同步）
            kb.store(rec)
            _print_edges(rec, verbose=args.verbose)
            print(f"  ✓ [{i}/{len(targets)}] {pid}: {len(rec.route_mechanism_edges)} 条 edge"
                  f"  [{rec.extraction_status}]")

    kb.close()
    print("\n重抽完成。人工检查上面每篇的 edge 是否合理（谁→谁、evidence 是否支撑）。")
    print("复查: python tools/re_extract_seed_papers.py --dump-only")


if __name__ == "__main__":
    asyncio.run(main())
