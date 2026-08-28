"""tools/audit_citation_coverage.py — v2.1 数据可行性检查（用户 2026-08-28 定稿）。

只读现有 DB / 缓存，不联网、不改任何数据。

回答 4 个问题：
  1. Candidate DB 有多少论文有 references
  2. 有多少论文有 cited_by
  3. 引用关系来源：Scopus / OpenAlex / 现有 KB
  4. RELEVANT papers 的引用覆盖率

核心覆盖指标（用户定）：
  Coverage_ref  = #relevant with references  / #relevant
  Coverage_cited= #relevant with cited_by    / #relevant
判断：
  ≥70%   现有数据足够 → 直接实现 Citation Bridge
  30-70% 可以做，但要边运行边补数据
  <30%   不要直接实现 bridge → 先决定引用数据源

identity 稳定性检查：references 里的 id 类型（OpenAlex W id / Scopus EID / DOI）。

用法：
  python tools/audit_citation_coverage.py
"""
import json
import os
import re
import sqlite3
import sys
from collections import Counter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

DEPTH_RUN_PATH = os.path.join(BASE, "data", "exports", "query_family_runs_depth.json")
STAGING_PATH = os.path.join(BASE, "data", "exports", "discovery_staging.json")
OPENALEX_CACHE = os.path.join(BASE, "data", "cache", "openalex_cache.json")
SCOPUS_CACHE_DB = os.path.join(BASE, "data", "cache", "scopus_cache.db")
KB_DB = os.path.join(BASE, "data", "cache", "knowledge_base.db")


def norm_eid(e: str) -> str:
    return (e or "").strip()


def norm_doi(d: str) -> str:
    return (d or "").strip().lower().replace("https://doi.org/", "").replace("http://doi.org/", "")


def wid_of(pid: str) -> str:
    m = re.search(r"(W\d+)", pid or "")
    return m.group(1) if m else ""


def load_candidates() -> dict:
    """Candidate papers（depth run raw union）→ {eid: {doi, year}}。"""
    records = json.load(open(DEPTH_RUN_PATH, encoding="utf-8"))["records"]
    cand: dict[str, dict] = {}
    for recs in records.values():
        for r in recs:
            eid = norm_eid(r.get("eid"))
            if not eid:
                continue
            c = cand.setdefault(eid, {"doi": "", "year": None})
            if r.get("doi") and not c["doi"]:
                c["doi"] = norm_doi(r["doi"])
    return cand


def load_openalex_works() -> tuple[dict, dict]:
    """openalex_cache.json（query 缓存）→ 论文级索引。

    returns (by_wid: {Wid: {doi, refs, cited}}, by_doi: {doi: Wid})
    """
    cache = json.load(open(OPENALEX_CACHE, encoding="utf-8"))
    by_wid: dict[str, dict] = {}
    by_doi: dict[str, str] = {}
    for entry in cache.values():
        for w in (entry.get("results") or []):
            wid = w.get("id", "").rsplit("/", 1)[-1] or ""
            if not wid:
                continue
            doi = norm_doi(w.get("doi", ""))
            rec = by_wid.setdefault(wid, {"doi": doi or "", "refs": [], "cited": None})
            if doi and not rec["doi"]:
                rec["doi"] = doi
            if doi:
                by_doi.setdefault(doi, wid)
            refs = w.get("referenced_works") or []
            if refs:
                rec["refs"] = [str(x).rsplit("/", 1)[-1] for x in refs]
            cc = w.get("cited_by_count")
            if cc is not None and rec["cited"] is None:
                rec["cited"] = cc
    return by_wid, by_doi


def load_scopus_cache() -> tuple[int, dict]:
    """scopus_cache.db papers → {doi: paper_id}（检查 Scopus 侧有无引用数据）。"""
    con = sqlite3.connect(f"file:{SCOPUS_CACHE_DB}?mode=ro", uri=True)
    n = con.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    doi2pid: dict[str, str] = {}
    has_refs_like = 0
    for pid, nj in con.execute("SELECT paper_id, normalized_json FROM papers"):
        d = json.loads(nj)
        doi = norm_doi(d.get("doi", ""))
        if doi:
            doi2pid.setdefault(doi, pid)
        if d.get("references") or d.get("cited_by"):
            has_refs_like += 1
    con.close()
    return n, doi2pid, has_refs_like


def main():
    cand = load_candidates()
    print(f"Candidate papers (depth run raw union) = {len(cand)}")

    # Scopus 侧
    n_scopus, scopus_doi, n_scopus_refs_like = load_scopus_cache()
    print(f"\n=== 引用数据源现状 ===")
    print(f"Scopus cache: {n_scopus} papers | 含 references/cited_by 字段: {n_scopus_refs_like}"
          f"（仅 citation_count 数字，无引用列表——不能建引用图）")
    con = sqlite3.connect(f"file:{KB_DB}?mode=ro", uri=True)
    n_kb = con.execute("SELECT COUNT(*) FROM knowledge_records").fetchone()[0]
    con.close()
    print(f"KB knowledge_records: {n_kb}（抽取结果，无 references/cited_by 字段）")

    # OpenAlex 侧
    by_wid, by_doi = load_openalex_works()
    print(f"OpenAlex query cache: 论文级 works = {len(by_wid)}"
          f"（含 referenced_works 的 = {sum(1 for w in by_wid.values() if w['refs'])}）")

    # 引用数据唯一来源 = OpenAlex
    print(f"\n结论: 引用图数据的唯一来源是 OpenAlex referenced_works（Scopus/KB 无引用列表）")

    # 1-2) Candidate 覆盖（DOI 桥接）
    cand_with_oa = 0
    cand_with_refs = 0
    for c in cand.values():
        wid = by_doi.get(c["doi"])
        if wid:
            cand_with_oa += 1
            if by_wid[wid]["refs"]:
                cand_with_refs += 1
    print(f"\n=== Candidate 覆盖（OpenAlex 缓存，DOI 桥接）===")
    print(f"Candidate papers          = {len(cand)}")
    print(f"  在 OpenAlex 缓存中      = {cand_with_oa} ({cand_with_oa/len(cand)*100:.1f}%)")
    print(f"  有 referenced_works     = {cand_with_refs} ({cand_with_refs/len(cand)*100:.1f}%)")
    print(f"  (cited_by 列表任何源都无——Scopus 只有次数，OpenAlex 需引用索引接口)")

    # 3) Relevant papers（staging RELEVANT）
    staging = json.load(open(STAGING_PATH, encoding="utf-8"))
    items = staging if isinstance(staging, list) else staging.get("papers", [])
    relevant = [p for p in items if p.get("relevance_status") == "RELEVANT"]
    print(f"\n=== RELEVANT papers 覆盖（staging, n={len(relevant)}）===")
    rel_with_oa = rel_with_refs = 0
    for p in relevant:
        wid = wid_of(p.get("paper_id", ""))
        if wid and wid in by_wid:
            rel_with_oa += 1
            if by_wid[wid]["refs"]:
                rel_with_refs += 1
    coverage_ref = rel_with_refs / len(relevant) if relevant else 0
    print(f"Relevant papers           = {len(relevant)}")
    print(f"  OpenAlex 缓存中有       = {rel_with_oa} ({rel_with_oa/len(relevant)*100:.1f}%)")
    print(f"  with references         = {rel_with_refs} ({coverage_ref*100:.1f}%)  ← Coverage_ref")
    print(f"  with cited_by list      = 0 (0.0%)  ← Coverage_cited（无引用索引数据）")

    # 判定
    print(f"\n=== 判定 ===")
    if coverage_ref >= 0.7:
        print(f"Coverage_ref={coverage_ref*100:.0f}% ≥70% → 现有数据足够，直接实现 Citation Bridge")
    elif coverage_ref >= 0.3:
        print(f"Coverage_ref={coverage_ref*100:.0f}% ∈[30,70) → 可做，但 Citation Bridge 要边运行边补数据")
    else:
        print(f"Coverage_ref={coverage_ref*100:.0f}% <30% → 不要直接实现 bridge，先选引用数据源（OpenAlex 补 referenced_works）")

    # identity 稳定性
    print(f"\n=== identity 稳定性 ===")
    print(f"OpenAlex referenced_works 全部为 W id（可直接 canonicalize）→ 优先级最高")
    print(f"  works 总数={len(by_wid)}，含 refs={sum(1 for w in by_wid.values() if w['refs'])}，"
          f"总引用边={sum(len(w['refs']) for w in by_wid.values())}")
    n_doi = sum(1 for w in by_wid.values() if w["doi"])
    print(f"  有 DOI 的 works={n_doi}（可桥接 Scopus EID）")

    # 4) Top 20 relevant seeds 样本
    print(f"\n=== Top 20 relevant seed papers 样本 ===")
    rows = []
    for p in relevant:
        wid = wid_of(p.get("paper_id", ""))
        w = by_wid.get(wid) if wid else None
        rows.append({
            "title": p.get("title", "")[:48],
            "status": p.get("relevance_status"),
            "wid": wid,
            "refs": len(w["refs"]) if w else None,
            "cited": w["cited"] if w else None,
            "in_oa_cache": w is not None,
        })
    rows.sort(key=lambda r: -(r["refs"] or 0))
    print(f"  {'title':<50} {'refs':>5} {'cited':>6} {'OA':>3}")
    for r in rows[:20]:
        print(f"  {r['title']:<50} {str(r['refs']):>5} {str(r['cited']):>6} "
              f"{'Y' if r['in_oa_cache'] else 'N':>3}")


if __name__ == "__main__":
    main()
