"""离线构建知识库：论文 → Knowledge Extractor → Knowledge Base → historical queries。

不依赖 OpenAlex，只依赖 DeepSeek（extractor）。

用法（需 DEEPSEEK_API_KEY）:
    python build_knowledge_base.py data/exports/foundational_baseline.csv [篇数]
"""

import asyncio
import csv
import os
import sys
from search_engine.llm import DeepSeekBackend
from search_engine.knowledge_extractor import KnowledgeExtractor
from search_engine.knowledge_base import KnowledgeBase
from search_engine.models import Paper


async def main():
    csv_path = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 30

    papers: list[Paper] = []
    with open(csv_path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row.get("title") and row.get("doi"):
                papers.append(Paper(
                    paper_id=row["doi"], title=row["title"], abstract=row.get("abstract", ""),
                ))
            if len(papers) >= n:
                break

    backend = DeepSeekBackend(api_key=os.environ.get("DEEPSEEK_API_KEY", ""))
    extractor = KnowledgeExtractor(backend)
    kb = KnowledgeBase()

    print(f"提取 {len(papers)} 篇论文的知识并落库...\n")
    for p in papers:
        rec = await extractor.extract(p)
        if rec:
            kb.store(rec)

    records = kb.get_all()
    print(f"\n落库 {len(records)} 条知识记录\n")

    # 生成 historical queries
    hist_queries = kb.generate_historical_queries()
    print(f"=== knowledge-derived historical queries ({len(hist_queries)} 条) ===")
    for q in hist_queries:
        print(f"  {q}")

    print(f"\n=== 术语统计 ===")
    print(f"strategy_routes: {len(kb.collect_terms('strategy_routes'))} 个")
    print(f"historical_terms: {len(kb.collect_terms('historical_terms'))} 个")
    print(f"materials: {len(kb.collect_terms('materials'))} 个")

    # 对比硬编码 ROUTE_QUERIES
    from search_engine.foundational_recovery import FoundationalRecovery
    hardcoded = FoundationalRecovery.ROUTE_QUERIES
    print(f"\n硬编码 ROUTE_QUERIES: {len(hardcoded)} 条")
    print(f"  {hardcoded}")
    print(f"\nknowledge-derived 已覆盖硬编码中的 {sum(1 for h in hardcoded if any(h in q or q in h for q in hist_queries))}/{len(hardcoded)} 条")

    kb.close()


if __name__ == "__main__":
    asyncio.run(main())
