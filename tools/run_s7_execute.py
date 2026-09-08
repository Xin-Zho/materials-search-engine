#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/run_s7_execute.py — S7 RUN 队列真实检索执行器（清纸面 RUN，2026-09-07）

背景（S7.3 coverage matrix 三维结论）：
  run_queue 46 条 QA=RUN 全是"纸面"——从未真实检索，reward=0。
  coverage matrix 揭示：46 RUN → 11 组 (domain × B 概念)，每组 1-10 个 A 锚。
  本工具把 RUN 队列编译成 Scopus query（组粒度，防同 B 空间重复扫）→ 真实检索 →
  new_vs_S6_SEEN 判定 → 产出 delta 供 memory community 层落地（reward）。

编译规则（deterministic，不调 LLM）：
  group = (domain, concept_B) 聚合所有 RUN 的 concept_A 作左锚
  query = TITLE-ABS-KEY( <left> AND <B 词面> AND <domain CTX> )
    left 含 process 锚 → 展开为 PROCESS_CLAUSE 冻结模板 OR 组
    left 为 mechanism/observable → group_or 词面（加引号防词组切分）
    B 词面 = concept_B（VERIFIED_NORMALIZED/SEMANTIC 词面一律加引号）
  R_discovery B（delamination/dimensional accuracy）单列报告层（R_main/R_discovery 分流）

纪律：
  - base seen = s6_seen_set.json（S6_SEEN 21782，FROZEN）——new = 相对 S6 的新论文
  - 内容词全部来自 term layers/冻结词表（RUN 队列本身是 QA 裁决产物，无 LLM 注入）
  - 只读 memory；产物写 data/exports/terminology/s7_execute_*.json
  - Scopus live（skip_cache）或 --from-cache 重放（复用 S6 runner 模式）

用法：
  .venv\\Scripts\\python.exe tools\\run_s7_execute.py --plan-only   # 只编译 query 不发送
  .venv\\Scripts\\python.exe tools\\run_s7_execute.py               # live Scopus
  .venv\\Scripts\\python.exe tools\\run_s7_execute.py --from-cache # 缓存重放
"""
import argparse
import asyncio
import datetime
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
MEMORY = os.path.join(T, "s7_relation_memory.json")
S6_SEEN = os.path.join(T, "s6_seen_set.json")
COVERAGE = os.path.join(T, "s7_coverage_matrix.json")
# P0-2: 以下为 v1.0 legacy 冻结原址；非 legacy topic 自动路由 topics/<id>/runs/ 通用名
DEFAULT_Q_REC = os.path.join(T, "s7_execute_query_records.json")
DEFAULT_DELTA = os.path.join(T, "s7_execute_delta.json")
DEFAULT_DEPTH = 1000

# 冻结 query 常量（复制自 build_s6_bridge_queries.py——S6 freeze 已 sha256 锚定，
# 此处 literal 复制防 import 循环；若 S6 变更须同步）
PROCESS_CLAUSE = '(curing OR polymeriz* OR photocuring OR photopolymeriz*)'
CTX = {
    "dental": '(dental OR dentistry OR restorative)',
    "optics": '(optical OR holograph* OR lithograph* OR photoresist OR "data storage")',
    "3dp":    '("3d print*" OR "additive manufacturing" OR stereolithograph* OR sla)',
    "sla":    '("3d print*" OR "additive manufacturing" OR stereolithograph* OR sla)',
    "coatings": '(coating* OR "thin film" OR varnish)',
    "packaging": '(encapsulant* OR "molding compound" OR packaging OR semiconductor)',
    "composites": '(composite* OR resin-matrix OR fiber-reinforced)',
    "general": None,
}
# process 词面（判定左锚是否 process 类；去星号比较——polymeriz* 匹配 polymeriz）
PROCESS_WORDS = {"curing", "polymeriz", "photocuring", "photopolymeriz"}


def q(s):
    return f'"{s}"'


def group_or(items):
    return "(" + " OR ".join(q(x) for x in items) + ")"


def compile_query(dom, b_concept, a_concepts, need_process_bridge=False):
    """11 组 RUN → Scopus Boolean。
    left: A 锚 OR 组（process 词展开为冻结模板；其余词面加引号）。
    规则：若 A 锚含任意 process 词 → left 用 PROCESS_CLAUSE（模板优先），其余
    mechanism/observable 锚词面 OR 追加（防 process-only 漏 delayed gel point 等）。
    否则全部 A 锚词面 group_or。
    need_process_bridge=True（组内含 observable_transfer_with_process relation）→
    process 桥强制 AND 上（S6 D 类裸 domain transfer 失败教训：observable→observable
    迁移必须带 process 第三约束）。
    process 通配词（polymeriz*/photopolymeriz*）必须保留星号（Scopus 通配）——
    rstrip("*") 只用于配对比较，不用于 query 词面。
    B 端词组（concept_B 多词）→ 加引号词面。domain CTX 存在则 AND。"""
    # 归一：词尾通配归一，保留星号用于 query；去星号仅用于 process 判定
    def norm_a(x):
        x = x.strip().lower()
        return x  # 保留原样（含 *）
    a_norm = [norm_a(a) for a in a_concepts]
    a_dewild = {a.rstrip("*") for a in a_norm}   # 仅判定用
    has_process = bool(a_dewild & PROCESS_WORDS)
    if has_process:
        # process 锚存在 → 冻结模板；其余机制/observable 锚（词面原文）OR 追加
        others = [a for a in a_norm if a.rstrip("*") not in PROCESS_WORDS]
        left = PROCESS_CLAUSE
        if others:
            left = "(" + left + " OR " + " OR ".join(q(x) for x in others) + ")"
    else:
        left = group_or(sorted(a_norm))
    parts = [left, q(b_concept)]
    if need_process_bridge and not has_process:
        # observable_transfer 第三约束：A/B 皆 observable，须 process 桥
        # （不能只靠 A 锚——组内若全是 observable A 锚，无 process 则桥缺失）
        parts.append(PROCESS_CLAUSE)
    ctx = CTX.get(dom)
    if ctx:
        parts.append(ctx)
    return "TITLE-ABS-KEY(" + " AND ".join(parts) + ")"


def build_groups(memory_path: str):
    """memory 未执行 RUN → (group_id, domain, B, A 锚组, recall_layer, run_ids)。

    组级 dedup（round4 粒度盲区修复 2026-09-07）：deny 是 (A,B) pair 级、检索是
    (domain,B) 组级——EX 执行后同 (domain,B) 的新 A 锚 RUN 不再聚合（重复扫零增益）。
    只取：decision==RUN ∧ 未 searched ∧ 非 ABANDONED ∧ (domain,B) 不在已执行 EX 组。
    group_id 从已执行 EX 最大编号续排（防与 EX-01~11 冲突）。"""
    mem = json.load(open(memory_path, encoding="utf-8"))
    # 已执行/处置的 (domain, B) 组合
    executed = set()
    ex_ids = []
    for c in mem["relations"]:
        if c.get("kind") == "community" and c.get("source") == "s7_execute":
            ex_ids.append(c.get("id", ""))
            b = (c.get("observables") or [""])[0]
            executed.add((c.get("domain"), (b or "").lower().rstrip("*")))
    nums = [int(i.split("-")[1]) for i in ex_ids
            if "-" in i and i.split("-")[1].isdigit()]
    next_num = (max(nums) if nums else 0) + 1

    runs = [r for r in mem["relations"]
            if r["kind"] == "proposal" and r["decision"] == "RUN"
            and not r.get("searched")
            and (r.get("execute_outcome") or {}).get("status") != "ABANDONED"
            and (r.get("domain"),
                 r["concept_B"].lower().rstrip("*")) not in executed]
    skipped = [r for r in mem["relations"]
               if r["kind"] == "proposal" and r["decision"] == "RUN"
               and not r.get("searched")
               and (r.get("domain"),
                    r["concept_B"].lower().rstrip("*")) in executed]
    # 按 (domain, concept_B) 聚合（大小写不敏感）
    from collections import defaultdict
    agg = defaultdict(lambda: {"A": set(), "ids": [], "layers": set(),
                               "rts": set()})
    for r in runs:
        key = (r["domain"], r["concept_B"].lower().rstrip("*"))
        agg[key]["A"].update([r["concept_A"]])
        agg[key]["ids"].append(r["id"])
        agg[key]["layers"].add(r.get("recall_layer", "R_main"))
        agg[key]["rts"].add(r.get("relation_type", ""))
    groups = []
    for i, ((dom, b_low), info) in enumerate(sorted(agg.items())):
        layer = "R_discovery" if "R_discovery" in info["layers"] else "R_main"
        # observable_transfer_with_process ∈ 组 → 需 process 桥
        need_bridge = "observable_transfer_with_process" in info["rts"]
        groups.append({
            "group_id": f"EX-{next_num + i:02d}",
            "domain": dom,
            "concept_B": b_low,
            "a_anchors": sorted(info["A"]),
            "recall_layer": layer,
            "relation_types": sorted(info["rts"]),
            "need_process_bridge": need_bridge,
            "run_ids": info["ids"],
        })
    if skipped:
        print(f"[dedup] 组级过滤 {len(skipped)} 条 RUN（已执行 EX 组同 "
              f"(domain,B)）：{sorted({(r.get('domain'), r['concept_B']) for r in skipped})}")
    return groups


def main():
    ap = argparse.ArgumentParser(description="S7 RUN 真实检索（清纸面 RUN）")
    ap.add_argument("--topic", default=None,
                    help="topic_id（默认 v1.0 legacy 主题；输出自动路由 topics/<id>/runs/）")
    ap.add_argument("--depth", type=int, default=DEFAULT_DEPTH)
    ap.add_argument("--query-records", default=None,
                    help="默认: pc001 legacy → data/exports/terminology/s7_execute_query_records.json；"
                         "新主题 → topics/<topic>/runs/execute_query_records.json")
    ap.add_argument("--delta", default=None)
    ap.add_argument("--engine-data-dir", default="data")
    ap.add_argument("--plan-only", action="store_true")
    ap.add_argument("--live", action="store_true",
                    help="允许真实 Scopus 检索写缓存（P0-2 默认 dry-run：防误跑）")
    ap.add_argument("--from-cache", action="store_true")
    ap.add_argument("--only", default=None)
    args = ap.parse_args()

    # ── P0-2: 主题命名空间（relation memory / seen / 输出默认随 topic 路由）──
    if not args.topic:
        args.topic = DEFAULT_TOPIC
    memory_path = resolve_input(args.topic, MEMORY, "relation_memory.json")
    seen_path = resolve_input(args.topic, S6_SEEN, "seen_set.json")
    if not args.query_records:
        args.query_records = resolve_output(args.topic, DEFAULT_Q_REC,
                                            "execute_query_records.json")
    if not args.delta:
        args.delta = resolve_output(args.topic, DEFAULT_DELTA, "execute_delta.json")

    # ── --live 门禁（P0-2 默认 dry-run；真实 Scopus 检索须显式 --live）──
    if not args.plan_only and not args.live and not args.from_cache:
        raise SystemExit("[dry-run] 真实 Scopus 检索被禁止：传 --live 执行，"
                         "--plan-only 预览，或 --from-cache 缓存重放")

    groups = build_groups(memory_path)
    # --only 组过滤（逗号分隔组号，如 --only EX-10,EX-11；也接受裸序号 10）
    if args.only:
        only = {x.strip().upper()
                for x in args.only.replace("EX-", "").split(",")}
        groups = [g for g in groups
                  if g["group_id"].replace("EX-", "") in only]
        if not groups:
            print(f"[WARN] --only={args.only} 无匹配组；全部组如下：")
            for g in build_groups(memory_path):
                print(f"  {g['group_id']} [{g['domain']}] {g['concept_B']}")
            return
    # 编译
    for g in groups:
        g["query_string"] = compile_query(g["domain"], g["concept_B"],
                                          g["a_anchors"],
                                          g["need_process_bridge"])
        g["type"] = "scopus_query"
        g["round"] = "S7_EXECUTE"

    s6_keys = set(json.load(open(seen_path, encoding="utf-8"))["keys"])
    print("=" * 78)
    print("S7 RUN 真实检索（清纸面 RUN —— reward 落地）")
    print("=" * 78)
    print(f"RUN groups       = {len(groups)}（46 条 RUN 聚合，防同 B 重复扫）")
    print(f"  R_main         = {sum(1 for g in groups if g['recall_layer']=='R_main')}")
    print(f"  R_discovery    = {sum(1 for g in groups if g['recall_layer']=='R_discovery')}")
    print(f"base S6_SEEN     = {len(s6_keys)}（FROZEN 21782）")
    print(f"depth            = {args.depth}")

    if args.plan_only:
        print("\n[plan-only] 编译的 query（不发送不写盘）：")
        for g in groups:
            tag = " [R_discovery]" if g["recall_layer"] == "R_discovery" else ""
            bridge = " [proc-bridge]" if g["need_process_bridge"] else ""
            print(f"  {g['group_id']} [{g['domain']:10s}] "
                  f"{g['concept_B']:<28s}{tag}{bridge}")
            print(f"      {g['query_string']}")
            print(f"      A锚={g['a_anchors']}  RUN={len(g['run_ids'])}条")
        print("\n[plan-only] 结束：未发送任何请求。")
        return

    from search_engine.engine import ScopusSearchEngine
    resolver, r_old_keys = build_r_old()
    engine = ScopusSearchEngine(data_dir=args.engine_data_dir)
    if args.from_cache:
        mode = "from-cache 重放"
    else:
        mode = "live Scopus（skip_cache）"
        # 直接调用 engine.start（不强制 live 浏览器，若失败提示用户）
    print(f"mode             = {mode}")
    if not args.from_cache:
        asyncio.run(run_live(groups, args, s6_keys, engine, resolver,
                             r_old_keys))
    else:
        asyncio.run(run_from_cache(groups, args, s6_keys, engine, resolver,
                                   r_old_keys))


async def run_live(groups, args, s6_keys, engine, resolver, r_old_keys):
    await engine.start()
    await execute(groups, args, s6_keys, engine, from_cache=False,
                  resolver=resolver, r_old_keys=r_old_keys)


async def run_from_cache(groups, args, s6_keys, engine, resolver, r_old_keys):
    await execute(groups, args, s6_keys, engine, from_cache=True,
                  resolver=resolver, r_old_keys=r_old_keys)


async def execute(groups, args, s6_keys, engine, from_cache, *,
                  resolver=None, r_old_keys=None):
    out = {}
    for i, g in enumerate(groups, 1):
        try:
            if from_cache:
                cached = engine.cache.get_cached_result(g["query_string"])
                if cached is None:
                    print(f"  [{i}/{len(groups)}] {g['group_id']} CACHE_MISS"
                          f"（需 live 补跑）")
                    out[g["group_id"]] = dict(g, cache_miss=True, rows=[])
                    continue
                papers = cached.papers
                total_hits = getattr(cached, "total_count", len(papers))
            else:
                res = await engine.search(g["query_string"], limit=args.depth,
                                          skip_cache=True)
                papers = res.papers
                total_hits = getattr(res, "total_count", len(papers))
        except Exception as e:
            print(f"    [WARN] {g['group_id']}: 检索失败 {e}")
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
        g.update({
            "total_hits": total_hits, "raw_returned": len(papers),
            "unique_returned": len(keys),
            "identity_unknown": sum(1 for r in rows if not r["key"]),
            "usable_returned": sum(1 for r in rows if r["usable"]),
            "new_vs_S6": len(keys - s6_keys),
            "cache_miss": False,
        })
        out[g["group_id"]] = dict(g, rows=rows)
        print(f"  [{i}/{len(groups)}] {g['group_id']} [{g['domain']:8s}] "
              f"{g['concept_B']:<26s} hits={total_hits:<6} "
              f"unique={len(keys):<5} new_vs_S6={len(keys - s6_keys)}")
        await asyncio.sleep(0.5)
    finalize(out, groups, args, s6_keys)


def finalize(out, groups, args, s6_keys):
    """汇总写盘。--only/补跑模式下 merge 已存在 records（不覆盖其他组）。"""
    rec_path = args.query_records
    merged = {}
    if os.path.exists(rec_path):
        try:
            old = json.load(open(rec_path, encoding="utf-8"))
            merged = old.get("records_by_group", {})
        except Exception:
            merged = {}
    for gid, w in out.items():
        if w.get("cache_miss") and merged.get(gid, {}).get("rows"):
            continue   # cache_miss 空组不覆盖已有真实 rows（防 --only/重放毁数据）
        merged[gid] = w
    # aggregate 基于 merged 全组（而非仅本轮 out）
    q_keys = set()
    for wrap in merged.values():
        for r in wrap.get("rows", []):
            if r.get("key"):
                q_keys.add(r["key"])
    q_new = q_keys - s6_keys
    cache_miss = [gid for gid, w in merged.items() if w.get("cache_miss")]
    aggregate = {
        "query_union_unique": len(q_keys),
        "new_vs_S6": len(q_new),
        "status": "DEVELOPMENT_EXECUTE",
        "cache_miss_groups": cache_miss,
        "n_groups": len(merged),
        "by_layer": {
            layer: {"groups": sum(1 for g in merged.values()
                                  if g.get("recall_layer") == layer),
                    "new_vs_S6": len({r["key"] for w in merged.values()
                                      for r in w.get("rows", [])
                                      if r.get("key") and w.get(
                                          "recall_layer") == layer} - s6_keys)}
            for layer in ("R_main", "R_discovery")
        },
    }
    now = datetime.datetime.now().isoformat(timespec="seconds")
    delta_path = args.delta
    with open(rec_path, "w", encoding="utf-8") as f:
        json.dump({"round": "S7_EXECUTE", "status": "DEVELOPMENT_EXECUTE",
                   "executed_at": now, "depth": args.depth,
                   "base_seen": "s6_seen_set.json",
                   "base_seen_size": len(s6_keys),
                   "records_by_group": merged}, f, ensure_ascii=False, indent=1)
    with open(delta_path, "w", encoding="utf-8") as f:
        json.dump({"round": "S7_EXECUTE", "executed_at": now,
                   "delta_vs_S6": aggregate}, f, ensure_ascii=False, indent=1)
    print(f"\n[OK] records : {rec_path}（merged {len(merged)} 组）")
    print(f"[OK] delta   : {delta_path}")
    print("\n=== S7 EXECUTE aggregate ===")
    for k, v in aggregate.items():
        if k != "by_layer":
            print(f"  {k:<22} {v}")
    print("\n→ 下一步：memory community 层落地（KEEP/FAIL + R/U 标注）→ QA 盲评新论文")


if __name__ == "__main__":
    main()
