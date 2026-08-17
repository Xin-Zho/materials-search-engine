"""检查 term matrix 的 mechanism 维度是否稳定。

回答：Silorane/filler 等机制在 term matrix 里是否每次稳定出现？
- 稳定 → query composition 不稳定，双层 query generation 已修复
- 不稳定 → 上游知识分解也需要修

用法（需 DEEPSEEK_API_KEY）:
    python check_term_matrix_stability.py [次数]
"""

import asyncio
import os
import sys
from search_engine.llm import DeepSeekBackend
from search_engine.term_matrix import TermMatrixGenerator
from search_engine.knowledge import get_domain_context

QUESTION = "光固化聚合物降低聚合收缩与收缩应力的机制"


async def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    backend = DeepSeekBackend(api_key=os.environ.get("DEEPSEEK_API_KEY", ""))
    gen = TermMatrixGenerator(backend)
    ctx = get_domain_context("photocuring")

    print(f"跑 {n} 次 term matrix 生成，检查 route family（归一化后）稳定性...\n")
    all_families: list[list[str]] = []
    all_route_counts: list[int] = []
    for i in range(n):
        m = await gen.generate(QUESTION, ctx)
        # 归一化后的 route families（gen.generate 内部会调 normalize_routes）
        families = [f.get("family", "").lower() for f in m.route_families]
        all_families.append(families)
        all_route_counts.append(len(m.get("strategy_route")))
        print(f"[{i+1}] raw routes ({len(m.get('strategy_route'))}) → {len(families)} families:")
        for f in m.route_families:
            print(f"      [{f.get('family')}] rep={f.get('representative')} ({len(f.get('members',[]))} 成员)")

    # 统计 route family 出现次数（核心 backbone）
    from collections import Counter
    family_counter = Counter()
    for families in all_families:
        family_counter.update(set(families))

    print("\n=== Route Family 稳定性（核心 backbone，要求 5/5）===")
    for fam, count in family_counter.most_common():
        bar = "█" * count + "░" * (n - count)
        print(f"  {fam}: {count}/{n} {bar}")

    unstable = [f for f, c in family_counter.items() if c < n]
    if unstable:
        print(f"\n不稳定 family（<{n}/{n}）: {unstable}")
        print("结论：route family 覆盖不稳，需继续修")
    else:
        print(f"\n所有 route family {n}/{n} 稳定覆盖")
        print("结论：coverage backbone 稳定，Phase 0 可冻结")

    print(f"\nraw route 数量范围: {min(all_route_counts)}~{max(all_route_counts)}（高召回，具体表达允许变化）")


if __name__ == "__main__":
    asyncio.run(main())
