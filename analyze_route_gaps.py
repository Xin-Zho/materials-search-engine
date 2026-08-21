"""Phase 1.5：Knowledge-driven 新增论文的 route-level 盲区分析。

回答：knowledge-driven 找到的新增相关论文，补了主搜索哪些搜索盲区（route）。

用法（需 DEEPSEEK_API_KEY）:
    python analyze_route_gaps.py data/exports/foundational_baseline.csv
"""

import asyncio
import csv
import json
import os
import sys
from collections import Counter
from search_engine.llm import DeepSeekBackend
from search_engine.knowledge_extractor import KnowledgeExtractor
from search_engine.citation_tracker import CitationTracker, RateLimitError
from search_engine.evaluator import normalize_doi
from search_engine.relevance import RelevanceFilter
from search_engine.models import Paper

QUESTION = "光固化聚合物降低聚合收缩与收缩应力的机制"

# benchmark 的 gold route（用于对比"盲区"）
GOLD_ROUTES = {
    "Silorane/阳离子开环", "Spiro-orthocarbonate 膨胀单体", "Thiol-ene 步增长延迟凝胶",
    "AFCT 网络重排", "无机填料高填充", "理论基准",
}


async def main():
    csv_path = sys.argv[1]

    papers: list[Paper] = []
    with open(csv_path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row.get("title") and row.get("doi"):
                papers.append(Paper(paper_id=row["doi"], title=row["title"], abstract=row.get("abstract", "")))
    original_dois = {normalize_doi(p.doi) for p in papers}

    backend = DeepSeekBackend(api_key=os.environ.get("DEEPSEEK_API_KEY", ""))
    extractor = KnowledgeExtractor(backend)

    # 加载冻结的 knowledge queries（前 24 条）
    knowledge_queries = json.load(open("data/cache/knowledge_queries.json", encoding="utf-8"))
    queries = [q[0] for q in knowledge_queries[:24]]
    print(f"加载 {len(queries)} 条 knowledge queries\n")

    # 搜索 + relevance 判断
    rf = RelevanceFilter(backend)
    new_papers: dict[str, Paper] = {}
    async with CitationTracker() as tracker:
        for q in queries:
            try:
                results = await tracker.search(q, limit=20)
            except RateLimitError:
                continue
            except Exception:
                continue
            for p in results:
                if normalize_doi(p.doi) not in original_dois:
                    new_papers.setdefault(normalize_doi(p.doi) or p.paper_id, p)

    print(f"新增候选: {len(new_papers)} 篇\n")

    if new_papers:
        scored = await rf.filter(list(new_papers.values()), research_question=QUESTION,
                                 threshold=0, top_k=len(new_papers))
        relevant = [sp.paper for sp in scored if sp.score >= 70]
    else:
        relevant = []

    print(f"新增相关论文（≥70）: {len(relevant)} 篇\n")

    # 对每篇相关论文提取 route
    print("=== 新增相关论文的 route 分析 ===\n")
    raw_route_counter = Counter()
    paper_routes: list[tuple[str, list[str]]] = []
    for p in relevant:
        rec = await extractor.extract(p)
        routes = rec.strategy_routes if rec else []
        raw_route_counter.update(routes)
        paper_routes.append((p.title, routes))
        t = (p.title or "").encode('ascii', 'replace').decode('ascii')[:70]
        print(f"  [{p.year}] {t}")
        print(f"      routes: {routes}")

    # Route normalization（raw → canonical）
    from search_engine.route_normalizer import RouteNormalizer
    normalizer = RouteNormalizer(backend)
    all_raw = list(raw_route_counter.keys())
    canonical_map = await normalizer.normalize(all_raw)

    canonical_counter = Counter()
    for raw, count in raw_route_counter.items():
        canonical_counter[canonical_map.get(raw, raw)] += count

    print("\n=== canonical route 分布（归一化后）===")
    for route, count in canonical_counter.most_common():
        print(f"  {route}: {count} 篇")

    # 对比 gold route（盲区分析，用 canonical）
    print("\n=== 盲区分析（vs benchmark gold routes，canonical 对齐）===")
    # 用 canonical 名做模糊匹配
    gold_kw = {
        "AFCT 网络重排": ["addition-fragmentation", "chain transfer", "aft"],
        "Silorane/阳离子开环": ["silorane", "oxirane", "ring-opening", "cationic"],
        "Spiro-orthocarbonate 膨胀单体": ["spiro", "orthocarbonate", "expanding monomer"],
        "Thiol-ene 步增长延迟凝胶": ["thiol", "ene"],
        "无机填料高填充": ["filler", "silica", "particle", "packing"],
        "理论基准": ["shrinkage stress", "polymerization shrinkage", "contraction"],
    }
    matched = set()
    for route in canonical_counter:
        rl = route.lower()
        for g, kws in gold_kw.items():
            if any(kw in rl for kw in kws):
                matched.add(g)
    blind_spots = set(gold_kw) - matched
    print(f"knowledge 新增论文覆盖了这些 gold route: {sorted(matched) if matched else '无'}")
    print(f"gold route 盲区（仍未覆盖）: {sorted(blind_spots) if blind_spots else '无'}")
    print(f"\n新增论文引入的 gold 之外的新 route:")
    for r in canonical_counter:
        if not any(any(kw in r.lower() for kw in kws) for kws in gold_kw.values()):
            print(f"  - {r}")


if __name__ == "__main__":
    asyncio.run(main())
