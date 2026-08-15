"""FoundationalRecovery — 根基文献溯源，找奠基/早期/综述论文。

主搜索擅长"发现现在有什么"，但不擅长"追溯这些从哪来"。
根基论文搜索本质是图遍历（引用链回溯），不是文本相似度检索。

流程：
    代表论文（种子）
      → Backward Citation（引用链回溯）
      → 早期 + 高被引筛选
      → LLM 历史角色分类（奠基/早期机制/综述）

使用方式:
    fr = FoundationalRecovery(citation_tracker, backend)
    foundational = await fr.recover(seed_papers, early_year=2015)
"""

import logging
from .models import Paper
from .llm import LLMBackend
from .evaluator import normalize_doi

logger = logging.getLogger(__name__)

ROLE_PROMPT = """You are a materials science historian. Classify the historical role of each paper in its research route.

For each paper, given its title, year, and citation count, classify as:
- FOUNDATIONAL: the original/landmark work that established this mechanism or route
- EARLY_MECHANISM: early work on the mechanism (before it matured)
- REVIEW: a review or survey paper
- OTHER: not clearly a foundational or early paper

Research question context: {question}

Papers:
{papers_text}

Output JSON array:
[{{"index": 0, "role": "FOUNDATIONAL", "why": "one-line justification"}}, ...]"""


class FoundationalRecovery:
    """根基文献溯源器。"""

    def __init__(self, citation_tracker, backend: LLMBackend):
        self.citation_tracker = citation_tracker
        self.backend = backend

    async def recover(
        self,
        seed_papers: list[Paper],
        research_question: str = "",
        early_year: int = 2015,
        top_n: int = 30,
    ) -> list[dict]:
        """从种子论文回溯引用链，找到奠基/早期/综述论文。

        Args:
            seed_papers: 主搜索找到的代表论文（需有 DOI）
            early_year: 只保留该年份及之前的论文（根基论文通常较早）
            top_n: LLM 分类前最多保留的候选数（按被引排序）

        Returns:
            [{paper, role, why}, ...] 按被引降序
        """
        # 1. Backward citation：收集所有种子的参考文献
        candidates: dict[str, Paper] = {}
        for seed in seed_papers:
            if not seed.doi:
                continue
            try:
                backward = await self.citation_tracker.backward(seed.doi, limit=50)
                for p in backward:
                    key = normalize_doi(p.doi) or p.paper_id
                    candidates[key] = p
            except Exception as e:
                logger.debug("backward 失败 %s: %s", seed.doi, e)

        logger.info("Foundational Recovery: %d 篇种子 → %d 篇参考文献",
                     len(seed_papers), len(candidates))

        # 2. 筛选早期 + 高被引
        early = [p for p in candidates.values() if p.year and p.year <= early_year]
        early.sort(key=lambda p: p.citation_count or 0, reverse=True)
        early = early[:top_n]

        if not early:
            logger.info("无早期论文（year <= %d）", early_year)
            return []

        # 3. LLM 判断历史角色
        return await self._classify_roles(early, research_question)

    async def _classify_roles(self, papers: list[Paper], question: str) -> list[dict]:
        """LLM 判断每篇论文的历史角色。"""
        papers_text = "\n".join(
            f"[{i}] ({p.year}, cited {p.citation_count or 0}) {p.title[:120]}"
            for i, p in enumerate(papers)
        )

        prompt = ROLE_PROMPT.format(question=question, papers_text=papers_text)

        response = await self.backend.chat(
            system_prompt="You are a materials science historian. Output only valid JSON.",
            user_message=prompt,
            temperature=0.1,
            max_tokens=2048,
        )

        roles = self._parse_roles(response)

        result = []
        for item in roles:
            idx = item.get("index", -1)
            if 0 <= idx < len(papers):
                result.append({
                    "paper": papers[idx],
                    "role": item.get("role", "OTHER"),
                    "why": item.get("why", ""),
                })

        logger.info("历史角色分类: %d 篇", len(result))
        return result

    @staticmethod
    def _parse_roles(response: str) -> list[dict]:
        """解析历史角色分类响应。"""
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
            items = json.loads(text[start:end])
            return [it for it in items if isinstance(it, dict)]
        except (json.JSONDecodeError, ValueError):
            return []
