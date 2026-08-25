"""MechanismCoverageAnalyzer — 知识库的 route × mechanism 覆盖矩阵。

只负责"算 coverage"这一件事：把 KnowledgeRecord 流归一化成 canonical / core
route，统计策略级覆盖和机制级覆盖。缺口识别交给 gap_detector，query 生成交给
gap_query_generator——本类不碰这俩。

与 coverage.route_coverage.CoverageMap 区分：
- CoverageMap 看的是"搜索结果按路线聚类，哪条路线论文少"（搜索侧）。
- 本类看的是"知识库里哪个 route 的哪个机制缺证据"（知识侧）。
"""

import logging
from collections import Counter

from ..llm import LLMBackend
from ..route_normalizer import RouteNormalizer
from ..route_ontology import RouteOntology
from ..route_mechanism_ontology import match_route, get_mechanisms
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

        Returns:
            {"route_coverage": {core_route: 论文数},
             "mechanism_coverage": {core_route: {mechanism: {covered, evidence, confidence}}},
             "missing_mechanisms": {core_route: [未覆盖 mechanism]}}
        """
        mech_normalizer = MechanismNormalizer()

        # 1. raw → canonical route 映射（classify 一次）
        raw_routes = [r for rec in records for r in rec.strategy_routes]
        classified = await self.ontology.classify(raw_routes)
        raw_to_canonical: dict[str, str] = {}
        for c in classified:
            if c["type"] == "strategy_family":
                raw_to_canonical[c["route"]] = c.get("family") or c["route"]

        # 2. canonical route → core route（7 个核心 route 之一）
        canonical_to_core: dict[str, str] = {}
        for canonical_route in set(raw_to_canonical.values()):
            core = match_route(canonical_route)
            if core:
                canonical_to_core[canonical_route] = core

        # 3. route-level coverage（每篇论文的每个 core route 计一次）
        route_coverage = Counter()
        for rec in records:
            rec_cores = self._paper_cores(rec, raw_to_canonical, canonical_to_core)
            for core in rec_cores:
                route_coverage[core] += 1

        # 4. mechanism-level coverage：per-route + 全文弱别名匹配。
        #    每个 checklist mechanism 独立用自己的别名对论文机制的全部文本字段
        #    （canonical/cause/mechanism/effect/evidence）做子串匹配，
        #    这样每个 ✓ 都来自各自的证据，而非一个关键词触发多个标签。
        core_mech_texts: dict[str, list[tuple[str, Mechanism]]] = {}
        for rec in records:
            rec_cores = self._paper_cores(rec, raw_to_canonical, canonical_to_core)
            for core in rec_cores:
                for m in rec.physical_mechanisms:
                    text = " ".join(t for t in (m.canonical, m.cause, m.mechanism, m.effect, m.evidence) if t).lower()
                    core_mech_texts.setdefault(core, []).append((text, m))

        mechanism_coverage: dict[str, dict] = {}
        missing_mechanisms: dict[str, list] = {}
        for core in route_coverage:
            standard_mechs = get_mechanisms(core)
            texts = core_mech_texts.get(core, [])
            mechanism_coverage[core] = {}
            missing_mechanisms[core] = []
            for mech in standard_mechs:
                aliases = mech_normalizer.aliases_for(mech)
                best: Mechanism | None = None
                for text, m in texts:
                    if any(alias in text for alias in aliases):
                        if best is None or m.confidence > best.confidence:
                            best = m
                covered = best is not None
                mechanism_coverage[core][mech] = {
                    "covered": covered,
                    "evidence": best.evidence if best else "",
                    "confidence": best.confidence if best else 0.0,
                }
                if not covered:
                    missing_mechanisms[core].append(mech)

        return {
            "route_coverage": dict(route_coverage),
            "mechanism_coverage": mechanism_coverage,
            "missing_mechanisms": missing_mechanisms,
        }

    @staticmethod
    def _paper_cores(rec, raw_to_canonical: dict, canonical_to_core: dict) -> set[str]:
        """一篇论文归属的 core routes（按 strategy_routes 归一化）。"""
        cores: set[str] = set()
        for route in rec.strategy_routes:
            canonical = raw_to_canonical.get(route, route)
            core = canonical_to_core.get(canonical)
            if core:
                cores.add(core)
        return cores
