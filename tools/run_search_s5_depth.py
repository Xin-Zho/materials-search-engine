#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/run_search_s5_depth.py — S5 Retrieval-Depth 实验（B 语义，2026-09-01 用户拍板）。

问题：R05 的 46% recall 到底是 Query Generator 不够好，还是 query 没被搜深
（A 语义 depth=1000 = raw top-1000 截断长尾）？

方案（用户定）：
  A → B：depth 语义从 "top-N raw" 改为 "N new（相对基准 seen）"。
  S5 的 36 条 query 完全冻结（query_string 不动，config hash 校验），
  只做一个 retrieval-depth 实验：每条 query 深翻页直到 new_vs_S5 >= 1000
  或翻页耗尽（Scopus 深 offset 上限 ~5000）。

设计要点：
- new 判定基准 = S5_SEEN（20417 keys，canonical 后 key 级）——回答
  "在 S5 基础上搜深能额外捞多少"（S5_DEPTH_SEEN = S5_SEEN ∪ new）
- 从 offset=1000 起翻页（S5_SEEN 已含 top-1000 全部有 key 论文，offset=0
  页对 new_vs_S5 贡献为 0——省 1/5 时间），page=1000，最多 4 页（1000/2000/3000/4000）
- engine.search(skip_cache=True, write_cache=False)：深 offset 页与 top-N 页共用
  query 缓存 key，写缓存会覆盖正确 top-N 结果并污染后续 pilot QA（engine.py
  2026-09-01 加 write_cache 参数）
- 实验只产出 s5_depth_experiment.json + s5_depth_seen_set.json（实验 snapshot），
  不改正式 s5_seen_set.json（S5 冻结产物不动）

输出：
  s5_depth_experiment.json
    per-query：翻页数 / raw 累计 / unique 累计 / new_vs_S5 累计 / target 达标 / hits
    aggregate：额外 raw 总量 / 额外 new 总量 / S5_DEPTH_SEEN / 达标 query 数
  s5_depth_seen_set.json（实验 seen，非正式；正式需 identity reconciliation）

用法：
  python tools/run_search_s5_depth.py --plan-only    # 冻结校验（不发送不写盘）
  python tools/run_search_s5_depth.py                # 正式实验（query engine）
"""
import argparse
import asyncio
import datetime
import hashlib
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tools"))

from pilot_round3_query_utility import (  # noqa: E402
    IdentityResolver, build_r_old, connect_cache_ro, paper_key_and_info,
)
from build_r02_seen import _norm_doi, _norm_title  # noqa: E402
from run_search_s1 import is_usable  # noqa: E402

T = os.path.join(BASE, "data", "exports", "terminology")
CONFIG = os.path.join(T, "s5_final_actions.json")
S5_SEEN = os.path.join(T, "s5_seen_set.json")
DEFAULT_OUT = os.path.join(T, "s5_depth_experiment.json")
DEFAULT_SEEN_OUT = os.path.join(T, "s5_depth_seen_set.json")

Q_EXPECTED = 36
S5_EXPECTED = 20417
DEFAULT_TARGET = 1000    # B 语义：new-N 的 N
DEFAULT_PAGE = 1000      # 每页 raw 量（Scopus itemCount）
DEFAULT_MAX_OFFSET = 4000  # 深 offset 上限 ~5000（engine 注释）；最后一页覆盖 4001-5000
OFFSET_START = 1000      # S5_SEEN 已含 top-1000 有 key 论文，直接跳过 offset=0 页


def config_hash(cfg: dict) -> str:
    """对冻结 query actions 关键字段计算 sha256（与 run_search_s5 同口径，防篡改）。"""
    q = [(a["action_id"], a["query_string"], a.get("bridge_type"))
         for a in cfg["actions"]]
    blob = json.dumps({"query": q}, sort_keys=True,
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def main():
    ap = argparse.ArgumentParser(
        description="S5 Retrieval-Depth 实验（B 语义：new-N 翻页；36 条冻结 query）")
    ap.add_argument("--config", default=CONFIG)
    ap.add_argument("--s5-seen", default=S5_SEEN)
    ap.add_argument("--target", type=int, default=DEFAULT_TARGET,
                    help="B 语义 new 目标数（depth 语义 = N new）")
    ap.add_argument("--page", type=int, default=DEFAULT_PAGE)
    ap.add_argument("--max-offset", type=int, default=DEFAULT_MAX_OFFSET)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--seen-out", default=DEFAULT_SEEN_OUT)
    ap.add_argument("--engine-data-dir", default="data")
    ap.add_argument("--plan-only", action="store_true")
    args = ap.parse_args()

    # ── 冻结校验（query 完全不动）──
    cfg = json.load(open(args.config, encoding="utf-8"))
    assert cfg["status"] == "FROZEN", "s5_final_actions 未 FROZEN"
    q_acts = cfg["actions"]
    assert len(q_acts) == Q_EXPECTED, f"query actions != {Q_EXPECTED}"
    ch = config_hash(cfg)
    s5_keys = set(json.load(open(args.s5_seen, encoding="utf-8"))["keys"])
    assert len(s5_keys) == S5_EXPECTED, \
        f"S5 seen != {S5_EXPECTED}（实测 {len(s5_keys)}——必须 canonical 20417）"

    print("=" * 78)
    print("S5 Retrieval-Depth 实验（B 语义：new-N 翻页）")
    print("=" * 78)
    print(f"S5 query actions    = {len(q_acts)}（冻结不动，hash={ch[:12]}...）")
    print(f"new 基准            = S5_SEEN {len(s5_keys)}（canonical 20417）")
    print(f"B 语义 depth        = 每 query 目标 new_vs_S5 >= {args.target}")
    print(f"翻页                = offset {OFFSET_START} 起，page={args.page}，"
          f"max-offset={args.max_offset}（Scopus 深 offset 上限 ~5000）")
    print(f"缓存保护            = skip_cache=True, write_cache=False（防深页覆盖 top-N 缓存）")
    print(f"不动               = 正式 s5_seen_set.json / s5_final_actions.json")

    if args.plan_only:
        print("\n[plan-only] 校验通过，将执行（不发送不写盘）：")
        for q in q_acts:
            print(f"    {q['action_id']:<10}[{q.get('bridge_type')}] {q['query_string'][:70]}")
        print("\n[plan-only] 结束：未发送任何请求，未写盘。")
        return

    # ── B 语义翻页检索（v2：canonical 三通道 new 判定）──
    per_query, canon_new_keys = asyncio.run(run_depth_layer(q_acts, args, s5_keys))

    # ── 汇总 ──
    extra_raw = sum(q["raw_pages_total"] for q in per_query.values())
    extra_new = len(canon_new_keys)   # canonical 三通道未见（论文级，R05 seen join 同口径）
    n_reached = sum(1 for q in per_query.values() if q["target_reached"])
    s5_depth_union = s5_keys | canon_new_keys

    aggregate = {
        "semantics": "B（new-N 翻页，相对 S5_SEEN）",
        "query_count": len(q_acts),
        "target_new_per_query": args.target,
        "extra_raw_pages_total": extra_raw,
        "extra_new_vs_S5_canonical": extra_new,
        "s5_depth_union_keys": len(s5_depth_union),
        "s5_seen": len(s5_keys),
        "queries_target_reached": n_reached,
        "queries_exhausted": len(q_acts) - n_reached,
        "growth_vs_S5": round((len(s5_depth_union) - len(s5_keys)) / len(s5_keys), 4),
        "status": "PRELIMINARY（2026-09-01 v2：canonical 三通道 new 判定已修——旧 key 级判定"
                  "把 S5 canonical 合并掉的 alias key 误判为 new，实测 145 = 旧 nominal−canonical"
                  " gap 指纹；offset 生效性待 probe_offset_compare.py 实测确认）",
        "answer": "A→B 语义下 36 条冻结 query 能额外捞多少 canonical new"
                  "（是否证明 depth 截断长尾）",
    }
    # 硬校验：|S5_DEPTH| == |S5| + extra_new（canonical）
    assert len(s5_depth_union) == len(s5_keys) + extra_new, \
        f"union invariant broken: {len(s5_depth_union)} != {len(s5_keys)} + {extra_new}"
    print(f"\n[assert] s5_depth_union == {len(s5_keys)} + {extra_new} "
          f"= {len(s5_depth_union)} ✓")

    now = datetime.datetime.now().isoformat(timespec="seconds")
    exp = {
        "version": "s5_depth_experiment_v1",
        "created_at": now,
        "config_hash": ch,
        "semantics": "B: depth = N new（相对 S5_SEEN 20417），非 A: top-N raw",
        "query_frozen": {"source": "s5_final_actions.json", "n": Q_EXPECTED,
                         "hash": ch[:16]},
        "design": {
            "offset_start": OFFSET_START, "page": args.page,
            "max_offset": args.max_offset, "target_new": args.target,
            "why_skip_offset_0": "S5_SEEN 已含 A 语义 top-1000 全部有 key 论文，"
                                 "offset=0 页对 new_vs_S5 贡献为 0",
            "cache_protection": "skip_cache=True + write_cache=False（engine.py 新参数）",
        },
        "aggregate": aggregate,
        "per_query": per_query,
        "interpretation": "如果 extra_new 大 → 46% 的瓶颈含 depth 截断（长尾相关文献在 "
                          "top-1000 外）；如果 extra_new≈0 → 瓶颈在 query 本身。"
                          "实验 seen 非正式，正式需 identity reconciliation + fresh audit",
    }
    seen_out = {
        "version": "s5_depth_seen_set_v1", "created_at": now,
        "status": "EXPERIMENT（非正式 S5_SEEN）", "config_hash": ch,
        "definition": "S5_SEEN(20417) ∪ B 语义深翻页 new（key 级，未 canonical 去重）",
        "size": len(s5_depth_union), "keys": sorted(s5_depth_union),
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(exp, f, ensure_ascii=False, indent=1)
    with open(args.seen_out, "w", encoding="utf-8") as f:
        json.dump(seen_out, f, ensure_ascii=False, indent=1)

    print(f"\n[OK] 实验报告 : {args.out}")
    print(f"[OK] 实验 seen : {args.seen_out}")
    print("\n=== S5 Depth 实验 aggregate ===")
    for k, v in aggregate.items():
        print(f"  {k:<28} {v}")
    print("\n=== 回答 ===")
    print(f"S5_SEEN 20417 → B 语义深翻页后 key 级 union = {len(s5_depth_union)}"
          f"（额外 new {extra_new}，{n_reached}/{len(q_acts)} query 达标）")


async def run_depth_layer(q_acts: list[dict], args, s5_keys: set) -> tuple[dict, set]:
    """B 语义翻页：每 query 从 offset=1000 起翻页直到 new_vs_S5>=target 或翻页耗尽。

    2026-09-01 v2：new 判定升级为 canonical 三通道（build_s5_found_sets 的
    eids/dois/titles，与 R05 seen join 同口径）——旧版 key 级判定把 S5 canonical
    合并掉的 alias key 误判为 new（实测 145 = 旧 nominal−canonical gap 指纹）。
    key 级只作 raw 统计。
    """
    from search_engine.engine import ScopusSearchEngine
    from build_r05_seen import build_s5_found_sets
    from build_r02_seen import _norm_title
    found5 = build_s5_found_sets()
    f_eids, f_dois, f_titles = found5["eids"], found5["dois"], found5["titles"]
    resolver, r_old_keys = build_r_old()
    engine = ScopusSearchEngine(data_dir=args.engine_data_dir)
    await engine.start()
    per_query = {}
    depth_keys = set()
    canon_new_keys = set()
    try:
        for i, q in enumerate(q_acts, 1):
            qs = q["query_string"]
            new_count = 0          # canonical 三通道未见（论文级）
            canon_new_this = set()
            raw_total = 0
            pages = 0
            offset = OFFSET_START
            reached = False
            exhausted = False
            try:
                while new_count < args.target and offset <= args.max_offset:
                    res = await engine.search(qs, limit=args.page, offset=offset,
                                              skip_cache=True, write_cache=False)
                    papers = res.papers
                    total_hits = getattr(res, "total_count", len(papers))
                    if not papers:
                        exhausted = True
                        break
                    rows = []
                    for p in papers:
                        key, info = paper_key_and_info(p, resolver, r_old_keys)
                        eid = info["eid"]
                        doi = (info["doi"] or "").lower().replace("https://doi.org/", "")
                        title = (getattr(p, "title", None) or "").strip()
                        rows.append({"key": key, "eid": eid, "doi": doi,
                                     "title": title})
                    # canonical 三通道未见判定（R05 seen join 同口径）
                    for r in rows:
                        seen = (r["eid"] and r["eid"] in f_eids) or \
                               (r["doi"] and r["doi"] in f_dois) or \
                               (r["title"] and _norm_title(r["title"]) in f_titles)
                        if not seen and r["key"]:
                            canon_new_this.add(r["key"])
                    page_keys = {r["key"] for r in rows if r["key"]}
                    depth_keys |= page_keys          # 累计（含已 seen，供 union 统计）
                    new_count = len(canon_new_this)
                    canon_new_keys |= canon_new_this
                    raw_total += len(papers)
                    pages += 1
                    offset += args.page
                    if len(papers) < args.page or offset >= total_hits:
                        exhausted = True
                        break
                reached = new_count >= args.target
            except Exception as e:
                q["error"] = str(e)
                print(f"    [WARN] {q['action_id']}: 深翻页失败 {e}")
                await asyncio.sleep(1)
            per_query[q["action_id"]] = {
                "query_string": qs, "bridge_type": q.get("bridge_type"),
                "pages_fetched": pages, "raw_pages_total": raw_total,
                "new_vs_S5_key_level": len(page_keys - s5_keys - depth_keys)
                if "page_keys" in locals() else 0,
                "new_vs_S5_canonical": new_count,
                "target_reached": reached, "exhausted": exhausted,
                "error": q.get("error"),
            }
            flag = "D" if reached else ("X" if exhausted else "E")
            print(f"  [{i}/{len(q_acts)}]{flag} {q['action_id']:<10} "
                  f"pages={pages:>2} raw={raw_total:>4} canon_new={new_count:>4} "
                  f"{qs[:46]}")
            await asyncio.sleep(1)
    finally:
        await engine.close()
    return per_query, canon_new_keys


if __name__ == "__main__":
    main()
