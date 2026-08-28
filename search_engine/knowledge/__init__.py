"""知识覆盖子包（Knowledge Coverage）。

route × mechanism 覆盖矩阵 + 缺口检测 + 缺口转 query。
与 coverage/（搜索路线覆盖）区分：这里关心的是知识库里"哪个机制的证据缺失"。

流程：
    Knowledge Base records
        ↓ MechanismCoverageAnalyzer.analyze / analyze_route_coverage
    coverage 矩阵
        ↓ GapDetector.detect_gaps / detect_missing_mechanisms
    缺口列表
        ↓ GapQueryGenerator.generate_gap_queries
    recall queries → 喂回搜索后端
"""

from .coverage import MechanismCoverageAnalyzer
from .gap_detector import GapDetector
from .gap_query_generator import GapQueryGenerator
from .expander import CoverageAwareExpander
from .autonomous_loop import AutonomousLoop

__all__ = [
    "MechanismCoverageAnalyzer",
    "GapDetector",
    "GapQueryGenerator",
    "CoverageAwareExpander",
    "AutonomousLoop",
]
