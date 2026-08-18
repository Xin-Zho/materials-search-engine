"""Phase 1 实验 + 逐条 failure diagnosis。

诊断每个 knowledge query 的失败类型：
NO_HITS / ALL_ALREADY_FOUND / NEW_BUT_IRRELEVANT / NEW_RELEVANT。

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
    all_queries: list[tuple[str, str, str]] = []
    for p in papers:
        rec = await extractor.extract(p)
        if not rec:
            continue
        for h in rec.search_hypotheses:
            for q in h.queries:
                if q and q not in [x[0] for x in all_queries]:
                    all_queries.append((q, h.support_type, p.title[:40]))

    print(f"共 {len(all_queries)} 条 knowledge queries\n")

    # 逐条搜索 + 记录
    query_records = []  # dict per query
    new_papers: dict[str, Paper] = {}
    async with CitationTracker() as tracker:
        for query, support, src in all_queries:
            try:
                results = await tracker.search(query, limit=20)
                total_hits = getattr(tracker, "last_total_hits", len(results))
            except Exception as e:
                query_records.append({"query": query, "support": support, "src": src,
                                      "total_hits": 0, "overlap": 0, "novel_dois": [], "error": str(e)})
                continue
            overlap = sum(1 for p in results if normalize_doi(p.doi) in original_dois)
            novel = [p for p in results if normalize_doi(p.doi) not in original_dois]
            novel_dois = [normalize_doi(p.doi) or p.paper_id for p in novel]
            for p in novel:
                new_papers.setdefault(normalize_doi(p.doi) or p.paper_id, p)
            query_records.append({"query": query, "support": support, "src": src,
                                  "total_hits": total_hits, "overlap": overlap, "novel_dois": novel_dois})

    # relevance 判断所有新候选
    rf = RelevanceFilter(backend)
    scored = await rf.filter(list(new_papers.values()), research_question=QUESTION,
                             threshold=0, top_k=len(new_papers))
    relevant_dois = {normalize_doi(sp.paper.doi) or sp.paper.paper_id for sp in scored if sp.score >= 70}

    # 逐条分类
    print("=== 逐条 failure diagnosis ===\n")
    failure_counter = Counter()
    support_failure = Counter()
    for rec in query_records:
        if rec.get("error"):
            failure = "ERROR"
        elif rec["total_hits"] == 0:
            failure = "NO_HITS"
        elif not rec["novel_dois"]:
            failure = "ALL_ALREADY_FOUND"
        elif not any(d in relevant_dois for d in rec["novel_dois"]):
            failure = "NEW_BUT_IRRELEVANT"
        else:
            failure = "NEW_RELEVANT"
        failure_counter[failure] += 1
        support_failure[(rec["support"], failure)] += 1

        if failure in ("NEW_RELEVANT", "NO_HITS") or rec["support"] == "mechanism_inference":
            print(f"  [{failure}] ({rec['support']}) hits={rec['total_hits']} overlap={rec['overlap']} novel={len(rec['novel_dois'])}")
            print(f"      {rec['query'][:80]}")

    print("\n=== 总 failure 分布 ===")
    for f, c in failure_counter.most_common():
        print(f"  {f}: {c}")

    print("\n=== mechanism_inference 的 failure 分布 ===")
    mech_total = sum(c for (s, f), c in support_failure.items() if s == "mechanism_inference")
    for (s, f), c in sorted(support_failure.items()):
        if s == "mechanism_inference":
            print(f"  {f}: {c}/{mech_total}")

    useful = sum(c for (s, f), c in support_failure.items() if f == "NEW_RELEVANT")
    print(f"\nUseful Query Yield (relevant): {useful}/{len(all_queries)} = {useful/len(all_queries)*100:.1f}%")


if __name__ == "__main__":
    asyncio.run(main())
