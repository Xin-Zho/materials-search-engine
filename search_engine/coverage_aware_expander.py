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
from .route_ontology import RouteOntology

logger = logging.getLogger(__name__)


class CoverageAwareExpander:
    """从覆盖缺口（family 层面）生成 query。"""

    def __init__(self, backend: LLMBackend, normalizer: RouteNormalizer,
                 ontology: RouteOntology | None = None,
                 gap_threshold: int = 1):
        self.backend = backend
        self.normalizer = normalizer
        self.ontology = ontology or RouteOntology(backend)
        self.gap_threshold = gap_threshold

    async def analyze(self, records) -> dict:
        """分析 route family coverage，识别缺口。

        Args:
            records: list[KnowledgeRecord]

        Returns:
            {"coverage": {family: count}, "gaps": [薄弱 family],
             "non_family": [机制/过程类 route 数]}
        """
        # 1. 提取所有 raw routes
        raw_routes: list[str] = []
        for rec in records:
            raw_routes.extend(rec.strategy_routes)

        if not raw_routes:
            return {"coverage": Counter(), "gaps": [], "non_family": []}

        # 2. 归一化成 canonical
        canonical_map = await self.normalizer.normalize(raw_routes)
        canonical_routes = [canonical_map.get(raw, raw) for raw in raw_routes]

        # 3. 分类到 family（区分技术路线 vs 机制/过程）
        classified = await self.ontology.classify(canonical_routes)

        # 4. family-level coverage（只统计 strategy_family，机制/过程不计入）
        coverage = Counter()
        non_family = []
        for c in classified:
            if c["type"] == "strategy_family":
                family = c["family"] or c["route"]
                coverage[family] += 1
            else:
                non_family.append(c["route"])

        # 5. 识别缺口（family coverage ≤ threshold）
        gaps = [family for family, count in coverage.items() if count <= self.gap_threshold]

        logger.info("coverage 分析: %d family, %d 缺口, %d 非 family 类",
                     len(coverage), len(gaps), len(non_family))
        return {"coverage": coverage, "gaps": gaps, "non_family": non_family}

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
