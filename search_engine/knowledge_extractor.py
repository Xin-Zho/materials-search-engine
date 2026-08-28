"""KnowledgeExtractor — 从相关论文提取可驱动进一步搜索的知识。

Phase 1 核心：不是摘要，而是"知识学习器"。
Paper Evidence → Generalized Hypothesis → Search Query。

关键区分：
- strategy_routes（技术路线）vs characterization_methods（表征方法）
- physical_mechanisms（cause→mechanism→effect 因果三元组）
- historical_terms（旧称/别名，可驱动历史文献检索）
- search_hypotheses（泛化假设 + 理由 + 具体 query）
"""

import json
import logging
from .models import Paper, KnowledgeRecord, Mechanism, SearchHypothesis
from .llm import LLMBackend

logger = logging.getLogger(__name__)

EXTRACT_PROMPT = """You are a materials science knowledge extractor. Read this paper and extract knowledge that can DRIVE FURTHER SEARCH (not a summary).

## Paper
Title: {title}
Year: {year}
Abstract: {abstract}

## Extract these fields

1. problem — the problem this paper addresses (one short sentence)

2. strategy_routes — the material/chemical/process SOLUTION ROUTES used (list). These are design approaches, e.g. "ring-opening polymerization", "thiol-ene", "filler loading". Do NOT include measurement/characterization techniques.
   CRITICAL — route must be the POLYMERIZATION STRATEGY, not a specific chemical entity. Do NOT use monomer names, catalyst names, specific functional groups, or additive names as a route (e.g. "vinyl sulfonate ester", "benzylidene acetal", "HABI", "spiro-orthoester" are IMPLEMENTATIONS, not routes). Map them UPWARD to the strategy they implement: "vinyl sulfonate ester" → "AFCT", "benzylidene acetal" → "ring-opening", "increasing molecular weight" → "monomer-design". If a paper uses a specific implementation, still report the strategy as the route.

3. route_mechanism_edges — THE PRIMARY OUTPUT (fill this FIRST). For EACH mechanism in this paper, identify the route that CAUSES it. Each edge is {{"route": "...", "mechanism": "...", "evidence": "...", "confidence": 0.9, "relation_type": "direct"}}.
   - "route" MUST be the POLYMERIZATION STRATEGY (e.g. "AFCT", "ring-opening", "thiol-ene", "filler", "monomer-design", "dual-curing") — NOT a specific monomer/catalyst/functional-group/additive name (e.g. use "AFCT", NOT "vinyl sulfonate ester"; use "ring-opening", NOT "benzylidene acetal"). If the paper implements a strategy via a specific chemistry, report the STRATEGY as route; the implementation detail belongs in evidence.
   - "mechanism" is the mechanism's EFFECT-LEVEL name that a coverage checklist would use, e.g. "stress relaxation", "delayed gelation", "reduced shrinkage", "volumetric expansion" — NOT the intermediate step alone. "chain transfer" is a MEANS; "stress relaxation" is the mechanism it achieves.
   - "evidence" is a quote/paraphrase from the abstract supporting route → mechanism (the causal chain belongs here, e.g. "AFCT introduces chain transfer reactions that relax stress").
   - "relation_type" is "direct" if the paper explicitly states the route-mechanism relation, "inferred" if you infer it from a mechanism chain the paper does not state directly.
   - If the paper does NOT establish which route causes a mechanism, output it as an UNBOUND edge: {{"route": null, "mechanism": "...", "evidence": "...", "confidence": 0.5, "relation_type": "inferred"}}.
   - EVERY mechanism in physical_mechanisms MUST appear here (as an edge or unbound). Output AT LEAST as many edges as mechanisms — prefer an explicit edge whenever the abstract supports the causal relation.

4. physical_mechanisms — causal explanation of WHY a route works, as objects with cause/mechanism/effect PLUS canonical/evidence/confidence. e.g. {{"cause": "ring-opening polymerization", "mechanism": "volumetric expansion during bond formation", "effect": "offsets polymerization shrinkage", "canonical": "volumetric expansion", "evidence": "quote/paraphrase from abstract supporting this mechanism", "confidence": 0.9}}. canonical is the SHORT standard mechanism name; evidence is WHY you attribute this mechanism to the paper; confidence is 0-1. Do NOT put mere result variables as mechanisms. DECOMPOSE: if one statement contains multiple distinct mechanisms, split it into separate objects — one object = ONE mechanism + its own evidence. e.g. "chain transfer reactions delay gelation and relax stress" → [{{cause: "AFCT", mechanism: "chain transfer", effect: "delayed gelation", ...}}, {{cause: "AFCT", mechanism: "chain transfer", effect: "stress relaxation", ...}}]. This decomposition is critical for coverage tracking (each checklist mechanism must be independently matchable). EVERY mechanism listed here MUST also appear in route_mechanism_edges (edge or unbound).

5. characterization_methods — measurement/characterization techniques (list), e.g. "near-infrared spectroscopy", "dynamic mechanical analysis", "tensometry". Keep these SEPARATE from strategy_routes.

6. concepts — key concepts/terms (list)

7. synonyms — strict synonyms/variants of the SAME term (list)

8. broader_terms — hypernyms / more general concepts (list)

9. historical_terms — OLDER NAMES or ALTERNATIVE NOMENCLATURE for the SAME concept/substance (list). These must be historical synonyms of a term already in routes/materials/concepts — e.g. "spiroorthoester" vs "spiro-orthoester", or an old IUPAC name vs current name. NOT related-but-different concepts. These help retrieve foundational/early literature.

10. search_hypotheses — 2-3 GENERALIZED search directions that go BEYOND the specific chemistry in this paper, as {{hypothesis, rationale, support_type, evidence, queries}} objects. The hypothesis should generalize the paper's finding to a broader class (e.g. "cyclic ring-opening monomers may reduce shrinkage beyond spiro-orthoesters"). support_type is one of: direct_experiment / mechanism_inference / literature_suggestion / speculative. evidence is the specific finding/statement in THIS paper that grounds the hypothesis (quote or paraphrase from the abstract). queries are concrete Scopus search phrases.

11. materials — specific materials/chemicals/substances (list)

## Rules for route_mechanism_edges (CRITICAL)
- For each mechanism, identify the route that CAUSES it. A mechanism without a clear route-mechanism causal relation must NOT create an edge — output it as unbound (route: null).
- Attach a mechanism ONLY to the route that actually causes it. Do NOT attach a mechanism to every route mentioned in the paper.
- "mechanism" uses the effect-level mechanism name (what the route achieves), e.g. "stress relaxation", "delayed gelation". Intermediate steps (e.g. "chain transfer") belong in evidence, not as the mechanism.
- EVERY physical_mechanism must be represented in route_mechanism_edges (edge or unbound). Prefer explicit edges whenever the abstract supports the causal relation — a slightly lower confidence edge is more valuable than an unbound mechanism.
- The routes in edges should be a subset of strategy_routes (or their natural variants).
- One mechanism may appear in multiple edges ONLY if the evidence genuinely links it to multiple routes.

## Rules
- The goal is generating NEW queries that find papers the original query missed, NOT rearranging this paper's own keywords.
- Return ONLY valid JSON object, no markdown.

## Output Format
{{"problem": "...", "strategy_routes": [...], "materials": [...],
  "route_mechanism_edges": [{{"route": "...", "mechanism": "...", "evidence": "...", "confidence": 0.9, "relation_type": "direct"}}],
  "physical_mechanisms": [{{"cause": "...", "mechanism": "...", "effect": "..."}}],
  "characterization_methods": [...], "concepts": [...], "synonyms": [...],
  "broader_terms": [...], "historical_terms": [...],
  "search_hypotheses": [{{"hypothesis": "...", "rationale": "...", "queries": [...]}}]}}"""


class KnowledgeExtractor:
    """论文 → 可搜索知识。"""

    def __init__(self, backend: LLMBackend, extractor_version: str = "1.1"):
        self.backend = backend
        self.extractor_version = extractor_version

    async def extract(self, paper: Paper) -> KnowledgeRecord | None:
        """从一篇论文提取知识。"""
        prompt = EXTRACT_PROMPT.format(
            title=paper.title or "",
            year=paper.year or "?",
            abstract=(paper.abstract or "(no abstract)")[:1500],
        )

        try:
            response = await self.backend.chat(
                system_prompt="You are a materials science knowledge extractor. Output only valid JSON.",
                user_message=prompt,
                temperature=0.1,
                max_tokens=2048,
            )
        except Exception as e:
            logger.warning("知识提取失败 %s: %s", paper.paper_id, e)
            return None

        data = self._parse(response)
        if not data:
            return None

        mechanisms = [
            Mechanism(
                cause=m.get("cause", ""),
                mechanism=m.get("mechanism", ""),
                effect=m.get("effect", ""),
                canonical=m.get("canonical", ""),
                evidence=m.get("evidence", ""),
                confidence=float(m.get("confidence", 0.0) or 0.0),
            )
            for m in data.get("physical_mechanisms", [])
            if isinstance(m, dict)
        ]
        hypotheses = [
            SearchHypothesis(
                hypothesis=h.get("hypothesis", ""),
                rationale=h.get("rationale", ""),
                support_type=h.get("support_type", "") or "unspecified",
                evidence=h.get("evidence", ""),
                queries=self._as_list(h.get("queries")),
            )
            for h in data.get("search_hypotheses", [])
            if isinstance(h, dict)
        ]

        # Phase 1.8: route—mechanism 证据边（raw → canonical 本地归并）
        from .route_mechanism_ontology import build_edge
        edges = [
            build_edge(
                paper_id=paper.paper_id,
                raw_route=e.get("route") or "",
                raw_mechanism=e.get("mechanism") or "",
                evidence=e.get("evidence", ""),
                confidence=float(e.get("confidence", 0.0) or 0.0),
                relation_type=e.get("relation_type", "direct"),
            )
            for e in data.get("route_mechanism_edges", [])
            if isinstance(e, dict) and (e.get("route") or e.get("mechanism"))
        ]

        # Phase 1.8: identity（canonical_paper_id/doi/openalex_id 分离，不随删行丢失）
        doi = (paper.doi or "").strip()
        openalex_id = ""
        if (paper.paper_id or "").startswith("openalex:"):
            oid = paper.paper_id[len("openalex:"):]
            if oid.startswith("https://openalex.org/"):
                oid = oid[len("https://openalex.org/"):]
            openalex_id = oid

        return KnowledgeRecord(
            paper_id=paper.paper_id,
            canonical_paper_id=f"doi:{doi}" if doi else paper.paper_id,
            doi=doi,
            openalex_id=openalex_id,
            problem=data.get("problem", ""),
            strategy_routes=self._as_list(data.get("strategy_routes")),
            materials=self._as_list(data.get("materials")),
            physical_mechanisms=mechanisms,
            characterization_methods=self._as_list(data.get("characterization_methods")),
            concepts=self._as_list(data.get("concepts")),
            synonyms=self._as_list(data.get("synonyms")),
            broader_terms=self._as_list(data.get("broader_terms")),
            historical_terms=self._as_list(data.get("historical_terms")),
            search_hypotheses=hypotheses,
            route_mechanism_edges=edges,
            source_text=(paper.abstract or paper.title or "")[:500],
            extractor_version=self.extractor_version,
            confidence=1.0,
        )

    async def extract_many(self, papers: list[Paper]) -> list[KnowledgeRecord]:
        """批量提取（顺序）。"""
        records = []
        for p in papers:
            rec = await self.extract(p)
            if rec:
                records.append(rec)
        logger.info("知识提取: %d 篇 → %d 条记录", len(papers), len(records))
        return records

    @staticmethod
    def _as_list(v) -> list[str]:
        if isinstance(v, list):
            return [t.strip() for t in v if isinstance(t, str) and t.strip()]
        if isinstance(v, str) and v.strip():
            return [v.strip()]
        return []

    @staticmethod
    def _parse(response: str) -> dict | None:
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
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, ValueError):
            return None
