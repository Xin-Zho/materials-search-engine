"""离线诊断 Foundational Recovery：检查 Gold 根基论文是否在引用候选池里。

不跑完整搜索，只做 backward citation 回溯 + 候选池诊断。
快速（只用 OpenAlex，约 2-5 分钟）。

用法:
    python diagnose_foundational.py data/exports/foundational_baseline.csv benchmarks/benchmarks_v1.json:pc_001
"""

import asyncio
import csv
import sys
from search_engine import CitationTracker
from search_engine.foundational_recovery import FoundationalRecovery
from search_engine.evaluator import Benchmark
from search_engine.models import Paper


async def main():
    csv_path = sys.argv[1]
    benchmark_path, qid = sys.argv[2].split(":")

    # 读种子论文（主搜索的 scored 结果，含 DOI）
    seeds: list[Paper] = []
    with open(csv_path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row.get("doi"):
                seeds.append(Paper(
                    paper_id=row["doi"],
                    title=row.get("title", ""),
                    doi=row["doi"],
                ))
    print(f"种子论文: {len(seeds)} 篇\n")

    # backward citation 回溯 + 诊断（不需要 LLM）
    async with CitationTracker() as tracker:
        fr = FoundationalRecovery(tracker, backend=None)
        await fr.collect_candidates(seeds, depth=2, per_layer_limit=50)
        benchmark = Benchmark(benchmark_path)
        print(fr.diagnose_candidates(benchmark, qid, early_year=2015))


if __name__ == "__main__":
    asyncio.run(main())
