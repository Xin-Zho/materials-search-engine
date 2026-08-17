"""QueryPopulation — 查询种群管理，从术语矩阵组合生成多条互补查询并记录收益。

核心：不做单条查询，而是维护一个查询种群，记录每条查询的收益
（新增候选/评分/相关论文、新增路线、重复率、成本），据此选择下一轮查询。

使用方式:
    pop = QueryPopulation(backend)
    queries = await pop.generate_queries(matrix, question)   # 从矩阵组合生成
    ...
    pop.record(query_id, result_papers, new_candidates, new_scored,
               new_relevant, new_routes, duplicate_rate, cost)
    top = pop.top_queries()  # 高收益查询
"""

import logging
import uuid
from .models import TermMatrix, QueryEntry
from .llm import LLMBackend

logger = logging.getLogger(__name__)

QUERY_GEN_PROMPT = """You are a literature search strategist. Build a diverse set of Scopus queries from a TERM MATRIX.

## Term Matrix
{term_matrix}

## Research Question
{question}

## Task
Generate {n_queries} Scopus queries. Each query must combine terms from DIFFERENT dimensions
(never two terms from the same dimension unless they're synonyms). Cover these combination patterns:

- material_system + target_properties
- strategy_route + target_properties
- physical_mechanism + failure_problem
- process + failure_problem
- composition + target_properties
- application + material_system
- target_properties + trade-off / opposing concept
- material_system + failure_problem
- process + target_properties

## Scopus Syntax
- Wrap every concept in TITLE-ABS-KEY(...)
- Synonyms joined by OR, different concepts by AND
- Multi-word phrases: use PRE/n (e.g., degree PRE/2 conversion)
- Prefer broad queries (20-200 results) over narrow (<5)

## Output
JSON array of objects, each with "strategy" (which dimension combination) and "query":
```json
[{{"strategy": "material_system+target_properties", "query": "TITLE-ABS-KEY(...) AND TITLE-ABS-KEY(...)"}}]
```"""


class QueryPopulation:
    """查询种群管理器。"""

    def __init__(self, backend: LLMBackend):
        self.backend = backend
        self.queries: dict[str, QueryEntry] = {}

    @staticmethod
    def _scopus_term(term: str) -> str:
        """多词短语加双引号，单词不加。"""
        term = term.strip().strip('"').strip("'")
        if " " in term or "-" in term:
            return f'"{term}"'
        return term

    def build_coverage_queries(self, matrix: TermMatrix) -> list[str]:
        """从术语矩阵确定性生成 coverage queries（每个机制 cluster 至少一条）。

        不依赖 LLM，temperature 恒为 0。保证 term matrix 里出现的每个
        mechanism 都获得搜索机会，避免 LLM 随机组合时漏掉某条路线。
        """
        mechanisms = matrix.get("strategy_route")  # 强制覆盖策略路线，不是物理机制
        target_props = matrix.get("target_properties")
        failure_probs = matrix.get("failure_problem")

        queries: list[str] = []
        for mech in mechanisms:
            mech_term = self._scopus_term(mech)
            # 路线 × 目标性能（取第一个）
            if target_props:
                prop_term = self._scopus_term(target_props[0])
                queries.append(f"TITLE-ABS-KEY({mech_term}) AND TITLE-ABS-KEY({prop_term})")
            # 路线 × 失效问题（取第一个）
            if failure_probs:
                fail_term = self._scopus_term(failure_probs[0])
                q = f"TITLE-ABS-KEY({mech_term}) AND TITLE-ABS-KEY({fail_term})"
                if q not in queries:
                    queries.append(q)

        logger.info("coverage queries: %d 个策略路线 → %d 条确定性查询",
                     len(mechanisms), len(queries))
        return queries

    async def generate_queries(
        self,
        matrix: TermMatrix,
        question: str,
        n_queries: int = 8,
    ) -> list[tuple[str, str]]:
        """从术语矩阵生成查询种群。

        Returns:
            [(query_id, query_string, strategy), ...]
        """
        # 格式化术语矩阵
        matrix_text = "\n".join(
            f"- {dim}: {', '.join(terms) if terms else '(empty)'}"
            for dim in TermMatrix.DIMENSIONS
            if (terms := matrix.get(dim))
        )

        prompt = QUERY_GEN_PROMPT.format(
            term_matrix=matrix_text,
            question=question,
            n_queries=n_queries,
        )

        response = await self.backend.chat(
            system_prompt="You are a literature search strategist. Output only valid JSON.",
            user_message=prompt,
            temperature=0.4,
            max_tokens=2048,
        )

        items = self._parse_queries(response)
        result = []
        for item in items:
            qid = f"q_{uuid.uuid4().hex[:8]}"
            query_str = item["query"]
            strategy = item.get("strategy", "")
            self.queries[qid] = QueryEntry(
                query_id=qid,
                query=query_str,
                strategy=strategy,
            )
            result.append((qid, query_str, strategy))

        logger.info("查询种群: 生成 %d 条查询", len(result))
        return result

    def record(
        self,
        query_id: str,
        result_papers: list[str],
        new_candidates: int,
        new_scored: int,
        new_relevant: int,
        new_routes: int,
        duplicate_rate: float,
        cost: float,
        n_returned: int | None = None,
    ):
        """记录一次查询的执行结果。

        Args:
            n_returned: 查询返回的论文总数（用于计算比例指标），None 则用 result_papers 长度
        """
        entry = self.queries.get(query_id)
        if not entry:
            return

        entry.result_papers = result_papers
        entry.new_candidates = new_candidates
        entry.new_scored = new_scored
        entry.new_relevant = new_relevant
        entry.new_routes = new_routes
        entry.duplicate_rate = duplicate_rate
        entry.cost = cost
        entry.return_score = self.score(entry, n_returned=n_returned)

    def score(self, entry: QueryEntry, n_returned: int | None = None) -> float:
        """计算查询收益得分（比例指标，避免数量尺度不一致）。

        S = 40·(R/N) + 20·(U/N) + 25·min(F/2,1) − 10·(C/Cmax) − 5·(1−S/U)
        """
        n = n_returned if n_returned is not None else len(entry.result_papers)
        if n == 0:
            return 0.0

        r = entry.new_relevant          # 相关论文数
        u = entry.new_candidates        # 新增候选数
        f = entry.new_routes            # 新增路线数
        s = entry.new_scored            # 成功评分数
        c = entry.cost                  # 成本（秒）
        c_max = 120.0                   # 成本归一化上限（秒）

        return (
            40.0 * (r / n)
            + 20.0 * (u / n)
            + 25.0 * min(f / 2.0, 1.0)
            - 10.0 * min(c / c_max, 1.0)
            - 5.0 * (1.0 - (s / u) if u > 0 else 0.0)
        )

    def top_queries(self, k: int = 5) -> list[QueryEntry]:
        """返回收益最高的 k 条查询。"""
        scored = sorted(
            self.queries.values(),
            key=lambda q: q.return_score,
            reverse=True,
        )
        return scored[:k]

    def all_query_strings(self) -> list[str]:
        """返回所有查询字符串。"""
        return [q.query for q in self.queries.values()]

    @staticmethod
    def _parse_queries(response: str) -> list[dict]:
        """解析查询列表。"""
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
            return [it for it in items if isinstance(it, dict) and it.get("query")]
        except (json.JSONDecodeError, ValueError):
            return []
