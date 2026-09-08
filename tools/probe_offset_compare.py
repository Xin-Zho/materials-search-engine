#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/probe_offset_compare.py — Scopus offset 生效性实测（2026-09-01 用户要求）。

背景：S5 depth 实验（B 语义翻页）结果异常——offset=1000 起的 raw 与 S5 正式
（offset=0 top-1000）逐条完全一致（PA_004 277=277 / PA_008 789=789 ...），
怀疑 Scopus bulk CSV export API 忽略 resultSet.offset，永远返回第一页。

本工具：对同一 query 分别导出 offset=0 与 offset=1000，对比：
  total_hits / requested_offset / returned_count / first 10 (EID, DOI, title)
判定：
  两页前 10 完全重叠  → offset 被忽略（B 语义翻页在 export API 上不可行）
  完全不同          → offset 生效（depth 实验本身需 canonical 重判）

用法（需已登录 Scopus 会话）：
  python tools/probe_offset_compare.py --action-id PA_008 [--offset 1000] [--limit 10]
默认用 s5_final_actions.json 里 PA_008 的 query_string（36 条冻结 query 之一）。
"""
import argparse
import asyncio
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tools"))

T = os.path.join(BASE, "data", "exports", "terminology")
ACTIONS = os.path.join(T, "s5_final_actions.json")


async def main_async(args):
    from search_engine.engine import ScopusSearchEngine

    acts = json.load(open(ACTIONS, encoding="utf-8"))["actions"]
    q = next((a for a in acts if a["action_id"] == args.action_id), None)
    if q is None:
        raise SystemExit(f"✗ 未找到 action_id={args.action_id}（可用: "
                         f"{[a['action_id'] for a in acts][:8]}...）")
    qs = q["query_string"]
    print(f"[query] {q['action_id']} | {qs[:80]}")

    engine = ScopusSearchEngine(data_dir=args.engine_data_dir)
    await engine.start()
    try:
        for off in (0, args.offset):
            res = await engine.search(qs, limit=args.limit, offset=off,
                                      skip_cache=True, write_cache=False)
            papers = res.papers
            total = getattr(res, "total_count", len(papers))
            print(f"\n=== offset={off} ===")
            print(f"  total_hits       = {total}")
            print(f"  requested_offset = {off}")
            print(f"  returned_count   = {len(papers)}")
            print(f"  first_result_id  = {getattr(papers[0], 'paper_id', '?') if papers else '-'}")
            print(f"  last_result_id   = {getattr(papers[-1], 'paper_id', '?') if papers else '-'}")
            for i, p in enumerate(papers[:args.limit], 1):
                doi = (getattr(p, "doi", None) or "").replace("https://doi.org/", "")
                print(f"    {i:>2}. EID={getattr(p, 'paper_id', '?')} "
                      f"DOI={doi[:36]:<38} {(getattr(p, 'title', None) or '')[:60]}")
            globals()[f"papers_{off}"] = [getattr(p, "paper_id", "?") for p in papers]
    finally:
        await engine.close()

    ids0 = globals().get("papers_0", [])
    ids1 = globals().get(f"papers_{args.offset}", [])
    n = min(len(ids0), len(ids1))
    overlap = len(set(ids0[:n]) & set(ids1[:n]))
    print(f"\n=== 判定 ===")
    print(f"offset=0 vs offset={args.offset} 前 {n} 条 EID 重叠: {overlap}/{n}")
    if overlap == n and n > 0:
        print("→ ❌ OFFSET_IGNORED：offset 未生效，export API 返回同一批第一页结果")
        print("  B 语义（new-N 翻页）在 Scopus bulk CSV export API 上不可行，"
              "depth 实验结论 INVALID")
    elif overlap == 0 and n > 0:
        print("→ ✅ OFFSET_EFFECTIVE：offset 生效，深页内容不同")
        print("  depth 实验的 raw 一致是另一回事（需查翻页循环 bug）；"
              "new 判定必须 canonical 化（identity manifest）")
    else:
        print(f"→ ⚠️ 部分重叠 {overlap}/{n}——需要人工检查排序稳定性（probe E4 结论）")


def main():
    ap = argparse.ArgumentParser(description="Scopus offset 生效性实测")
    ap.add_argument("--action-id", default="PA_008",
                    help="s5_final_actions.json 里的 action_id（默认 PA_008）")
    ap.add_argument("--offset", type=int, default=1000, help="深 offset（默认 1000）")
    ap.add_argument("--limit", type=int, default=10, help="每页导出条数（默认 10）")
    ap.add_argument("--engine-data-dir", default="data")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
