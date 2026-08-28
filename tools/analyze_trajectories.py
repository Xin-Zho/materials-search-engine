"""Phase 2.1c trajectory 分析 CLI（用户定 2026-08-26）。

只汇总事实，不下 RL 结论。回答用户问题：
    哪类 query 最容易找到 NEW paper？
    哪类 query 最容易产生 NEW candidate / NEW edge？
    NODE / RELATION / MECHANISM / ADJACENT 哪类收益高？
    cost 与 discovery yield 怎么权衡？

用法:
    python tools/analyze_trajectories.py
"""

import json
import os
import sys

from search_engine.discovery.trajectory import (
    load_trajectories, analyze_trajectories, TRAJECTORIES_PATH,
)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def main():
    records = load_trajectories()
    if not records:
        print(f"✗ 无 trajectory（先跑 run_expansion_queries / run_staging_pipeline 记录）")
        return
    print("=" * 72)
    print(f"Trajectory Analysis（{len(records)} 条，只汇总事实——不定义 reward）")
    print("=" * 72)
    analysis = analyze_trajectories(records)
    header = (f"{'family':<14}{'n':>4}{'retr':>7}{'newP':>7}{'rel':>6}"
              f"{'edge':>6}{'cand':>6}{'unseen':>7}{'api':>6}")
    print(header)
    print("-" * 72)
    for fam, m in analysis.items():
        if fam == "_total":
            continue
        print(f"{fam:<14}{m['count']:>4}{m['avg_retrieved']:>7}{m['avg_new_unique_papers']:>7}"
              f"{m['avg_new_relevant_papers']:>6}{m['avg_new_edges']:>6}"
              f"{m['avg_new_candidates']:>6}{m['new_candidate_not_seen_before']:>7}"
              f"{m['total_api_calls']:>6}")
    print("-" * 72)
    print(f"total: {analysis.get('_total', {}).get('trajectories', 0)} 条 trajectory")
    print("\n解读（事实层面，不预设 reward 公式）:")
    print("  - avg_new_unique_papers 高 → 该 family 最会开拓搜索空间（NODE 常重复、ADJACENT 常新）")
    print("  - new_candidate_not_seen_before 高 → 该 family 最会扩张知识边界（候选级收益）")
    print("  - avg_new_edges / avg_new_candidates 对比 → 知识结构贡献 vs 候选量")


if __name__ == "__main__":
    main()
