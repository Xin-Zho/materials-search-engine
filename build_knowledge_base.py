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

    # 生成 historical queries（term + anchor 组合，带语义去重）
    from search_engine.knowledge_base import HistoricalQueryBuilder
    builder = HistoricalQueryBuilder(anchor="polymerization shrinkage")
    hist_queries = builder.build(records)
    print(f"=== knowledge-derived historical queries ({len(hist_queries)} 条) ===")
    for q in hist_queries[:30]:
        print(f"  [{q.source_type}] {q.query}")

    # 三个指标
    from collections import Counter
    type_counter = Counter(q.source_type for q in hist_queries)
    print(f"\n=== 三指标 ===")
    print(f"Generality（source_type 分布）: {dict(type_counter)}")
    print(f"Redundancy（去重后 query 数 / 总 term 数）: "
          f"{len(hist_queries)} 条 query（canonicalize 后）")

    # 对比硬编码 ROUTE_QUERIES
    from search_engine.foundational_recovery import FoundationalRecovery
    hardcoded = FoundationalRecovery.ROUTE_QUERIES
    covered = sum(1 for h in hardcoded if any(h.lower() in q.source_term.lower() or q.source_term.lower() in h.lower() for q in hist_queries))
    print(f"\n硬编码 ROUTE_QUERIES: {len(hardcoded)} 条")
    print(f"knowledge-derived 已覆盖: {covered}/{len(hardcoded)} 条")

    kb.close()


if __name__ == "__main__":
    asyncio.run(main())
