"""测试 CoverageAwareExpander：从知识缺口生成 query。

用法（需 DEEPSEEK_API_KEY）:
    python test_coverage_expander.py
"""

import asyncio
import os
from search_engine.llm import DeepSeekBackend
from search_engine.knowledge_base import KnowledgeBase
from search_engine.route_normalizer import RouteNormalizer
from search_engine.route_ontology import RouteOntology
from search_engine.coverage_aware_expander import CoverageAwareExpander


async def main():
    backend = DeepSeekBackend(api_key=os.environ.get("DEEPSEEK_API_KEY", ""))
    kb = KnowledgeBase()
    normalizer = RouteNormalizer(backend)
    ontology = RouteOntology(backend)
    expander = CoverageAwareExpander(backend, normalizer, ontology=ontology, gap_threshold=1)

    records = kb.get_all()
    print(f"Knowledge Base 记录: {len(records)} 条\n")

    # 分析 coverage（strategy-level）
    result = await expander.analyze(records)
    coverage = result["coverage"]
    gaps = result["gaps"]
    non_family = result.get("non_family", [])

    print("=== research strategy coverage ===")
    for route, count in coverage.most_common():
        bar = "█" * min(count, 10)
        print(f"  {route}: {count} 篇 {bar}")

    print(f"\n=== 机制/过程类（不计入）: {len(non_family)} 个 ===")

    print(f"\n=== 缺口（strategy coverage ≤ 1）: {len(gaps)} 个 ===")
    for g in gaps:
        print(f"  - {g}")

    # Route ontology（关系图）
    print("\n=== Route Ontology（strategy → canonical_routes / aliases / mechanisms / historical_terms）===")
    onto = await ontology.build(records)
    for strategy, info in onto.items():
        print(f"\n[{strategy}]")
        print(f"  canonical_routes: {info['canonical_routes']}")
        print(f"  aliases: {info['aliases'][:8]}")
        print(f"  historical_terms: {info['historical_terms'][:5]}")

    # Mechanism ontology（清洗后的机制归属）
    print("\n=== Mechanism Ontology（strategy → 清洗后的 mechanisms）===")
    mech_onto = await ontology.build_mechanism_ontology(onto)
    for strategy, mechs in mech_onto.items():
        print(f"  {strategy}: {mechs}")

    kb.close()


if __name__ == "__main__":
    asyncio.run(main())
