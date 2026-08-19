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

    def relax(self, query: str) -> list[str]:
        """放宽一条 query，返回多条（含原 query）。

        - 拆 AND 概念
        - 区分 anchor（保留）vs 非 anchor（放宽）
        - 每个非 anchor 单独 + anchor 生成一条宽查询
        - 原 query 保留（最窄）
        """
        concepts = self._split_and(query)
        if len(concepts) <= 1:
            return [query]

        anchors = [c for c in concepts if self._is_anchor(c)]
        non_anchors = [c for c in concepts if not self._is_anchor(c)]

        relaxed = []
        # 每个非 anchor 单独 + 所有 anchor（最宽，recall-first）
        for na in non_anchors:
            q = " AND ".join([na] + anchors)
            if q not in relaxed:
                relaxed.append(q)
        # 原 query（最窄，precision）
        if query not in relaxed:
            relaxed.append(query)

        logger.debug("relax %d concepts → %d queries", len(concepts), len(relaxed))
        return relaxed

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
