"""跑 Phase 1.7 Autonomous Knowledge Loop（有界自主知识循环）。

默认用 OpenAlexBackend 搜索（无需登录），LLM 用 DeepSeek。闭环跑：
coverage 分析 → 缺口 → gap query → 搜索 → 筛 relevant → 抽取 → 入库 → 重算。

用法（需 DEEPSEEK_API_KEY）:
    python tools/run_autonomous_loop.py "光固化聚合物降低聚合收缩的机制" [--rounds 3] [--max-queries 10]

可选 OPENALEX_MAILTO 进入 polite pool 提高额度。
"""

import argparse
import asyncio
import json
import os
import sys

from search_engine.llm import DeepSeekBackend
from search_engine.knowledge_base import KnowledgeBase
from search_engine.knowledge import AutonomousLoop
from search_engine.backends import OpenAlexBackend

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


async def main():
    ap = argparse.ArgumentParser(description="Phase 1.7 Autonomous Knowledge Loop")
    ap.add_argument("question", help="研究问题")
    ap.add_argument("--rounds", type=int, default=3, help="max_rounds（默认 3）")
    ap.add_argument("--max-queries", type=int, default=10, help="每轮 gap query 上限（默认 10）")
    ap.add_argument("--max-results", type=int, default=20, help="每 query 结果上限（默认 20）")
    ap.add_argument("--mailto", default=os.environ.get("OPENALEX_MAILTO", ""),
                    help="OpenAlex polite pool email")
    ap.add_argument("--anchor", default="polymerization shrinkage", help="query anchor term")
    ap.add_argument("--baseline", default="",
                    help="Phase 1.7 baseline manifest（tools/phase17_baseline.py 生成）——"
                         "冻结搜索集，loop 只搜这些 gap（优先 initial_search_gaps=SEARCH_GAP，"
                         "回退 initial_true_open_mechanism）")
    ap.add_argument("--mode", default="completeness",
                    choices=["completeness", "discovery", "all"],
                    help="搜索模式（用户定 2026-08-25）：completeness=只搜 confirmed_missing/SEARCH_GAP 补漏；"
                         "discovery=只搜 hypothesis 探索新机制；all=不过滤（默认 completeness）")
    args = ap.parse_args()

    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        print("⚠️  未设 DEEPSEEK_API_KEY，归一化/抽取/筛选会退化或失败。", file=sys.stderr)

    # Phase 1.7 baseline：冻结搜索集（只搜这些，新发现单独记）
    # 优先级：initial_search_gaps（SEARCH_GAP，KNOWLEDGE_STATUS 权威标注）>
    #         initial_true_open_mechanism（旧启发式审计）> initial_true_open
    initial_gap_targets = None
    if args.baseline:
        with open(args.baseline, encoding="utf-8") as f:
            bl = json.load(f)
        raw = (bl.get("initial_search_gaps") or bl.get("initial_true_open_mechanism")
               or bl.get("initial_true_open") or [])
        initial_gap_targets = {
            (str(r), str(m)) for r, m in raw
        }
        src = ("SEARCH_GAP" if bl.get("initial_search_gaps")
               else ("TRUE_OPEN mechanism" if bl.get("initial_true_open_mechanism") else "TRUE_OPEN"))
        print(f"baseline: {len(initial_gap_targets)} 个冻结 {src} targets "
              f"（records={bl.get('records')}, extraction_miss={bl.get('extraction_miss')} 不搜）")

    llm = DeepSeekBackend(api_key=key)
    kb = KnowledgeBase()
    mailto = args.mailto or None
    async with OpenAlexBackend(mailto=mailto) as search:
        loop = AutonomousLoop(
            llm, search, kb,
            max_rounds=args.rounds,
            max_gap_queries_per_round=args.max_queries,
            max_results_per_query=args.max_results,
            anchor=args.anchor,
            initial_gap_targets=initial_gap_targets,
            mode=args.mode,
        )
        result = await loop.run(args.question)

    # dump trace 供 analyze_gap_failures.py 诊断
    os.makedirs("data/exports", exist_ok=True)
    trace_path = "data/exports/gap_failures_trace.json"
    with open(trace_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 80)
    print(f"Autonomous Loop 结果（Phase 1.7: 只看搜索系统产生的价值）[mode={args.mode}]")
    print("=" * 80)
    for r in result["rounds"]:
        print(f"\nRound {r['round']}")
        print(f"  initial_true_open_before = {r.get('initial_true_open_before', '?')}")
        print(f"  new_relevant_papers      = {r['new_relevant_papers']}")
        print(f"  new_direct_model_edges   = {r.get('new_direct_model_edges', 0)}")
        print(f"  target_gaps_closed       = {r.get('target_gaps_closed', 0)}")
        print(f"  cross_gaps_closed        = {r.get('cross_gaps_closed', 0)}")
        print(f"  initial_true_open_after  = {r.get('initial_true_open_after', '?')}")
        print(f"  newly_discovered_gaps    = {r.get('newly_discovered', 0)}")
        print(f"  TRUE_OPEN_INITIAL        = {r.get('true_open_initial', '?')}   (冻结 baseline)")
        print(f"  TRUE_OPEN_REMAINING      = {r.get('true_open_remaining', '?')}")
        print(f"  OTHER_OPEN               = {r.get('other_open', '?')}   (EXTRACTION_MISS 机制 + 新发现)")
        print(f"  gap_queries              = {r.get('gap_queries', 0)}")
        print(f"  q_hit% / p_hit%          = {int(r.get('query_gap_hit_rate', 0) * 100)} / "
              f"{int(r.get('paper_gap_hit_rate', 0) * 100)}")
    print(f"\n停止原因: {result['stop_reason']}")
    print(f"总轮数: {result['total_rounds']}")
    print(f"最终剩余 gaps: {result['final_remaining_gaps']}")
    print(f"\ntrace 已存: {trace_path}")
    print(f"诊断: python tools/analyze_gap_failures.py")
    kb.close()


if __name__ == "__main__":
    asyncio.run(main())
