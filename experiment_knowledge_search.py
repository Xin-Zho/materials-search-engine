"""Phase 1 实验：Knowledge → Hypothesis → Query → Search（含 relevance 判断 + support_type 分析）。

验证 knowledge-driven queries 能否找到主搜索遗漏的【相关】新论文。

用法（需 DEEPSEEK_API_KEY）:
    python experiment_knowledge_search.py data/exports/foundational_baseline.csv [篇数]
"""

import asyncio
import csv
import os
import sys
from collections import Counter
from search_engine.llm import DeepSeekBackend
from search_engine.knowledge_extractor import KnowledgeExtractor
from search_engine.citation_tracker import CitationTracker
from search_engine.evaluator import normalize_doi
from search_engine.relevance import RelevanceFilter
from search_engine.models import Paper

QUESTION = "光固化聚合物降低聚合收缩与收缩应力的机制"


async def main():
    csv_path = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 10

    papers: list[Paper] = []
    with open(csv_path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row.get("title") and row.get("doi"):
                papers.append(Paper(
                    paper_id=row["doi"], title=row["title"], abstract=row.get("abstract", ""),
                ))
            if len(papers) >= n:
                break

    original_dois = {normalize_doi(p.doi) for p in papers}

    backend = DeepSeekBackend(api_key=os.environ.get("DEEPSEEK_API_KEY", ""))
    extractor = KnowledgeExtractor(backend)

    print(f"从 {len(papers)} 篇论文提取知识...\n")
    all_queries: list[tuple[str, str, str]] = []  # (query, support_type, source_title)
    for p in papers:
        rec = await extractor.extract(p)
        if not rec:
            continue
        for h in rec.search_hypotheses:
            for q in h.queries:
                if q and q not in [x[0] for x in all_queries]:
                    all_queries.append((q, h.support_type, p.title[:40]))

    print(f"共 {len(all_queries)} 条 knowledge queries\n")

    # 搜索 + 收集新候选（记录每个 query 找到的新 DOI）
    new_papers: dict[str, Paper] = {}
    query_novel: dict[str, list[str]] = {}  # query -> 找到的新 DOI
    async with CitationTracker() as tracker:
        for query, support, src in all_queries:
            try:
                results = await tracker.search(query, limit=20)
            except Exception as e:
                print(f"  [ERR] {query[:50]}: {e}")
                continue
            novel = [p for p in results if normalize_doi(p.doi) not in original_dois]
            if novel:
                query_novel[query] = [normalize_doi(p.doi) or p.paper_id for p in novel]
                for p in novel:
                    new_papers.setdefault(normalize_doi(p.doi) or p.paper_id, p)

    print(f"去重后新候选: {len(new_papers)} 篇\n")

    # relevance 判断
    print("对新候选做 relevance 判断...\n")
    rf = RelevanceFilter(backend)
    scored = await rf.filter(
        list(new_papers.values()), research_question=QUESTION, threshold=0, top_k=len(new_papers)
    )
    relevant_new = {normalize_doi(sp.paper.doi) or sp.paper.paper_id: sp for sp in scored if sp.score >= 70}
    print(f"相关新论文（score>=70）: {len(relevant_new)} 篇\n")

    # 统计 useful queries（找到 ≥1 relevant 新论文）
    useful_queries = []
    for query, novos in query_novel.items():
        if any(d in relevant_new for d in novos):
            useful_queries.append(query)

    # support_type 分布
    support_counter = Counter()
    for q, support, _ in all_queries:
        if q in useful_queries:
            support_counter[support] += 1
    total_counter = Counter(s for _, s, _ in all_queries)

    print("=== 实验结果 ===")
    print(f"knowledge queries: {len(all_queries)}")
    print(f"产生新候选的 query: {len(query_novel)}")
    print(f"新候选: {len(new_papers)}")
    print(f"相关新论文 (>=70): {len(relevant_new)}")
    print(f"useful queries（找到≥1相关新论文）: {len(useful_queries)}")
    print(f"Useful Query Yield (candidate): {len(query_novel)/len(all_queries)*100:.0f}%")
    print(f"Useful Query Yield (relevant):  {len(useful_queries)/len(all_queries)*100:.0f}%")

    print("\n=== 成功 query 的 support_type 分布 ===")
    for st in total_counter:
        u = support_counter.get(st, 0)
        t = total_counter[st]
        print(f"  {st}: {u}/{t} useful ({u/t*100 if t else 0:.0f}%)")

    if relevant_new:
        print("\n相关新论文示例:")
        for doi, sp in list(relevant_new.items())[:15]:
            t = (sp.paper.title or "").encode('ascii', 'replace').decode('ascii')[:70]
            print(f"  - [{sp.score}%] [{sp.paper.year}] {t}")


if __name__ == "__main__":
    asyncio.run(main())
