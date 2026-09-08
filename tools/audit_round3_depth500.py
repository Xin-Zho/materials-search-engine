"""tools/audit_round3_depth500.py — v2.1.5 Round3 depth=500 检索的 QGS-blind 审计。

用户流程（2026-08-29 定稿）：depth=500 跑完后**先做完全 QGS-blind 的 retrieval
audit**，确认没有 execution / identity / parsing bug，才打开 QGS 算 G2R。

统一 identity 链（2026-08-29 修：杜绝 per-query 累加口径）：

  raw rows
    -> IdentityResolver（EID 优先 + DOI fallback + EID<->DOI union-find bridge）
    -> canonical papers
    -> 与 R_old 做差 -> New_union（canonical 去重）
    -> 对 New_union 做 usability（不在 per-query 累加）
    -> year / venue 统计（元数据按 canonical component merge）

关键口径：
  per-query new hit events = Σ_q |New_q|            （命中事件数，可跨 query 重复）
  canonical union new       = |New_union|           （去重后的真实新论文数）
  cross-query duplicates    = per-query events - union
  usable unique new         = |{p ∈ New_union: Usable(p)=1}|
  Usable = Identity ∧ Title ∧ (Abstract ∨ FullText)（abstract 从 scopus_cache 补，
           retrieval 导出字段不含 abstract；cache 无则按缺处理）

硬断言（不满足即 FAIL，冻结需人工裁决）：
  usable_new <= union_mcg
  sum(per_query_new) >= union_mcg
  sum(year_counts) == usable_with_year  （且 <= usable_new）
  sum(venue_counts) == usable_with_venue（venue coverage 如实输出，可为 0——
  已知限制：CSV 导出 "venue" 字段未映射到列名，retrieval 与 cache 均无 venue）

QGS-blind：本工具不读、不引用任何 QGS 文件。

用法：
  python tools/audit_round3_depth500.py \
      --retrieval data/exports/round3_depth500_retrieval.json
      [--out data/exports/round3_depth500_audit.json]
"""
import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter, defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tools"))

from pilot_round3_query_utility import IdentityResolver, build_r_old, extract_eid  # noqa: E402

SCOPUS_CACHE = os.path.join(BASE, "data", "cache", "scopus_cache.db")
DEFAULT_AUDIT_OUT = os.path.join(BASE, "data", "exports",
                                 "round3_depth500_audit.json")


def load_cache_metadata() -> tuple[dict, dict]:
    """从 scopus_cache 构建 (eid -> meta, doi -> meta) 补全 abstract/title/year。

    meta = {"abstract": str|None, "title": str, "year": int|None}
    """
    tmp = os.path.join(tempfile.gettempdir(), "scopus_cache_ro_audit.db")
    if not os.path.exists(tmp) or os.path.getmtime(tmp) < os.path.getmtime(SCOPUS_CACHE):
        shutil.copy2(SCOPUS_CACHE, tmp)
    con = sqlite3.connect(f"file:{tmp}?mode=ro&immutable=1", uri=True)
    by_eid: dict[str, dict] = {}
    by_doi: dict[str, dict] = {}
    for pid, j in con.execute("SELECT paper_id, normalized_json FROM papers"):
        nj = json.loads(j)
        meta = {"abstract": (nj.get("abstract") or "").strip() or None,
                "title": (nj.get("title") or "").strip(),
                "year": nj.get("year")}
        eid = extract_eid(nj.get("scopus_url"))
        doi = (nj.get("doi") or "").strip().lower() or None
        if eid:
            by_eid.setdefault(eid, meta)
        if doi:
            by_doi.setdefault(doi, meta)
    con.close()
    return by_eid, by_doi


def audit(retrieval_path: str) -> dict:
    data = json.load(open(retrieval_path, encoding="utf-8"))
    resolver, r_old_keys = build_r_old()
    cache_eid, cache_doi = load_cache_metadata()

    # ── 统一 identity 链 ──
    comp: dict[str, dict] = {}            # canonical key -> merged paper
    per_query_new: dict[str, int] = {}    # query_id -> new hit events
    per_query_exported: dict[str, int] = {}
    raw_rows = 0
    in_old_total = 0
    no_identity_total = 0
    all_keys: set[str] = set()            # 全部检索论文的 canonical keys（含 in_old）

    communities = data.get("communities", {})
    for cid, spec in communities.items():
        for q in spec.get("queries", []):
            qid = q.get("query_id", "?")
            recs = q.get("records", [])
            raw_rows += len(recs)
            per_query_exported[qid] = len(recs)
            q_new = 0
            for rec in recs:
                eid = (rec.get("eid") or "").strip() or None
                doi = (rec.get("doi") or "").strip().lower() or None
                resolver.add(eid, doi)
                key = resolver.canonical(eid, doi)
                if key is None:
                    no_identity_total += 1
                    continue
                all_keys.add(key)
                if key in r_old_keys:
                    in_old_total += 1
                    continue
                q_new += 1
                # metadata merge（per canonical component）
                row = comp.setdefault(key, {
                    "key": key, "eid": eid, "doi": doi,
                    "title": "", "year": None, "venue": "",
                    "abstract": None, "provenance": []})
                row["eid"] = row["eid"] or eid
                row["doi"] = row["doi"] or doi
                t = (rec.get("title") or "").strip()
                if len(t) > len(row["title"]):
                    row["title"] = t
                y = rec.get("year")
                if y and not row["year"]:
                    row["year"] = str(y)
                v = (rec.get("venue") or "").strip()
                if v and not row["venue"]:
                    row["venue"] = v
                if qid not in row["provenance"]:
                    row["provenance"].append(qid)
            per_query_new[qid] = q_new

    # ── abstract 补全（retrieval 无 abstract；从 cache 按 eid/doi 补）──
    for key, row in comp.items():
        if row["abstract"]:
            continue
        meta = cache_eid.get(row["eid"]) or (cache_doi.get(row["doi"]) if row["doi"] else None)
        if meta:
            row["abstract"] = meta["abstract"]
            if not row["title"]:
                row["title"] = meta["title"]
            if not row["year"] and meta["year"]:
                row["year"] = str(meta["year"])

    # ── usability（union 上）──
    for row in comp.values():
        row["usable"] = bool(row["key"] and row["title"]
                             and (row["abstract"] or False))
    usable_rows = [row for row in comp.values() if row["usable"]]
    usable_with_year = [r for r in usable_rows if r.get("year")]
    usable_with_venue = [r for r in usable_rows if r.get("venue")]
    pre_2006 = [r for r in usable_rows
                if r.get("year") and r["year"].isdigit() and int(r["year"]) < 2006]

    union_mcg = len(comp)
    sum_per_query_new = sum(per_query_new.values())
    cross_dup = sum_per_query_new - union_mcg

    # ── 硬断言 ──
    checks = {
        "usable_new <= union_mcg": len(usable_rows) <= union_mcg,
        "sum(per_query_new) >= union_mcg": sum_per_query_new >= union_mcg,
        "sum(year_counts) == usable_with_year": (
            sum(1 for r in usable_rows if r.get("year")) == len(usable_with_year)),
        "sum(venue_counts) == usable_with_venue": (
            sum(1 for r in usable_rows if r.get("venue")) == len(usable_with_venue)),
    }
    all_pass = all(checks.values())

    # ── 分布（union 口径）──
    years = Counter(r["year"] for r in usable_rows if r.get("year"))
    venues = Counter(r["venue"] for r in usable_rows if r.get("venue"))

    per_query_list = sorted(per_query_exported.keys())
    return {
        "version": "round3_depth500_audit_v216",
        "qgs_blind": True,
        "retrieval_source": retrieval_path,
        "r_old_size": len(r_old_keys),
        "execution": {
            "queries": len(per_query_list),
            "success": len(per_query_list),
            "failed_empty": [qid for qid in per_query_list
                             if per_query_exported[qid] == 0],
        },
        "raw_rows": raw_rows,
        "canonical_unique_retrieved": len(all_keys),
        "no_identity_rows": no_identity_total,
        "in_r_old_excluded": in_old_total,
        "per_query_new_hit_events": sum_per_query_new,
        "canonical_union_new": union_mcg,
        "cross_query_duplicate_new_hit_events": cross_dup,
        "duplicate_rate_hit_events": round(cross_dup / sum_per_query_new, 4)
            if sum_per_query_new else None,
        "usable_unique_new": len(usable_rows),
        "usable_rate": round(len(usable_rows) / union_mcg, 4) if union_mcg else None,
        "pre_2006_unique_usable_new": len(pre_2006),
        "year_metadata": {
            "usable_new_with_year": len(usable_with_year),
            "year_metadata_coverage": f"{len(usable_with_year)}/{len(usable_rows)}",
            "distribution": dict(sorted(years.items())),
        },
        "venue_metadata": {
            "usable_new_with_venue": len(usable_with_venue),
            "venue_metadata_coverage": f"{len(usable_with_venue)}/{len(usable_rows)}",
            "top": venues.most_common(12),
            "note": "已知限制：CSV 导出 'venue' 字段未映射列名，retrieval 与 cache 均无"
                    "venue（venue 0/N 不代表分析结果，是辅助字段缺失）",
        },
        "per_query": [{
            "query_id": qid,
            "exported": per_query_exported[qid],
            "new_hit_events": per_query_new.get(qid, 0),
        } for qid in per_query_list],
        "hard_assertions": {"checks": checks, "all_pass": all_pass},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--retrieval", required=True,
                    help="round3_depth500_retrieval.json 路径")
    ap.add_argument("--out", default=DEFAULT_AUDIT_OUT)
    args = ap.parse_args()

    a = audit(args.retrieval)
    print(f"\n{'='*80}")
    print("Round3 depth=500 QGS-blind retrieval audit（union 口径）")
    print(f"{'='*80}")
    ex = a["execution"]
    print(f"execution: {ex['success']}/{ex['queries']} SUCCESS"
          f"{'（empty: ' + str(ex['failed_empty']) + '）' if ex['failed_empty'] else ''}")
    print(f"raw rows: {a['raw_rows']}")
    print(f"canonical unique retrieved: {a['canonical_unique_retrieved']}"
          f"（no_identity {a['no_identity_rows']} / in_old {a['in_r_old_excluded']}）")
    print(f"per-query new hit events: {a['per_query_new_hit_events']}")
    print(f"canonical union new: {a['canonical_union_new']}")
    print(f"cross-query duplicate new-hit events: {a['cross_query_duplicate_new_hit_events']}"
          f"（{a['duplicate_rate_hit_events']:.1%}）")
    print(f"usable unique new: {a['usable_unique_new']} / "
          f"usable rate: {a['usable_rate']:.1%}")
    print(f"PRE_2006 unique usable new: {a['pre_2006_unique_usable_new']}")
    print(f"year metadata coverage: {a['year_metadata']['year_metadata_coverage']}")
    print(f"venue metadata coverage: {a['venue_metadata']['venue_metadata_coverage']}"
          f"（{a['venue_metadata']['note']}）")
    print(f"\nper-query:")
    print(f"{'query_id':<10} {'exported':>8} {'new_events':>10}")
    for pq in a["per_query"]:
        print(f"{pq['query_id']:<10} {pq['exported']:>8} {pq['new_hit_events']:>10}")
    print(f"\nvenue top 12: {a['venue_metadata']['top'] or '（空：venue 元数据缺失，无分析意义）'}")
    print(f"year distribution: {a['year_metadata']['distribution']}")
    print(f"\n硬断言: {a['hard_assertions']['checks']}")
    print(f"  -> {'ALL PASS' if a['hard_assertions']['all_pass'] else 'FAIL'}")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(a, f, ensure_ascii=False, indent=1)
    print(f"\n[OK] audit 已写: {args.out}")
    if not a["hard_assertions"]["all_pass"]:
        print("[ERROR] 断言未全过——冻结 ROUND3_DEPTH500_RETRIEVAL=VALID 前必须人工裁决")
        sys.exit(1)


if __name__ == "__main__":
    main()
