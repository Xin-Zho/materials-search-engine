"""跨领域离线迁移测试：验证 Term Matrix / Knowledge Extractor 的 prompt 是否领域无关。

用电机 / ML 研究问题跑 Term Matrix，看能否生成该领域的 strategy_route
（而不是光固化专用词）。这验证 Phase 1 是否从"光固化专用"变成"跨领域知识驱动"。

用法（需 DEEPSEEK_API_KEY）:
    python test_cross_domain.py
"""

import asyncio
import os
from search_engine.llm import DeepSeekBackend
from search_engine.term_matrix import TermMatrixGenerator
from search_engine.knowledge import get_domain_context

QUESTIONS = {
    "motor": ("提高永磁同步电机转矩密度并降低转矩脉动", "motor"),
    "ml": ("小样本下深度学习模型的泛化能力提升", "ml"),
}


async def main():
    backend = DeepSeekBackend(api_key=os.environ.get("DEEPSEEK_API_KEY", ""))
    gen = TermMatrixGenerator(backend)

    for domain, (question, ctx_key) in QUESTIONS.items():
        ctx = get_domain_context(ctx_key)
        print(f"=== 领域: {domain} ===")
        print(f"问题: {question}\n")
        m = await gen.generate(question, ctx)

        routes = m.get("strategy_route")
        print(f"strategy_route ({len(routes)}):")
        for r in routes:
            print(f"  - {r}")

        mechs = m.get("physical_mechanism")
        print(f"\nphysical_mechanism ({len(mechs)}):")
        for mch in mechs:
            print(f"  - {mch}")

        families = [f.get("family", "") for f in m.route_families]
        print(f"\nroute families ({len(families)}):")
        for f in families:
            print(f"  - {f}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
