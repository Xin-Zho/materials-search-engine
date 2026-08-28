"""MechanismCoverageAnalyzer — 知识库的 route × mechanism 覆盖矩阵。

只负责"算 coverage"这一件事：把 KnowledgeRecord 流归一化成 canonical / core
route，统计策略级覆盖和机制级覆盖。缺口识别交给 gap_detector，query 生成交给
gap_query_generator——本类不碰这俩。

与 coverage.route_coverage.CoverageMap 区分：
- CoverageMap 看的是"搜索结果按路线聚类，哪条路线论文少"（搜索侧）。
- 本类看的是"知识库里哪个 route 的哪个机制缺证据"（知识侧）。

route × mechanism 匹配使用 CoverageMatcher（唯一语义源）：
- route：ROUTE_HIERARCHY 有方向 is_a（thiol-ene → step-growth）+ CORE aliases
- mechanism：MECHANISM_CANONICAL 概念层级（stress relief → stress relaxation）+ 别名兜底
与 global rematch / diagnostic 共用同一套 matcher，避免"本地说关、matrix 不认"的 divergence。
"""

import logging
from collections import Counter

from ..llm import LLMBackend
from ..route_normalizer import RouteNormalizer
from ..route_ontology import RouteOntology
from ..route_mechanism_ontology import (
    CORE_ROUTE_MECHANISMS, get_mechanisms, assign_route, route_match_type, CoverageMatcher,
    mechanism_type,
)
from ..mechanism_normalizer import MechanismNormalizer
from ..models import Mechanism

logger = logging.getLogger(__name__)


class MechanismCoverageAnalyzer:
    """route × mechanism 覆盖分析器。"""

    def __init__(self, backend: LLMBackend, normalizer: RouteNormalizer,
                 ontology: RouteOntology | None = None):
        self.backend = backend
        self.normalizer = normalizer
        self.ontology = ontology or RouteOntology(backend)

    async def analyze(self, records) -> dict:
        """策略级 coverage：raw route → canonical → strategy family，按论文计覆盖。

        Returns:
            {"coverage": Counter(strategy->论文数), "non_family": [机制/过程类 route]}
            （缺口由 GapDetector 从 coverage 算，不在这里）
        """
        raw_routes: list[str] = []
        for rec in records:
            raw_routes.extend(rec.strategy_routes)

        if not raw_routes:
            return {"coverage": Counter(), "non_family": []}

        canonical_map = await self.normalizer.normalize(list(set(raw_routes)))

        canonical_routes = list(set(canonical_map.values()))
        classified = await self.ontology.classify(canonical_routes)

        route_to_strategy: dict[str, str] = {}
        non_family: set[str] = set()
        for c in classified:
            if c["type"] == "strategy_family":
                route_to_strategy[c["route"]] = c.get("strategy") or c.get("family") or c["route"]
            else:
                non_family.add(c["route"])

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

        logger.info("coverage 分析: %d strategy, %d 非 family 类",
                     len(coverage), len(non_family))
        return {"coverage": coverage, "non_family": sorted(non_family)}

    async def analyze_route_coverage(self, records) -> dict:
        """route-level + mechanism-level coverage（知道哪个机制没覆盖）。

        Phase 1.8：coverage 的唯一证据来源是 route_mechanism_edges（RouteMechanismEvidenceEdge）。
        covered = 存在 supporting edge（CoverageMatcher.edge_supports_gap 返回 DIRECT/INHERITED）。
        旧记录（extractor_version < 2.0，无 edges）不贡献 coverage —— 宁缺毋滥，等重抽。

        route 归并使用本地 CoverageMatcher（有方向 is_a + aliases），
        mechanism 用 MECHANISM_CANONICAL 概念层级 + 别名兜底——与 global rematch
        / diagnostic 共用同一套语义，保证 matrix 是唯一 authoritative closure truth。

        Returns:
            {"route_coverage": {core_route: 论文数},
             "mechanism_coverage": {core_route: {mechanism: {covered, evidence, confidence,
                                                            relation, paper_id, inferred_candidates}}},
             "missing_mechanisms": {core_route: [未覆盖 mechanism]}}
        """
        matcher = CoverageMatcher()

        # 1. paper → canonical routes（metadata 用途，检索/分类仍用 strategy_routes）
        paper_canonicals: list[tuple[object, set[str]]] = []
        for rec in records:
            routes: set[str] = set()
            for phrase in rec.strategy_routes:
                r = assign_route([phrase])
                if r:
                    routes.add(r)
            paper_canonicals.append((rec, routes))

        # 2. 收集全部证据边（跨所有 records；旧记录无 edges → 不贡献）
        all_edges = []
        for rec in records:
            all_edges.extend(rec.route_mechanism_edges)

        # 3. route + mechanism coverage（对 7 个 core route）
        route_coverage = Counter()
        mechanism_coverage: dict[str, dict] = {}
        missing_mechanisms: dict[str, list] = {}
        for core in CORE_ROUTE_MECHANISMS:
            core_papers = [
                (rec, routes) for rec, routes in paper_canonicals
                if routes and route_match_type(routes, core) != "NO_MATCH"
            ]
            route_coverage[core] = len(core_papers)
            mechanism_coverage[core] = {}
            missing_mechanisms[core] = []
            for mech in get_mechanisms(core):
                supporting = []
                inferred = []
                for e in all_edges:
                    st = matcher.edge_supports_gap(e, core, mech)
                    if st == "INFERRED":
                        inferred.append(e)
                    elif st != "NO_MATCH":
                        supporting.append((st, e))
                if supporting:
                    # 优先级：DIRECT_MODEL > DIRECT_HUMAN > INHERITED（与 compute_gap_coverage 同）
                    st, best = "INHERITED", None
                    for pref in ("DIRECT_MODEL", "DIRECT_HUMAN", "INHERITED"):
                        chosen = [x for x in supporting if x[0] == pref]
                        if chosen:
                            st, best = max(chosen, key=lambda x: x[1].confidence)
                            break
                    mechanism_coverage[core][mech] = {
                        "covered": True,
                        "evidence": best.evidence or "",
                        "confidence": best.confidence or 0.0,
                        "relation": st,                 # DIRECT_MODEL / DIRECT_HUMAN / INHERITED
                        "paper_id": best.paper_id,
                        "type": mechanism_type(core, mech),   # MECHANISM / ROUTE_PROPERTY / EFFECT
                        "inferred_candidates": [
                            {"paper_id": e.paper_id, "evidence": (e.evidence or "")[:60],
                             "confidence": e.confidence}
                            for e in inferred
                        ],
                    }
                else:
                    mechanism_coverage[core][mech] = {
                        "covered": False,
                        "evidence": "",
                        "confidence": 0.0,
                        "relation": "NO_MATCH",
                        "paper_id": "",
                        "type": mechanism_type(core, mech),   # MECHANISM / ROUTE_PROPERTY / EFFECT
                        "inferred_candidates": [
                            {"paper_id": e.paper_id, "evidence": (e.evidence or "")[:60],
                             "confidence": e.confidence}
                            for e in inferred
                        ],
                    }
                    missing_mechanisms[core].append(mech)

        return {
            "route_coverage": dict(route_coverage),
            "mechanism_coverage": mechanism_coverage,
            "missing_mechanisms": missing_mechanisms,
        }
