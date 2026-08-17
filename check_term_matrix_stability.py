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

    print(f"跑 {n} 次 term matrix 生成，检查 structure_mechanism 稳定性...\n")
    all_mechs: list[list[str]] = []
    for i in range(n):
        m = await gen.generate(QUESTION, ctx)
        mechs = [t.lower() for t in m.get("structure_mechanism", [])]
        all_mechs.append(mechs)
        print(f"[{i+1}] ({len(mechs)} 机制): {m.get('structure_mechanism')}")

    # 统计每个机制出现次数
    from collections import Counter
    counter = Counter()
    for mechs in all_mechs:
        counter.update(set(mechs))

    print("\n=== 机制出现稳定性 ===")
    for mech, count in counter.most_common():
        bar = "█" * count + "░" * (n - count)
        print(f"  {mech}: {count}/{n} {bar}")

    unstable = [m for m, c in counter.items() if c < n]
    if unstable:
        print(f"\n不稳定机制（<{n}/{n}）: {unstable}")
        print("结论：term matrix 本身不稳定，需修上游知识分解")
    else:
        print(f"\n所有机制 {n}/{n} 稳定出现")
        print("结论：term matrix 稳定，query composition 是唯一不稳定源（双层 query generation 已修复）")


if __name__ == "__main__":
    asyncio.run(main())
