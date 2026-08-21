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


class RouteOntology:
    """route → family 层级分类。"""

    def __init__(self, backend: LLMBackend):
        self.backend = backend

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
