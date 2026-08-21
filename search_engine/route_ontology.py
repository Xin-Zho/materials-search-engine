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

CLASSIFY_PROMPT = """Classify these research routes into ONE of three types, and if it's a strategy route, assign a family.

## Types
1. strategy_family — a material/chemical/process ROUTE that can independently form a literature body (a SOLUTION approach, e.g. ring-opening polymerization, thiol-ene, filler loading, silorane). Assign a short family name (2-4 words, canonical).
2. physical_mechanism — an underlying WHY-it-works mechanism (e.g. modulus evolution, gel point, free volume, stress relaxation). NOT a searchable route on its own.
3. process_parameter — a fabrication/processing condition (e.g. irradiation interval, hot lithography, curing protocol, light intensity). NOT a material route.

## Rules
- family name must be SHORT CANONICAL (e.g. "ring-opening", "thiol-ene", "filler", "AFCT"), not descriptive phrases
- Merge synonyms under same family
- physical_mechanism and process_parameter do NOT need a family (leave empty)
- Output ONLY valid JSON array

## Routes
{routes}

## Output Format
[{{"route": "silorane", "type": "strategy_family", "family": "ring-opening"}},
 {{"route": "power-law modulus evolution", "type": "physical_mechanism", "family": ""}},
 {{"route": "hot lithography", "type": "process_parameter", "family": ""}}]"""


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
                result.append({
                    "route": it["route"],
                    "type": it.get("type", "strategy_family"),
                    "family": it.get("family", "") if it.get("type") == "strategy_family" else "",
                })

        # 兜底：没分类的 route 归为 strategy_family（用自身做 family）
        classified_routes = {c["route"] for c in result}
        for r in routes:
            if r not in classified_routes:
                result.append({"route": r, "type": "strategy_family", "family": r})
        return result
