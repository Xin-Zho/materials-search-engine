"""搜索路线覆盖子包（Search Coverage）。

与 knowledge/ 下的机制覆盖（Knowledge Coverage）刻意区分命名，避免混淆：
- ``coverage.route_coverage.CoverageMap``
      主搜索迭代用：把论文聚类成技术路线，识别路线级缺口，驱动下一轮查询。
- ``knowledge.coverage.MechanismCoverageAnalyzer``
      知识库用：route × mechanism 矩阵，知道哪个机制的证据缺失。
"""

from .route_coverage import CoverageMap

__all__ = ["CoverageMap"]
