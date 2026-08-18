"""离线测试 Knowledge Extractor：从已确认相关论文提取知识，输出供人工审查。

用法（需 DEEPSEEK_API_KEY）:
    python test_knowledge_extractor.py data/exports/foundational_baseline.csv [篇数]
"""

import asyncio
import csv
import os
import sys
from search_engine.llm import DeepSeekBackend
from search_engine.knowledge_extractor import KnowledgeExtractor
from search_engine.models import Paper


async def main():
    csv_path = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 30

    papers: list[Paper] = []
    with open(csv_path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row.get("title") and row.get("doi"):
                papers.append(Paper(
                    paper_id=row["doi"],
                    title=row["title"],
                    abstract=row.get("abstract", ""),
                    year=int(row["year"]) if row.get("year", "").isdigit() else None,
                ))
            if len(papers) >= n:
                break

    print(f"提取 {len(papers)} 篇论文的知识...\n")

    backend = DeepSeekBackend(api_key=os.environ.get("DEEPSEEK_API_KEY", ""))
    extractor = KnowledgeExtractor(backend)

    for i, p in enumerate(papers, 1):
        rec = await extractor.extract(p)
        if not rec:
            print(f"[{i}] {p.title[:60]} → 提取失败")
            continue
        print(f"[{i}] {p.title[:70]}")
        print(f"    problem:     {rec.problem[:80]}")
        print(f"    route:       {rec.strategy_route}")
        print(f"    mechanism:   {rec.physical_mechanism}")
        print(f"    materials:   {rec.materials}")
        print(f"    synonyms:    {rec.synonyms}")
        print(f"    hypotheses:  {rec.search_hypotheses}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
