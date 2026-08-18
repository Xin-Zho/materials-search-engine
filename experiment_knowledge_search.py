"""Phase 1 关键实验：Knowledge → Hypothesis → Query → Search。

验证 Knowledge Extractor 泛化出的 hypotheses queries，能否找到
主搜索此前没找到的新相关论文（Useful Query Yield）。

用法（需 DEEPSEEK_API_KEY）:
    python experiment_knowledge_search.py data/exports/foundational_baseline.csv [篇数]
"""

import asyncio
import csv
import os
import sys
from search_engine.llm import DeepSeekBackend
from search_engine.knowledge_extractor import KnowledgeExtractor
from search_engine.citation_tracker import CitationTracker
from search_engine.evaluator import normalize_doi
from search_engine.models import Paper


async def main():
    csv_path = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 10

    # 1. 读已确认相关论文
    papers: list[Paper] = []
    with open(csv_path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row.get("title") and row.get("doi"):
                papers.append(Paper(
                    paper_id=row["doi"],
                    title=row["title"],
                    abstract=row.get("abstract", ""),
                ))
            if len(papers) >= n:
                break

    original_dois = {normalize_doi(p.doi) for p in papers}

    # 2. 跑 Knowledge Extractor，收集 hypotheses queries
    backend = DeepSeekBackend(api_key=os.environ.get("DEEPSEEK_API_KEY", ""))
    extractor = KnowledgeExtractor(backend)

    print(f"从 {len(papers)} 篇论文提取知识...\n")
    all_queries: list[tuple[str, str, str]] = []  # (query, source_title, support_type)
    for p in papers:
        rec = await extractor.extract(p)
        if not rec:
            continue
        for h in rec.search_hypotheses:
            for q in h.queries:
                if q and q not in [x[0] for x in all_queries]:
                    all_queries.append((q, p.title[:50], h.support_type))

    print(f"共生成 {len(all_queries)} 条 knowledge-driven queries\n")

    # 3. 用 OpenAlex 搜索每个 query，看能否找到新论文
    new_papers: dict[str, Paper] = {}  # 新论文（不在原始 CSV）
    useful_queries = 0

    async with CitationTracker() as tracker:
        for query, source, support in all_queries:
            try:
                results = await tracker.search(query, limit=20)
            except Exception as e:
                print(f"  [ERR] {query}: {e}")
                continue

            # 新论文 = 不在原始 CSV 里的
            novel = [p for p in results if normalize_doi(p.doi) not in original_dois]
            if novel:
                useful_queries += 1
                for p in novel:
                    new_papers.setdefault(normalize_doi(p.doi) or p.paper_id, p)

            print(f"  [{support or '?'}] {query[:60]}")
            print(f"        → {len(results)} 篇, 其中 {len(novel)} 篇新论文")

    # 4. 汇总
    yield_rate = useful_queries / len(all_queries) if all_queries else 0
    print("\n=== 实验结果 ===")
    print(f"knowledge-driven queries: {len(all_queries)}")
    print(f"useful queries（找到新论文）: {useful_queries}")
    print(f"Useful Query Yield: {yield_rate*100:.0f}%")
    print(f"去重后新论文数: {len(new_papers)}")

    if new_papers:
        print("\n新论文示例（主搜索未找到，但 knowledge query 找到的）:")
        for doi, p in list(new_papers.items())[:15]:
            t = (p.title or "").encode('ascii', 'replace').decode('ascii')[:70]
            print(f"  - [{p.year}] {t}")


if __name__ == "__main__":
    asyncio.run(main())
