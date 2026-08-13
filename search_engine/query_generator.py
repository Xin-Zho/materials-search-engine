"""QueryGenerator — 自然语言 → 多条 Scopus 高级搜索查询。

核心思路：LLM 不靠自己记忆领域术语，而是由知识库提供术语、同义词、
核心物质等上下文。LLM 的职责是理解和组装。

使用方式:
    generator = QueryGenerator(backend)
    queries = await generator.generate(
        "光固化复合材料的聚合收缩研究",
        domain_context=kb.get_context("photocuring"),
        n_queries=4,
    )
    # → ["TITLE-ABS-KEY((photocuring OR ...) AND ...)", ...]
"""

import json
import logging
from .llm import LLMBackend

logger = logging.getLogger(__name__)

# ── System Prompt ──────────────────────────────────────

SYSTEM_PROMPT = """You are a materials science literature search expert.
Your job is to translate a researcher's question into multiple complementary
Scopus advanced search queries that maximize discovery coverage.

## Scopus Syntax Reference

Field codes:
- TITLE-ABS-KEY(...) — search in title, abstract, keywords (most common)
- TITLE(...) — title only
- AUTH(...) — author surname
- DOCTYPE(ar) — articles only, DOCTYPE(re) — reviews only
- PUBYEAR > 2019 — year filter
- LANGUAGE(english)

Operators:
- AND / OR / AND NOT
- W/n — words within n words, any order (e.g., "degree W/2 conversion")
- PRE/n — word A precedes word B within n words (e.g., "polymerization PRE/3 shrinkage")
- "phrase" — loose phrase; {exact phrase} — exact match
- * — wildcard (e.g., polymer* matches polymer, polymers, polymeric)

## Your Task

Given a research question AND domain knowledge context (synonyms, key substances,
key metrics, opposing concepts), generate {n_queries} Scopus queries that together
achieve comprehensive coverage:

1. **Main query**: core keywords + all synonyms
2. **Specific query**: key substances/materials + core metrics
3. **Review query**: same topic, DOCTYPE(re), recent years
4. **Opposing view query**: counter-examples, limitations, alternative approaches

## Rules
- Every term MUST be wrapped in TITLE-ABS-KEY() unless it's an author or filter
- Use OR to connect synonyms, AND to connect different concepts
- Use PRE/n or W/n for multi-word phrases (never bare phrases)
- Separate concepts with AND, synonyms with OR
- Output ONLY valid JSON array of strings, no markdown, no explanation
- Each query should be a complete, executable Scopus advanced search string
- Use the domain context provided — prioritize KB terms over your own knowledge

## Output Format
```json
["query1", "query2", "query3", "query4"]
```"""


class QueryGenerator:
    """自然语言 → Scopus 查询生成器。"""

    def __init__(self, backend: LLMBackend):
        self.backend = backend

    async def generate(
        self,
        research_question: str,
        domain_context: str = "",
        n_queries: int = 4,
        language: str = "english",
    ) -> list[str]:
        """根据研究问题生成多条 Scopus 查询。

        Args:
            research_question: 自然语言研究问题
            domain_context: 知识库提供的领域术语、同义词、核心物质等
            n_queries: 生成查询数量
            language: 输出查询的语言限定

        Returns:
            Scopus 高级搜索查询字符串列表
        """
        user_message = self._build_user_message(
            research_question, domain_context, n_queries, language
        )

        logger.info("生成 %d 条查询 (domain_context: %d chars)",
                     n_queries, len(domain_context))

        response = await self.backend.chat(
            system_prompt=SYSTEM_PROMPT.replace("{n_queries}", str(n_queries)),
            user_message=user_message,
            temperature=0.3,
            max_tokens=2048,
        )

        queries = self._parse_response(response)
        logger.info("生成 %d 条查询", len(queries))
        return queries

    def _build_user_message(
        self,
        question: str,
        domain_context: str,
        n_queries: int,
        language: str,
    ) -> str:
        parts = [f"## Research Question\n{question}"]

        if domain_context:
            parts.append(
                f"## Domain Knowledge (AUTHORITATIVE — use these terms)\n{domain_context}"
            )

        parts.append(
            f"## Requirements\n"
            f"- Generate exactly {n_queries} queries\n"
            f"- Language filter: LANGUAGE({language})\n"
            f"- Output as JSON string array"
        )

        return "\n\n".join(parts)

    @staticmethod
    def _parse_response(response: str) -> list[str]:
        """从 LLM 响应中提取查询列表。"""
        # 尝试直接解析 JSON
        text = response.strip()

        # 移除 markdown 代码块
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:])
            if text.endswith("```"):
                text = text[:-3]

        # 尝试提取 JSON 数组
        try:
            # 找到第一个 [ 和最后一个 ]
            start = text.index("[")
            end = text.rindex("]") + 1
            queries = json.loads(text[start:end])
            if isinstance(queries, list):
                return [q.strip() for q in queries if isinstance(q, str) and q.strip()]
        except (json.JSONDecodeError, ValueError):
            pass

        # 备选：按行解析，去掉编号
        lines = text.strip().split("\n")
        queries = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # 去掉编号前缀 "1.", "1)", "- " 等
            if line[0].isdigit() and (line[1:].startswith(".") or line[1:].startswith(")")):
                line = line[2:].strip()
            elif line.startswith("- "):
                line = line[2:].strip()
            # 去掉引号包裹
            if line.startswith('"') and line.endswith('"'):
                line = line[1:-1]
            if line and "TITLE-ABS-KEY" in line:
                queries.append(line)

        return queries[:n_queries] if queries else []
