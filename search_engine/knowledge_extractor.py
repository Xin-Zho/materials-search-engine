"""KnowledgeExtractor — 从相关论文结构化提取可继续搜索的知识。

Phase 1 核心：不是摘要，而是提取"后面还能拿去继续搜索的知识"。
尤其 search_hypotheses —— 能发现原查询漏掉的新相关论文的搜索假设。

使用方式:
    extractor = KnowledgeExtractor(backend)
    record = await extractor.extract(paper)
    # record.search_hypotheses = ["filler loading AND polymerization shrinkage", ...]
"""

import json
import logging
from .models import Paper, KnowledgeRecord
from .llm import LLMBackend

logger = logging.getLogger(__name__)

EXTRACT_PROMPT = """You are a materials science knowledge extractor. Read this paper and extract knowledge that can DRIVE FURTHER SEARCH.

## Paper
Title: {title}
Year: {year}
Abstract: {abstract}

## Task
Extract these fields (searchable knowledge, NOT a summary):
1. problem — the problem this paper addresses (one short sentence)
2. strategy_route — technical/chemical/process ROUTES used (list, 1-4 terms)
3. physical_mechanism — underlying physical/chemical mechanisms explaining WHY the route works (list, 1-4 terms)
4. materials — specific materials/chemicals/substances (list, 1-6 terms)
5. concepts — key concepts/terms (list, 1-6 terms)
6. synonyms — synonyms/variants of key terms that would appear in OTHER papers (list, 1-6 terms)
7. search_hypotheses — 2-4 NEW search statements that could find MORE relevant papers the original query missed. Formulate each as a concrete search phrase (e.g. "filler loading AND polymerization shrinkage", "spiro orthocarbonate anti-shrinkage"). These should explore routes/mechanisms/materials NOT obvious from the title alone.

## Rules
- Focus on SEARCHABLE knowledge. The goal is generating NEW queries, not summarizing.
- search_hypotheses must be concrete enough to become Scopus queries.
- Return ONLY valid JSON object, no markdown.

## Output Format
{{"problem": "...", "strategy_route": [...], "physical_mechanism": [...], "materials": [...], "concepts": [...], "synonyms": [...], "search_hypotheses": [...]}}"""


class KnowledgeExtractor:
    """论文 → 可搜索知识。"""

    def __init__(self, backend: LLMBackend, extractor_version: str = "1.0"):
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

        return KnowledgeRecord(
            paper_id=paper.paper_id,
            problem=data.get("problem", ""),
            strategy_route=self._as_list(data.get("strategy_route")),
            physical_mechanism=self._as_list(data.get("physical_mechanism")),
            materials=self._as_list(data.get("materials")),
            concepts=self._as_list(data.get("concepts")),
            synonyms=self._as_list(data.get("synonyms")),
            search_hypotheses=self._as_list(data.get("search_hypotheses")),
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
