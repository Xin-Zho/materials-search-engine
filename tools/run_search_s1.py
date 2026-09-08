"""tools/run_search_s1.py — v3.0 Search S1 正式执行（2026-08-29 S1_FINAL=19 冻结）。

S1 = S0 + 19 条冻结 repair queries（16 CORE + 3 REWRITE），统一正式 retrieval budget，
正式产生 S1 Candidate Snapshot + S1_SEEN_SET（R02 agent_seen 的唯一依据）。

与 pilot 的本质区别（用户 2026-08-29 定稿）：
- pilot = development evidence（depth=50，MCG_after_core / RMCG 等 selection 指标）
- S1    = 正式 Search snapshot（统一 budget，默认 depth=500 与 S0 对齐）
- S1 固定 19 条，不再 selection；new_vs_S0 相对 S0 全集（非 MCG 顺序累计）
- S1 snapshot 冻结后不再回头改这 19 条；下一次允许改变 Search 的时点 = R02 完成后
- S1 内部结果可报告（S0 unique / repair union / delta / per-query yield 等工程指标），
  但结论只能写 "S1 expanded the retrieved candidate set"，不能说 recall 从 X 到 Y
  ——真正 recall 判定交给独立 Audit R02

SEEN SET 语义（2026-08-29 用户修复定稿，勿再回退）：
  SEARCH_S1_SEEN_UNION = S0_SEEN ∪ S1_REPAIR_QUERY_UNION   ← R02 agent_seen 唯一依据
  S0_SEEN               = build_r_old()（旧 Candidate DB canonical keys）
  S1_REPAIR_QUERY_UNION = 19 条 repair query 检索结果 union（单独存 s1_repair_seen_set.json）
  SEARCH_S1_SEEN_UNION  单独存 s1_seen_set.json（size = |S0| + new_vs_S0，当前 5986+2546=8532）

输出（写死冻结元数据，development_source=AUDIT_R01 / search_baseline=S0 /
eligible_for_r01_evaluation=false）：
  data/exports/terminology/s1_candidate_snapshot.json   （汇总 + per-query 指标 + seen set 引用）
  data/exports/terminology/s1_seen_set.json             （SEARCH_S1_SEEN_UNION，R02 agent_seen 依据）
  data/exports/terminology/s1_repair_seen_set.json      （仅 repair union，审计语义参考）
  data/exports/terminology/s1_raw_records.json          （raw records 明细，query -> [papers]）

用法：
  python tools/run_search_s1.py --plan-only                 # 只读预览（不检索不写盘）
  python tools/run_search_s1.py                             # 真实检索（CloakBrowser，默认 depth=500，强制 bypass 缓存）
  python tools/run_search_s1.py --depth 200                 # 自定义统一 budget
  python tools/run_search_s1.py --rebuild-from-raw          # 从已落盘 raw records 重算 artifact（不检索；修 seen set 语义）
  python tools/run_search_s1.py --offline-cache             # 复用 api_cache 重算（仅验证用，depth 受缓存限制）
"""
import argparse
import asyncio
import hashlib
import json
import os
import sys
import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tools"))

from pilot_round3_query_utility import (  # noqa: E402
    IdentityResolver, build_r_old, connect_cache_ro, paper_key_and_info,
    normalize_doi,
)


def is_usable(title: str, abstract: str, key_ok: bool) -> bool:
    """usable = IdentityValid ∧ Title ∧ (Abstract ∨ FullText)（pilot 同口径）。"""
    return bool(key_ok) and bool(title and title.strip()) \
        and bool(abstract and abstract.strip())


def _usable_text_from_cache(doi: str | None, eid: str | None, con) -> str:
    """从 scopus_cache 补 abstract（S1 检索返回可能无 abstract）。"""
    if not doi and not eid:
        return ""
    try:
        if doi:
            row = con.execute("SELECT normalized_json FROM papers WHERE paper_id=?",
                              (f"scopus:{doi.strip().lower()}",)).fetchone()
        else:
            row = None
        if row:
            return (json.loads(row[0]).get("abstract") or "").strip()
    except Exception:
        pass
    return ""


DEFAULT_QS = os.path.join(BASE, "data", "exports", "terminology",
                          "s1_final_query_set.json")
DEFAULT_SNAP = os.path.join(BASE, "data", "exports", "terminology",
                            "s1_candidate_snapshot.json")
DEFAULT_SEEN = os.path.join(BASE, "data", "exports", "terminology",
                            "s1_seen_set.json")
DEFAULT_REPAIR_SEEN = os.path.join(BASE, "data", "exports", "terminology",
                                   "s1_repair_seen_set.json")
DEFAULT_RAW = os.path.join(BASE, "data", "exports", "terminology",
                           "s1_raw_records.json")
DEFAULT_DEPTH = 500          # 统一正式 budget，与 S0（depth=500）对齐


def main():
    ap = argparse.ArgumentParser(description="Search S1 正式执行（S1_FINAL=19 冻结）")
    ap.add_argument("--json", default=DEFAULT_QS)
    ap.add_argument("--depth", type=int, default=DEFAULT_DEPTH)
    ap.add_argument("--snapshot", default=DEFAULT_SNAP)
    ap.add_argument("--seen-set", default=DEFAULT_SEEN)
    ap.add_argument("--repair-seen", default=DEFAULT_REPAIR_SEEN)
    ap.add_argument("--raw", default=DEFAULT_RAW,
                    help="raw records 路径（检索/rebuild 时输入；rebuild 时也作为输出写入）")
    ap.add_argument("--raw-out", default=None,
                    help="raw records 输出路径（默认与 --raw 相同；rebuild 时内容不变，可省略）")
    ap.add_argument("--rebuild-from-raw", action="store_true",
                    help="从已落盘 s1_raw_records.json 重算 artifact（不检索；"
                         "seen set 语义修复专用，保留原始 execution_timestamp）")
    ap.add_argument("--offline-cache", action="store_true",
                    help="从 api_cache 读缓存重算（不发请求；仅重算验证用，depth 受缓存限制）")
    ap.add_argument("--engine-data-dir", default="data",
                    help="ScopusSearchEngine data_dir")
    ap.add_argument("--plan-only", action="store_true",
                    help="只读：打印 19 条 + S0 规模 + budget，不检索不写盘")
    args = ap.parse_args()

    qs_data = json.load(open(args.json, encoding="utf-8"))
    queries = qs_data["queries"]
    assert len(queries) == qs_data["S1_FINAL_count"] == 19, "S1 query set 必须 19 条"
    query_set_hash = qs_data["query_set_hash"]

    # 校验 query_set_hash 与冻结一致（防篡改）
    h = hashlib.sha256("\n".join(q["query_string"] for q in
                                 sorted(queries, key=lambda x: x["order"]))
                       .encode("utf-8")).hexdigest()
    if h != query_set_hash:
        print(f"[FATAL] query_set_hash mismatch: 文件 {query_set_hash[:16]}"
              f" vs 重算 {h[:16]} —— S1 query set 被改动，拒绝执行")
        sys.exit(2)

    resolver, r_old_keys = build_r_old()
    print(f"S1 queries = {len(queries)} | S0(canonical keys) = {len(r_old_keys)}"
          f" | depth = {args.depth} | query_set_hash = {query_set_hash[:16]}...")

    if args.rebuild_from_raw:
        rebuild_from_raw(args, queries, resolver, r_old_keys, query_set_hash)
        return

    if args.plan_only:
        print("\n[plan-only] 将执行（不发送不写盘）：")
        for q in queries:
            print(f"  [{q['order']:>2}] [{q['query_mode']:<15}] "
                  f"{q['canonical_term'][:36]:<36} -> {q['query_string'][:70]}")
        print(f"\n[plan-only] 结束：{len(queries)} 条 × depth={args.depth}"
              f"（Scopus cursor 25/page）。")
        return

    # ── 检索 ──
    def run_offline() -> None:
        con = connect_cache_ro(os.path.join(BASE, "data", "cache", "scopus_cache.db"))
        rows = {q: j for q, j in con.execute("SELECT query_string, result_json FROM api_cache")}
        con.close()
        from search_engine.cache import SearchCache
        n_miss = 0
        for q in queries:
            cached = rows.get(q["query_string"])
            if cached is None:
                n_miss += 1
                q["error"] = "NO_CACHE"
                print(f"  [{q['order']:>2}] MISSING-CACHE {q['canonical_term'][:40]}")
                continue
            res = SearchCache._deserialize_result(cached)
            measure(q, res, resolver, r_old_keys)
        if n_miss:
            print(f"\n[WARN] {n_miss} 条缓存缺失——请用普通模式（浏览器）跑一次补全。")

    async def run_engine() -> None:
        from search_engine.engine import ScopusSearchEngine
        engine = ScopusSearchEngine(data_dir=args.engine_data_dir)
        await engine.start()
        try:
            for q in queries:
                try:
                    # S1 是正式 snapshot：强制 skip_cache=True——engine.search 缓存命中
                    # 即返回且不区分 limit，若吃 pilot(depth=50) 缓存会导致 S1_SEEN_SET
                    # 错误地基于 top50 口径（= 用户禁止的"把 pilot 当 S1 seen universe"）。
                    res = await engine.search(q["query_string"], limit=args.depth,
                                              skip_cache=True)
                except Exception as e:
                    print(f"    [WARN] {q['canonical_term'][:30]}: S1 检索失败 {e}")
                    q["error"] = str(e)
                    await asyncio.sleep(1)
                    continue
                measure(q, res, resolver, r_old_keys)
                await asyncio.sleep(1)
        finally:
            await engine.close()

    def measure(q: dict, res, resolver: IdentityResolver,
                r_old_keys: set[str]) -> None:
        papers = res.papers
        total_hits = getattr(res, "total_count", len(papers))
        raw = len(papers)
        con = connect_cache_ro(os.path.join(BASE, "data", "cache", "scopus_cache.db"))
        rows: list[dict] = []
        for p in papers:
            key, info = paper_key_and_info(p, resolver, r_old_keys)
            eid, doi = info["eid"], info["doi"]
            title = (getattr(p, "title", None) or "").strip()
            abstract = (getattr(p, "abstract", None) or "").strip()
            if not abstract:
                abstract = _usable_text_from_cache(doi, eid, con)
            usable = is_usable(title, abstract, bool(key))
            rows.append({"key": key, "eid": eid, "doi": doi, "title": title,
                         "abstract": abstract, "in_old": info["in_old"],
                         "usable": usable})
        con.close()
        seen: set[str] = set()
        unknown = 0
        usable_all = 0
        for r in rows:
            if r["key"] is None:
                unknown += 1
            else:
                seen.add(r["key"])
                if r["usable"]:
                    usable_all += 1
        unique_returned = len(seen)
        # new_vs_S0（相对 S0 全集）
        new_keys = {r["key"] for r in rows if r["key"] and not r["in_old"]}
        new_usable = sum(1 for r in rows
                         if r["key"] in new_keys and r["usable"])
        q.update({
            "total_hits": total_hits,
            "raw_returned": raw,
            "unique_returned": unique_returned,
            "identity_resolved": unique_returned,
            "identity_unknown": unknown,
            "usable_returned": usable_all,
            "new_vs_S0": len(new_keys),
            "new_usable_vs_S0": new_usable,
            "new_keys": sorted(new_keys),
            "depth_saturated": (raw >= args.depth and total_hits > args.depth),
            "rows": rows,
        })
        cen = "C" if q["depth_saturated"] else " "
        print(f"  [{q['order']:>2}/{len(queries)}]{cen} hits={total_hits:>6} "
              f"raw={raw:>4} uniq={unique_returned:>4} new_S0={len(new_keys):>4} "
              f"newU={new_usable:>4} unk={unknown:>2} {q['canonical_term'][:34]}")

    if args.offline_cache:
        run_offline()
    else:
        asyncio.run(run_engine())

    finalize(args, queries, resolver, r_old_keys, query_set_hash,
             execution_timestamp=datetime.datetime.now().isoformat(timespec="seconds"),
             rebuilt=False)


def rebuild_from_raw(args, queries: list[dict], resolver: IdentityResolver,
                     r_old_keys: set[str], query_set_hash: str) -> None:
    """从已落盘 s1_raw_records.json 重算（不检索）。

    raw records 里已含 key/eid/doi/title/abstract/in_old/usable，直接回填 queries
    的 rows + per-query 指标，再走同一 finalize。execution_timestamp 保留原始检索
    时间（读旧 snapshot），另加 rebuilt_at 标注修复时点。
    """
    raw = json.load(open(args.raw, encoding="utf-8"))
    rbq = raw["records_by_query"]
    old_snap = None
    old_exec = None
    old_hits: dict[str, int] = {}
    if os.path.exists(args.snapshot):
        try:
            old_snap = json.load(open(args.snapshot, encoding="utf-8"))
            old_exec = old_snap.get("execution_timestamp")
            for q in old_snap.get("queries", []):
                th = q.get("total_hits")
                if th is not None:
                    old_hits[q["query_string"]] = th
        except Exception:
            pass
    for q in queries:
        recs = rbq.get(q["query_string"], [])
        rows = [dict(r) for r in recs]
        q["rows"] = rows
        seen = {r["key"] for r in rows if r["key"]}
        new_keys = {r["key"] for r in rows if r["key"] and not r["in_old"]}
        total_hits = old_hits.get(q["query_string"],
                                  len(recs) if len(recs) < 500 else None)
        q.update({
            "total_hits": total_hits,
            "raw_returned": len(rows),
            "unique_returned": len(seen),
            "identity_resolved": len(seen),
            "identity_unknown": sum(1 for r in rows if not r["key"]),
            "usable_returned": sum(1 for r in rows if r["usable"]),
            "new_vs_S0": len(new_keys),
            "new_usable_vs_S0": sum(1 for r in rows
                                    if r["key"] in new_keys and r["usable"]),
            "new_keys": sorted(new_keys),
            "depth_saturated": (len(rows) >= args.depth
                                and (total_hits or 0) > args.depth),
        })
    print("[rebuild-from-raw] rows 回填完成，重算 snapshot / seen set / repair seen set...")
    finalize(args, queries, resolver, r_old_keys, query_set_hash,
             execution_timestamp=old_exec or
             datetime.datetime.now().isoformat(timespec="seconds"),
             rebuilt=True)


def finalize(args, queries: list[dict], resolver: IdentityResolver,
             r_old_keys: set[str], query_set_hash: str,
             execution_timestamp: str, rebuilt: bool) -> None:
    """统一汇总 + 落盘（S0 ∪ repair 语义，2026-08-29 用户修复定稿）。"""
    repair_keys: set[str] = set()
    repair_usable: set[str] = set()
    raw_records: dict[str, list[dict]] = {}
    for q in queries:
        qrows = q.get("rows", [])
        raw_records[q["query_string"]] = qrows
        for r in qrows:
            if r["key"]:
                repair_keys.add(r["key"])
                if r["usable"]:
                    repair_usable.add(r["key"])

    search_s1_keys = r_old_keys | repair_keys            # ← SEARCH_S1_SEEN_UNION
    search_s1_usable = set(r_old_keys) | repair_usable   # S0 keys 均满足 build_r_old 文本口径

    snap_id = old_snap_id(args, query_set_hash)
    aggregate = {
        "s0_unique": len(r_old_keys),
        "repair_union_unique": len(repair_keys),
        "repair_union_usable_unique": len(repair_usable),
        "repair_shared_with_s0": len(repair_keys & r_old_keys),
        "repair_new_vs_s0": len(repair_keys - r_old_keys),
        "repair_new_usable_vs_s0": len(repair_usable - r_old_keys),
        "search_s1_union_unique": len(search_s1_keys),
        "search_s1_union_usable_unique": len(search_s1_usable),
        "identity_unknown_total": sum(q.get("identity_unknown", 0) for q in queries),
        "per_query_new_sum_undeduped": sum(q.get("new_vs_S0", 0) for q in queries),
        "per_query_new_usable_sum_undeduped": sum(q.get("new_usable_vs_S0", 0)
                                                  for q in queries),
        "check_s0_plus_new": len(r_old_keys) + len(repair_keys - r_old_keys),
    }
    snapshot = {
        "search_snapshot_id": snap_id,
        "status": "FROZEN",
        "query_set_hash": query_set_hash,
        "query_set_file": os.path.basename(args.json),
        "query_strings": [q["query_string"] for q in
                          sorted(queries, key=lambda x: x["order"])],
        "retrieval_budget": {"depth": args.depth,
                             "pagination": "Scopus cursor 25/page",
                             "note": "统一正式 budget；S0 同为 depth=500"},
        "execution_timestamp": execution_timestamp,
        "rebuilt_at": (datetime.datetime.now().isoformat(timespec="seconds")
                       if rebuilt else None),
        "execution_mode": "offline_cache" if getattr(args, "offline_cache", False)
                          else ("rebuild_from_raw" if rebuilt else "engine"),
        "development_source": "AUDIT_R01",
        "search_baseline": "S0",
        "eligible_for_r01_evaluation": False,
        "identity_rule": "EID 优先（pages/publications 三格式统一）+ DOI fallback"
                         " + EID<->DOI union-find bridge",
        "seen_set_semantics": "SEARCH_S1_SEEN_UNION = S0_SEEN ∪ S1_REPAIR_QUERY_UNION"
                              "（R02 agent_seen 唯一依据；2026-08-29 修复定稿）",
        "aggregate": aggregate,
        "queries": [{k: q[k] for k in ("order", "canonical_term", "query_mode",
                                       "query_string", "source", "total_hits",
                                       "raw_returned", "unique_returned",
                                       "identity_resolved", "identity_unknown",
                                       "usable_returned", "new_vs_S0",
                                       "new_usable_vs_S0", "depth_saturated")
                     if k in q} | ({"error": q["error"]} if "error" in q else {})
                    for q in queries],
        "seen_set_file": os.path.basename(args.seen_set),
        "repair_seen_set_file": os.path.basename(args.repair_seen),
        "raw_records_file": os.path.basename(args.raw),
        "interpretation": "S1 expanded the retrieved candidate set and discovered "
                          "additional relevant literature. NOT a recall claim; "
                          "recall improvement must be established by independent "
                          "Audit R02.",
    }
    seen_set = {
        "search_snapshot_id": snap_id,
        "query_set_hash": query_set_hash,
        "depth": args.depth,
        "frozen_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "definition": "SEARCH_S1_SEEN_UNION = S0_SEEN ∪ S1_REPAIR_QUERY_UNION；"
                      "R02 agent_seen 的唯一依据（S0 内论文只要在 S0 即视为 seen）",
        "size": len(search_s1_keys),
        "keys": sorted(search_s1_keys),
    }
    repair_seen = {
        "search_snapshot_id": snap_id,
        "query_set_hash": query_set_hash,
        "depth": args.depth,
        "frozen_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "definition": "S1_REPAIR_QUERY_UNION：仅 19 条 repair query 检索结果 union"
                      "（不含 S0）；审计语义参考，不作为 R02 agent_seen",
        "size": len(repair_keys),
        "keys": sorted(repair_keys),
    }

    for path, obj in ((args.snapshot, snapshot), (args.seen_set, seen_set),
                      (args.repair_seen, repair_seen),
                      (args.raw_out or args.raw, {"search_snapshot_id": snap_id,
                                                  "query_set_hash": query_set_hash,
                                                  "records_by_query": raw_records})):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
    print(f"\n[OK] snapshot    : {args.snapshot}")
    print(f"[OK] seen set    : {args.seen_set} ({len(search_s1_keys)} keys = S0∪repair)")
    print(f"[OK] repair seen : {args.repair_seen} ({len(repair_keys)} keys)")
    print(f"[OK] raw         : {args.raw}")
    print(f"\n=== S1 aggregate（修复后语义）===")
    for k, v in aggregate.items():
        print(f"  {k:<34} {v}")
    assert aggregate["check_s0_plus_new"] == aggregate["search_s1_union_unique"], \
        "check: |S0| + new_vs_S0 != |S0 ∪ repair| —— seen set 语义错误！"


def old_snap_id(args, query_set_hash: str) -> str:
    """rebuild 时沿用原 snapshot_id（同一检索），否则新生成。"""
    if os.path.exists(args.snapshot):
        try:
            old = json.load(open(args.snapshot, encoding="utf-8"))
            sid = old.get("search_snapshot_id")
            if sid and sid.endswith(query_set_hash[:8]):
                return sid
        except Exception:
            pass
    return f"S1_{datetime.datetime.now():%Y%m%d_%H%M%S}_{query_set_hash[:8]}"


if __name__ == "__main__":
    main()
