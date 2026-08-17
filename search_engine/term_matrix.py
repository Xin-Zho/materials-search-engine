"""TermMatrixGenerator — 把研究问题拆解成多维度术语矩阵。

这是"覆盖驱动"的第一环：不再让 AI 直接生成一条长查询，
而是先拆解成 8 个维度的术语，再组合成多条互补查询。

使用方式:
    gen = TermMatrixGenerator(backend)
    matrix = await gen.generate("光固化弹性体的低黏度高伸长研究", domain_context=...)
    # matrix.dimensions["material_system"] = ["elastomer", "resin", ...]
"""

import json
import logging
from .models import TermMatrix
from .llm import LLMBackend

logger = logging.getLogger(__name__)

MATRIX_PROMPT = """You are a materials science information retrieval strategist.

Decompose the research question into a TERM MATRIX across 9 dimensions.
Each dimension is one aspect of the question; each contains candidate search terms (English).

## The 9 Dimensions
1. material_system — the class of material (elastomer, hydrogel, resin, polymer network...)
2. composition — chemical constituents (monomer, oligomer, crosslinker, photoinitiator, filler...)
3. strategy_route — a material/chemical/process ROUTE that can independently form a distinct body of literature. These are SOLUTION or DESIGN approaches (e.g. ring-opening polymerization, thiol-ene, addition-fragmentation chain transfer, phase separation, interpenetrating network, filler loading). CRITICAL: do NOT put physical-quantity or kinetic-state terms here (e.g. gel point, vitrification, free volume, chain mobility).
4. physical_mechanism — the underlying physical/chemical mechanism explaining WHY a route works (e.g. gel point, vitrification, free volume, chain mobility, stress relaxation, crosslink density). CRITICAL: do NOT put full technical routes here.
5. process — fabrication methods (DLP, SLA, UV curing, post-curing, reactive diluent...)
6. target_properties — desired properties (stretchability, toughness, low viscosity, high resolution...)
7. application — use cases (soft robot, wearable, actuator, coating, dental...)
8. failure_problem — failure modes or challenges (brittleness, shrinkage, oxygen inhibition, degradation...)
9. metrics — measurable quantities (elongation at break, fracture energy, storage modulus, conversion...)

## Rules
- strategy_route MUST list 20-30 terms (HIGH RECALL, do not pre-filter, allow synonyms and near-duplicates — a later normalization step merges them). Never truncate to "top 8".
- Other dimensions: list 3-8 terms (English, lowercase except proper nouns/acronyms)
- Include synonyms and variants — this is what enables comprehensive search
- Base terms on BOTH the question AND the domain knowledge provided
- If a dimension doesn't apply to this question, return an empty list
- Return ONLY valid JSON object, no markdown, no explanation

## Research Question
{question}

## Domain Knowledge (AUTHORITATIVE)
{domain_context}

## Output Format
{{"material_system": [...], "composition": [...], "strategy_route": [...],
  "physical_mechanism": [...], "process": [...], "target_properties": [...],
  "application": [...], "failure_problem": [...], "metrics": [...]}}"""


ROUTE_NORMALIZE_PROMPT = """Group these strategy routes into semantic FAMILIES.
Routes that are synonyms, near-synonyms, or hyponym/hypernym relations should merge into one family.

## Routes
{routes}

## Rules
- Merge synonyms (e.g. "thiol-ene" and "thiol-ene addition" → one family)
- Merge hyponyms under a hypernym (e.g. "ring-opening polymerization", "cationic polymerization", "silorane" → one family)
- Each family needs a short canonical name and one representative member
- Do NOT merge genuinely different routes (e.g. keep AFCT separate from thiol-ene)
- Output ONLY valid JSON array

## Output Format
[{{"family": "ring-opening/cationic", "representative": "ring-opening polymerization",
   "members": ["ring-opening polymerization", "cationic polymerization", "silorane"]}}, ...]"""


class TermMatrixGenerator:
    """研究问题 → 术语矩阵。"""

    def __init__(self, backend: LLMBackend):
        self.backend = backend

    async def normalize_routes(self, routes: list[str]) -> list[dict]:
        """用 LLM 把 route 聚类成 semantic family（同义/上下位合并）。"""
        if not routes:
            return []
        prompt = ROUTE_NORMALIZE_PROMPT.format(routes="\n".join(f"- {r}" for r in routes))
        response = await self.backend.chat(
            system_prompt="You are a materials science taxonomy expert. Output only valid JSON.",
            user_message=prompt,
            temperature=0,
            max_tokens=2048,
        )
        families = self._parse_families(response)
        logger.info("route 归一化: %d 个 route → %d 个 family", len(routes), len(families))
        return families

    @staticmethod
    def _parse_families(response: str) -> list[dict]:
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
            return [it for it in items if isinstance(it, dict) and it.get("representative")]
        except (json.JSONDecodeError, ValueError):
            return []

    async def generate(
        self,
        research_question: str,
        domain_context: str = "",
    ) -> TermMatrix:
        """生成术语矩阵。"""
        prompt = MATRIX_PROMPT.format(
            question=research_question,
            domain_context=domain_context,
        )

        response = await self.backend.chat(
            system_prompt="You are a literature search strategist. Output only valid JSON.",
            user_message=prompt,
            temperature=0,  # 确定性 backbone（strategy_route 必须稳定）
            max_tokens=2048,
        )

        matrix = self._parse(response)

        # 对 strategy_route 做语义归一化聚类（同义/上下位合并成 family）
        routes = matrix.get("strategy_route")
        if routes:
            families = await self.normalize_routes(routes)
            matrix.route_families = families
        else:
            matrix.route_families = []

        total_terms = sum(len(v) for v in matrix.dimensions.values())
        logger.info("术语矩阵: %d 维度, %d 术语, %d route family",
                     len([v for v in matrix.dimensions.values() if v]),
                     total_terms, len(matrix.route_families))
        return matrix

    @staticmethod
    def _parse(response: str) -> TermMatrix:
        """解析术语矩阵 JSON。"""
        text = response.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:])
            if text.endswith("```"):
                text = text[:-3]

        matrix = TermMatrix()
        try:
            start = text.index("{")
            end = text.rindex("}") + 1
            data = json.loads(text[start:end])
        except (json.JSONDecodeError, ValueError):
            logger.warning("术语矩阵解析失败，返回空矩阵")
            return matrix

        # 只保留标准维度
        for dim in TermMatrix.DIMENSIONS:
            if dim in data and isinstance(data[dim], list):
                matrix.dimensions[dim] = [t.strip() for t in data[dim]
                                           if isinstance(t, str) and t.strip()]

        return matrix
