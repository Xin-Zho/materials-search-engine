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

    print(f"跑 {n} 次 term matrix 生成，检查 strategy_route（backbone）稳定性...\n")
    all_routes: list[list[str]] = []
    all_mechs: list[list[str]] = []
    for i in range(n):
        m = await gen.generate(QUESTION, ctx)
        routes = [t.lower() for t in m.get("strategy_route")]
        mechs = [t.lower() for t in m.get("physical_mechanism")]
        all_routes.append(routes)
        all_mechs.append(mechs)
        print(f"[{i+1}] route ({len(routes)}): {m.get('strategy_route')}")
        print(f"      mech ({len(mechs)}): {m.get('physical_mechanism')}")

    # 统计 strategy_route 出现次数
    from collections import Counter
    route_counter = Counter()
    for routes in all_routes:
        route_counter.update(set(routes))

    print("\n=== strategy_route 稳定性（核心 backbone，要求 5/5）===")
    for route, count in route_counter.most_common():
        bar = "█" * count + "░" * (n - count)
        print(f"  {route}: {count}/{n} {bar}")

    unstable_routes = [m for m, c in route_counter.items() if c < n]
    if unstable_routes:
        print(f"\n不稳定 strategy_route（<{n}/{n}）: {unstable_routes}")
        print("结论：backbone 不稳，需继续修 term matrix 上游")
    else:
        print(f"\n所有 strategy_route {n}/{n} 稳定出现")
        print("结论：coverage backbone 稳定，Phase 0 可冻结")

    # physical_mechanism 是探索支路，可以波动
    mech_counter = Counter()
    for mechs in all_mechs:
        mech_counter.update(set(mechs))
    print("\n=== physical_mechanism（探索支路，允许波动）===")
    for mech, count in mech_counter.most_common():
        bar = "█" * count + "░" * (n - count)
        print(f"  {mech}: {count}/{n} {bar}")


if __name__ == "__main__":
    asyncio.run(main())
