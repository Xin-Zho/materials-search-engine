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
    queries_file = sys.argv[3] if len(sys.argv) > 3 else None

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

    # 生成（或加载）knowledge queries
    if queries_file and os.path.exists(queries_file):
        import json as _json
        all_queries = [tuple(q) for q in _json.load(open(queries_file, encoding="utf-8"))]
        print(f"从 {queries_file} 加载 {len(all_queries)} 条固定 queries\n")
    else:
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
        # 保存固定 queries
        default_qf = "data/cache/knowledge_queries.json"
        import json as _json
        _json.dump(all_queries, open(default_qf, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"共 {len(all_queries)} 条 knowledge queries（已保存到 {default_qf}）\n")

    # 逐条搜索 + 记录
    query_records = []  # dict per query
    new_papers: dict[str, Paper] = {}
    from search_engine.citation_tracker import RateLimitError, RateLimitExhaustedError
    async with CitationTracker() as tracker:
        # 请求前检查额度
        try:
            remaining = await tracker.check_rate_limit()
            if remaining > 0:
                print(f"OpenAlex 剩余额度: {remaining}\n")
        except RateLimitExhaustedError as e:
            print(f"\n[终止] {e}")
            return

        for query, support, src in all_queries:
            try:
                results = await tracker.search(query, limit=20)
                total_hits = getattr(tracker, "last_total_hits", len(results))
            except RateLimitError as e:
                query_records.append({"query": query, "support": support, "src": src,
                                      "total_hits": -1, "overlap": 0, "novel_dois": [], "error": "RATE_LIMITED"})
                continue
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
        if rec.get("error") == "RATE_LIMITED":
            failure = "RATE_LIMITED"
        elif rec.get("error"):
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
    rate_limited = failure_counter.get("RATE_LIMITED", 0)
    evaluable = len(all_queries) - rate_limited

    print(f"\n=== 核心指标 ===")
    print(f"total queries: {len(all_queries)}")
    print(f"RATE_LIMITED (排除): {rate_limited}")
    print(f"evaluable: {evaluable}")
    print(f"useful queries (NEW_RELEVANT): {useful}")
    print(f"Useful Query Yield (relevant): {useful}/{evaluable} = {useful/evaluable*100:.1f}%")
    print(f"Unique New Relevant Papers: {len(relevant_dois)}")
    print(f"New Relevant per useful query: {len(relevant_dois)/useful:.1f}" if useful else "N/A")


if __name__ == "__main__":
    asyncio.run(main())
