#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/run_s6_pilot.py — Search S6 pilot retrieval（2026-09-04 用户定，development 非正式）。

S6_PILOT = S5_SEEN ∪ QueryResults(17 s6 semantic bridge actions)
  Query: 17 frozen actions（A8/B2/C5/D2；anchor_free 12）→ depth 参数化，skip_cache=True
  base   : S5 canonical seen（20417）——pilot 的 new_vs_S5 判定基准

性质（纪律，写死）：
- 这是 DEVELOPMENT PILOT：验证 anchor-free semantic bridge 的 new/yield 趋势，
  不是正式 S6 snapshot。freeze S6 在 pilot + candidate QA + 分层分析之后（另行执行）。
- 只读冻结配置 s6_bridge_queries.json（17 actions）+ s6_freeze_manifest.json（sha256 校验防篡改）。
- R04/R05 miss 不供词；query 内容词 = 40 VERIFIED_EXACT ∪ 冻结 context（assembler 已断言）。
- 输出 per-query records，供 analyze_s6_pilot.py 做 family/anchor/overlap 分层。

用法：
  python tools/run_s6_pilot.py --plan-only          # 冻结校验（不发送不写盘）
  python tools/run_s6_pilot.py                      # pilot retrieval（depth=1000）
  python tools/run_s6_pilot.py --depth 500          # 小 budget 预跑（趋势用，不比正式）

输出：data/exports/terminology/s6_pilot_query_records.json
      data/exports/terminology/s6_pilot_delta_vs_s5.json
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
    IdentityResolver, build_r_old, paper_key_and_info,
)
from run_search_s1 import is_usable  # noqa: E402
from search_engine.topic_config import DEFAULT_TOPIC, resolve_input, resolve_output  # noqa: E402

T = os.path.join(BASE, "data", "exports", "terminology")
CONFIG = os.path.join(T, "s6_bridge_queries.json")
MANIFEST = os.path.join(T, "s6_freeze_manifest.json")
S5_SEEN = os.path.join(T, "s5_seen_set.json")
# P0-2: 以下为 v1.0 legacy 冻结原址；非 legacy topic 自动路由 topics/<id>/runs/ 通用名
DEFAULT_Q_REC = os.path.join(T, "s6_pilot_query_records.json")
DEFAULT_DELTA = os.path.join(T, "s6_pilot_delta_vs_s5.json")
DEFAULT_ZERO_HITS = os.path.join(T, "s6_pilot_zero_hits.json")
DEFAULT_DEPTH = 1000

Q_EXPECTED = 17
S5_EXPECTED = 20417
MANIFEST_VERSION = "S6_FREEZE_MANIFEST_V1"


def sha256_file(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def main():
    ap = argparse.ArgumentParser(description="S6 pilot retrieval（17 semantic bridge actions, development）")
    ap.add_argument("--topic", default=None,
                    help="topic_id（默认 v1.0 legacy 主题；输出自动路由 topics/<id>/runs/）")
    ap.add_argument("--config", default=CONFIG)
    ap.add_argument("--s5-seen", default=S5_SEEN)
    ap.add_argument("--manifest", default=MANIFEST)
    ap.add_argument("--depth", type=int, default=DEFAULT_DEPTH)
    ap.add_argument("--query-records", default=None,
                    help="默认: pc001 legacy → data/exports/terminology/s6_pilot_query_records.json；"
                         "新主题 → topics/<topic>/runs/pilot_query_records.json")
    ap.add_argument("--delta", default=None)
    ap.add_argument("--engine-data-dir", default="data")
    ap.add_argument("--live", action="store_true",
                    help="允许真实 Scopus 检索写缓存（P0-2 默认 dry-run：防误跑）")
    ap.add_argument("--plan-only", action="store_true")
    ap.add_argument("--from-cache", action="store_true",
                    help="从 scopus_cache.db 重放（不启动浏览器不重抓 Scopus）。"
                         "用于 live 跑崩在汇总后救回 records——17 条结果已写缓存")
    ap.add_argument("--only", default=None,
                    help="live 补跑单条：逗号分隔 action_id（如 S6-C-14）。"
                         "只检索指定 query 写缓存；随后 --from-cache 全量重建 records")
    ap.add_argument("--zero-hits", default=None,
                    help="0-hit query 登记文件（live 验证 hits=0 的 action_id 清单）。"
                         "engine 语义：total_count==0 不写 api_cache（L198），from-cache 无法"
                         "重放 → 经此登记恢复为 ZERO_HIT record，而非 CACHE_MISS")
    args = ap.parse_args()

    # ── P0-2: 主题命名空间（输出默认落点随 topic；pc001 legacy → v1.0 冻结原址）──
    if not args.topic:
        args.topic = DEFAULT_TOPIC
    if not args.query_records:
        args.query_records = resolve_output(args.topic, DEFAULT_Q_REC,
                                            "pilot_query_records.json")
    if not args.delta:
        args.delta = resolve_output(args.topic, DEFAULT_DELTA, "pilot_delta.json")
    if not args.zero_hits:
        args.zero_hits = resolve_output(args.topic, DEFAULT_ZERO_HITS,
                                        "pilot_zero_hits.json")

    # ── --live 门禁（P0-2 默认 dry-run；真实 Scopus 检索须显式 --live）──
    if not args.plan_only and not args.live and not args.from_cache:
        raise SystemExit("[dry-run] 真实 Scopus 检索被禁止：传 --live 执行，"
                         "--plan-only 预览，或 --from-cache 缓存重放")

    # ── 冻结校验 ──
    cfg = json.load(open(args.config, encoding="utf-8"))
    q_acts = cfg["actions"]
    assert len(q_acts) == Q_EXPECTED, f"actions != {Q_EXPECTED}"
    assert cfg["family_counts"] == {"A": 8, "B": 2, "C": 5, "D": 2}, "family 分布漂移"
    assert cfg["anchor_free_actions"] == 12, "anchor-free 数漂移"
    # manifest 防篡改：本 config 文件 sha256 必须 == freeze manifest 记录
    mf = json.load(open(args.manifest, encoding="utf-8"))
    assert mf["manifest_version"] == MANIFEST_VERSION
    rec = next(f for f in mf["files"] if f["path"].endswith("s6_bridge_queries.json"))
    assert sha256_file(args.config) == rec["sha256"], \
        f"[篡改] s6_bridge_queries.json hash 失配 freeze manifest（{rec['sha256'][:12]}...）"
    ch = sha256_file(args.config)

    s5_keys = set(json.load(open(args.s5_seen, encoding="utf-8"))["keys"])
    assert len(s5_keys) == S5_EXPECTED, \
        f"S5 seen != {S5_EXPECTED}（实测 {len(s5_keys)}——必须 canonical 20417）"

    print("=" * 78)
    print("S6 pilot retrieval（DEVELOPMENT，非正式 snapshot）")
    print("=" * 78)
    print(f"S6 actions         = {len(q_acts)}（A8/B2/C5/D2；anchor_free {cfg['anchor_free_actions']}；"
          f"exploratory {cfg['exploratory_actions']}）")
    print(f"pilot depth        = {args.depth}")
    print(f"base S5 seen       = {len(s5_keys)}（canonical）")
    print(f"config sha256      = {ch[:16]}...（freeze manifest 校验 ✓）")
    print(f"disciplines        = R04/R05 miss 不供词 | VERIFIED_EXACT 唯一词源 | 16 SEMANTIC secondary-only")

    if args.plan_only:
        print("\n[plan-only] 冻结校验通过，将执行（不发送不写盘）：")
        for q in q_acts:
            tag = " [EXP]" if q.get("exploratory") else ""
            anc = "anchor" if q["contains_shrinkage_anchor"] else "free  "
            print(f"    {q['action_id']:<10}[{q['family']}] {anc}{tag} {q['query_string'][:62]}")
        print("\n[plan-only] 结束：未发送任何请求，未写盘。")
        return

    # ── Query 层 ──
    mode = "from-cache 重放（不启动浏览器）" if args.from_cache else "live Scopus（skip_cache=True）"
    print(f"pilot mode         = {mode}")
    if args.only:
        # --only：冻结校验后过滤，只检索指定 query（补跑用，写缓存供 from-cache 全量重建）
        wanted = {x.strip() for x in args.only.split(",")}
        avail = {q["action_id"] for q in q_acts}
        assert wanted <= avail, f"--only 含未知 action_id: {wanted - avail}"
        q_acts = [q for q in q_acts if q["action_id"] in wanted]
        print(f"[--only] 只检索 {len(q_acts)} 条: {sorted(wanted)}"
              f"（其它 query 不发送；随后 --from-cache 全量重建）")
    q_rows_by_query = asyncio.run(run_query_layer(q_acts, args, s5_keys,
                                                  from_cache=args.from_cache))

    # ── 汇总（records_by_query value = wrapper dict {…, 'rows': [...]}）──
    def iter_all_rows():
        for wrap in q_rows_by_query.values():
            for r in wrap.get("rows", []):
                yield r

    q_keys = set()
    for r in iter_all_rows():
        if r.get("key"):
            q_keys.add(r["key"])
    q_new = q_keys - s5_keys

    cache_miss = [aid for aid, w in q_rows_by_query.items() if w.get("cache_miss")]
    aggregate = {
        "pilot_query_union_unique": len(q_keys),
        "pilot_new_vs_s5": len(q_new),
        "status": "DEVELOPMENT_PILOT",
        "identity_unknown_query": sum(1 for r in iter_all_rows() if not r.get("key")),
        "cache_miss_queries": cache_miss,
    }
    if cache_miss:
        print(f"\n[WARN] {len(cache_miss)} 条 query 缓存缺失（需 live 补跑）：{cache_miss}")
    now = datetime.datetime.now().isoformat(timespec="seconds")

    delta = {
        "s5_seen": len(s5_keys),
        "pilot_query_union": len(q_keys),
        "pilot_new_vs_s5": len(q_new),
        "note": "S6 pilot = S5_SEEN ∪ QueryResults(17)；pilot 非正式，不产生新 seen set；"
                "freeze S6 后才出正式 s6_seen_set",
    }

    for path, obj in ((args.query_records, {
        "round": "S6_PILOT", "status": "DEVELOPMENT_PILOT",
        "config_hash": ch, "pilot_at": now, "depth": args.depth,
        "n_queries": len(q_rows_by_query),
        "records_by_query": q_rows_by_query}),
        (args.delta, {"round": "S6_PILOT", "pilot_at": now,
                      "delta_vs_s5": delta})):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)

    print(f"\n[OK] query records : {args.query_records}")
    print(f"[OK] delta vs S5   : {args.delta}")
    print("\n=== S6 pilot aggregate（development）===")
    for k, v in aggregate.items():
        print(f"  {k:<24} {v}")
    print("\n→ 下一步：candidate QA（relevance labels）→ analyze_s6_pilot.py 分层分析"
          "（anchor-free vs anchor / family / pairwise overlap）→ freeze S6 → fresh R06")


async def run_query_layer(q_acts: list[dict], args, s5_keys: set,
                          from_cache: bool = False) -> dict:
    from search_engine.engine import ScopusSearchEngine
    resolver, r_old_keys = build_r_old()
    engine = ScopusSearchEngine(data_dir=args.engine_data_dir)
    if not from_cache:
        await engine.start()
    out = {}
    try:
        for i, q in enumerate(q_acts, 1):
            try:
                if from_cache:
                    # 只读缓存重放：命中则构 rows，miss 则标 cache_miss 跳过（不重抓）
                    cached = engine.cache.get_cached_result(q["query_string"])
                    if cached is None:
                        zid = q["action_id"]
                        zero = {}
                        if os.path.exists(args.zero_hits):
                            zero = json.load(open(args.zero_hits, encoding="utf-8"))\
                                .get("queries", {})
                        if zid in zero:
                            # live 已验证 hits=0（engine 不缓存空结果 → 显式登记恢复）
                            vat = zero[zid].get("verified_at", "?")
                            print(f"  [{i}/{len(q_acts)}] {q['action_id']:<10} "
                                  f"ZERO_HIT（登记 live 验证 hits=0 @{vat}）")
                            out[q["action_id"]] = {
                                "action_id": q["action_id"], "family": q["family"],
                                "domain": q["domain"], "strategy": q["strategy"],
                                "exploratory": q.get("exploratory", False),
                                "contains_shrinkage_anchor": q["contains_shrinkage_anchor"],
                                "query_string": q["query_string"], "cache_miss": False,
                                "zero_hit": True,
                                "total_hits": 0, "raw_returned": 0, "unique_returned": 0,
                                "identity_unknown": 0, "usable_returned": 0,
                                "new_vs_S5": 0, "depth_saturated": False, "rows": [],
                                "note": "ZERO_HIT：Scopus Boolean 无任何命中（dead query）；"
                                        "0-hit 不写 cache，经 s6_pilot_zero_hits.json 登记恢复"}
                            continue
                        print(f"  [{i}/{len(q_acts)}] {q['action_id']:<10} "
                              f"CACHE_MISS（未缓存，需 live 补跑）")
                        out[q["action_id"]] = {
                            "action_id": q["action_id"], "family": q["family"],
                            "domain": q["domain"], "strategy": q["strategy"],
                            "exploratory": q.get("exploratory", False),
                            "contains_shrinkage_anchor": q["contains_shrinkage_anchor"],
                            "query_string": q["query_string"], "cache_miss": True,
                            "rows": []}
                        continue
                    papers = cached.papers
                    total_hits = getattr(cached, "total_count", len(papers))
                else:
                    # 强制 skip_cache：防缓存命中覆盖（S1 depth=50 教训）
                    res = await engine.search(q["query_string"], limit=args.depth,
                                              skip_cache=True)
                    papers = res.papers
                    total_hits = getattr(res, "total_count", len(papers))
            except Exception as e:
                print(f"    [WARN] {q['action_id']}: 检索失败 {e}")
                q["error"] = str(e)
                await asyncio.sleep(1)
                continue
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
                "new_vs_S5": len(keys - s5_keys),
                "depth_saturated": (len(papers) >= args.depth
                                    and total_hits > args.depth),
            })
            out[q["action_id"]] = {"action_id": q["action_id"], "family": q["family"],
                                   "domain": q["domain"], "strategy": q["strategy"],
                                   "exploratory": q.get("exploratory", False),
                                   "contains_shrinkage_anchor": q["contains_shrinkage_anchor"],
                                   "query_string": q["query_string"],
                                   "total_hits": total_hits, "raw_returned": len(papers),
                                   "unique_returned": len(keys),
                                   "identity_unknown": q["identity_unknown"],
                                   "usable_returned": q["usable_returned"],
                                   "new_vs_S5": q["new_vs_S5"],
                                   "depth_saturated": q["depth_saturated"],
                                   "rows": rows}
            cen = "C" if q["depth_saturated"] else " "
            exp = " [EXP]" if q.get("exploratory") else ""
            print(f"  [{i}/{len(q_acts)}]{cen} {q['action_id']:<10} "
                  f"hits={total_hits:>6} raw={len(papers):>4} uniq={len(keys):>4} "
                  f"new_S5={len(keys - s5_keys):>4}{exp} {q['query_string'][:42]}")
            await asyncio.sleep(1)
    finally:
        if not from_cache:
            await engine.close()
    return out


if __name__ == "__main__":
    main()
