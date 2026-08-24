"""RouteOntology — 把 route 分类到 family 层级，区分「技术路线」vs「机制/过程」。

解决 coverage 统计的粒度混淆：mechanism/process 类词
（power-law modulus evolution、hot lithography）不应该和
strategy route 类（silorane、thiol-ene、filler）竞争搜索预算。

三层：
  Level 0: raw route（原文表达）
  Level 1: canonical route（标准技术路线）
  Level 2: route family（技术路线族，coverage 统计用）

使用方式:
    onto = RouteOntology(backend)
    classified = await onto.classify(routes)
    # → [{"route": "...", "type": "strategy_family", "family": "ring-opening"}, ...]
"""

import json
import logging
from .llm import LLMBackend

logger = logging.getLogger(__name__)

CLASSIFY_PROMPT = """Classify these research routes into ONE of three types. If strategy route, assign BOTH a family and a higher-level research strategy.

## Types
1. strategy_family — a material/chemical/process ROUTE that can independently form a literature body (a SOLUTION approach). Assign: (a) a short canonical family (2-4 words), (b) a higher-level research strategy.
2. physical_mechanism — an underlying WHY-it-works mechanism (modulus evolution, gel point, free volume, stress relaxation). NOT a searchable route.
3. process_parameter — a fabrication condition (irradiation interval, hot lithography, curing protocol). NOT a material route.

## Research strategy (higher-level grouping, merge related families)
Use one of these strategy labels to group families:
- "network rearrangement" (AFCT, bond exchange, chain transfer, stress relaxation via topology)
- "delayed gelation" (thiol-ene, step-growth, delayed gel point)
- "ring-opening compensation" (silorane, spiro orthocarbonate, expanding monomer, ring-opening)
- "filler reinforcement" (silica, nanoparticle, inorganic filler, hybrid)
- "monomer design" (low-shrinkage monomer, oligomer design, monomer modification)
- "cationic/anionic polymerization" (cationic, anionic, polarity-reversal catalysis)
- "dual/step curing" (dual-curing, gradient, bulk-fill)
- other — pick a concise label if none fit

## Rules
- family: SHORT canonical (e.g. "ring-opening", "thiol-ene", "filler", "AFCT")
- strategy: use the labels above; merge related families under the SAME strategy
- physical_mechanism / process_parameter: family and strategy left empty
- Output ONLY valid JSON array

## Routes
{routes}

## Output Format
[{{"route": "silorane", "type": "strategy_family", "family": "ring-opening", "strategy": "ring-opening compensation"}},
 {{"route": "power-law modulus evolution", "type": "physical_mechanism", "family": "", "strategy": ""}},
 {{"route": "hot lithography", "type": "process_parameter", "family": "", "strategy": ""}}]"""

MECHANISM_PROMPT = """For each research strategy below, list its associated MECHANISMS — the physical/chemical reasons WHY this route reduces polymerization shrinkage or shrinkage stress (3-5 concise mechanisms each).

## Strategies (strategy: canonical route)
{strategies}

## Rules
- mechanisms must be WHY-it-works explanations (e.g. "reversible bond exchange", "volumetric expansion", "reduced polymerizable fraction")
- do NOT list measurement techniques or unrelated mechanisms
- Output ONLY valid JSON array

## Output Format
[{{"strategy": "network rearrangement", "mechanisms": ["reversible bond exchange", "stress relaxation", "delayed gelation"]}}, ...]"""


class RouteOntology:
    """route → family 层级分类。"""

    def __init__(self, backend: LLMBackend):
        self.backend = backend

    async def build_mechanism_ontology(self, ontology: dict[str, dict]) -> dict[str, list[str]]:
        """为每个 strategy 清洗 mechanisms（用 LLM 判断机制归属，避免错乱）。

        Args:
            ontology: build() 的输出 {strategy: {canonical_routes, aliases, ...}}

        Returns:
            {strategy: [清洗后的 mechanisms]}
        """
        if not ontology:
            return {}

        # 构造 strategy → canonical route 列表
        strategies_text = "\n".join(
            f"- {s}: {', '.join(info.get('canonical_routes', []))}"
            for s, info in ontology.items()
        )
        prompt = MECHANISM_PROMPT.format(strategies=strategies_text)

        try:
            response = await self.backend.chat(
                system_prompt="You are a materials science mechanism taxonomist. Output only valid JSON.",
                user_message=prompt,
                temperature=0,
                max_tokens=2000,
            )
        except Exception as e:
            logger.warning("mechanism ontology 生成失败: %s", e)
            return {s: info.get("mechanisms", []) for s, info in ontology.items()}

        items = self._parse_mechanism(response)
        result = {s: info.get("mechanisms", []) for s, info in ontology.items()}  # 兜底用原 mechanisms
        for it in items:
            if it.get("strategy") and it.get("mechanisms"):
                result[it["strategy"]] = it["mechanisms"]
        logger.info("mechanism ontology: %d strategy", len(result))
        return result

    @staticmethod
    def _parse_mechanism(response: str) -> list[dict]:
        import json
        text = response.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:])
            if text.endswith("```"):
                text = text[:-3]
        try:
            start = text.index("[")
            end = text.rindex("]") + 1
            return [it for it in json.loads(text[start:end]) if isinstance(it, dict)]
        except (json.JSONDecodeError, ValueError):
            return []

    async def build_route_graph(self, records) -> dict[str, dict]:
        """建立 route graph（route 节点为中心，不再 strategy → route）。

        输出结构：{canonical_route: {strategies, mechanisms, historical_terms}}
        一个 route 可属于多个 strategy（如 AFCT 属于 network rearrangement 和
        ring-opening compensation），coverage 以 route 为节点统计。
        """
        from collections import defaultdict

        raw_routes: list[str] = []
        for rec in records:
            raw_routes.extend(rec.strategy_routes)
        classified = await self.classify(raw_routes)

        graph: dict[str, dict] = defaultdict(
            lambda: {"strategies": set(), "mechanisms": set(), "historical_terms": set()}
        )

        # raw route → canonical family 映射
        raw_to_family: dict[str, str] = {}
        for c in classified:
            if c["type"] == "strategy_family":
                family = c.get("family") or c["route"]
                raw_to_family[c["route"]] = family
                strategy = c.get("strategy") or family
                graph[family]["strategies"].add(strategy)

        # 聚合 mechanisms + historical_terms（按 family 归属）
        for rec in records:
            rec_families = set()
            for route in rec.strategy_routes:
                family = raw_to_family.get(route)
                if family:
                    rec_families.add(family)
            for family in rec_families:
                for m in rec.physical_mechanisms:
                    graph[family]["mechanisms"].add(m.mechanism or m.cause or m.effect)
                for h in rec.historical_terms:
                    graph[family]["historical_terms"].add(h)
                for h in rec.synonyms:
                    graph[family]["historical_terms"].add(h)

        result = {
            route: {
                "strategies": sorted(v["strategies"]),
                "mechanisms": sorted(v["mechanisms"]),
                "historical_terms": sorted(v["historical_terms"]),
            }
            for route, v in graph.items()
        }
        logger.info("route graph: %d route 节点", len(result))
        return result

    async def build(self, records) -> dict[str, dict]:
        """从 KnowledgeRecord 建立 route ontology。

        输出结构：strategy → canonical_routes / aliases / mechanisms / historical_terms。
        - canonical_routes：标准技术路线名（classify 的 family，归一化后的）
        - aliases：raw 表达的别名（同一路线的不同说法）
        这样 coverage 统计时同一路线不会被当成多个路线。
        """
        from collections import defaultdict

        # 1. 提取所有 raw routes，分类（family = canonical，route = raw）
        raw_routes: list[str] = []
        for rec in records:
            raw_routes.extend(rec.strategy_routes)
        classified = await self.classify(raw_routes)

        # 2. 按 strategy 聚合（canonical_routes + aliases 分开）
        ontology: dict[str, dict] = defaultdict(
            lambda: {"canonical_routes": set(), "aliases": set(),
                     "mechanisms": set(), "historical_terms": set()}
        )
        for c in classified:
            if c["type"] == "strategy_family":
                strategy = c.get("strategy") or c.get("family") or c["route"]
                family = c.get("family") or c["route"]
                ontology[strategy]["canonical_routes"].add(family)
                if c["route"] != family:  # raw 表达作为别名
                    ontology[strategy]["aliases"].add(c["route"])

        # 3. 聚合 mechanism + historical_terms
        for rec in records:
            rec_strategies = set()
            for c in classified:
                if c["type"] == "strategy_family" and c["route"] in rec.strategy_routes:
                    rec_strategies.add(c.get("strategy") or c.get("family") or c["route"])
            for s in rec_strategies:
                for m in rec.physical_mechanisms:
                    ontology[s]["mechanisms"].add(m.mechanism or m.cause or m.effect)
                for h in rec.historical_terms:
                    ontology[s]["historical_terms"].add(h)
                for h in rec.synonyms:
                    ontology[s]["historical_terms"].add(h)

        # 转成可序列化 dict
        result = {
            s: {
                "canonical_routes": sorted(v["canonical_routes"]),
                "aliases": sorted(v["aliases"]),
                "mechanisms": sorted(v["mechanisms"]),
                "historical_terms": sorted(v["historical_terms"]),
            }
            for s, v in ontology.items()
        }
        logger.info("route ontology: %d strategy", len(result))
        return result

    async def classify(self, routes: list[str]) -> list[dict]:
        """把 routes 分类为 strategy_family / physical_mechanism / process_parameter。"""
        if not routes:
            return []

        unique_routes = list(dict.fromkeys(routes))
        prompt = CLASSIFY_PROMPT.format(routes="\n".join(f"- {r}" for r in unique_routes))

        try:
            response = await self.backend.chat(
                system_prompt="You are a materials science route taxonomist. Output only valid JSON.",
                user_message=prompt,
                temperature=0,
                max_tokens=3000,
            )
        except Exception as e:
            logger.warning("route 分类失败: %s", e)
            return [{"route": r, "type": "strategy_family", "family": r} for r in unique_routes]

        classified = self._parse(response, unique_routes)
        n_family = sum(1 for c in classified if c["type"] == "strategy_family")
        logger.info("route 分类: %d route → %d strategy_family",
                     len(unique_routes), n_family)
        return classified

    @staticmethod
    def _parse(response: str, routes: list[str]) -> list[dict]:
        text = response.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:])
            if text.endswith("```"):
                text = text[:-3]
        try:
            start = text.index("[")
            end = text.rindex("]") + 1
            items = json.loads(text[start:end])
        except (json.JSONDecodeError, ValueError):
            items = []

        result = []
        for it in items:
            if isinstance(it, dict) and it.get("route"):
                is_family = it.get("type", "strategy_family") == "strategy_family"
                result.append({
                    "route": it["route"],
                    "type": it.get("type", "strategy_family"),
                    "family": it.get("family", "") if is_family else "",
                    "strategy": it.get("strategy", "") if is_family else "",
                })

        # 兜底：没分类的 route 归为 strategy_family（用自身做 family/strategy）
        classified_routes = {c["route"] for c in result}
        for r in routes:
            if r not in classified_routes:
                result.append({"route": r, "type": "strategy_family", "family": r, "strategy": r})
        return result
