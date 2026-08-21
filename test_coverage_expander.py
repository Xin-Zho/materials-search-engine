"""测试 CoverageAwareExpander：从知识缺口生成 query。

用法（需 DEEPSEEK_API_KEY）:
    python test_coverage_expander.py
"""

import asyncio
import os
from search_engine.llm import DeepSeekBackend
from search_engine.knowledge_base import KnowledgeBase
from search_engine.route_normalizer import RouteNormalizer
from search_engine.coverage_aware_expander import CoverageAwareExpander


async def main():
    backend = DeepSeekBackend(api_key=os.environ.get("DEEPSEEK_API_KEY", ""))
    kb = KnowledgeBase()
    normalizer = RouteNormalizer(backend)
    expander = CoverageAwareExpander(backend, normalizer, gap_threshold=1)

    records = kb.get_all()
    print(f"Knowledge Base 记录: {len(records)} 条\n")

    # 分析 coverage
    result = await expander.analyze(records)
    coverage = result["coverage"]
    gaps = result["gaps"]

    print("=== canonical route coverage ===")
    for route, count in coverage.most_common():
        bar = "█" * min(count, 10)
        print(f"  {route}: {count} 篇 {bar}")

    print(f"\n=== 缺口（coverage ≤ 1）: {len(gaps)} 个 ===")
    for g in gaps:
        print(f"  - {g}")

    # 生成缺口 query
    queries = await expander.generate_gap_queries(gaps, anchor="polymerization shrinkage")
    print(f"\n=== 缺口 query（{len(queries)} 条）===")
    for q in queries:
        print(f"  {q}")

    kb.close()


if __name__ == "__main__":
    asyncio.run(main())
