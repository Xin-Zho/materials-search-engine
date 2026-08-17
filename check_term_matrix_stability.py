"""检查 term matrix 的 route backbone 稳定性（跨运行 global semantic canonicalization）。

不是逐次运行内部归一化后 exact match，而是：
1. 收集 N 次运行的所有 raw strategy_route
2. 全局归一化一次，得到 canonical route families
3. 每次运行映射回 canonical families，统计覆盖次数

这样能区分：backbone family（5/5 稳定）vs 长尾 family（允许波动）。

用法（需 DEEPSEEK_API_KEY）:
    python check_term_matrix_stability.py [次数]
"""

import asyncio
import os
import sys
from collections import Counter
from search_engine.llm import DeepSeekBackend
from search_engine.term_matrix import TermMatrixGenerator
from search_engine.knowledge import get_domain_context

QUESTION = "光固化聚合物降低聚合收缩与收缩应力的机制"


async def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    backend = DeepSeekBackend(api_key=os.environ.get("DEEPSEEK_API_KEY", ""))
    gen = TermMatrixGenerator(backend)
    ctx = get_domain_context("photocuring")

    print(f"跑 {n} 次 term matrix 生成，收集 raw strategy_route...\n")
    all_raw_runs: list[list[str]] = []
    for i in range(n):
        m = await gen.generate(QUESTION, ctx)
        raw = [t.lower() for t in m.get("strategy_route")]
        all_raw_runs.append(raw)
        print(f"[{i+1}] {len(raw)} 个 raw routes")

    # 全局归一化（所有 raw routes 合并，一次聚类）
    all_routes_union = []
    for raw in all_raw_runs:
        for r in raw:
            if r not in all_routes_union:
                all_routes_union.append(r)

    print(f"\n全局归一化：{len(all_routes_union)} 个去重 route → family...")
    global_families = await gen.normalize_routes(all_routes_union)

    # route -> family 映射
    route_to_family: dict[str, str] = {}
    for f in global_families:
        fam = f["family"].lower()
        for member in f.get("members", []):
            route_to_family[member.lower()] = fam
        # representative 也可能单独出现
        if f.get("representative"):
            route_to_family[f["representative"].lower()] = fam

    # 每次运行映射到 global family
    family_counter = Counter()
    for raw in all_raw_runs:
        covered = set()
        for r in raw:
            fam = route_to_family.get(r.lower())
            if fam:
                covered.add(fam)
        family_counter.update(covered)

    print(f"\n=== Global Route Family 覆盖稳定性 ===")
    backbone = []
    tail = []
    for fam, count in family_counter.most_common():
        bar = "█" * count + "░" * (n - count)
        tag = "backbone" if count == n else "长尾"
        print(f"  [{tag}] {fam}: {count}/{n} {bar}")
        if count == n:
            backbone.append(fam)
        else:
            tail.append(fam)

    print(f"\nbackbone family（{n}/{n} 稳定）: {len(backbone)} 个")
    if backbone:
        print(f"  {backbone}")
    print(f"长尾 family（波动）: {len(tail)} 个")
    if tail:
        print(f"  {tail}")

    # 判定
    if len(backbone) >= 4:
        print(f"\n结论：backbone 稳定（{len(backbone)} 个 family 跨运行一致），长尾正常波动")
        print("→ Phase 0 可冻结，进入 Phase 1 Knowledge Extractor")
    else:
        print(f"\n结论：backbone 不稳定（仅 {len(backbone)} 个 family 一致），仍需修")


if __name__ == "__main__":
    asyncio.run(main())
