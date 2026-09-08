#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/run_search_s5.py — Search S5 正式执行（2026-08-31 用户冻结口径）。

S5_SEEN = S4_SEEN ∪ QueryResults(36 cross-layer queries)
  Query: 36 frozen cross-layer queries（PA 28 + SF 8）→ depth=1000, skip_cache=True
  无新 citation 层：S4 已含 P2_DIVERSE_64（64 seeds 1-hop）——S5 只加 query 机制。

机制（用户定 2026-08-31，V1→V3 实验链后冻结）：
  PA = Property × Application（primary 直接 anchor；secondary 带 SHRINKAGE_CORE）
  SF = CoreShrinkage × Structure × Formulation（三元；STRUCTURE DIRECT/ANCHORED_ONLY/REJECT）

纪律（写死不可改）：
- 只读冻结配置 s5_final_actions.json（status=FROZEN, 36 actions）；绝不读 residual miss
- R04 miss papers used as term source: FALSE；R04 recovery used for query selection: FALSE
- 正式执行结束只宣布 Search S5 snapshot = FROZEN / S5_SEEN_SET = N，
  然后进入 identity reconciliation → fresh R05（paired：Recall(S4|R05) vs Recall(S5|R05)）

输出：
  s5_candidate_snapshot.json / s5_seen_set.json / s5_query_records.json / s5_delta_vs_s4.json

用法：
  python tools/run_search_s5.py --plan-only         # 冻结校验（不发送不写盘）
  python tools/run_search_s5.py                     # 正式执行（query engine）
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
S4_SEEN = os.path.join(T, "s4_seen_set.json")
DEFAULT_SNAP = os.path.join(T, "s5_candidate_snapshot.json")
DEFAULT_SEEN = os.path.join(T, "s5_seen_set.json")
DEFAULT_Q_REC = os.path.join(T, "s5_query_records.json")
DEFAULT_DELTA = os.path.join(T, "s5_delta_vs_s4.json")
DEFAULT_DEPTH = 1000

Q_EXPECTED = 36
S4_EXPECTED = 19194


def config_hash(cfg: dict) -> str:
    """对冻结 query actions 关键字段计算 sha256（防篡改）。"""
    q = [(a["action_id"], a["query_string"], a.get("bridge_type"))
         for a in cfg["actions"]]
    blob = json.dumps({"query": q}, sort_keys=True,
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def main():
    ap = argparse.ArgumentParser(description="Search S5 正式执行（36 cross-layer queries）")
    ap.add_argument("--config", default=CONFIG)
    ap.add_argument("--s4-seen", default=S4_SEEN)
    ap.add_argument("--depth", type=int, default=DEFAULT_DEPTH)
    ap.add_argument("--snapshot", default=DEFAULT_SNAP)
    ap.add_argument("--seen-set", default=DEFAULT_SEEN)
    ap.add_argument("--query-records", default=DEFAULT_Q_REC)
    ap.add_argument("--delta", default=DEFAULT_DELTA)
    ap.add_argument("--engine-data-dir", default="data")
    ap.add_argument("--plan-only", action="store_true")
    args = ap.parse_args()

    # ── 冻结校验（用户定，写死）──
    cfg = json.load(open(args.config, encoding="utf-8"))
    assert cfg["status"] == "FROZEN", "s5_final_actions 未 FROZEN"
    q_acts = cfg["actions"]
    assert len(q_acts) == Q_EXPECTED, f"query actions != {Q_EXPECTED}"
    assert args.depth == 1000, "S5 正式 depth 必须 = 1000（基础设施参数冻结）"
    ch = config_hash(cfg)
    s4_keys = set(json.load(open(args.s4_seen, encoding="utf-8"))["keys"])
    assert len(s4_keys) == S4_EXPECTED, \
        f"S4 seen != {S4_EXPECTED}（实测 {len(s4_keys)}——必须 canonical 19194，非 nominal 19445）"

    print("=" * 78)
    print("Search S5 正式执行（冻结口径校验通过）")
    print("=" * 78)
    print(f"S5 query actions    = {len(q_acts)}（PA {sum(1 for a in q_acts if a.get('bridge_type')=='PA')}"
          f" + SF {sum(1 for a in q_acts if a.get('bridge_type')=='SF')}）")
    print(f"query depth         = {args.depth}")
    print(f"base S4 seen        = {len(s4_keys)}（canonical）")
    print(f"config hash         = {ch[:16]}...")
    print(f"disciplines         = R04 miss as term source: FALSE | R04 recovery for selection: FALSE")

    if args.plan_only:
        print("\n[plan-only] 冻结校验通过，将执行（不发送不写盘）：")
        for q in q_acts:
            print(f"    {q['action_id']:<10}[{q.get('bridge_type')}] {q['query_string'][:70]}")
        print("\n[plan-only] 结束：未发送任何请求，未写盘。")
        return

    # ── Query 层（engine，depth=1000 skip_cache=True）──
    q_rows_by_query = asyncio.run(run_query_layer(q_acts, args, s4_keys))

    # ── 汇总 ──
    q_keys = set()
    for rows in q_rows_by_query.values():
        for r in rows:
            if r.get("key"):
                q_keys.add(r["key"])
    q_new = q_keys - s4_keys
    s5_keys = s4_keys | q_new

    aggregate = {
        "s4_seen_unique": len(s4_keys),
        "query_union_unique": len(q_keys),
        "query_new_vs_s4": len(q_new),
        "s5_new_vs_s4": len(q_new),
        "search_s5_union_unique": len(s5_keys),
        "identity_unknown_query": sum(
            1 for rows in q_rows_by_query.values()
            for r in rows if not r.get("key")),
    }
    # 硬校验（用户定）：|S5| == |S4| + |S5\S4|
    assert len(s5_keys) == len(s4_keys) + len(q_new), \
        f"union invariant broken: {len(s5_keys)} != {len(s4_keys)} + {len(q_new)}"
    print(f"\n[assert] search_s5_union_unique == {len(s4_keys)} + {len(q_new)} "
          f"= {len(s4_keys) + len(q_new)} ✓")

    snap_id = f"S5_{datetime.datetime.now():%Y%m%d_%H%M%S}_{ch[:8]}"
    now = datetime.datetime.now().isoformat(timespec="seconds")

    delta = {
        "s4_seen": len(s4_keys), "s5_seen": len(s5_keys),
        "query_new_vs_s4": len(q_new),
        "s4_only": len(s4_keys - s5_keys),
        "note": "S5 = S4 ∪ QueryResults(36 cross-layer)；S4_SEEN 保留冻结不动",
    }

    snapshot = {
        "search_snapshot_id": snap_id,
        "version": "S5", "status": "FROZEN",
        "config_file": os.path.basename(args.config),
        "config_hash": ch,
        "retrieval_budget": {"query_depth": args.depth,
                             "pagination": "Scopus cursor 25/page",
                             "citation_layer": "none（S4 已含 P2_DIVERSE_64）"},
        "execution_timestamp": now,
        "development_source": "AUDIT_R04",
        "eligible_for_r05_evaluation": False,
        "identity_rule": "query: EID 优先 + DOI fallback（R05 found sets 由 s5_query_records 三通道构建）",
        "aggregate": aggregate,
        "delta_vs_s4": delta,
        "mechanisms": cfg.get("mechanisms"),
        "disciplines": cfg.get("disciplines"),
        "interpretation": "S5 是 cross-layer query mechanism（V1→V3 实验链后冻结："
                          "KnownRelevantKnowledge 跨层关系 + 目标性质约束）；snapshot 冻结后"
                          "不再改 36 actions；正式召回率必须 fresh R05 paired",
    }
    seen_set = {
        "search_snapshot_id": snap_id, "config_hash": ch,
        "frozen_at": now,
        "definition": "SEARCH_S5_SEEN_UNION = S4_SEEN(19194) ∪ QueryResults(36, depth=1000)；"
                      "R05 agent_seen 唯一依据",
        "size": len(s5_keys), "keys": sorted(s5_keys),
    }
    for path, obj in ((args.snapshot, snapshot), (args.seen_set, seen_set),
                      (args.query_records, {
                          "search_snapshot_id": snap_id, "config_hash": ch,
                          "frozen_at": now,
                          "n_queries": len(q_rows_by_query),
                          "records_by_query": q_rows_by_query}),
                      (args.delta, {"search_snapshot_id": snap_id,
                                    "delta_vs_s4": delta})):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)

    print(f"\n[OK] snapshot      : {args.snapshot}")
    print(f"[OK] seen set      : {args.seen_set}")
    print(f"[OK] query records : {args.query_records}")
    print(f"[OK] delta vs S4   : {args.delta}")
    print("\n=== S5 aggregate ===")
    for k, v in aggregate.items():
        print(f"  {k:<26} {v}")
    print("\n=== Search S5 = FROZEN ===")
    print(f"S5_SEEN_SET = {len(s5_keys)}")
    print("→ 下一步：identity reconciliation → S5 canonical seen → freeze → "
          "fresh R05 paired（Recall(S4|R05) vs Recall(S5|R05)）")


async def run_query_layer(q_acts: list[dict], args, s4_keys: set) -> dict:
    from search_engine.engine import ScopusSearchEngine
    resolver, r_old_keys = build_r_old()
    engine = ScopusSearchEngine(data_dir=args.engine_data_dir)
    await engine.start()
    out = {}
    try:
        for i, q in enumerate(q_acts, 1):
            try:
                # 强制 skip_cache：防 depth<1000 缓存命中（S1 depth=50 教训）
                res = await engine.search(q["query_string"], limit=args.depth,
                                          skip_cache=True)
            except Exception as e:
                print(f"    [WARN] {q['action_id']}: S5 检索失败 {e}")
                q["error"] = str(e)
                await asyncio.sleep(1)
                continue
            papers = res.papers
            total_hits = getattr(res, "total_count", len(papers))
            rows = []
            for p in papers:
                key, info = paper_key_and_info(p, resolver, r_old_keys)
                rows.append({"key": key, "eid": info["eid"], "doi": info["doi"],
                             "title": (getattr(p, "title", None) or "").strip(),
                             "usable": is_usable(
                                 (getattr(p, "title", None) or "").strip(),
                                 (getattr(p, "abstract", None) or "").strip(),
                                 bool(key))})
            keys = {r["key"] for r in rows if r["key"]}
            q.update({
                "total_hits": total_hits, "raw_returned": len(papers),
                "unique_returned": len(keys),
                "identity_unknown": sum(1 for r in rows if not r["key"]),
                "usable_returned": sum(1 for r in rows if r["usable"]),
                "new_vs_S4": len(keys - s4_keys),
                "depth_saturated": (len(papers) >= args.depth
                                    and total_hits > args.depth),
            })
            out[q["query_string"]] = rows
            cen = "C" if q["depth_saturated"] else " "
            print(f"  [{i}/{len(q_acts)}]{cen} {q['action_id']:<10} "
                  f"hits={total_hits:>6} raw={len(papers):>4} uniq={len(keys):>4} "
                  f"new_S4={len(keys - s4_keys):>4} {q['query_string'][:46]}")
            await asyncio.sleep(1)
    finally:
        await engine.close()
    return out


if __name__ == "__main__":
    main()
