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

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


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

    # 按 source_type 分流生成 query
    from search_engine.knowledge_base import HistoricalQueryBuilder
    builder = HistoricalQueryBuilder(anchor="polymerization shrinkage")
    channels = builder.build_by_channel(records)

    print("=== 分流 query channel ===")
    channel_names = {
        "historical_term": "Historical Recall（旧称/别名，主力）",
        "route": "Route Expansion（技术路线）",
        "synonym": "Lexical Expansion（同义词变体）",
        "material": "Material-conditioned Search（材料）",
    }
    all_queries = []
    for ch, qs in channels.items():
        all_queries.extend(qs)
        print(f"\n[{ch}] {channel_names.get(ch, ch)}: {len(qs)} 条")
        for q in qs[:8]:
            print(f"    {q.query}")

    # semantic coverage（canonicalize 后匹配，不是 exact string）
    from search_engine.foundational_recovery import FoundationalRecovery
    hardcoded = FoundationalRecovery.ROUTE_QUERIES
    learned_terms = {builder._canonicalize(q.source_term) for q in all_queries}
    covered = []
    for h in hardcoded:
        hc = builder._canonicalize(h)
        # 语义覆盖：hardcoded 的 canonical 是否被某个 learned term 包含或包含它
        if any(hc in lt or lt in hc for lt in learned_terms):
            covered.append(h)
    print(f"\n=== semantic coverage of legacy ROUTE_QUERIES ===")
    print(f"knowledge-derived 语义覆盖: {len(covered)}/{len(hardcoded)} 条")
    for h in hardcoded:
        mark = "✓" if h in covered else "✗"
        print(f"  {mark} {h}")

    kb.close()


if __name__ == "__main__":
    asyncio.run(main())
