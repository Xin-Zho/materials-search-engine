"""QueryRelaxer — 把过严的知识 query 放宽成多条 recall-first 查询。

原则：知识表示可以复杂，检索表达应该更宽。
一条 AND 连接的复杂 query，拆成多条"保留核心 anchor + 少量概念"的宽查询，
但始终保留 target anchor（如 shrinkage），防止放宽后偏离主题。

使用方式:
    relaxer = QueryRelaxer(anchor_terms=["shrinkage", "shrinkage stress", "contraction"])
    relaxed = relaxer.relax("cyclic monomers AND ring-opening AND shrinkage")
    # → ["cyclic monomers AND shrinkage", "ring-opening AND shrinkage", ...]
"""

import logging

logger = logging.getLogger(__name__)


class QueryRelaxer:
    """把复杂 knowledge query 放宽成多条 recall-first 查询。"""

    def __init__(self, anchor_terms: list[str] | None = None):
        # anchor = 必须保留的核心目标词（研究问题的 target property）
        self.anchor_terms = anchor_terms or [
            "shrinkage", "shrinkage stress", "contraction",
            "polymerization shrinkage", "stress",
        ]

    def relax(self, query: str, max_level: int = 2) -> list[str]:
        """逐层放宽（progressive / bounded），每层至少保留 anchor + ≥1 非 anchor。

        - 不放宽到单词（禁止单个泛词）
        - 每层减少一个非 anchor 概念，最多 max_level 层
        - anchor（target）始终保留

        例: cyclic monomers AND ring-opening AND photopolymerization AND shrinkage
          → level1: cyclic monomers AND ring-opening AND shrinkage
          → level2: cyclic monomers AND shrinkage
        """
        concepts = self._split_and(query)
        if len(concepts) <= 2:
            return []  # 已经够宽，不放宽

        anchors = [c for c in concepts if self._is_anchor(c)]
        non_anchors = [c for c in concepts if not self._is_anchor(c)]

        if not anchors or len(non_anchors) <= 1:
            return []  # 没有 anchor 或非 anchor 太少，不放宽到单词

        levels = []
        # 从 len(non_anchors)-1 个非 anchor 递减到 max(len-1-max_level, 1) 个
        min_k = max(len(non_anchors) - 1 - max_level, 1)
        for k in range(len(non_anchors) - 1, min_k - 1, -1):
            q = " AND ".join(non_anchors[:k] + anchors)
            if q not in levels:
                levels.append(q)

        logger.debug("relax %d concepts → %d levels", len(concepts), len(levels))
        return levels

    def relax_many(self, queries: list[str]) -> list[tuple[str, str]]:
        """批量放宽，返回 (original_query, relaxed_query) 对。"""
        result = []
        for q in queries:
            for r in self.relax(q):
                result.append((q, r))
        return result

    def _is_anchor(self, concept: str) -> bool:
        c = concept.lower()
        return any(a in c for a in self.anchor_terms)

    @staticmethod
    def _split_and(query: str) -> list[str]:
        """按 AND 拆分（大小写不敏感），去引号，去空。"""
        parts = [p.strip().strip('"').strip("'") for p in query.split(" AND ")]
        return [p for p in parts if p]
