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
from .llm import LLMBackend, TruncatedResponse

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
- Other dimensions: list 3-8 terms MAXIMUM (English, lowercase except proper nouns/acronyms). NEVER exceed 8 terms per non-strategy_route dimension.
- Do NOT enumerate exhaustive lists of test methods, measurement techniques, or synonym compendia — list only the most distinctive, discriminating terms.
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
- family name MUST be a SHORT CANONICAL term (2-4 words max), e.g. "filler", "ring-opening", "thiol-ene", "network architecture", "monomer design", "chain-transfer stress-relaxation". Do NOT use long descriptive phrases — use the stable core concept.
- Do NOT merge genuinely different routes. IMPORTANT guard: "RAFT / reversible addition-fragmentation chain-transfer polymerization" (controlled radical polymerization for molar-mass control) is DIFFERENT from "AFCT / addition-fragmentation chain transfer for stress relaxation" (network rearrangement) — keep them as separate families.
- Output ONLY valid JSON array

## Output Format
[{{"family": "ring-opening", "representative": "ring-opening polymerization",
   "members": ["ring-opening polymerization", "cationic polymerization", "silorane"]}}, ...]"""


class TermMatrixGenerator:
    """研究问题 → 术语矩阵。"""

    def __init__(self, backend: LLMBackend):
        self.backend = backend

    # 每个维度：说明 + 建议 term 数 + max_tokens（输出上限与信息量绑定）
    DIM_SPEC = {
        "material_system": ("the class of material (elastomer, hydrogel, resin, polymer network...)", 6, 500),
        "composition": ("chemical constituents (monomer, oligomer, crosslinker, photoinitiator, filler...)", 6, 500),
        "strategy_route": ("material/chemical/process ROUTE that independently forms a distinct literature body — SOLUTION/DESIGN approaches (e.g. ring-opening polymerization, thiol-ene, phase separation). NOT physical-quantity terms (gel point, vitrification, free volume)", 20, 1000),
        "physical_mechanism": ("underlying physical/chemical mechanism explaining WHY a route works (gel point, vitrification, free volume, stress relaxation). NOT full technical routes", 6, 500),
        "process": ("fabrication methods (DLP, SLA, UV curing, post-curing...)", 6, 500),
        "target_properties": ("desired properties (stretchability, toughness, low viscosity, high resolution...)", 6, 500),
        "application": ("use cases (soft robot, wearable, coating, dental...)", 5, 400),
        "failure_problem": ("failure modes or challenges (brittleness, shrinkage, oxygen inhibition...)", 5, 400),
        "metrics": ("measurable quantities (elongation at break, fracture energy, storage modulus...)", 5, 400),
    }

    async def _generate_strategy_routes(self, question: str, domain_context: str) -> list[str]:
        """strategy_route 两阶段生成：主流路线 + 补充路线（避免隐形 Top-K）。

        Pass A: 主流、直接解决路线（10 条）
        Pass B: 与 A 机制不同的补充方案（10 条，避免重复同类 polymerization chemistry）
        合并 + canonicalize + dedup，保留约 20 条。
        """
        desc = self.DIM_SPEC["strategy_route"][0]

        # Pass A：主流直接路线
        prompt_a = (
            f"Extract the MAINSTREAM strategy routes for this materials science research question.\n\n"
            f"Dimension meaning: {desc}\n\n"
            f"Research question: {question}\n\n"
            f"Domain knowledge (AUTHORITATIVE): {domain_context}\n\n"
            f"List 10 mainstream, direct-solution routes (English, lowercase). "
            f"These are the most common/established approaches.\n\n"
            f'Return ONLY JSON: {{"strategy_route": ["route1", "route2", ...]}}'
        )
        routes_a = await self._generate_route_pass(prompt_a, "strategy_route")

        # Pass B：补充方案（机制不同于 A）
        avoid_text = ", ".join(routes_a) if routes_a else "(none)"
        prompt_b = (
            f"Extract COMPLEMENTARY strategy routes for this materials science research question.\n\n"
            f"Dimension meaning: {desc}\n\n"
            f"Research question: {question}\n\n"
            f"Domain knowledge (AUTHORITATIVE): {domain_context}\n\n"
            f"Already-covered mainstream routes (AVOID these and avoid same-category polymerization chemistry): {avoid_text}\n\n"
            f"List 10 complementary routes with DIFFERENT mechanisms/approaches than the above. "
            f"Include less-common, emerging, or alternative routes.\n\n"
            f'Return ONLY JSON: {{"strategy_route": ["route1", "route2", ...]}}'
        )
        routes_b = await self._generate_route_pass(prompt_b, "strategy_route")

        # 合并 + canonicalize + dedup
        seen: set[str] = set()
        result = []
        for r in routes_a + routes_b:
            key = r.lower().strip().replace("-", "").replace(" ", "")
            if key and key not in seen:
                seen.add(key)
                result.append(r)

        logger.info("strategy_route 两阶段: %d (主流) + %d (补充) → %d 去重",
                     len(routes_a), len(routes_b), len(result))
        return result

    async def _generate_route_pass(self, prompt: str, dim: str) -> list[str]:
        """生成一阶段 route（带 retry）。"""
        for attempt in range(3):
            try:
                response = await self.backend.chat(
                    system_prompt="You are a materials science term extractor. Output only valid JSON.",
                    user_message=prompt,
                    temperature=0.1,
                    max_tokens=700,
                    raise_on_truncation=True,
                )
            except TruncatedResponse:
                continue
            data = self._parse_single_dim(response, dim)
            if data:
                return data
        logger.warning("route pass 生成失败（3 次重试）")
        return []

    async def _generate_dimension(self, question: str, domain_context: str, dim: str) -> list[str]:
        """单独生成一个维度（短 prompt + 维度绑定 max_tokens，防止 degenerate 无限列举）。"""
        desc, n_terms, max_tokens = self.DIM_SPEC[dim]
        prompt = (
            f"Extract terms for the dimension '{dim}' of this materials science research question.\n\n"
            f"Dimension meaning: {desc}\n\n"
            f"Research question: {question}\n\n"
            f"Domain knowledge (AUTHORITATIVE): {domain_context}\n\n"
            f"List EXACTLY {n_terms} terms (English, lowercase). Do NOT exceed {n_terms}. "
            f"Do NOT enumerate exhaustive lists — only the most distinctive terms.\n\n"
            f'Return ONLY JSON: {{"{dim}": ["term1", "term2", ...]}}'
        )

        for attempt in range(3):
            try:
                response = await self.backend.chat(
                    system_prompt="You are a materials science term extractor. Output only valid JSON.",
                    user_message=prompt,
                    temperature=0.1,
                    max_tokens=max_tokens,  # 维度绑定，不给无限列举的空间
                    raise_on_truncation=True,
                )
            except TruncatedResponse:
                continue

            data = self._parse_single_dim(response, dim)
            if data:
                return data

        logger.warning("维度 %s 生成失败（3 次重试）", dim)
        return []

    @staticmethod
    def _parse_single_dim(response: str, dim: str) -> list[str]:
        """解析单维度 JSON。"""
        text = response.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:])
            if text.endswith("```"):
                text = text[:-3]
        try:
            start = text.index("{")
            end = text.rindex("}") + 1
            data = json.loads(text[start:end])
            if isinstance(data, dict) and dim in data and isinstance(data[dim], list):
                return [t.strip() for t in data[dim] if isinstance(t, str) and t.strip()]
        except (json.JSONDecodeError, ValueError):
            pass
        return []

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
        """生成术语矩阵（分维度生成，防止长 JSON 导致 degenerate）。"""
        matrix = TermMatrix()

        # 每个维度单独生成（短 prompt + 低 max_tokens，LLM 无空间无限列举）
        for dim in TermMatrix.DIMENSIONS:
            if dim == "strategy_route":
                # 两阶段生成（主流 + 补充），避免单次 20 条的隐形 Top-K
                terms = await self._generate_strategy_routes(research_question, domain_context)
            else:
                terms = await self._generate_dimension(research_question, domain_context, dim)
            matrix.dimensions[dim] = terms

        if not matrix.get("strategy_route"):
            raise RuntimeError(
                "术语矩阵生成失败：strategy_route 为空。这是 term extraction 的硬失败。"
            )

        # 跨维度 dedup（同义 term 不重复出现在多个维度）
        seen_terms: set[str] = set()
        for dim in TermMatrix.DIMENSIONS:
            deduped = []
            for t in matrix.dimensions[dim]:
                key = t.lower().strip()
                if key not in seen_terms:
                    seen_terms.add(key)
                    deduped.append(t)
            matrix.dimensions[dim] = deduped

        # 对 strategy_route 做语义归一化聚类（同义/上下位合并成 family）
        routes = matrix.get("strategy_route")
        families = await self.normalize_routes(routes)
        matrix.route_families = families

        total_terms = sum(len(v) for v in matrix.dimensions.values())
        logger.info("术语矩阵: %d 维度, %d 术语, %d route family",
                     len([v for v in matrix.dimensions.values() if v]),
                     total_terms, len(matrix.route_families))
        return matrix

    @staticmethod
    def _extract_json_object(text: str) -> dict | None:
        """括号平衡扫描，提取第一个平衡的 JSON 对象（容忍前后杂质）。"""
        depth = 0
        start = -1
        in_string = False
        escape = False
        for i, ch in enumerate(text):
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start >= 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        pass
                    start = -1
        return None

    @staticmethod
    def _parse(response: str) -> TermMatrix:
        """解析术语矩阵 JSON（强容错：严格 parse → bracket balancing）。"""
        text = response.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:])
            if text.endswith("```"):
                text = text[:-3]

        matrix = TermMatrix()
        data: dict | None = None

        # 1. 严格 JSON parse
        try:
            start = text.index("{")
            end = text.rindex("}") + 1
            data = json.loads(text[start:end])
        except (json.JSONDecodeError, ValueError, ValueError):
            # 2. bracket balancing 提取对象
            data = TermMatrixGenerator._extract_json_object(text)

        if not data:
            logger.warning("术语矩阵解析失败（严格 + bracket balancing 都失败）")
            return matrix

        # 只保留标准维度
        for dim in TermMatrix.DIMENSIONS:
            if dim in data and isinstance(data[dim], list):
                matrix.dimensions[dim] = [t.strip() for t in data[dim]
                                           if isinstance(t, str) and t.strip()]

        return matrix
