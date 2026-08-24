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

        # 2. 归一化成 canonical（raw → canonical）
        canonical_map = await self.normalizer.normalize(list(set(raw_routes)))

        # 3. 分类 canonical → strategy（一次）
        canonical_routes = list(set(canonical_map.values()))
        classified = await self.ontology.classify(canonical_routes)

        route_to_strategy: dict[str, str] = {}
        non_family: set[str] = set()
        for c in classified:
            if c["type"] == "strategy_family":
                route_to_strategy[c["route"]] = c.get("strategy") or c.get("family") or c["route"]
            else:
                non_family.add(c["route"])

        # 4. 论文级 coverage（每篇论文的每个 strategy 计一次，避免 raw 表达重复计数）
        coverage = Counter()
        for rec in records:
            rec_strategies: set[str] = set()
            for route in rec.strategy_routes:
                canonical = canonical_map.get(route, route)
                strategy = route_to_strategy.get(canonical)
                if strategy:
                    rec_strategies.add(strategy)
            for s in rec_strategies:
                coverage[s] += 1

        # 5. 识别缺口（strategy coverage ≤ threshold）
        gaps = [s for s, count in coverage.items() if count <= self.gap_threshold]

        logger.info("coverage 分析: %d strategy, %d 缺口, %d 非 family 类",
                     len(coverage), len(gaps), len(non_family))
        return {"coverage": coverage, "gaps": gaps, "non_family": sorted(non_family)}

    async def analyze_route_coverage(self, records) -> dict:
        """route-level + mechanism-level coverage（知道哪个机制没覆盖）。

        Returns:
            {"route_coverage": {route: paper_count},
             "mechanism_coverage": {route: {mechanism: covered_bool}},
             "missing_mechanisms": {route: [未覆盖 mechanism]}}
        """
        from collections import Counter

        # 1. build route graph（route → mechanisms）
        route_graph = await self.ontology.build_route_graph(records)

        # 2. raw → canonical route 映射
        raw_routes = [r for rec in records for r in rec.strategy_routes]
        classified = await self.ontology.classify(raw_routes)
        raw_to_canonical: dict[str, str] = {}
        for c in classified:
            if c["type"] == "strategy_family":
                raw_to_canonical[c["route"]] = c.get("family") or c["route"]

        # 3. route-level coverage（每篇论文的每个 route 计一次）
        route_coverage = Counter()
        for rec in records:
            rec_routes = set()
            for route in rec.strategy_routes:
                canonical = raw_to_canonical.get(route, route)
                rec_routes.add(canonical)
            for r in rec_routes:
                route_coverage[r] += 1

        # 4. mechanism-level coverage（每个 route 的 mechanism 是否被论文提到）
        all_mechs: set[str] = set()
        for rec in records:
            for m in rec.physical_mechanisms:
                for term in (m.mechanism, m.cause, m.effect):
                    if term:
                        all_mechs.add(term.lower())

        mechanism_coverage: dict[str, dict] = {}
        missing_mechanisms: dict[str, list] = {}
        for route, info in route_graph.items():
            mechanism_coverage[route] = {}
            missing_mechanisms[route] = []
            for mech in info.get("mechanisms", []):
                covered = mech.lower() in all_mechs
                mechanism_coverage[route][mech] = covered
                if not covered:
                    missing_mechanisms[route].append(mech)

        return {
            "route_coverage": dict(route_coverage),
            "mechanism_coverage": mechanism_coverage,
            "missing_mechanisms": missing_mechanisms,
        }

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
