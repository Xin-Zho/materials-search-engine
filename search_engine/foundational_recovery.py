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
        depth: int = 2,
        top_n: int = 30,
        per_layer_limit: int = 50,
    ) -> list[dict]:
        """从种子论文回溯引用链（depth 层），找到奠基/早期/综述论文。

        Args:
            seed_papers: 主搜索找到的代表论文（需有 DOI）
            early_year: 只保留该年份及之前的论文（根基论文通常较早）
            depth: 回溯层数（1 = 种子的参考文献，2 = 参考文献的参考文献）
            top_n: LLM 分类前最多保留的候选数（按被引排序）
            per_layer_limit: 每层每篇种子最多回溯的参考文献数

        Returns:
            [{paper, role, why, depth}, ...] 按被引降序
        """
        # 1. Backward citation，逐层回溯
        # candidates: dedup_key -> (paper, depth)
        candidates: dict[str, tuple[Paper, int]] = {}
        seen_dois: set[str] = set()

        # 第 1 层：所有种子
        frontier: list[Paper] = list(seed_papers)

        for layer in range(1, depth + 1):
            next_frontier: list[Paper] = []
            for paper in frontier:
                if not paper.doi:
                    continue
                doi_key = normalize_doi(paper.doi)
                if doi_key in seen_dois:
                    continue
                seen_dois.add(doi_key)
                try:
                    backward = await self.citation_tracker.backward(paper.doi, limit=per_layer_limit)
                except Exception as e:
                    logger.debug("backward 失败 %s: %s", paper.doi, e)
                    continue
                for bp in backward:
                    key = normalize_doi(bp.doi) or bp.paper_id
                    if key not in candidates:
                        candidates[key] = (bp, layer)
                        next_frontier.append(bp)
            frontier = next_frontier
            logger.debug("第 %d 层: 累计 %d 篇候选", layer, len(candidates))

        # 保存完整候选池（诊断用：检查 Gold 是否在候选池）
        self.last_candidates = candidates

        logger.info("Foundational Recovery: %d 篇种子 → depth=%d → %d 篇候选",
                     len(seed_papers), depth, len(candidates))

        # 2. 筛选早期 + 高被引
        early = [(p, d) for (p, d) in candidates.values() if p.year and p.year <= early_year]
        early.sort(key=lambda x: x[0].citation_count or 0, reverse=True)
        early = early[:top_n]

        if not early:
            logger.info("无早期论文（year <= %d）", early_year)
            return []

        # 3. LLM 判断历史角色
        roles = await self._classify_roles([p for p, _ in early], research_question)
        role_map = {item["paper"].paper_id: item for item in roles}

        result = []
        for paper, d in early:
            item = role_map.get(paper.paper_id, {"role": "OTHER", "why": ""})
            result.append({
                "paper": paper,
                "role": item["role"],
                "why": item["why"],
                "depth": d,
            })

        return result

    def evaluate(
        self,
        benchmark,
        question_id: str,
        recovered: list[dict],
        early_year: int = 2015,
    ) -> dict:
        """对比基准集，计算 Foundational Recall / Candidate Recall。

        Args:
            recovered: recover() 的返回（[{paper, role, why, depth}, ...]）

        Returns:
            {foundational_recall, candidate_recall, target_total, found, missed}
        """
        question = benchmark.get_question(question_id)
        if not question:
            return {}

        # 目标根基论文 = key_papers 里 year <= early_year 的
        target = [k for k in question.get("key_papers", [])
                  if (k.get("year") or 9999) <= early_year]

        recovered_dois = {normalize_doi(item["paper"].doi) for item in recovered
                          if item["paper"].doi}

        found = [k for k in target if normalize_doi(k.get("doi")) in recovered_dois]
        missed = [k for k in target if normalize_doi(k.get("doi")) not in recovered_dois]

        return {
            "foundational_recall": len(found) / len(target) if target else 0.0,
            "target_total": len(target),
            "found": len(found),
            "found_papers": found,
            "missed_papers": missed,
        }

    def diagnose_candidates(self, benchmark, question_id: str, early_year: int = 2015) -> str:
        """检查 Gold 根基论文是否在候选池里，区分 expansion 失败 vs ranking 失败。

        回答关键问题：12 篇 Gold 是否被引用回溯扩进了候选池？
        - 在候选池 → expansion 成功，失败在 ranking（rank 太靠后没进 top 30）
        - 不在候选池 → expansion 失败（seed/图/depth 问题）
        """
        question = benchmark.get_question(question_id)
        if not question:
            return "未知问题"

        candidates = getattr(self, "last_candidates", {})
        target = [k for k in question.get("key_papers", [])
                  if (k.get("year") or 9999) <= early_year]

        # 按被引降序的候选池排名
        ranked = sorted(
            candidates.items(),
            key=lambda x: (x[1][0].citation_count or 0),
            reverse=True,
        )
        rank_by_doi = {doi: i + 1 for i, (doi, _) in enumerate(ranked)}

        lines = ["=== Foundational Recovery Diagnostic ===",
                 f"候选池总数: {len(candidates)}"]
        in_pool = 0
        for k in target:
            doi = normalize_doi(k.get("doi"))
            if doi in candidates:
                paper, depth = candidates[doi]
                rank = rank_by_doi.get(doi, "?")
                in_pool += 1
                lines.append(
                    f"  [{k.get('year')}] {k.get('title','')[:50]}\n"
                    f"      IN POOL: depth={depth}, raw_rank={rank}/{len(candidates)}, "
                    f"cited={paper.citation_count or 0}"
                )
            else:
                lines.append(
                    f"  [{k.get('year')}] {k.get('title','')[:50]}\n"
                    f"      NOT IN POOL"
                )

        lines.append(f"\nCitation Candidate Recall: {in_pool}/{len(target)}")
        if in_pool == len(target):
            lines.append("→ 结论：expansion 成功，失败在 FOUNDATION RANKING")
        elif in_pool > 0:
            lines.append("→ 结论：部分 expansion 失败 + ranking 失败")
        else:
            lines.append("→ 结论：expansion 失败（seed 选择 / 引用图 / depth 问题）")

        return "\n".join(lines)

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
