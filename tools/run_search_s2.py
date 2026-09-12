"""tools/run_search_s2.py — Search S2（v1.1：depth=1000）——S1 的加深版（2026-08-29 用户定）。

背景：S1（19 queries, depth=500）已冻结为基线，保留不改。用户审 R02 后决定加深：
19 条中只有 5 条撞 500 上限会真正多抓（rank 501-1000）：
  acrylate 726 / composite resin 1570 / volume shrinkage 3534 /
  methacrylate 1662 / low shrinkage 2086
其余 14 条总 hits < 500，depth=1000 不会多出结果。
S2 = SAME 19 queries, depth=1000，统一正式参数。

版本纪律（用户定）：
- S1 = 19 queries depth=500：FROZEN 基线（s1_seen_set.json 8532 不动）
- S2 = 19 queries depth=1000：新版本 v1.1（s2_seen_set.json = S0 ∪ repair1000）
- R02 已被看过 → 依据 R02 选择 1000 属 R02-informed 决策；S2 的正式召回率必须用
  fresh R03 验证。ΔSeen / Recovery 只是工程诊断，不是 recall 证明。

输出（写死冻结元数据，development_source=AUDIT_R01 / search_baseline=S0）：
  s2_candidate_snapshot.json / s2_seen_set.json / s2_repair_seen_set.json /
  s2_raw_records.json / s2_delta_vs_s1.json（ΔSeen + R02 recovery）

用法：
  python tools/run_search_s2.py --plan-only
  python tools/run_search_s2.py [--depth 1000] [--r02-labels <filled.json>]
  python tools/run_search_s2.py --offline-cache
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
from search_engine.identity import scopus_cache_key  # noqa: E402
from build_r02_seen import (  # noqa: E402
    build_s1_found_sets as _s1_found, resolve_seen_s1 as _resolve_seen,
    _norm_doi, _norm_title,
)
from run_search_s1 import is_usable  # noqa: E402


def _usable_text_from_cache(doi: str | None, eid: str | None, con) -> str:
    if not doi and not eid:
        return ""
    try:
        if doi:
            row = con.execute("SELECT normalized_json FROM papers WHERE paper_id=?",
                              (scopus_cache_key(doi),)).fetchone()
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
                            "s2_candidate_snapshot.json")
DEFAULT_SEEN = os.path.join(BASE, "data", "exports", "terminology",
                            "s2_seen_set.json")
DEFAULT_REPAIR = os.path.join(BASE, "data", "exports", "terminology",
                              "s2_repair_seen_set.json")
DEFAULT_RAW = os.path.join(BASE, "data", "exports", "terminology",
                           "s2_raw_records.json")
DEFAULT_DELTA = os.path.join(BASE, "data", "exports", "terminology",
                             "s2_delta_vs_s1.json")
S1_SEEN_PATH = os.path.join(BASE, "data", "exports", "terminology",
                            "s1_seen_set.json")
DEFAULT_DEPTH = 1000


def main():
    ap = argparse.ArgumentParser(description="Search S2 执行（19 queries depth=1000，v1.1）")
    ap.add_argument("--json", default=DEFAULT_QS)
    ap.add_argument("--depth", type=int, default=DEFAULT_DEPTH)
    ap.add_argument("--snapshot", default=DEFAULT_SNAP)
    ap.add_argument("--seen-set", default=DEFAULT_SEEN)
    ap.add_argument("--repair-seen", default=DEFAULT_REPAIR)
    ap.add_argument("--raw", default=DEFAULT_RAW)
    ap.add_argument("--delta", default=DEFAULT_DELTA)
    ap.add_argument("--r02-labels", default="",
                    help="R02 filled labels（可选）：算 R02 miss 被 S2 追回数")
    ap.add_argument("--offline-cache", action="store_true",
                    help="从 api_cache 重算（不发请求；仅验证，depth 受缓存限制）")
    ap.add_argument("--engine-data-dir", default="data")
    ap.add_argument("--plan-only", action="store_true")
    args = ap.parse_args()

    qs_data = json.load(open(args.json, encoding="utf-8"))
    queries = qs_data["queries"]
    assert len(queries) == qs_data["S1_FINAL_count"] == 19
    query_set_hash = qs_data["query_set_hash"]
    h = hashlib.sha256("\n".join(q["query_string"] for q in
                                 sorted(queries, key=lambda x: x["order"]))
                       .encode("utf-8")).hexdigest()
    if h != query_set_hash:
        print("[FATAL] query_set_hash mismatch——S2 必须复用 S1 冻结的 19 条")
        sys.exit(2)

    resolver, r_old_keys = build_r_old()
    print(f"S2 queries = {len(queries)} | S0 = {len(r_old_keys)} | depth = {args.depth}"
          f" | hash = {query_set_hash[:16]}...（SAME 19 queries as S1）")

    if args.plan_only:
        print("\n[plan-only] 将执行（不发送不写盘）：")
        for q in queries:
            print(f"  [{q['order']:>2}] {q['canonical_term'][:36]:<36} -> {q['query_string'][:66]}")
        print(f"\n[plan-only] 结束：{len(queries)} 条 × depth={args.depth}。")
        return

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
                print(f"  MISSING-CACHE {q['canonical_term'][:40]}")
                continue
            measure(q, SearchCache._deserialize_result(cached), resolver, r_old_keys, args)
        if n_miss:
            print(f"[WARN] {n_miss} 条缓存缺失。")

    async def run_engine() -> None:
        from search_engine.engine import ScopusSearchEngine
        engine = ScopusSearchEngine(data_dir=args.engine_data_dir)
        await engine.start()
        try:
            for q in queries:
                try:
                    # 强制 bypass 缓存：S1(depth=500) 缓存命中即返回，S2 必须抓满 1000
                    res = await engine.search(q["query_string"], limit=args.depth,
                                              skip_cache=True)
                except Exception as e:
                    print(f"    [WARN] {q['canonical_term'][:30]}: S2 检索失败 {e}")
                    q["error"] = str(e)
                    await asyncio.sleep(1)
                    continue
                measure(q, res, resolver, r_old_keys, args)
                await asyncio.sleep(1)
        finally:
            await engine.close()

    def measure(q: dict, res, resolver, r_old_keys, args) -> None:
        papers = res.papers
        total_hits = getattr(res, "total_count", len(papers))
        raw = len(papers)
        con = connect_cache_ro(os.path.join(BASE, "data", "cache", "scopus_cache.db"))
        rows = []
        for p in papers:
            key, info = paper_key_and_info(p, resolver, r_old_keys)
            eid, doi = info["eid"], info["doi"]
            title = (getattr(p, "title", None) or "").strip()
            abstract = (getattr(p, "abstract", None) or "").strip()
            if not abstract:
                abstract = _usable_text_from_cache(doi, eid, con)
            rows.append({"key": key, "eid": eid, "doi": doi, "title": title,
                         "abstract": abstract, "in_old": info["in_old"],
                         "usable": is_usable(title, abstract, bool(key))})
        con.close()
        seen = {r["key"] for r in rows if r["key"]}
        new_keys = {r["key"] for r in rows if r["key"] and not r["in_old"]}
        q.update({
            "total_hits": total_hits, "raw_returned": raw,
            "unique_returned": len(seen),
            "identity_unknown": sum(1 for r in rows if not r["key"]),
            "usable_returned": sum(1 for r in rows if r["usable"]),
            "new_vs_S0": len(new_keys),
            "new_usable_vs_S0": sum(1 for r in rows
                                    if r["key"] in new_keys and r["usable"]),
            "new_keys": sorted(new_keys),
            "depth_saturated": (raw >= args.depth and total_hits > args.depth),
            "rows": rows,
        })
        cen = "C" if q["depth_saturated"] else " "
        print(f"  [{q['order']:>2}/{len(queries)}]{cen} hits={total_hits:>6} "
              f"raw={raw:>4} uniq={len(seen):>4} new_S0={len(new_keys):>4} "
              f"{q['canonical_term'][:34]}")

    if args.offline_cache:
        run_offline()
    else:
        asyncio.run(run_engine())

    # ── 汇总 ──
    s2_repair_keys = set()
    raw_records = {}
    for q in queries:
        qrows = q.get("rows", [])
        raw_records[q["query_string"]] = qrows
        for r in qrows:
            if r["key"]:
                s2_repair_keys.add(r["key"])
    s2_keys = set(r_old_keys) | s2_repair_keys        # SEARCH_S2_SEEN_UNION

    snap_id = f"S2_{datetime.datetime.now():%Y%m%d_%H%M%S}_{query_set_hash[:8]}"
    aggregate = {
        "s0_unique": len(r_old_keys),
        "repair_union_unique": len(s2_repair_keys),
        "repair_shared_with_s0": len(s2_repair_keys & r_old_keys),
        "repair_new_vs_s0": len(s2_repair_keys - r_old_keys),
        "search_s2_union_unique": len(s2_keys),
        "depth": args.depth,
    }
    # ΔSeen vs S1
    s1_keys = set(json.load(open(S1_SEEN_PATH, encoding="utf-8"))["keys"])
    delta = {
        "s1_seen": len(s1_keys), "s2_seen": len(s2_keys),
        "delta_seen_new_vs_s1": len(s2_keys - s1_keys),
        "s1_only": len(s1_keys - s2_keys),
        "shared": len(s1_keys & s2_keys),
        "note": "S2 = S0 ∪ repair1000；S1 = S0 ∪ repair500（8532）",
    }

    # ── R02 recovery（--r02-labels，可选）──
    recovery = None
    if args.r02_labels and os.path.exists(args.r02_labels):
        labs = json.load(open(args.r02_labels, encoding="utf-8"))["labels"]
        found_s1 = _s1_found()
        found_s2 = _s2_found(queries)
        rel = [l for l in labs if l.get("label") == "RELEVANT"]
        seen_s1 = _resolve_seen([l["paper_id"] for l in rel], found_s1)
        seen_s2 = _resolve_seen([l["paper_id"] for l in rel], found_s2)
        n_false_s1 = 0
        recovered = []
        unknown_resolved = []
        for l in rel:
            v1 = seen_s1.get(l["paper_id"], {}).get("agent_seen_s1", "UNKNOWN")
            v2 = seen_s2.get(l["paper_id"], {}).get("agent_seen_s1", "UNKNOWN")
            if v1 == "FALSE":
                n_false_s1 += 1
                if v2 == "TRUE":
                    recovered.append(l["paper_id"])
            elif v1 == "UNKNOWN" and v2 == "TRUE":
                unknown_resolved.append(l["paper_id"])
        recovery = {
            "r02_resolved_miss_s1": n_false_s1,
            "recovered_by_s2": len(recovered),
            "recovery_rate": round(len(recovered) / n_false_s1, 4) if n_false_s1 else None,
            "recovered_wids": recovered,
            "unknown_resolved_by_s2": len(unknown_resolved),
            "unknown_resolved_wids": unknown_resolved,
            "note": "FALSE→TRUE = R02 miss 被 depth=1000 追回；UNKNOWN→TRUE = identity 改善",
        }
        print(f"\n[R02 recovery] miss={n_false_s1} recovered={len(recovered)}"
              f" ({len(recovered) / n_false_s1:.1%}) | UNKNOWN→TRUE={len(unknown_resolved)}")

    snapshot = {
        "search_snapshot_id": snap_id,
        "version": "S2 / v1.1", "status": "FROZEN",
        "query_set_hash": query_set_hash,
        "query_set_file": os.path.basename(args.json),
        "retrieval_budget": {"depth": args.depth, "pagination": "Scopus cursor 25/page",
                             "note": "S1(depth=500) 保留冻结基线；S2 统一 depth=1000，"
                                     "仅 5 条撞 500 上限的 query 多抓 rank 501-1000"},
        "execution_timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "execution_mode": "offline_cache" if args.offline_cache else "engine",
        "development_source": "AUDIT_R01", "search_baseline": "S0",
        "eligible_for_r01_evaluation": False,
        "identity_rule": "EID 优先 + DOI fallback + EID<->DOI union-find bridge",
        "aggregate": aggregate,
        "delta_vs_s1": delta,
        "r02_recovery": recovery,
        "queries": [{k: q[k] for k in ("order", "canonical_term", "query_mode",
                                       "query_string", "source", "total_hits",
                                       "raw_returned", "unique_returned",
                                       "identity_unknown", "usable_returned",
                                       "new_vs_S0", "new_usable_vs_S0",
                                       "depth_saturated") if k in q}
                    for q in queries],
        "interpretation": "S2 相对 S1 的 ΔSeen/Recovery 是工程诊断，不是 recall 证明；"
                          "S2 是 R02-informed 决策，正式召回率需 fresh R03 验证。",
    }
    seen_set = {
        "search_snapshot_id": snap_id, "query_set_hash": query_set_hash,
        "depth": args.depth,
        "frozen_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "definition": "SEARCH_S2_SEEN_UNION = S0_SEEN ∪ S2_REPAIR_QUERY_UNION(depth=1000)；"
                      "R03 agent_seen 依据（如启用）",
        "size": len(s2_keys), "keys": sorted(s2_keys),
    }
    repair_seen = {
        "search_snapshot_id": snap_id, "query_set_hash": query_set_hash,
        "depth": args.depth,
        "frozen_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "definition": "S2_REPAIR_QUERY_UNION（depth=1000）",
        "size": len(s2_repair_keys), "keys": sorted(s2_repair_keys),
    }
    for path, obj in ((args.snapshot, snapshot), (args.seen_set, seen_set),
                      (args.repair_seen, repair_seen),
                      (args.delta, {"search_snapshot_id": snap_id,
                                    "delta_vs_s1": delta,
                                    "r02_recovery": recovery}),
                      (args.raw, {"search_snapshot_id": snap_id,
                                  "query_set_hash": query_set_hash,
                                  "records_by_query": raw_records})):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
    print(f"\n[OK] snapshot    : {args.snapshot}")
    print(f"[OK] seen set    : {args.seen_set} ({len(s2_keys)} keys = S0∪repair1000)")
    print(f"[OK] delta vs S1 : {args.delta}")
    print(f"\n=== S2 aggregate ===")
    for k, v in aggregate.items():
        print(f"  {k:<30} {v}")
    print(f"\n=== ΔSeen vs S1 ===")
    for k, v in delta.items():
        print(f"  {k:<24} {v}")


def _s2_found(queries: list[dict]) -> dict:
    """S2 三通道 found sets（repair1000 部分；S0 部分由 build_s1_found_sets 提供）。"""
    found = _s1_found()          # S0 ∪ S1 repair（含 S1 全部——S2 ⊇ S1，无妨）
    for q in queries:
        for r in q.get("rows", []):
            if r.get("eid"):
                found["eids"].add(str(r["eid"]).strip())
            if r.get("doi"):
                found["dois"].add(_norm_doi(r["doi"]))
            if r.get("title"):
                found["titles"].add(_norm_title(r["title"]))
    return found


if __name__ == "__main__":
    main()
