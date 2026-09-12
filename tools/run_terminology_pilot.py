"""tools/run_terminology_pilot.py — v3.0 Terminology Pilot（2026-08-29 用户规格冻结）。

对 Queryability v1 selected 25 条（PRE-PILOT_SANITY）做 depth=50 统一预算 pilot，
收集进入 S1 开发证据的正式指标。口径全部冻结（用户定稿，pilot 后不改实现）：

输入：data/exports/terminology/queryability_greedy.json（25 条，含 query template/顺序）
      + repair_term_candidates_sanitized.json（family_id -> miss_ids，补 audit evidence）
depth = 50（统一预算，避免比较失真）
baseline = Search S0 = build_r_old()（旧 Candidate DB：depth run ∩ scopus_cache 有文本，
          EID 优先 canonical + EID↔DOI bridge）

每条 query 输出：
  order / family_id / canonical_term / query_mode / query_string
  audit_miss_gain / audit_miss_ids          （R01 development evidence，与 pilot 指标分栏）
  total_hits / raw_returned                  （Scopus 总命中 / 实际抓取）
  unique_returned / identity_resolved / identity_unknown
  NCG / NCG_usable                           （|R_q - S0|）
  MCG / MCG_usable                           （按 greedy 顺序累计 |R_qi - (S0 ∪ R_<i)|）
  duplicate_with_S0 / duplicate_with_previous_pilot / duplicate_rate
  efficiency_mcg / efficiency_mcg_usable     （= X / log1p(total_hits)，只报告不排序）
  depth_saturated                            （= raw_returned >= depth and total_hits > depth）

冻结口径：
  identity：EID -> DOI -> WID -> normalized title（复用 IdentityResolver；不另发明 dedup）
  NCG 与 MCG 严格分开；MCG 顺序 = 当前 greedy 顺序（pilot 后不得重排 = hindsight leakage）
  MCG_usable 主指标：usable = IdentityValid ∧ Title ∧ (Abstract ∨ FullText)
  duplicate_rate = 1 - MCG / unique_returned（unique_returned = canonical 去重后，不含 identity unknown）
  Pilot v1 只报告 Efficiency，不按它重新排序

防泄漏 metadata（写死）：
  development_source = AUDIT_R01 / search_baseline = S0 / queryability_version = v1
  depth = 50 / selection_order_frozen = true / eligible_for_r01_evaluation = false

用法：
  python tools/run_terminology_pilot.py --plan-only           # 只读预览
  python tools/run_terminology_pilot.py                       # 真实检索（CloakBrowser）
  python tools/run_terminology_pilot.py --offline-cache       # 复用 api_cache 重算
"""
import argparse
import asyncio
import csv
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tools"))

from pilot_round3_query_utility import (  # noqa: E402
    IdentityResolver, build_r_old, connect_cache_ro, extract_eid,
    normalize_doi, paper_key_and_info,
)
from search_engine.identity import scopus_cache_key  # noqa: E402

DEFAULT_IN = os.path.join(BASE, "data", "exports", "terminology",
                          "queryability_greedy.json")
DEFAULT_FAMS = os.path.join(BASE, "data", "exports", "terminology",
                            "repair_term_candidates_sanitized.json")
DEFAULT_OUT = os.path.join(BASE, "data", "exports", "terminology",
                           "pilot_results.json")
DEFAULT_CSV = os.path.join(BASE, "data", "exports", "terminology",
                           "pilot_query_utility.csv")

DEPTH = 50


# ── usability（IdentityValid ∧ Title ∧ (Abstract ∨ FullText)）──
def _usable_text_from_cache(doi: str | None, eid: str | None,
                            con) -> str:
    """从 scopus_cache 补 abstract（pilot 检索返回可能无 abstract）。"""
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


def is_usable(title: str, abstract: str, key_ok: bool) -> bool:
    return key_ok and bool(title and title.strip()) and bool(abstract and abstract.strip())


def main():
    ap = argparse.ArgumentParser(description="Terminology Pilot（depth=50，口径冻结）")
    ap.add_argument("--json", default=DEFAULT_IN,
                    help="queryability_greedy.json（25 条 selected）")
    ap.add_argument("--families", default=DEFAULT_FAMS,
                    help="repair_term_candidates（family_id -> miss_ids）")
    ap.add_argument("--depth", type=int, default=DEPTH)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--csv", default=DEFAULT_CSV)
    ap.add_argument("--offline-cache", action="store_true",
                    help="从 api_cache 读缓存重算（不发请求；缓存缺失标 NO_CACHE）")
    ap.add_argument("--skip-cache", action="store_true",
                    help="普通模式强制真实 Scopus 检索")
    ap.add_argument("--engine-data-dir", default="data",
                    help="ScopusSearchEngine data_dir")
    ap.add_argument("--covered-keys", default=None,
                    help="pre-covered canonical keys JSON（list[str]）——MCG 相对 "
                         "S0 ∪ covered 计算（S1 rewrite pilot 用：covered=S1_CORE union）")
    ap.add_argument("--plan-only", action="store_true",
                    help="只读：打印 query 清单 + S0 规模，不发请求")
    args = ap.parse_args()

    # covered baseline（S0 ∪ pre-covered；MCG 相对它计算）
    covered_pre: set[str] = set()
    if args.covered_keys and os.path.exists(args.covered_keys):
        covered_pre = set(json.load(open(args.covered_keys, encoding="utf-8")))
        print(f"[covered] pre-covered keys = {len(covered_pre)}"
              f"（MCG 相对 S0 ∪ covered）")

    d_in = json.load(open(args.json, encoding="utf-8"))
    sel = d_in.get("selected", d_in.get("queries", []))
    # families 源：sanitized 优先，回退 term_families.json（FULL，同样含 family_id/miss_ids）
    fams_path = args.families
    if not os.path.exists(fams_path):
        alt = os.path.join(BASE, "data", "exports", "terminology", "term_families.json")
        print(f"[WARN] {fams_path} 不存在——回退 {alt}")
        fams_path = alt
    fams_data = json.load(open(fams_path, encoding="utf-8"))
    fams = fams_data.get("candidates") if "candidates" in fams_data else fams_data["families"]
    fid2miss = {f["family_id"]: f.get("miss_ids", []) for f in fams}
    fid2gain = {f["family_id"]: f.get("miss_support", 0) for f in fams}

    # 顺序 = greedy 顺序（冻结）
    queries = []
    for i, s in enumerate(sel, 1):
        q = s.get("queryability") or {}
        queries.append({
            "order": i,
            "family_id": s.get("family_id"),
            "canonical_term": s.get("canonical_term"),
            "query_mode": q.get("q", s.get("query_mode", "ANCHOR")),
            "query_string": q.get("query", s.get("query_string")),
            "audit_miss_gain": fid2gain.get(s.get("family_id"), 0),
            "audit_miss_ids": fid2miss.get(s.get("family_id"), []),
        })

    resolver, r_old_keys = build_r_old()
    print(f"selected = {len(queries)} | R_old(S0, canonical keys) = {len(r_old_keys)} "
          f"| depth = {args.depth}")

    if args.plan_only:
        print("\n[plan-only] 将执行（不发送）：")
        for q in queries:
            print(f"  [{q['order']:>2}] [{q['query_mode']:<6}] "
                  f"miss={q['audit_miss_gain']:>2} {q['canonical_term'][:38]:<38} "
                  f"-> {q['query_string'][:70]}")
        print(f"\n[plan-only] 结束：{len(queries)} 条 × depth={args.depth}。")
        return

    # ── 检索（offline cache 或 engine）──
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
            measure(q, res, resolver, r_old_keys, args)
        if n_miss:
            print(f"\n[WARN] {n_miss} 条缓存缺失——请用普通模式（浏览器）跑一次补全。")

    async def run_engine() -> None:
        from search_engine.engine import ScopusSearchEngine
        engine = ScopusSearchEngine(data_dir=args.engine_data_dir)
        await engine.start()
        try:
            for q in queries:
                try:
                    res = await engine.search(q["query_string"], limit=args.depth,
                                              skip_cache=args.skip_cache)
                except Exception as e:
                    print(f"    [WARN] {q['canonical_term'][:30]}: pilot 失败 {e}")
                    q["error"] = str(e)
                    await asyncio.sleep(1)
                    continue
                measure(q, res, resolver, r_old_keys, args)
                await asyncio.sleep(1)
        finally:
            await engine.close()

    def measure(q: dict, res, resolver: IdentityResolver,
                r_old_keys: set[str], args) -> None:
        papers = res.papers
        total_hits = getattr(res, "total_count", len(papers))
        raw = len(papers)
        # 补 abstract（scopus_cache）
        con = connect_cache_ro(os.path.join(BASE, "data", "cache", "scopus_cache.db"))
        rows: list[dict] = []
        for p in papers:
            key, info = paper_key_and_info(p, resolver, r_old_keys)
            eid, doi = info["eid"], info["doi"]
            title = (getattr(p, "title", None) or "").strip()
            abstract = (getattr(p, "abstract", None) or "").strip()
            if not abstract:
                abstract = _usable_text_from_cache(doi, eid, con)
            rows.append({"key": key, "eid": eid, "doi": doi, "title": title,
                         "abstract": abstract, "in_old": info["in_old"]})
        con.close()
        # unique（canonical 去重）
        seen: set[str] = set()
        unknown = 0
        for r in rows:
            if r["key"] is None:
                unknown += 1
            else:
                seen.add(r["key"])
        unique_returned = len(seen)
        # NCG（相对 S0）
        ncg_keys = {r["key"] for r in rows if r["key"] and not r["in_old"]}
        ncg = len(ncg_keys)
        ncg_usable = sum(1 for r in rows
                         if r["key"] in ncg_keys and is_usable(r["title"], r["abstract"], True))
        # MCG（相对 S0 ∪ covered_pre ∪ 前序 pilot，greedy 顺序）
        mcg_keys = ncg_keys - covered_pre - covered_so_far
        mcg = len(mcg_keys)
        mcg_usable = sum(1 for r in rows
                         if r["key"] in mcg_keys and is_usable(r["title"], r["abstract"], True))
        covered_so_far.update(mcg_keys)
        # MCG 论文明细（Relevance layer 输入：canonical key + title + abstract）
        mcg_papers = [{"key": r["key"], "eid": r["eid"], "doi": r["doi"],
                       "title": r["title"], "abstract": r["abstract"]}
                      for r in rows if r["key"] in mcg_keys]
        # duplicate
        dup_s0 = unique_returned - ncg
        dup_prev = ncg - mcg
        dup_rate = round(1 - mcg / unique_returned, 4) if unique_returned else None
        # efficiency（只报告）
        log_hits = __import__("math").log1p(max(total_hits, 0))
        q.update({
            "total_hits": total_hits,
            "raw_returned": raw,
            "unique_returned": unique_returned,
            "identity_resolved": unique_returned,
            "identity_unknown": unknown,
            "NCG": ncg, "NCG_usable": ncg_usable,
            "MCG": mcg, "MCG_usable": mcg_usable,
            "duplicate_with_S0": dup_s0,
            "duplicate_with_previous_pilot": dup_prev,
            "duplicate_rate": dup_rate,
            "efficiency_mcg": round(mcg / log_hits, 4) if log_hits else None,
            "efficiency_mcg_usable": round(mcg_usable / log_hits, 4) if log_hits else None,
            "depth_saturated": (raw >= args.depth and total_hits > args.depth),
            "mcg_papers": mcg_papers,
        })
        cen = "C" if q["depth_saturated"] else " "
        print(f"  [{q['order']:>2}/{len(queries)}]{cen} hits={total_hits:>6} "
              f"raw={raw:>3} NCG={ncg:>3} MCG={mcg:>3} MCGu={mcg_usable:>3} "
              f"dup={dup_rate} {q['canonical_term'][:36]}")

    covered_so_far: set[str] = set()

    if args.offline_cache:
        run_offline()
    else:
        asyncio.run(run_engine())

    # ── 输出 ──
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    out = {
        "version": "terminology_pilot_v1",
        "development_source": "AUDIT_R01",
        "search_baseline": "S0",
        "queryability_version": "v1",
        "depth": args.depth,
        "selection_order_frozen": True,
        "eligible_for_r01_evaluation": False,
        "r_old_size": len(r_old_keys),
        "identity_rule": "EID -> DOI -> WID -> normalized title（IdentityResolver union-find）",
        "mcg_order_note": "MCG 严格按 Queryability greedy 顺序累计；pilot 后不得重排",
        "usability_rule": "IdentityValid ∧ Title ∧ (Abstract ∨ FullText)",
        "queries": queries,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    with open(args.csv, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["order", "family_id", "canonical_term", "query_mode", "query_string",
                    "audit_miss_gain", "audit_miss_ids",
                    "total_hits", "raw_returned", "unique_returned",
                    "identity_resolved", "identity_unknown",
                    "NCG", "NCG_usable", "MCG", "MCG_usable",
                    "duplicate_with_S0", "duplicate_with_previous_pilot", "duplicate_rate",
                    "efficiency_mcg", "efficiency_mcg_usable", "depth_saturated"])
        for q in queries:
            w.writerow([q["order"], q["family_id"], q["canonical_term"], q["query_mode"],
                        q["query_string"], q["audit_miss_gain"],
                        ";".join(q.get("audit_miss_ids", [])),
                        q.get("total_hits"), q.get("raw_returned"), q.get("unique_returned"),
                        q.get("identity_resolved"), q.get("identity_unknown"),
                        q.get("NCG"), q.get("NCG_usable"), q.get("MCG"), q.get("MCG_usable"),
                        q.get("duplicate_with_S0"), q.get("duplicate_with_previous_pilot"),
                        q.get("duplicate_rate"),
                        q.get("efficiency_mcg"), q.get("efficiency_mcg_usable"),
                        q.get("depth_saturated")])
    print(f"\n[OK] JSON: {args.out}")
    print(f"[OK] CSV : {args.csv}")


if __name__ == "__main__":
    main()
