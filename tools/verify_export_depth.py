"""tools/verify_export_depth.py — v2.0.1 targeted depth 验证（用户 2026-08-28 定稿 + 统计修复）。

目标：回答"100 篇 EXPORT_DEPTH_CANDIDATE 到底是不是因为 Top100 太浅？"
——按 query 分组深度导出（不是逐篇请求），EID exact 定位 rank。

设计（用户定）：
  1. 按 frozen matching_query 分组（100 篇 → 10~30 unique queries）
  2. 每组深度导出一次（depth_cap=2000，诊断用不做多档曲线）
  3. EID exact 定位目标论文 rank（DOI exact 仅 fallback，不用 W-id）
  4. 四态判定：
       FOUND_DEEP           rank>100 → F3a EXPORT_DEPTH 实锤
       FOUND_TOP100         rank<=100 → 理论上 v2.0 该抓到 → 查 run/dedup/budget
       ABSENT_EXHAUSTIVE    导出未满 cap 且论文不在 → 词面归因错 → 退回 F1/F2
       NOT_FOUND_WITHIN_CAP 导出满 cap 且论文不在 → 只说 rank>cap 或 query 不匹配，
                            不能判 F1/F2（未证实）
  5. 每 query 保存 total_count（导出行数近似）/exported_count/depth_cap/targets

2026-08-28 统计修复（用户抓的 3 个 bug）：
  A. --resume 曾"追加分类"导致同一 query 条目重复（100 target 统计成 137，
     RETRY 37→74 翻倍）→ 改为 results 按 query 的 dict，upsert 覆盖；最终统计
     按 eid 去重取最新 current_status，并 assert 总数 == 100。
  B. CURRENT_UNRESOLVED 曾按 verify matched-query rank 动态重算（41→126）
     → 冻结为 EID 集合：candidate.eid not in depth1000_eids（147 query Top1000
     全局 union），assert == 41。
  C. FOUND_TOP100 的 run_rank 曾来自旧条目缺失字段（depth_run_rank=None 误报
     RUN_INCONSISTENCY）→ 汇总时用全局 eid_min_rank 重查。
  另：export 前打印 request fingerprint（SHA256），与 diagnose_export_queue.py
  对比两条执行路径差异（已知差异：verify 先 engine.search（内部含一次
  limit=depth 导出 job）再分页导出；diagnose 直接 initiate）。

⚠️ 近似：Scopus 页面 total_count 解析不可靠（已踩坑），exhaustive 判定用
"导出行数 < depth_cap"近似（itemCount 请求 2000 时若结果只有 528 行则 CSV 有
528 行）；导出满 cap 时无法确认是否截断。

用法：
  python tools/verify_export_depth.py --plan-only    # 分组预览（不请求）
  python tools/verify_export_depth.py                # 执行（需 Scopus 会话）
  python tools/verify_export_depth.py --depth 2000   # 默认 cap
"""
import argparse
import asyncio
import csv
import hashlib
import io
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

ROUTE_PATH = os.path.join(BASE, "data", "exports", "qgs_reverse_route.json")
OUT_PATH = os.path.join(BASE, "data", "exports", "verify_export_depth.json")
DEPTH_RUN_PATH = os.path.join(BASE, "data", "exports", "query_family_runs_depth.json")
REGISTRY_PATH = os.path.join(BASE, "data", "query_registry_v2.json")

STATUS_ORDER = ("FOUND_DEEP", "FOUND_TOP100", "FOUND_VIA_ALT_QUERY",
                "ABSENT_EXHAUSTIVE", "NOT_FOUND_WITHIN_CAP",
                "EXPORT_FAILED_RETRY", "EXPORT_FAILED_TIMEOUT")

EXPORT_ENDPOINT = "https://www.scopus.com/gateway/export-service-reactive/export/bulk-job/initiate"
SORT_SPEC = [{"fieldName": "datesort", "order": "desc"},
             {"fieldName": "relevance", "order": "desc"}]


def norm_eid(e: str) -> str:
    return (e or "").strip()


def norm_doi(d: str) -> str:
    return (d or "").strip().lower().replace("https://doi.org/", "").replace("http://doi.org/", "")


def query_text_from_key(qkey: str) -> str:
    """matched_queries 格式 'FAM_xxx:TITLE-ABS-KEY(...)' → query 文本。"""
    if ":" in qkey:
        return qkey.split(":", 1)[1]
    return qkey


def request_fingerprint(query: str, limit: int, offset: int, fields: list[str]) -> dict:
    """export 请求指纹（与 diagnose_export_queue.py 同构，用于路径对比）。"""
    payload = {
        "query": query,
        "limit": limit,
        "offset": offset,
        "sort": SORT_SPEC,
        "field_groups": fields,
        "locale": "zh-CN",
        "endpoint": EXPORT_ENDPOINT,
        "document_classification": "PRIMARY",
        "export_type": "PUBLICATION",
        "file_type": "CSV",
    }
    h = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    return {"payload": payload, "sha256": h}


def load_registry_queries() -> list[tuple[str, str]]:
    """registry 完整 query 列表 [(family_id, query_text)]。"""
    reg = json.load(open(REGISTRY_PATH, encoding="utf-8"))
    return [(f["family_id"], q) for f in reg["families"]
            for q in f.get("generated_queries", [])]


def resolve_full_query(trunc: str, fulls: list[tuple[str, str]]) -> tuple[str, list[str]]:
    """reverse_route 截断 query → registry 完整 query（最短优先消歧）。

    2026-08-28 发现：reverse_route 的 matched_queries 存了截断文本（如
    '...AND "comp'，缺闭合引号/括号）——残缺 query 送 Scopus export-service
    大概率就是 RETRY 的直接原因（diagnose 用完整 query 成功）。
    """
    if any(trunc == full for _, full in fulls):
        return trunc, []
    hits = sorted({full for _, full in fulls if full.startswith(trunc)}, key=len)
    if not hits:
        return trunc, []     # 无法解析 → 原样（会失败，交由调用方警告）
    return hits[0], hits


def plan_only(route: dict) -> dict:
    """分组预览：unique queries + 每组的论文数。"""
    cands = [p for p in route["papers"] if p["failure_class"] == "EXPORT_DEPTH_CANDIDATE"]
    groups: dict[str, list] = {}
    for p in cands:
        qs = p.get("matched_queries") or []
        q = query_text_from_key(qs[0]) if qs else "(no matched query)"
        groups.setdefault(q, []).append(p)
    print(f"EXPORT_DEPTH_CANDIDATE = {len(cands)}")
    print(f"unique queries = {len(groups)}（按第一匹配 query 分组）")
    print(f"总导出请求 ≈ {len(groups)}（每个 query 一次深度导出）")
    print("\n分组明细:")
    for q, ps in sorted(groups.items(), key=lambda x: -len(x[1])):
        print(f"  {len(ps):>3} 篇 | {q[:70]}")
        for p in ps[:3]:
            print(f"        [{p['idx']}] ({p.get('year')}) {p['title'][:50]}")
        if len(ps) > 3:
            print(f"        ... 共 {len(ps)} 篇")
    return groups


async def export_query(engine, query: str, depth: int, page: int = 1000) -> tuple[list[dict], str]:
    """深度导出一条 query，返回 (records[{rank,eid,doi}], ok_flag)。

    2000 条单次导出实测 >300s 仍超时（2026-08-28）→ 拆 offset 分页每页 1000 条，
    rank 合并加 offset 偏移。

    ⚠️ 2026-08-28 修复（用户拍板）：彻底移除 engine.search() prewarm——旧路径
    search(limit=depth) 内部会先提交一次 limit=depth 的 export job，verify 再提交
    第二次 → 同一 query 连续两个大 job → RETRY。现改为 direct initiate（与
    diagnose_export_queue.py 已验证成功的路径一致：无 search、单一 export job）。
    本次只改这一件事，其余（depth/poll/retry/fields）全部 frozen。
    """
    fp = request_fingerprint(query, page, 0, ["eid", "doi"])
    print(f"    [fingerprint] sha256={fp['sha256']} limit={page} offset=0 "
          f"fields={fp['payload']['field_groups']} "
          f"search_prewarm_used=false pre_export_jobs_created_by_this_query=0")
    all_records: list[dict] = []
    offset = 0
    while offset < depth:
        csv_text = ""
        for attempt in range(3):
            csv_text = await engine._export_via_api(
                query, page, fields=["eid", "doi"], poll_retries=300,
                offset=offset, retry_abort=60)
            if csv_text.strip():
                break
            print(f"    ⚠️ offset={offset} 导出超时/空（第 {attempt + 1}/3 次），5s 后重试...")
            await asyncio.sleep(5)
        if not csv_text.strip():
            if offset > 0 and all_records:
                break          # 非首页且前面已有数据 → 总量已尽，正常终止
            return all_records, ""   # 首页失败 → 真失败（EXPORT_FAILED_TIMEOUT）
        rows = list(csv.DictReader(io.StringIO(csv_text)))
        n = 0
        for i, row in enumerate(rows, 1):
            eid = norm_eid(row.get("EID") or "")
            doi = norm_doi(row.get("DOI") or "")
            if eid or doi:
                all_records.append({"rank": offset + i, "eid": eid, "doi": doi})
                n += 1
        if n < page:
            break                # 本页未满 → 总量已尽
        offset += page
    return all_records, "ok"


def load_depth_run() -> tuple[set[str], dict[str, int]]:
    """depth run（147 queries × Top1000）的 eid 全集 + eid→min_rank。

    ⚠️ 语义（用户 2026-08-28 定）：raw1000 = 任意 query 在 Top1000 内找到的 EID
    全集（不是 matched query 单看）；CURRENT_UNRESOLVED 必须由它冻结计算。
    """
    records = json.load(open(DEPTH_RUN_PATH, encoding="utf-8"))["records"]
    raw1000: set[str] = set()
    eid_min_rank: dict[str, int] = {}
    for recs in records.values():
        for r in recs:
            eid = norm_eid(r.get("eid"))
            if not eid or r["rank"] > 1000:
                continue
            raw1000.add(eid)
            if eid not in eid_min_rank or r["rank"] < eid_min_rank[eid]:
                eid_min_rank[eid] = r["rank"]
    return raw1000, eid_min_rank


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan-only", action="store_true")
    ap.add_argument("--depth", type=int, default=2000, help="depth cap（默认 2000）")
    ap.add_argument("--route", default=ROUTE_PATH)
    ap.add_argument("--resume", action="store_true",
                    help="断点续跑：跳过已成功导出（exported_count>0）的 query；失败条目按 query 覆盖更新")
    ap.add_argument("--only", default=None,
                    help="smoke test：只处理 query 文本包含该子串的组（如 --only 'AND \"composite\"'）")
    args = ap.parse_args()

    route = json.load(open(args.route, encoding="utf-8"))
    if args.plan_only:
        plan_only(route)
        return

    cands = [p for p in route["papers"] if p["failure_class"] == "EXPORT_DEPTH_CANDIDATE"]
    groups: dict[str, list] = {}
    for p in cands:
        qs = p.get("matched_queries") or []
        q = query_text_from_key(qs[0]) if qs else "(no matched query)"
        groups.setdefault(q, []).append(p)
    # 截断 query 修复（reverse_route 存储缺陷）：残缺文本 → registry 完整文本
    fulls = load_registry_queries()
    resolved_groups: dict[str, list] = {}
    n_fixed = 0
    for q, ps in groups.items():
        full, cands_full = resolve_full_query(q, fulls)
        resolved_groups[full] = ps
        if cands_full:
            n_fixed += 1
            print(f"  ↻ query 截断修复: {q[:52]}... → {full[:64]}"
                  f"（候选 {len(cands_full)}，取最短）")
    if n_fixed:
        print(f"  共修复 {n_fixed} 个截断 query（之前残缺 query 送 Scopus 可能即 RETRY 根因）")
    groups = resolved_groups
    if args.only:
        groups = {q: ps for q, ps in groups.items() if args.only in q}
        print(f"[--only] 过滤后 = {len(groups)} 个 query（smoke test）")
        if not groups:
            print("⚠️ --only 未匹配任何 query；可用 --plan-only 查看分组文本")
            return
    print(f"EXPORT_DEPTH_CANDIDATE = {len(cands)} → {len(groups)} unique queries "
          f"（depth_cap={args.depth}）")

    raw1000, eid_min_rank = load_depth_run()
    # CURRENT_UNRESOLVED 冻结为 EID 集合（用户定：candidate.eid not in depth1000_eids）
    frozen_unresolved = {norm_eid(p.get("eid")) for p in cands
                         if norm_eid(p.get("eid")) not in raw1000}
    n_frozen = len(frozen_unresolved)
    assert n_frozen == 41, (
        f"CURRENT_UNRESOLVED 必须恒为 41，实际 {n_frozen}——EID 集合定义被破坏，停止输出")
    print(f"  CURRENT_UNRESOLVED 冻结 = {n_frozen}（EID 集合，恒 41）")

    # results 按 query 的 dict（upsert 语义：resume 覆盖旧条目，绝不追加重复）
    results: dict[str, dict] = {}
    done_queries: set[str] = set()
    if args.resume and os.path.exists(OUT_PATH):
        prev = json.load(open(OUT_PATH, encoding="utf-8"))
        results = {r["query"]: r for r in prev.get("queries", [])}
        done_queries = {q for q, r in results.items() if r.get("exported_count", 0) > 0}
        if done_queries:
            print(f"  ↻ 断点续跑：跳过 {len(done_queries)} 个已成功导出的 query，"
                  f"失败条目将覆盖更新（不重复累计）")

    # 全局借位索引：失败 query 的 targets 去已成功 query 的 records 并集里找
    alt_eid: dict[str, int] = {}
    alt_doi: dict[str, int] = {}

    from search_engine.engine import ScopusSearchEngine
    engine = ScopusSearchEngine()
    await engine.start()   # 必须先启动（已踩坑：漏 start → 'NoneType' url）
    debug_dir = os.path.join(BASE, "data", "cache")
    os.makedirs(debug_dir, exist_ok=True)
    try:
        for qi, (query, papers) in enumerate(sorted(groups.items()), 1):
            if query in done_queries:
                print(f"  ⏭ [{qi}/{len(groups)}] 跳过（已导出）: {query[:60]}")
                # 已成功 query 的 records 补进借位索引（从 debug 文件回填）
                dp = os.path.join(debug_dir, f"verify_export_{qi:02d}.json")
                if os.path.exists(dp):
                    for r in json.load(open(dp, encoding="utf-8")).get("records", []):
                        if r.get("eid"):
                            alt_eid[r["eid"]] = min(alt_eid.get(r["eid"], 10**9), r["rank"])
                        if r.get("doi"):
                            alt_doi[r["doi"]] = min(alt_doi.get(r["doi"], 10**9), r["rank"])
                continue
            records, ok_flag = await export_query(engine, query, args.depth)
            # 导出失败（RETRY/超时/空）→ 走借位，不进四态
            if not ok_flag.strip():
                targets = []
                n_alt = 0
                for p in papers:
                    eid = norm_eid(p.get("eid"))
                    doi = norm_doi(p.get("doi"))
                    alt_rank = (alt_eid.get(eid) if eid else None)
                    if alt_rank is None and doi:
                        alt_rank = alt_doi.get(doi)
                    if alt_rank is not None:
                        targets.append({"idx": p["idx"], "eid": eid, "doi": doi,
                                        "found": True, "rank": alt_rank,
                                        "status": "FOUND_VIA_ALT_QUERY"})
                        n_alt += 1
                    else:
                        targets.append({"idx": p["idx"], "eid": eid, "doi": doi,
                                        "found": False, "rank": None,
                                        "status": "EXPORT_FAILED_RETRY"})
                results[query] = {"query": query, "total_count": 0,
                                  "exported_count": 0, "depth_cap": args.depth,
                                  "export_failed": True, "targets": targets}
                print(f"  ⚠ [{qi}/{len(groups)}] {query[:60]} → bulk-job RETRY/超时，"
                      f"{n_alt}/{len(papers)} 篇借位成功，{len(papers)-n_alt} 篇 EXPORT_FAILED_RETRY")
                _persist(results, args.depth)
                continue
            with open(os.path.join(debug_dir, f"verify_export_{qi:02d}.json"),
                      "w", encoding="utf-8") as f:
                json.dump({"query": query, "n": len(records), "records": records},
                          f, ensure_ascii=False, indent=1)
            by_eid = {r["eid"]: r for r in records if r["eid"]}
            by_doi = {r["doi"]: r for r in records if r["doi"]}
            # 累加借位索引
            for r in records:
                if r["eid"]:
                    alt_eid[r["eid"]] = min(alt_eid.get(r["eid"], 10**9), r["rank"])
                if r["doi"]:
                    alt_doi[r["doi"]] = min(alt_doi.get(r["doi"], 10**9), r["rank"])
            targets = []
            for p in papers:
                eid = norm_eid(p.get("eid"))
                doi = norm_doi(p.get("doi"))
                rec = by_eid.get(eid) or (by_doi.get(doi) if doi else None)
                if rec:
                    rank = rec["rank"]
                    status = "FOUND_DEEP" if rank > 100 else "FOUND_TOP100"
                else:
                    rank = None
                    exported = len(records)
                    status = ("ABSENT_EXHAUSTIVE" if exported < args.depth
                              else "NOT_FOUND_WITHIN_CAP")
                targets.append({"idx": p["idx"], "eid": eid, "doi": doi,
                                "found": rec is not None, "rank": rank,
                                "status": status})
            results[query] = {"query": query, "total_count": len(records),
                              "exported_count": len(records), "depth_cap": args.depth,
                              "targets": targets}
            n_found = sum(1 for t in targets if t["found"])
            print(f"  ✓ [{qi}/{len(groups)}] {query[:60]} → 导出 {len(records)} 条, "
                  f"目标 {len(targets)} 篇, 找到 {n_found}")
            _persist(results, args.depth)
            await asyncio.sleep(1)
    finally:
        await engine.close()

    # ── 汇总：按 eid 去重取最新 current_status（unique targets）──
    # assert 基于当前处理的 target 总数：全量 = 100，--only 模式 = 该组论文数
    expected = sum(len(ps) for ps in groups.values())
    from collections import Counter
    by_eid_latest: dict[str, dict] = {}
    for r in results.values():
        for t in r.get("targets", []):
            eid = t.get("eid")
            if not eid:
                continue
            by_eid_latest[eid] = t      # dict 覆盖 = 最新状态（同 eid 不可能跨 query 重复，
                                        # 但防御性取最后写入）
    assert len(by_eid_latest) == expected, (
        f"target 唯一数必须为 {expected}（全量 100 / --only 过滤后为组内总数），"
        f"实际 {len(by_eid_latest)}——统计被污染，停止输出")
    status_cnt = Counter(t["status"] for t in by_eid_latest.values())
    assert sum(status_cnt.values()) == expected, "status 计数总和必须等于 target 总数"
    rank_buckets = Counter()
    alt_rank_buckets = Counter()
    for t in by_eid_latest.values():
        if not t["rank"]:
            continue
        rk = t["rank"]
        bucket = ("<=100" if rk <= 100 else "101-200" if rk <= 200
                  else "201-500" if rk <= 500 else "501-1000" if rk <= 1000
                  else "1001-2000" if rk <= 2000 else ">2000")
        if t["status"] == "FOUND_DEEP":
            rank_buckets[bucket] += 1
        elif t["status"] == "FOUND_VIA_ALT_QUERY":
            alt_rank_buckets[bucket] += 1

    print("\n" + "=" * 60)
    print(f"EXPORT_DEPTH_CANDIDATE = {len(by_eid_latest)}"
          f"（unique target，assert={expected} ✓）")
    for s in STATUS_ORDER:
        print(f"  {s:<22} {status_cnt.get(s, 0):>4}")
    if rank_buckets:
        print("\nFOUND_DEEP rank 分布（原 query 精确）:")
        for b in ("<=100", "101-200", "201-500", "501-1000", "1001-2000", ">2000"):
            print(f"  {b:<10} {rank_buckets.get(b, 0):>4}")
    if alt_rank_buckets:
        print("\nFOUND_VIA_ALT_QUERY rank 分布（借位，近似）:")
        for b in ("<=100", "101-200", "201-500", "501-1000", "1001-2000", ">2000"):
            print(f"  {b:<10} {alt_rank_buckets.get(b, 0):>4}")
    n_fail = (status_cnt.get("EXPORT_FAILED_RETRY", 0)
              + status_cnt.get("EXPORT_FAILED_TIMEOUT", 0))
    if n_fail:
        print(f"\n⚠️ {n_fail} 篇因 bulk-job RETRY/超时且无借位未判定；"
              f"可重跑: python tools/verify_export_depth.py --resume（覆盖更新，不重复）")

    # ── CURRENT_UNRESOLVED（冻结 EID 集合，全量恒 41）──
    # 全量模式必须 == 41（EID 集合定义）；--only 模式只统计当前组内的 unresolved 子集
    cur = {e: t for e, t in by_eid_latest.items() if e in frozen_unresolved}
    if args.only is None:
        assert len(cur) == n_frozen == 41, (
            f"CURRENT_UNRESOLVED 必须恒为 41，实际 {len(cur)}——停止输出")
    cur_status = Counter(t["status"] for t in cur.values())
    cur_deep = [(t["rank"], t["status"]) for t in cur.values()
                if t["status"] == "FOUND_DEEP" and t["rank"]]
    if args.only is None:
        print(f"\n── CURRENT_UNRESOLVED（冻结 EID 集合，恒 41）──")
    else:
        print(f"\n── CURRENT_UNRESOLVED（--only 模式：当前组内 unresolved 子集 "
              f"{len(cur)} 篇，全量恒 41）──")
    for s in ("FOUND_DEEP", "FOUND_TOP100", "FOUND_VIA_ALT_QUERY",
              "NOT_FOUND_WITHIN_CAP", "EXPORT_FAILED_RETRY",
              "EXPORT_FAILED_TIMEOUT"):
        if cur_status.get(s):
            print(f"  {s:<22} {cur_status[s]:>3}")
    if cur_deep:
        rb = Counter(("1001-2000" if 1000 < rk <= 2000 else ">2000"
                      if rk > 2000 else ("101-200" if rk <= 200 else
                      "201-500" if rk <= 500 else "501-1000"))
                     for rk, _ in cur_deep)
        print("  其中 FOUND_DEEP rank 细分:", dict(rb))

    # ── FOUND_TOP100 一致性核查（run_rank 用全局 eid_min_rank 重查）──
    top100 = [t for t in by_eid_latest.values() if t["status"] == "FOUND_TOP100"]
    if top100:
        print(f"\n── FOUND_TOP100 一致性核查（n={len(top100)}）──")
        for t in top100:
            run_rk = eid_min_rank.get(t["eid"])
            note = ("CONSISTENT: depth run 全局存在且 rank 相近 → 排序稳定，"
                    "属历史归因的已救回组"
                    if run_rk is not None and abs(run_rk - t["rank"]) <= 2
                    else f"RUN_INCONSISTENCY: depth run 全局 rank={run_rk} "
                         f"vs verify rank={t['rank']} → 查 query 一致性/排序变化")
            print(f"  idx={t['idx']} eid={t['eid']} verify_rank={t['rank']} "
                  f"depth_run_rank={run_rk} | {note}")
            if "RUN_INCONSISTENCY" in note:
                status_cnt["RUN_INCONSISTENCY"] += 1

    out = {"created_at": "2026-08-28", "depth_cap": args.depth,
           "note": "四态 + 借位；统计按 eid 唯一（assert 100/41）；"
                   "ABSENT_EXHAUSTIVE 用'导出行数<cap'近似",
           "summary": {"status": dict(status_cnt),
                       "rank_buckets": dict(rank_buckets),
                       "alt_rank_buckets": dict(alt_rank_buckets),
                       "current_unresolved": dict(cur_status)},
           "queries": list(results.values())}
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n✓ 已写: {OUT_PATH}")


def _persist(results: dict, depth: int) -> None:
    out = {"created_at": "2026-08-28", "depth_cap": depth,
           "note": "中间落盘（--resume 可续，条目按 query 覆盖）",
           "summary": {}, "queries": list(results.values())}
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    asyncio.run(main())
