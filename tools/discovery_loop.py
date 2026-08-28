"""Phase 2.1 discovery loop CLI（用户定 2026-08-26 Phase 2.1a）。

用法:
    # 只读规划（真正只读：不改 status / 不增 retry / 不写 verify run / manifest / queue）
    python tools/discovery_loop.py --rounds 3 --top-n 8 --plan-only

    # 真跑（默认 verify 读候选缓存，不烧 API；候选需先有 verification 结果）
    python tools/discovery_loop.py --rounds 1 --top-n 8

    # 查看 approval queue
    python tools/discovery_loop.py --show-queue

    # 逐条批准 / 拒绝（不提供一键全批准）
    python tools/discovery_loop.py --approve <proposal_id> --reason "..."
    python tools/discovery_loop.py --reject  <proposal_id> --reason "..."
"""

import argparse
import json
import os
import sys

from search_engine.discovery.controller import DiscoveryController
from search_engine.discovery.approval_queue import (
    load_queue, approve_item, reject_item, list_queue,
)
from search_engine.discovery.metrics import compute_round_metrics, format_round_report
from search_engine.discovery.round_state import load_rounds

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def _print_plan(plan: dict) -> None:
    print("=" * 72)
    print("Phase 2.1 下一轮规划（plan-only，真正只读——不写任何文件）")
    print("=" * 72)
    print(f"扫描: {plan['scanned']} raw（新增 {plan['new_candidates']}）")
    print(f"eligible: {plan['eligible']} 个（冻结/已处理/无效类型已排除）")
    print(f"预计选中: {plan['selected_count']} 个（硬配额，top_n 是上限）")
    print()
    print("候选排序（score 降序，含五分量；✓ = 本轮选中）:")
    for r in plan["ranked"]:
        mark = "✓" if r["selected"] else " "
        c = r["components"]
        print(f"  {mark} [{r['score']:5.3f}] {r['candidate_type']:<22} {r['raw_name'][:38]}")
        print(f"      N={c['novelty']:.2f} R={c['relevance']:.2f} E={c['evidence']:.2f} "
              f"S={c['structural']:.2f} C={c['cost']:.2f}  "
              f"~{r['est_queries']:.0f} queries, ~{r['est_cost']:.3f} cost")
    print()
    print(f"预计 queries: {plan['est_queries_total']:.0f}   预计 cost: {plan['est_cost_total']:.3f}")
    print("[plan-only] 未写任何东西。真跑: python tools/discovery_loop.py --rounds 1 --top-n 8")


def _print_round(r, stop: bool, reason: str | None) -> None:
    print(format_round_report(r, compute_round_metrics(r)))
    if stop:
        from search_engine.discovery.stopping import stop_reason_label
        print(f"\n⚠ 停止: {stop_reason_label(reason)}")


def _show_queue() -> None:
    queue = load_queue()
    if not queue:
        print("（approval queue 为空）")
        return
    print(f"approval queue（{len(queue)} 条）:")
    for q in queue:
        print(f"  [{q['status']:<8}] {q['proposal_id']}  "
              f"{q['action']} / {q['candidate_type']}  "
              f"(round {q['created_round']})")
        if q.get("reject_reason"):
            print(f"      拒绝理由: {q['reject_reason']}")


def main():
    ap = argparse.ArgumentParser(description="Phase 2.1 discovery loop（多轮自主发现）")
    ap.add_argument("--rounds", type=int, default=1, help="运行轮数（默认 1）")
    ap.add_argument("--top-n", type=int, default=8, help="每轮最多选几个（默认 8）")
    ap.add_argument("--plan-only", action="store_true", help="只读规划，不写任何东西")
    ap.add_argument("--show-queue", action="store_true", help="查看 approval queue")
    ap.add_argument("--approve", metavar="PROPOSAL_ID", help="逐条批准 proposal")
    ap.add_argument("--reject", metavar="PROPOSAL_ID", help="逐条拒绝 proposal")
    ap.add_argument("--reason", default="", help="approve/reject 理由")
    args = ap.parse_args()

    if args.show_queue:
        _show_queue()
        return
    if args.approve:
        queue = load_queue()
        ok, msg = approve_item(queue, args.approve)
        if ok:
            from search_engine.discovery.approval_queue import save_queue
            save_queue(queue)
        print(("✓ " if ok else "✗ ") + msg)
        return
    if args.reject:
        queue = load_queue()
        ok, msg = reject_item(queue, args.reject, args.reason)
        if ok:
            from search_engine.discovery.approval_queue import save_queue
            save_queue(queue)
        print(("✓ " if ok else "✗ ") + msg)
        return

    ctl = DiscoveryController(top_n=args.top_n)
    if args.plan_only:
        _print_plan(ctl.plan_round())
        return

    # 真跑：默认 verify 只读候选缓存（不烧 API）——无缓存的候选记 candidate_error 继续
    results = ctl.run(max_rounds=args.rounds)
    for i, r in enumerate(results, 1):
        stop = bool(r.stop_reason)
        _print_round(r, stop, r.stop_reason)
        if i < len(results):
            print()
    if any(r.candidate_errors for r in results):
        print("\n候选级错误（不影响整轮）:")
        for r in results:
            for e in r.candidate_errors:
                print(f"  ⚠ {e}")


if __name__ == "__main__":
    main()
