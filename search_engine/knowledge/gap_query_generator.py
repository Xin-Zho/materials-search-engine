"""GapQueryGenerator — 把缺口转成召回 query。

策略缺口 → route + anchor；机制缺口 → route + mechanism + anchor。
分级 fallback（L0-L3）：strict title/abstract 优先，fulltext rescue；
fulltext 层用 mechanism 别名做 query expansion（ontology 不止用于 coverage，
也用于 query expansion——Phase 1.8 方向）。
只负责拼字符串，不碰 LLM、不碰 coverage 计算。
"""

import logging

from ..mechanism_normalizer import MechanismNormalizer

logger = logging.getLogger(__name__)

DEFAULT_ANCHOR = "polymerization shrinkage"


class GapQueryGenerator:
    """缺口 → query 生成器。"""

    def __init__(self, anchor: str = DEFAULT_ANCHOR):
        self.anchor = anchor
        self.mech_normalizer = MechanismNormalizer()

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

    def generate_fallback_queries(self, route: str, mechanism: str | None,
                                  anchor: str | None = None) -> list[dict]:
        """单个 gap 的分级 fallback query 链（L0→L3，bounded）。

        L0: strict（title/abstract）R+M+A   —— 最高 precision
        L1: strict R+M                        —— 放宽 anchor
        L2: fulltext R+(M∨alias)+A            —— rescue + mechanism 别名扩展
        L3: fulltext R+(M∨alias)              —— 最宽

        strict 层保持精确短语（OpenAlex filter 不支持 OR 布尔）；fulltext 层
        用 mechanism 别名 OR 组合，弥补措辞差异（"reduced shrinkage" vs
        "shrinkage stress" vs "volumetric contraction"）。

        level/scope 是语义意图，由 backend 决定执行方式
        （strict → SearchBackend.search_strict；fulltext → SearchBackend.search）。
        """
        a = anchor or self.anchor
        if mechanism:
            mech_or = self._mechanism_or(mechanism)
            return [
                {"query": f'"{route}" AND "{mechanism}" AND "{a}"',
                 "level": 0, "scope": "strict", "terms": "R+M+A"},
                {"query": f'"{route}" AND "{mechanism}"',
                 "level": 1, "scope": "strict", "terms": "R+M"},
                {"query": f'"{route}" AND ({mech_or}) AND "{a}"',
                 "level": 2, "scope": "fulltext", "terms": "R+(M∨alias)+A"},
                {"query": f'"{route}" AND ({mech_or})',
                 "level": 3, "scope": "fulltext", "terms": "R+(M∨alias)"},
            ]
        return [
            {"query": f'"{route}" AND "{a}"',
             "level": 0, "scope": "strict", "terms": "R+A"},
            {"query": f'"{route}"',
             "level": 1, "scope": "strict", "terms": "R"},
            {"query": f'"{route}" AND "{a}"',
             "level": 2, "scope": "fulltext", "terms": "R+A"},
            {"query": f'"{route}"',
             "level": 3, "scope": "fulltext", "terms": "R"},
        ]

    def _mechanism_or(self, mechanism: str, max_aliases: int = 4) -> str:
        """mechanism + 其别名 OR 组合（fulltext query expansion）。"""
        aliases = self.mech_normalizer.aliases_for(mechanism)
        variants = [mechanism] + [
            a for a in aliases if a and a.lower() != mechanism.lower()
        ][:max_aliases]
        return " OR ".join(f'"{v}"' for v in variants)
