"""tools/evaluate_community_round1.py — v2.1 第一轮 community 检索评估（用户 2026-08-28 定稿）。

输入：community_round1_retrieval.json（⑥ 检索结果）+ query_family_runs_depth.json（R_old 基准）
指标（每 community 独立，用户定）：
  MCG_c            = |R_c − R_old|                      marginal candidate gain
  RetrievalNovelty = |R_c − R_old| / |R_c|              新增占比（核心指标）
  new venues / new year regions                        触达扩展
  QGS1 recovered   = |R_c ∩ QGS134| − 旧命中            REGRESSION ONLY（不证明泛化）
  provenance：community → queries → retrieved → new

用法：
  python tools/evaluate_community_round1.py
"""
import argparse
import json
import os
import sqlite3
import sys
from collections import Counter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

RETRIEVAL_PATH = os.path.join(BASE, "data", "exports", "community_round1_retrieval.json")
DEPTH_RUN_PATH = os.path.join(BASE, "data", "exports", "query_family_runs_depth.json")
BENCH_PATH = os.path.join(BASE, "data", "exports", "pc_001_external_qgs_v1.json")
SCOPUS_CACHE = os.path.join(BASE, "data", "cache", "scopus_cache.db")
OUT_PATH = os.path.join(BASE, "data", "exports", "community_round1_evaluation.json")

ABSTRACT_MIN = 40   # Research-Usable 规则（用户 2026-08-28 冻结，与 audit_research_usability 一致）


def norm_eid(e: str) -> str:
    return (e or "").strip()


def norm_doi(d: str) -> str:
    return (d or "").strip().lower()


def load_scopus_meta() -> dict[str, dict]:
    """scopus_cache（immutable 只读）→ {doi: {title, abstract}}——usable 判定数据源。"""
    out = {}
    con = sqlite3.connect(f"file:{SCOPUS_CACHE}?mode=ro&immutable=1", uri=True)
    for pid, j in con.execute("SELECT paper_id, normalized_json FROM papers"):
        nj = json.loads(j)
        doi = norm_doi(nj.get("doi") or pid.replace("scopus:", ""))
        if doi:
            out.setdefault(doi, {"title": nj.get("title") or "",
                                 "abstract": (nj.get("abstract") or "").strip()})
    con.close()
    return out


def usable_of(rec: dict, meta: dict[str, dict]) -> bool:
    """Research-Usable = IdentityValid ∧ Title ∧ (Abstract ∨ FullText)。
    abstract/title 优先 scopus_cache（DOI 桥），record 自带字段 fallback。"""
    doi = norm_doi(rec.get("doi"))
    m = meta.get(doi, {})
    title = m.get("title") or rec.get("title") or ""
    abstract = m.get("abstract") or ""
    return bool(doi or norm_eid(rec.get("eid"))) and bool(title) \
        and len(abstract) >= ABSTRACT_MIN


def load_old_universe() -> set[str]:
    """R_old：depth run raw union（v2.0 已检索 EID 全集）。"""
    records = json.load(open(DEPTH_RUN_PATH, encoding="utf-8"))["records"]
    old: set[str] = set()
    for recs in records.values():
        for r in recs:
            eid = norm_eid(r.get("eid"))
            if eid:
                old.add(eid)
    return old


def load_bench_eids() -> tuple[set[str], dict[str, dict]]:
    bench = json.load(open(BENCH_PATH, encoding="utf-8"))
    b_scopus = [p for p in bench["papers"] if p.get("scopus_eligibility") == "IN_SCOPUS"]
    eids = {norm_eid(p.get("scopus_eid")) for p in b_scopus}
    return eids, {norm_eid(p.get("scopus_eid")): p for p in b_scopus}


def load_qgs_usable_eids(meta: dict[str, dict]) -> set[str]:
    """QGS 134 篇里满足 Research-Usable 的 eid 集合（同一把尺子，DOI 桥）。"""
    bench = json.load(open(BENCH_PATH, encoding="utf-8"))
    out = set()
    for p in bench["papers"]:
        if p.get("scopus_eligibility") != "IN_SCOPUS":
            continue
        eid = norm_eid(p.get("scopus_eid"))
        doi = norm_doi(p.get("doi"))
        m = meta.get(doi, {})
        title = m.get("title") or p.get("title") or ""
        abstract = m.get("abstract") or ""
        if eid and title and len(abstract) >= ABSTRACT_MIN:
            out.add(eid)
    return out


def era_of(y) -> str:
    try:
        y = int(y)
    except (TypeError, ValueError):
        return "UNKNOWN"
    return "PRE_2006" if y <= 2005 else ("2006_2020" if y <= 2020 else "POST_2020")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--retrieval", default=RETRIEVAL_PATH)
    args = ap.parse_args()

    data = json.load(open(args.retrieval, encoding="utf-8"))
    old = load_old_universe()
    bench_eids, idx = load_bench_eids()
    meta = load_scopus_meta()
    qgs_usable = load_qgs_usable_eids(meta)
    print("=" * 80)
    print(f"Community ROUND 1 Evaluation（R_old = depth run raw {len(old)}）")
    print(f"usable 口径: Identity ∧ Title ∧ Abstract（abstract 源 scopus_cache，"
          f"QGS usable = {len(qgs_usable)}/134）")
    print("=" * 80)

    out = {"version": "round1_usable", "created_at": "2026-08-28",
           "note": "QGS1 为 REGRESSION ONLY，不证明泛化；usable 规则见 audit_research_usability.py",
           "communities": {}}
    for cid, c in data.get("communities", {}).items():
        uniq: dict[str, dict] = {}
        venues: Counter = Counter()
        years: list[int] = []
        for q in c["queries"]:
            for rec in q["records"]:
                if not rec["eid"]:
                    continue
                if rec["eid"] not in uniq:
                    uniq[rec["eid"]] = rec
                if rec.get("venue"):
                    venues[rec["venue"]] += 1
                if rec.get("year") and str(rec["year"]).isdigit():
                    years.append(int(rec["year"]))
        rc = set(uniq.keys())
        new = rc - old
        mcg = len(new)
        ret_novelty = mcg / len(rc) if rc else 0.0
        # usable 口径（用户定：真正可研究的新增）
        usable_rc = {e for e in rc if usable_of(uniq[e], meta)}
        usable_new = {e for e in new if usable_of(uniq[e], meta)}
        mcg_usable = len(usable_new)
        usable_ret_novelty = (mcg_usable / len(usable_rc)
                              if usable_rc else 0.0)
        qgs_new = len(rc & bench_eids)                # 本轮命中 QGS（regression）
        qgs_usable_new = len(rc & qgs_usable)         # 命中且 research-usable 的 QGS
        new_venues = {v for v in venues if v}
        yr_eras = Counter(era_of(y) for y in years)
        print(f"\n{cid}: retrieved_unique={len(rc)} | new-to-Candidate={mcg} "
              f"| RetrievalNovelty={ret_novelty*100:.1f}%")
        print(f"  usable: retrieved={len(usable_rc)} | new_usable(MCG_usable)={mcg_usable} "
              f"| UsableRetrievalNovelty={usable_ret_novelty*100:.1f}%")
        print(f"  new venues={len(new_venues)}（top: {', '.join(list(new_venues)[:3]) or '-'}）")
        print(f"  year eras={dict(yr_eras)} | QGS1 命中={qgs_new} "
              f"（usable {qgs_usable_new}）（REGRESSION ONLY）")
        out["communities"][cid] = {
            "retrieved_unique": len(rc),
            "new_to_candidate_MCG": mcg,
            "retrieval_novelty": round(ret_novelty, 4),
            "retrieved_usable": len(usable_rc),
            "new_usable_MCG": mcg_usable,
            "usable_retrieval_novelty": round(usable_ret_novelty, 4),
            "new_venues": sorted(new_venues),
            "year_eras": dict(yr_eras),
            "qgs1_hit": qgs_new,
            "qgs1_usable_hit": qgs_usable_new,
            "new_paper_eids": sorted(new),
        }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n✓ 已写: {OUT_PATH}")


if __name__ == "__main__":
    main()
