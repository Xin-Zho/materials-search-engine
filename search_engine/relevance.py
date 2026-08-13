"""RelevanceFilter — LLM 对搜索结果逐篇打分，筛选高相关论文。

使用方式:
    rf = RelevanceFilter(backend)
    scored = await rf.filter(
        papers,
        research_question="光固化复合材料的聚合收缩研究",
        threshold=70,    # 相关性 ≥ 70% 才保留
        top_k=20,        # 最多保留 20 篇
    )
"""

import json
import logging
from .models import Paper, ScoredPaper
from .llm import LLMBackend

logger = logging.getLogger(__name__)

FILTER_PROMPT = """You are a materials science literature reviewer. Your task is to evaluate how relevant each paper is to a specific research question AND classify its research route.

## Scoring Criteria (0-100)

- **90-100**: Directly addresses the research question. Core paper.
- **70-89**: Partially relevant — covers related materials, methods, or concepts.
- **50-69**: Tangentially relevant — same broad field but different focus.
- **30-49**: Same general area but unlikely to provide useful evidence.
- **0-29**: Not relevant at all.

## Rules
- Score based on the title and abstract content against the research question
- A paper doesn't need to match ALL aspects — even matching one key aspect can score high if informative
- Be strict: if the abstract clearly doesn't address the question, give a low score
- Write "reason" and "category" in Chinese.
- "category" is a short Chinese label (2-6 字) describing WHICH ASPECT this paper addresses (e.g. "单体配方", "填料改性", "收缩测量", "光引发剂体系").
- "route" is the TECHNICAL ROUTE this paper belongs to — a slightly broader Chinese label (2-8 字) describing its approach, e.g. "分子量调节", "双网络", "动态共价网络", "相分离增韧", "颗粒增强", "后固化控制", "新型工艺". Papers with the same route should be grouped together; the goal is a set of routes that covers the field.
- "info_gain" (0.0-1.0): how much NEW information this paper adds relative to the already-found routes. 1.0 = a completely new route/angle, 0.0 = fully redundant with existing routes.

## Already Found Routes
{existing_routes}

## Return ONLY a JSON array, no markdown, no explanation

## Research Question
{question}

## Papers to Score
{papers_text}

## Output Format
```json
[{{"index": 0, "score": 85, "reason": "直接研究该复合体系的填料改性", "category": "填料改性", "route": "颗粒增强", "info_gain": 0.8}}]
```"""


PRE_FILTER_PROMPT = """Quick scan — based on titles only, mark clearly irrelevant papers.

Research question: {question}

Papers:
{papers_text}

For each paper, decide: KEEP (likely relevant) or SKIP (clearly irrelevant).
A paper is SKIP if the title clearly indicates a completely different topic/field.
When in doubt, KEEP.

Output JSON array:
[{{"index": 0, "decision": "KEEP"}}, {{"index": 1, "decision": "SKIP"}}, ...]"""


class RelevanceFilter:
    """用 LLM 对论文进行相关性评分和筛选。"""

    MAX_BATCH = 15          # 每批最多发给 LLM 的论文数
    MAX_ABSTRACT_LEN = 500  # 每篇摘要最多保留的字符数

    def __init__(self, backend: LLMBackend):
        self.backend = backend

    async def filter(
        self,
        papers: list[Paper],
        research_question: str,
        threshold: int = 70,
        top_k: int = 20,
        existing_routes: list[str] | None = None,
    ) -> list[ScoredPaper]:
        """筛选论文，输出结构化标签（route + info_gain）。

        Returns:
            list[ScoredPaper] 按分数降序排列
        """
        if not papers:
            return []

        existing_routes = existing_routes or []
        routes_text = ", ".join(existing_routes) if existing_routes else "(none yet — first round)"

        # 分批处理
        all_scored: list[ScoredPaper] = []

        for batch_start in range(0, len(papers), self.MAX_BATCH):
            batch = papers[batch_start:batch_start + self.MAX_BATCH]
            papers_text = self._format_batch(batch, batch_start)

            prompt = FILTER_PROMPT.format(
                question=research_question,
                papers_text=papers_text,
                existing_routes=routes_text,
            )

            response = await self.backend.chat(
                system_prompt="You are an expert academic reviewer. Output only valid JSON.",
                user_message=prompt,
                temperature=0.1,
                max_tokens=2048,
            )

            batch_items = self._parse_scores(response, batch_start, len(batch))
            for item in batch_items:
                idx = item["index"]
                if 0 <= idx < len(batch):
                    all_scored.append(ScoredPaper(
                        paper=batch[idx],
                        score=item["raw_score"],
                        raw_score=item["raw_score"],
                        reason=item["reason"],
                        category=item["category"],
                        route=item["route"],
                        info_gain=item["info_gain"],
                    ))

            logger.debug("批次 %d-%d: %d 篇评分",
                         batch_start, batch_start + len(batch) - 1,
                         len(batch_items))

        # 按分数排序，过滤低于阈值
        all_scored.sort(key=lambda x: x.score, reverse=True)
        result = [sp for sp in all_scored if sp.score >= threshold][:top_k]

        logger.info("相关性筛选: %d → %d 篇 (阈值 ≥%d%%, top %d)",
                     len(papers), len(result), threshold, top_k)
        return result

    async def pre_filter(
        self,
        papers: list[Paper],
        research_question: str,
    ) -> list[Paper]:
        """快筛——只看标题，秒级排除明显无关的论文。

        返回 new_papers 列表（排除 SKIP 后的）。
        """
        if not papers:
            return []

        kept = []
        for batch_start in range(0, len(papers), self.MAX_BATCH):
            batch = papers[batch_start:batch_start + self.MAX_BATCH]
            papers_text = "\n".join(
                f"[{batch_start + i}] {p.title}"
                for i, p in enumerate(batch)
            )

            prompt = PRE_FILTER_PROMPT.format(
                question=research_question,
                papers_text=papers_text,
            )

            response = await self.backend.chat(
                system_prompt="You are a fast paper screener. Output only JSON. When in doubt, KEEP.",
                user_message=prompt,
                temperature=0,
                max_tokens=1024,
            )

            decisions = self._parse_decisions(response, batch_start)
            for idx, decision in decisions:
                if decision == "KEEP" and idx < len(papers):
                    kept.append(papers[idx])

            logger.debug("快筛: %d → %d 篇", len(batch), len(decisions))

        logger.info("快筛: %d → %d 篇", len(papers), len(kept))
        return kept

    @staticmethod
    def _parse_decisions(response: str, offset: int) -> list[tuple[int, str]]:
        """解析快筛响应。"""
        import json as _json
        text = response.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:])
            if text.endswith("```"):
                text = text[:-3]

        try:
            start = text.index("[")
            end = text.rindex("]") + 1
            items = _json.loads(text[start:end])
            return [(it.get("index", -1), it.get("decision", "KEEP")) for it in items]
        except (_json.JSONDecodeError, ValueError):
            return [(i + offset, "KEEP") for i in range(15)]  # fallback: keep all

    def _format_batch(self, papers: list[Paper], offset: int) -> str:
        """将一批论文格式化为文本。"""
        lines = []
        for i, p in enumerate(papers):
            abs_text = p.abstract or "(no abstract)"
            if len(abs_text) > self.MAX_ABSTRACT_LEN:
                abs_text = abs_text[:self.MAX_ABSTRACT_LEN] + "..."

            lines.append(
                f"---\n"
                f"[{offset + i}] Title: {p.title}\n"
                f"    Year: {p.year or '?'}\n"
                f"    Abstract: {abs_text}"
            )
        return "\n".join(lines)

    @staticmethod
    def _parse_scores(response: str, offset: int, count: int) -> list[dict]:
        """从 LLM 响应中解析评分列表，返回 dict 列表（index/score/reason/category/route/info_gain）。"""
        text = response.strip()

        # 移除 markdown
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
            # 宽松解析：逐行匹配
            import re
            items = []
            for line in text.split("\n"):
                match = re.search(
                    r'"index"\s*:\s*(\d+).*?"score"\s*:\s*(\d+)',
                    line
                )
                if match:
                    items.append({
                        "index": int(match.group(1)),
                        "score": int(match.group(2)),
                        "reason": "",
                        "category": "",
                        "route": "",
                        "info_gain": 0.0,
                    })

        result = []
        for item in items:
            idx = item.get("index", -1)
            score = item.get("score", 0)
            if not (isinstance(idx, int) and isinstance(score, (int, float))):
                continue
            result.append({
                "index": idx,
                "raw_score": int(score),
                "reason": item.get("reason", ""),
                "category": item.get("category", "") or item.get("aspect", ""),
                "route": item.get("route", ""),
                "info_gain": float(item.get("info_gain", 0.0) or 0.0),
            })

        return result
