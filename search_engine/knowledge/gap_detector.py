"""GapDetector — 从覆盖分析结果识别缺口。

薄逻辑层：把 MechanismCoverageAnalyzer 算出的 coverage / mechanism_coverage
转成"缺口列表"。策略级缺口（coverage ≤ threshold）和机制级缺口（未覆盖机制）
都从这里出，与"如何算 coverage"解耦。
"""

import logging
from collections import Counter

logger = logging.getLogger(__name__)


class GapDetector:
    """缺口识别器。gap_threshold 仅作用于策略级缺口。"""

    def __init__(self, gap_threshold: int = 1):
        self.gap_threshold = gap_threshold

    def detect_gaps(self, coverage: Counter | dict) -> list[str]:
        """策略级缺口：覆盖数 ≤ threshold 的 strategy。"""
        return [s for s, count in coverage.items() if count <= self.gap_threshold]

    def detect_missing_mechanisms(self, mechanism_coverage: dict) -> dict[str, list[str]]:
        """机制级缺口：每个 core route 下 covered=False 的机制。"""
        missing: dict[str, list[str]] = {}
        for route, mechs in mechanism_coverage.items():
            miss = [m for m, info in mechs.items() if not info.get("covered")]
            if miss:
                missing[route] = miss
        return missing
