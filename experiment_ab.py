"""Phase 1 最终 A/B：Baseline Expansion vs Knowledge-driven Expansion。

同数据源（OpenAlex）、同 query budget、同 top-K、同 relevance 判断。

用法（需 DEEPSEEK_API_KEY）:
    python experiment_ab.py data/exports/foundational_baseline.csv [budget]

比较指标：Unique New Relevant Papers / New Relevant per Query。
"""

import asyncio
import csv
import os
import sys
from search_engine.llm import DeepSeekBackend
from search_engine.term_matrix import TermMatrixGenerator
from search_engine.query_population import QueryPopulation
from search_engine.citation_tracker import CitationTracker, RateLimitError, RateLimitExhaustedError
from search_engine.evaluator import normalize_doi
from search_engine.relevance import RelevanceFilter
from search_engine.knowledge import get_domain_context
from search_engine.models import Paper

QUESTION = "光固化聚合物降低聚合收缩与收缩应力的机制"


async def generate_baseline_queries(backend, budget):
    """A 臂：Term Matrix coverage queries（普通 expansion）。"""
    gen = TermMatrixGenerator(backend)
    pop = QueryPopulation(backend)
    matrix = await gen.generate(QUESTION, get_domain_context("photocuring"))
    coverage = pop.build_coverage_queries(matrix)
    return coverage[:budget]


def load_knowledge_queries(path, budget):
    """B 臂：冻结的 knowledge queries。"""
    import json
    queries = json.load(open(path, encoding="utf-8"))
    return [q[0] for q in queries][:budget]


async def search_and_eval(tracker, backend, queries, original_dois):
    """搜索一批 query，做 relevance 判断，返回 (useful_queries, unique_new_relevant, rate_limited)。"""
    rf = RelevanceFilter(backend)
    new_papers: dict[str, Paper] = {}
    query_novel: dict[str, list[str]] = {}
    rate_limited = 0

    for q in queries:
        try:
            results = await tracker.search(q, limit=20)
        except RateLimitError:
            rate_limited += 1
            continue
        except Exception:
            continue
        novel = [p for p in results if normalize_doi(p.doi) not in original_dois]
        if novel:
            query_novel[q] = [normalize_doi(p.doi) or p.paper_id for p in novel]
            for p in novel:
                new_papers.setdefault(normalize_doi(p.doi) or p.paper_id, p)

    # relevance 判断
    relevant_dois = set()
    if new_papers:
        scored = await rf.filter(list(new_papers.values()), research_question=QUESTION,
                                 threshold=0, top_k=len(new_papers))
        relevant_dois = {normalize_doi(sp.paper.doi) or sp.paper.paper_id for sp in scored if sp.score >= 70}

    useful = sum(1 for novos in query_novel.values() if any(d in relevant_dois for d in novos))
    return useful, len(relevant_dois), rate_limited


async def main():
    csv_path = sys.argv[1]
    budget = int(sys.argv[2]) if len(sys.argv) > 2 else 50

    papers: list[Paper] = []
    with open(csv_path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row.get("title") and row.get("doi"):
                papers.append(Paper(paper_id=row["doi"], title=row["title"], abstract=row.get("abstract", "")))
    original_dois = {normalize_doi(p.doi) for p in papers}

    backend = DeepSeekBackend(api_key=os.environ.get("DEEPSEEK_API_KEY", ""))

    # A 臂：Baseline Expansion（冻结：保存/加载，避免重新生成导致变量变化）
    baseline_qf = "data/cache/baseline_queries.json"
    import json as _json
    if os.path.exists(baseline_qf):
        baseline_queries = _json.load(open(baseline_qf, encoding="utf-8"))
        print(f"从 {baseline_qf} 加载 {len(baseline_queries)} 条冻结 Baseline queries\n")
    else:
        print("生成 Baseline queries...")
        baseline_queries = await generate_baseline_queries(backend, budget)
        _json.dump(baseline_queries, open(baseline_qf, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"  Baseline: {len(baseline_queries)} 条（已冻结到 {baseline_qf}）\n")

    # 公平预算：对齐到 Baseline 实际数量（不凑 50）
    budget = len(baseline_queries)

    # B 臂：Knowledge-driven（按保存顺序取前 budget 条，确定性规则，不按成功挑）
    knowledge_queries = load_knowledge_queries("data/cache/knowledge_queries.json", budget)
    print(f"=== Phase 1 Final A/B（预算 {budget} queries/臂）===\n")
    print(f"Baseline: {len(baseline_queries)} 条 | Knowledge: {len(knowledge_queries)} 条\n")

    async with CitationTracker() as tracker:
        try:
            await tracker.check_rate_limit()
        except RateLimitExhaustedError as e:
            print(f"[终止] {e}")
            return

        print("搜索 A 臂（Baseline）...")
        base_useful, base_relevant, base_rl = await search_and_eval(tracker, backend, baseline_queries, original_dois)

        print("搜索 B 臂（Knowledge）...")
        kn_useful, kn_relevant, kn_rl = await search_and_eval(tracker, backend, knowledge_queries, original_dois)

    # evaluable rate 判定（<90% 则 A/B 无效）
    base_evaluable_rate = (len(baseline_queries) - base_rl) / len(baseline_queries) if baseline_queries else 0
    kn_evaluable_rate = (len(knowledge_queries) - kn_rl) / len(knowledge_queries) if knowledge_queries else 0

    print("\n=== Phase 1 Final A/B 结果 ===")
    print(f"{'指标':<32} {'Baseline':>10} {'Knowledge':>12}")
    print(f"{'Budget':<32} {len(baseline_queries):>10} {len(knowledge_queries):>12}")
    print(f"{'RATE_LIMITED':<32} {base_rl:>10} {kn_rl:>12}")
    print(f"{'Evaluable':<32} {len(baseline_queries)-base_rl:>10} {len(knowledge_queries)-kn_rl:>12}")
    print(f"{'Useful queries':<32} {base_useful:>10} {kn_useful:>12}")
    print(f"{'Unique New Relevant':<32} {base_relevant:>10} {kn_relevant:>12}")
    print(f"{'New Relevant / Query':<32} {base_relevant/len(baseline_queries) if baseline_queries else 0:>10.3f} {kn_relevant/len(knowledge_queries) if knowledge_queries else 0:>12.3f}")

    if base_evaluable_rate < 0.9 or kn_evaluable_rate < 0.9:
        print("\nAB_RESULT = INVALID / INCOMPLETE（某臂 evaluable rate < 90%，不能下结论）")
    else:
        gain = kn_relevant - base_relevant
        print("\nAB_RESULT = VALID（两臂都 ≥90% 可评价）")
        print(f"Incremental Unique Relevant Recall Gain: {gain:+d} 篇（Knowledge - Baseline）")
        if gain > 0:
            print(f"→ Knowledge-driven 在相同预算下多召回 {gain} 篇新相关论文，Phase 1 正式通过")
        elif gain == 0:
            print(f"→ 两者相当，Knowledge 价值主要在知识库/历史召回，据此进 Phase 2")
        else:
            print(f"→ Baseline 更高，需重新审视 Knowledge-driven query 的价值")


if __name__ == "__main__":
    asyncio.run(main())
