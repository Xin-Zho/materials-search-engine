"""IterativeSearcher — 覆盖驱动的多策略迭代搜索。

核心闭环（替代旧的"相似性驱动"）:
    1. 术语矩阵：研究问题 → 8 维度术语表
    2. 查询种群：从矩阵组合生成多条互补查询
    3. 搜索 + 结构化标签（相关性 + 技术路线 + 信息增益）
    4. 覆盖地图：聚类路线 → 识别缺口
    5. 缺口驱动：下一轮查询填补覆盖薄弱处

使用方式:
    searcher = IterativeSearcher(backend, engine)
    papers = await searcher.search(
        "光固化弹性体的低黏度高伸长研究",
        domain_context=kb_context,
        target_count=10,
        threshold=70,
        max_rounds=3,
    )
"""

import logging
import re
from .models import Paper, ScoredPaper, QueryEntry
from .engine import ScopusSearchEngine
from .llm import LLMBackend
from .relevance import RelevanceFilter
from .term_matrix import TermMatrixGenerator
from .query_population import QueryPopulation
from .coverage_map import CoverageMap
from .evaluator import normalize_doi

logger = logging.getLogger(__name__)


def dedup_key(paper: Paper) -> str:
    """跨源去重键：优先规范化 DOI，否则用标题+年份。

    Scopus 用 EID、OpenAlex 用 openalex:ID，同一篇论文两处 ID 不同。
    用 DOI 作为统一键才能正确去重。
    """
    if paper.doi:
        return "doi:" + paper.doi.strip().lower().rstrip(".")
    title = re.sub(r"[^a-z0-9]+", "", (paper.title or "").lower())
    return f"title:{title}:{paper.year}"

GAP_QUERY_PROMPT = """You are a materials science literature search strategist. Generate queries to fill COVERAGE GAPS.

## Research Question
{question}

## Coverage Gaps (underrepresented routes to fill)
{gaps}

## Queries Already Used
{used_queries}

## Task
Generate {n_queries} NEW Scopus queries targeting the gaps above.
Each query should target a DIFFERENT gap. Prefer broad queries (20-200 results).
Do NOT repeat already-used queries.

## Scopus Syntax
- Wrap every concept in TITLE-ABS-KEY(...)
- Synonyms joined by OR, different concepts by AND
- Multi-word phrases use PRE/n (e.g., degree PRE/2 conversion)
- Language filter: LANGUAGE(english)

## Output
JSON array of strings:
```json
["query1", "query2", ...]
```

## Domain Knowledge (AUTHORITATIVE)
{domain_context}"""


class IterativeSearcher:
    """覆盖驱动的迭代搜索器。"""

    MAX_PAPERS_PER_QUERY = 200  # 每条查询最多获取论文数
    N_QUERIES_FIRST_ROUND = 8   # 第一轮查询数（术语矩阵组合）
    N_QUERIES_GAP_ROUND = 4     # 缺口轮查询数
    PRE_FILTER_BATCH = 30       # 快筛阈值

    def __init__(self, backend: LLMBackend, engine: ScopusSearchEngine,
                 citation_tracker=None):
        self.backend = backend
        self.engine = engine
        self.citation_tracker = citation_tracker
        self.term_gen = TermMatrixGenerator(backend)
        self.population = QueryPopulation(backend)
        self.coverage = CoverageMap()
        self._relevance_filter = RelevanceFilter(backend)
        self.last_qualified: list[ScoredPaper] = []  # 最后一次搜索的全部达标论文
        # 检索失败诊断：记录每层候选集的 DOI，用于区分"检索漏"vs"排序/过滤漏"
        self.raw_dois: set[str] = set()          # 原始检索结果（所有 result.papers）
        self.prefilter_dois: set[str] = set()    # 快筛后保留
        self.scored_dois: set[str] = set()       # 精筛后（有评分）
        self.qualified_dois: set[str] = set()    # 达标（score >= threshold）

    async def search(
        self,
        research_question: str,
        domain_context: str = "",
        target_count: int = 10,
        threshold: int = 70,
        max_rounds: int = 3,
    ) -> list[ScoredPaper]:
        """迭代搜索直到覆盖充分。

        Returns:
            list[ScoredPaper] 按分数降序，最多 target_count 篇
        """
        all_scored: dict[str, ScoredPaper] = {}  # dedup_key -> ScoredPaper
        known_routes: set[str] = set()           # 已见路线（跨查询去重计数）
        used_queries: list[str] = []

        # 1. 生成术语矩阵
        logger.info("=== 生成术语矩阵 ===")
        matrix = await self.term_gen.generate(research_question, domain_context)

        # 2. 第一轮：从矩阵生成查询种群
        logger.info("=== 第 1 轮：术语矩阵 → 查询种群 ===")
        round_queries = await self.population.generate_queries(
            matrix, research_question, self.N_QUERIES_FIRST_ROUND
        )

        for round_num in range(1, max_rounds + 1):
            # 3. 执行本轮查询
            for qid, query, strategy in round_queries:
                if query in used_queries:
                    continue
                used_queries.append(query)

                try:
                    result = await self.engine.search(query, limit=self.MAX_PAPERS_PER_QUERY)
                except Exception as e:
                    logger.warning("搜索失败: %s — %s", query[:80], e)
                    continue

                n_returned = len(result.papers)

                # 记录原始检索结果的 DOI（诊断用）
                self.raw_dois.update(normalize_doi(p.doi) for p in result.papers if p.doi)

                # 去重（用 dedup_key 跨源统一）
                new_papers = [p for p in result.papers if dedup_key(p) not in all_scored]
                new_candidates = len(new_papers)
                duplicate_rate = 1.0 - new_candidates / max(n_returned, 1)

                if not new_papers:
                    self.population.record(
                        qid, [p.paper_id for p in result.papers],
                        new_candidates=0, new_scored=0, new_relevant=0,
                        new_routes=0, duplicate_rate=1.0, cost=result.time_taken,
                        n_returned=n_returned,
                    )
                    continue

                # 快筛
                if len(new_papers) > self.PRE_FILTER_BATCH:
                    new_papers = await self._relevance_filter.pre_filter(
                        new_papers, research_question
                    )

                if not new_papers:
                    continue

                # 记录快筛后的 DOI
                self.prefilter_dois.update(normalize_doi(p.doi) for p in new_papers if p.doi)

                # 精筛（输出 route + info_gain）
                scored = await self._relevance_filter.filter(
                    new_papers,
                    research_question=research_question,
                    threshold=0,
                    top_k=len(new_papers),
                    existing_routes=list(known_routes),
                )

                # 计算指标（拆分为候选/评分/相关）
                new_scored = len(scored)
                new_relevant = sum(1 for sp in scored if sp.score >= threshold)

                # 记录精筛后的 DOI（诊断用）
                self.scored_dois.update(normalize_doi(sp.paper.doi) for sp in scored if sp.paper.doi)

                # 新增路线（用实时 known_routes 避免同轮重复计数）
                new_routes = 0
                for sp in scored:
                    if sp.route and sp.route not in known_routes:
                        known_routes.add(sp.route)
                        new_routes += 1

                # 存入 all_scored
                for sp in scored:
                    key = dedup_key(sp.paper)
                    if key not in all_scored:
                        all_scored[key] = sp

                self.population.record(
                    qid,
                    [p.paper_id for p in new_papers],
                    new_candidates=new_candidates,
                    new_scored=new_scored,
                    new_relevant=new_relevant,
                    new_routes=new_routes,
                    duplicate_rate=duplicate_rate,
                    cost=result.time_taken,
                    n_returned=n_returned,
                )

            # 3.5 引文通道：对高相关种子论文做向前/向后/共被引扩展
            if self.citation_tracker:
                await self._expand_via_citations(all_scored, research_question, threshold)

            # 4. 构建覆盖地图 — 只用达标论文，避免无关论文污染路线聚类
            coverage_papers = [sp for sp in all_scored.values() if sp.score >= threshold]
            self.coverage.build(coverage_papers)
            logger.info("第 %d 轮覆盖地图:\n%s", round_num, self.coverage.summarize())

            # 5. 识别缺口 + 检查停止
            qualified = sorted(
                [sp for sp in all_scored.values() if sp.score >= threshold],
                key=lambda x: x.score, reverse=True,
            )
            # 记录达标的 DOI（诊断用）
            self.qualified_dois = {normalize_doi(sp.paper.doi) for sp in qualified if sp.paper.doi}
            gaps = self.coverage.identify_gaps()

            logger.info("第 %d 轮完成: %d 篇达标 (目标 %d), %d 个缺口",
                         round_num, len(qualified), target_count, len(gaps))

            if len(qualified) >= target_count and not gaps:
                logger.info("覆盖启发式满足，停止")
                break

            # 6. 缺口驱动生成下一轮查询
            if round_num < max_rounds:
                gap_desc = self.coverage.describe_gaps()
                logger.info("缺口: %s", gap_desc[:200])
                round_queries = await self._generate_gap_queries(
                    research_question, domain_context, gap_desc, used_queries
                )
                if not round_queries:
                    break

        # 7. MMR 多样性重排（避免 top-k 被同类论文占满）
        # 保存达标全集供覆盖度评估（评估应看"找到的所有相关论文"，不是 top-k）
        self.last_qualified = qualified
        from .mmr import MMRReranker
        reranker = MMRReranker(lambda_param=0.7)
        return reranker.rerank(qualified, top_k=target_count)

    def analyze_retrieval_failures(self, benchmark, question_id: str) -> str:
        """对漏检论文做 retrieval failure analysis。

        区分漏检发生在哪一层：
        - 检索阶段漏（查询没返回）→ 需改进查询/术语矩阵/引文通道
        - 快筛误杀（pre_filter SKIP）→ 需放宽快筛
        - 精筛未评分 → 解析/批次 bug
        - 分数低于阈值 → 需调阈值或评分 prompt
        """
        question = benchmark.get_question(question_id)
        if not question:
            return "未知问题"

        lines = ["=== 检索失败分析 ==="]
        for k in question.get("key_papers", []):
            doi = normalize_doi(k.get("doi"))
            if doi in self.qualified_dois:
                continue  # 命中，跳过

            if doi not in self.raw_dois:
                stage = "检索阶段漏（查询未返回此文）"
            elif doi not in self.prefilter_dois:
                stage = "快筛误杀（pre_filter 判 SKIP）"
            elif doi not in self.scored_dois:
                stage = "精筛未评分（解析/批次问题）"
            else:
                stage = "分数低于阈值（被 filter 过滤）"

            lines.append(f"  - [{k.get('year')}] {k.get('title','')[:55]} "
                         f"({k.get('route','')}) → {stage}")

        return "\n".join(lines)

    async def _generate_gap_queries(
        self,
        question: str,
        domain_context: str,
        gaps: str,
        used_queries: list[str],
    ) -> list[tuple[str, str, str]]:
        """缺口驱动生成下一轮查询。"""
        prompt = GAP_QUERY_PROMPT.format(
            question=question,
            gaps=gaps,
            used_queries="\n".join(used_queries[-10:]),
            n_queries=self.N_QUERIES_GAP_ROUND,
            domain_context=domain_context,
        )

        response = await self.backend.chat(
            system_prompt="You are a literature search strategist. Output valid JSON.",
            user_message=prompt,
            temperature=0.4,
            max_tokens=1536,
        )

        import json
        text = response.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:])
            if text.endswith("```"):
                text = text[:-3]

        queries = []
        try:
            start = text.index("[")
            end = text.rindex("]") + 1
            items = json.loads(text[start:end])
            for q in items:
                if isinstance(q, str) and q.strip():
                    qid = f"gap_{len(used_queries)}_{len(queries)}"
                    # 加入查询种群，使后续 record() 能记录收益
                    self.population.queries[qid] = QueryEntry(
                        query_id=qid,
                        query=q.strip(),
                        strategy="gap_filling",
                    )
                    queries.append((qid, q.strip(), "gap_filling"))
        except (json.JSONDecodeError, ValueError):
            logger.warning("缺口查询解析失败")

        return queries

    async def _expand_via_citations(
        self,
        all_scored: dict[str, ScoredPaper],
        question: str,
        threshold: int,
        max_seeds: int = 5,
    ):
        """引文通道 — 对高相关种子论文做向前/向后/共被引扩展。

        种子论文：score >= threshold 且有 DOI 的，取 top max_seeds。
        结果去重、精筛后加入 all_scored。
        """
        # 选种子论文
        seeds = [
            sp for sp in all_scored.values()
            if sp.score >= threshold and sp.paper.doi
        ]
        seeds.sort(key=lambda sp: sp.score, reverse=True)
        seeds = seeds[:max_seeds]

        if not seeds:
            return

        logger.info("引文通道: %d 篇种子论文", len(seeds))

        candidates: dict[str, Paper] = {}
        for sp in seeds:
            doi = sp.paper.doi
            try:
                backward = await self.citation_tracker.backward(doi, limit=15)
                forward = await self.citation_tracker.forward(doi, limit=15)
                related = await self.citation_tracker.related(doi, limit=10)
                for p in backward + forward + related:
                    candidates[dedup_key(p)] = p
            except Exception as e:
                logger.debug("引文追踪失败 %s: %s", doi, e)

        # 去重：排除已有论文（用 dedup_key）
        new_papers = [p for p in candidates.values() if dedup_key(p) not in all_scored]
        if not new_papers:
            return

        logger.info("引文通道: %d 篇新候选论文", len(new_papers))

        # 精筛
        scored = await self._relevance_filter.filter(
            new_papers,
            research_question=question,
            threshold=0,
            top_k=len(new_papers),
            existing_routes=list(self.coverage.routes.keys()),
        )
        for sp in scored:
            key = dedup_key(sp.paper)
            if key not in all_scored:
                all_scored[key] = sp
