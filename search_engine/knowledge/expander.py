"""CoverageAwareExpander — 编排层（facade），保留向后兼容。

历史入口。真正逻辑已拆到三个独立类，本类只做 analyze → detect → generate 的编排：

- MechanismCoverageAnalyzer  → 算 coverage（route × mechanism 矩阵）
- GapDetector                → 从 coverage 提缺口
- GapQueryGenerator          → 缺口转 query

新代码建议直接用细粒度类（更易测、可单独复用）；本类存在是为了让旧调用方
（test_coverage_expander 等）零改动。
"""

import logging

from .coverage import MechanismCoverageAnalyzer
from .gap_detector import GapDetector
from .gap_query_generator import GapQueryGenerator, DEFAULT_ANCHOR

logger = logging.getLogger(__name__)


class CoverageAwareExpander:
    """从覆盖缺口生成 query 的编排器（薄 facade）。"""

    def __init__(self, backend, normalizer, ontology=None, gap_threshold: int = 1):
        self.analyzer = MechanismCoverageAnalyzer(backend, normalizer, ontology)
        self.detector = GapDetector(gap_threshold)
        self.generator = GapQueryGenerator()
        self.gap_threshold = gap_threshold

    async def analyze(self, records) -> dict:
        """策略级 coverage 分析 + 缺口识别（兼容旧返回结构）。"""
        result = await self.analyzer.analyze(records)
        result["gaps"] = self.detector.detect_gaps(result["coverage"])
        return result

    async def analyze_route_coverage(self, records) -> dict:
        """route × mechanism 覆盖矩阵（含 missing_mechanisms）。"""
        return await self.analyzer.analyze_route_coverage(records)

    async def generate_gap_queries(self, gaps: list[str],
                                   anchor: str = DEFAULT_ANCHOR) -> list[str]:
        """缺口 route → recall query。"""
        return self.generator.generate_gap_queries(gaps, anchor)

    async def expand(self, records, anchor: str = DEFAULT_ANCHOR) -> list[str]:
        """一步完成：分析 coverage → 识别缺口 → 生成 query。"""
        result = await self.analyze(records)
        return await self.generate_gap_queries(result["gaps"], anchor)
