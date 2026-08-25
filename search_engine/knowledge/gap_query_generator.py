"""GapQueryGenerator — 把缺口转成召回 query。

策略缺口 → route + anchor；机制缺口 → route + mechanism + anchor。
只负责拼字符串，不碰 LLM、不碰 coverage 计算。
"""

import logging

logger = logging.getLogger(__name__)

DEFAULT_ANCHOR = "polymerization shrinkage"


class GapQueryGenerator:
    """缺口 → query 生成器。"""

    def __init__(self, anchor: str = DEFAULT_ANCHOR):
        self.anchor = anchor

    def generate_gap_queries(self, gaps: list[str], anchor: str | None = None) -> list[str]:
        """策略缺口 → `"<route>" AND "<anchor>"`。"""
        a = anchor or self.anchor
        queries = [f'"{route}" AND "{a}"' for route in gaps]
        logger.info("缺口 query: %d 条", len(queries))
        return queries

    def generate_mechanism_queries(self, missing_mechanisms: dict,
                                   anchor: str | None = None) -> list[str]:
        """机制缺口 → `"<route>" AND "<mechanism>" AND "<anchor>"`。"""
        a = anchor or self.anchor
        queries = []
        for route, mechs in missing_mechanisms.items():
            for m in mechs:
                queries.append(f'"{route}" AND "{m}" AND "{a}"')
        return queries
