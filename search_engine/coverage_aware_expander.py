"""CoverageAwareExpander — 从知识缺口生成 query，不从已有论文生成。

解决 Knowledge 继承 seed 分布的问题（AFCT vocabulary explosion）：
不是"从已有论文生成 query"，而是"从覆盖薄弱的 route 生成 query"。

流程：
  Knowledge Base records
    → 提取 raw strategy_routes
    → RouteNormalizer 归一化成 canonical
    → 统计 coverage（canonical route → 论文数）
    → 识别缺口（coverage ≤ 1 的 route）
    → 对缺口 route 生成 recall query

使用方式:
    expander = CoverageAwareExpander(backend, normalizer)
    result = await expander.analyze(records)
    # result["gaps"] = 覆盖薄弱的 canonical routes
    queries = await expander.generate_gap_queries(result["gaps"], anchor="polymerization shrinkage")
"""

import logging
from collections import Counter
from .llm import LLMBackend
from .route_normalizer import RouteNormalizer

logger = logging.getLogger(__name__)


class CoverageAwareExpander:
    """从覆盖缺口生成 query。"""

    def __init__(self, backend: LLMBackend, normalizer: RouteNormalizer,
                 gap_threshold: int = 1):
        self.backend = backend
        self.normalizer = normalizer
        self.gap_threshold = gap_threshold

    async def analyze(self, records) -> dict:
        """分析 route coverage，识别缺口。

        Args:
            records: list[KnowledgeRecord]

        Returns:
            {"coverage": {canonical_route: count}, "gaps": [薄弱 route]}
        """
        # 1. 提取所有 raw routes
        raw_routes: list[str] = []
        for rec in records:
            raw_routes.extend(rec.strategy_routes)

        if not raw_routes:
            return {"coverage": Counter(), "gaps": []}

        # 2. 归一化成 canonical
        canonical_map = await self.normalizer.normalize(raw_routes)

        # 3. 统计 canonical coverage
        coverage = Counter()
        for raw in raw_routes:
            coverage[canonical_map.get(raw, raw)] += 1

        # 4. 识别缺口（coverage ≤ threshold）
        gaps = [route for route, count in coverage.items() if count <= self.gap_threshold]

        logger.info("coverage 分析: %d canonical route, %d 缺口",
                     len(coverage), len(gaps))
        return {"coverage": coverage, "gaps": gaps}

    async def generate_gap_queries(self, gaps: list[str], anchor: str = "polymerization shrinkage") -> list[str]:
        """对缺口 route 生成 recall query（route + anchor）。"""
        queries = []
        for route in gaps:
            queries.append(f'"{route}" AND "{anchor}"')
        logger.info("缺口 query: %d 条", len(queries))
        return queries

    async def expand(self, records, anchor: str = "polymerization shrinkage") -> list[str]:
        """一步完成：分析 coverage → 识别缺口 → 生成 query。"""
        result = await self.analyze(records)
        return await self.generate_gap_queries(result["gaps"], anchor)
