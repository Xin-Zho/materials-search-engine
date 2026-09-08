#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/source_time_audit.py — S4 Step 4：SOURCE / TIME audit（2026-08-30 用户定）。

口径（用户定）：
- **SOURCE 是正式诊断通道；TIME 不单独作为 repair channel**，只作为解释
  SOURCE/CITATION 失效的机制特征（避免模糊标签 SOURCE/TIME）。
- SOURCE_PRESENT = TRUE/FALSE；SOURCE_GAP = TRUE（current sources 均缺失）/
  FALSE（source 中存在）/ UNKNOWN（无法可靠判断）。Relevant ∧ NotReachableByCurrentSources
  才可能 SOURCE_GAP——若论文已在 Scopus/OpenAlex 只是 Search 没找到，**不是 source failure**。
- TIME 量化（第一版只保留原始变量，不搞复杂模型）：
  Age = AuditDate − PublicationDate；CitationDegree = referenced_works + cited_by_count；
  CitationMaturity = f(Age, CitedBy, References) → LOW/MED/HIGH。
- 判定树（citation unreachable 时）：
    source 不存在        → SOURCE_GAP
    source 存在，degree 低/太新 → CITATION_MATURITY
    source 存在，network 正常  → TERM / COMMUNITY（看语言入口）

★ 已知限制（必须标注）：openalex_cache 是检索时抓取的，部分 work 的 referenced_works/
cited_by_count 缺失（refs=0 且 cited=0）——"citation 不可达"可能是**数据缺失假象**
而非真实 graph 远；此类标 DATA_GAP=WARN。R03 universe 论文全部来自 OpenAlex 宽检索快照
→ openalex_present=TRUE 全 37（抽样保证）；scopus_present 无法从 openalex 可靠判定
（indexed_in 不含 scopus）→ UNKNOWN。

输出：s4_source_time_audit.json
  per-miss {year, age_years, referenced_works, cited_by_count, openalex_present,
  scopus_present, citation_1hop, citation_2hop, citation_maturity, data_gap,
  term_entry, community, verdict}
"""
import argparse
import datetime
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "tools"))

from build_r02_seen import _norm_title  # noqa: E402

T = os.path.join(BASE, "data", "exports", "terminology")
MISSES = os.path.join(T, "s4_residual_misses.json")
REACH = os.path.join(T, "s4_citation_reachability.json")
TERM = os.path.join(T, "s4_term_evidence.json")
COMM = os.path.join(T, "s4_community_discovery.json")
OA_CACHE = os.path.join(BASE, "data", "cache", "openalex_cache.json")
DEFAULT_OUT = os.path.join(T, "s4_source_time_audit.json")
AUDIT_DATE = datetime.date(2026, 8, 30)


def load_oa_full() -> dict:
    """openalex 全字段元数据（cited_by_count/publication_year/type/is_paratext...）。"""
    meta = {}
    cache = json.load(open(OA_CACHE, encoding="utf-8"))
    for q, resp in cache.items():
        for w in resp.get("results", []):
            wid = (w.get("id") or "").replace("https://openalex.org/", "")
            if wid and wid not in meta:
                meta[wid] = {
                    "doi": (w.get("doi") or "").lower().replace("https://doi.org/", ""),
                    "title": w.get("title"),
                    "year": w.get("publication_year"),
                    "pub_date": w.get("publication_date"),
                    "cited_by_count": w.get("cited_by_count") or 0,
                    "referenced_works": w.get("referenced_works") or [],
                    "indexed_in": w.get("indexed_in") or [],
                    "type": w.get("type"),
                    "is_paratext": bool(w.get("is_paratext")),
                }
    return meta


def maturity(age: float, cited: int, refs: int) -> str:
    """启发式：LOW=新且低引用；MED；HIGH=老且被引。refs=0 时可能数据缺失，单独标注。"""
    if age >= 10 and cited >= 10:
        return "HIGH"
    if age >= 3 and cited >= 3:
        return "MED"
    return "LOW"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--misses", default=MISSES)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    data = json.load(open(args.misses, encoding="utf-8"))
    misses = [m for m in data["misses"] if m["wid"] != "W7110794929"]
    oa = load_oa_full()

    reach = {}
    if os.path.exists(REACH):
        for r in json.load(open(REACH, encoding="utf-8"))["per_miss"]:
            reach[r["miss_wid"]] = r
    comm = {}
    if os.path.exists(COMM):
        for c in json.load(open(COMM, encoding="utf-8"))["communities"]:
            for wid in c["members"]:
                comm[wid] = c["community_id"]

    rows = []
    for m in misses:
        wid = m["wid"]
        o = oa.get(wid, {})
        year = o.get("year") or m.get("year") or m.get("oa_year")
        age = round((AUDIT_DATE - datetime.date(year, 1, 1)).days / 365.25, 1) \
            if year else None
        cited = o.get("cited_by_count") or 0
        refs = len(o.get("referenced_works") or [])
        r = reach.get(wid, {})
        h1 = r.get("citation_1hop", False)
        h2 = r.get("citation_2hop", False)
        data_gap = refs == 0 and cited == 0
        mat = maturity(age or 0, cited, refs)
        # TERM entry：term evidence 中该 miss 是否有 eligible 词
        term_entry = None
        if os.path.exists(TERM):
            td = json.load(open(TERM, encoding="utf-8"))
            elig = [t["term_family"] for t in td["terms"] if t["eligible"] and wid in t["miss_wids"]]
            term_entry = "STRONG" if len(elig) >= 2 else ("MED" if elig else "WEAK")
        # verdict 判定树（citation unreachable 时）
        if h1 or h2:
            verdict = "CITATION_REACHABLE"
        else:
            if not o:
                verdict = "SOURCE_GAP"
            elif data_gap:
                verdict = "CITATION_MATURITY(DATA_GAP)"
            elif mat == "LOW":
                verdict = "CITATION_MATURITY"
            elif term_entry in ("STRONG", "MED"):
                verdict = "TERM_COMMUNITY"
            else:
                verdict = "UNEXPLAINED"
        rows.append({
            "wid": wid,
            "title": (m.get("oa_title") or m.get("title"))[:60],
            "year": year, "age_years": age,
            "referenced_works": refs, "cited_by_count": cited,
            "openalex_present": bool(o),
            "scopus_present": "UNKNOWN",
            "source_note": "openalex indexed_in 不含 scopus 字段，无法可靠判定（限制）",
            "citation_1hop": h1, "citation_2hop": h2,
            "citation_maturity": mat,
            "data_gap": data_gap,
            "data_gap_note": "refs=0 且 cited=0：openalex 缓存数据缺失——'不可达'可能是数据假象" if data_gap else None,
            "term_entry": term_entry,
            "community": comm.get(wid),
            "is_paratext": o.get("is_paratext"),
            "doc_type": o.get("type"),
            "verdict": verdict,
        })

    from collections import Counter
    vc = Counter(r["verdict"] for r in rows)
    print("=" * 78)
    print("S4 Step 4: SOURCE / TIME audit（37 canonical）")
    print("=" * 78)
    print(f"verdict 分布: {dict(vc)}")
    print(f"\n{'wid':<16}{'year':>6}{'age':>5}{'refs':>6}{'cited':>7}{'mat':>6}"
          f"{'gap':>5}{'term':>7}{'comm':<20}verdict")
    for r in rows:
        print(f"{r['wid']:<16}{str(r['year']):>6}{str(r['age_years']):>5}"
              f"{r['referenced_works']:>6}{r['cited_by_count']:>7}{r['citation_maturity']:>6}"
              f"{str(r['data_gap']):>5}{str(r['term_entry']):>7}"
              f"{(r['community'] or '—'):<20}{r['verdict']}")

    print("\n=== citation-unreachable 7 重点 QA ===")
    for r in rows:
        if not r["citation_1hop"] and not r["citation_2hop"]:
            print(f"  {r['wid']} {r['year']} age={r['age_years']} refs={r['referenced_works']} "
                  f"cited={r['cited_by_count']} data_gap={r['data_gap']} term={r['term_entry']} "
                  f"comm={r['community']} → {r['verdict']}")

    out = {
        "version": "s4_source_time_audit_v1",
        "frozen_at": "2026-08-30",
        "development_source": "AUDIT_R03",
        "denominator": "37 canonical",
        "definitions": {
            "SOURCE": "正式诊断通道；SOURCE_GAP=TRUE 仅当 current sources 均缺失（论文已在 source 只"
                      "是 Search 没找到 = 不是 source failure）",
            "TIME": "不单独作 repair channel；作为解释 SOURCE/CITATION 失效的机制特征（RECENCY/"
                    "CITATION_MATURITY）",
            "maturity": "f(Age, CitedBy, References) 启发式 LOW/MED/HIGH；refs=0∧cited=0 标 DATA_GAP",
            "scopus_present": "UNKNOWN——openalex indexed_in 不含 scopus 字段，无法可靠判定（数据源限制）",
        },
        "verdict_distribution": dict(vc),
        "rows": rows,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n[OK] written: {args.out}")


if __name__ == "__main__":
    main()
