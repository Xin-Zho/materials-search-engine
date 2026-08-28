"""Research-Usable Paper 审计（v2.1 前置，用户 2026-08-28 定稿）。

规则（用户冻结）：
    Usable = IdentityValid ∧ TitleAvailable ∧ (AbstractAvailable ∨ FullTextAvailable)
      - IdentityValid     : EID / DOI / W-id 至少一个非空
      - TitleAvailable    : title 非空（>=3 chars）
      - AbstractAvailable : abstract 非空（>=40 chars 过滤占位噪声）
      - FullTextAvailable : 本地 fulltext 文件存在（当前全局无 → False）

两层数据库（用户定稿）：
    Discovery Index    = 所有有稳定身份的检索论文（去重/citation/community discovery 用）
    Research-Usable DB = 满足 usability rule 的子集（relevance/KB/KG/研究分析用）

对两个集合应用完全相同规则：
    A. Candidate DB（depth run raw union @1000）
    B. QGS v1 B_scopus 134 篇

输出：
    Candidate total / usable / unusable（细分原因）
    QGS usable / unusable / UsableCoverage_QGS
    Discovery Candidate Recall（61.9% 基线） vs Research-Usable Recall
    RR_usable = |Agent usable ∩ QGS usable| / |QGS usable|

纯离线：读 depth run + scopus_cache(immutable 只读) + openalex_cache fallback，
不改任何数据、不联网。
"""
import argparse
import json
import os
import sqlite3
import sys
from collections import Counter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPTH_RUN_PATH = os.path.join(BASE, "data", "exports", "query_family_runs_depth.json")
BENCH_PATH = os.path.join(BASE, "data", "exports", "pc_001_external_qgs_v1.json")
SCOPUS_CACHE = os.path.join(BASE, "data", "cache", "scopus_cache.db")
OPENALEX_CACHE = os.path.join(BASE, "data", "cache", "openalex_cache.json")
OUT_PATH = os.path.join(BASE, "data", "exports", "research_usability_audit.json")

ABSTRACT_MIN = 40          # abstract 最小有效字符数
TITLE_MIN = 3
FULLTEXT_DIRS = ("data/fulltext", "data/pdf", "data/text")  # 本地全文目录（当前不存在）

# 不输出全量逐篇明细（可能很大），默认只输出汇总；--detail 打印 QGS 逐篇
def norm_eid(e: str) -> str:
    return (e or "").strip()

def norm_doi(d: str) -> str:
    return (d or "").strip().lower()

def title_ok(t) -> bool:
    return bool(t) and len(str(t).strip()) >= TITLE_MIN

def abstract_ok(a) -> bool:
    return bool(a) and len(str(a).strip()) >= ABSTRACT_MIN

def identity_ok(eid: str, doi: str, wid: str = "") -> bool:
    return bool(norm_eid(eid) or norm_doi(doi) or (wid or "").strip())


def load_fulltext_index() -> set[str]:
    """本地 fulltext 文件索引（当前无目录 → 空集）。key = eid/doi 归一化前缀。"""
    idx = set()
    for d in FULLTEXT_DIRS:
        p = os.path.join(BASE, d)
        if os.path.isdir(p):
            for fn in os.listdir(p):
                idx.add(fn.split(".")[0].strip().lower())
    return idx


def load_scopus_abstracts() -> dict[str, str]:
    """scopus_cache（immutable 只读）→ {doi: abstract}。

    paper_id 格式是 'scopus:{DOI}'（2026-08-28 实锤）——DOI 是唯一可靠桥，
    不能用 eid 匹配。19683/19783 有有效 abstract。
    """
    out = {}
    con = sqlite3.connect(f"file:{SCOPUS_CACHE}?mode=ro&immutable=1", uri=True)
    for pid, j in con.execute("SELECT paper_id, normalized_json FROM papers"):
        nj = json.loads(j)
        a = (nj.get("abstract") or "").strip()
        doi = norm_doi(nj.get("doi") or pid.replace("scopus:", ""))
        if abstract_ok(a) and doi:
            out.setdefault(doi, a)
    con.close()
    return out


def load_openalex_abstracts() -> dict[str, str]:
    """openalex_cache → {doi: abstract}（fallback，覆盖 scopus 缺的）。"""
    out = {}
    if not os.path.exists(OPENALEX_CACHE):
        return out
    d = json.load(open(OPENALEX_CACHE, encoding="utf-8"))
    for k, v in d.items():
        if not isinstance(v, dict):
            continue
        aii = v.get("abstract_inverted_index")
        if not aii:
            continue
        # inverted index → text
        pos = {}
        for w, idxs in aii.items():
            for i in idxs:
                pos[i] = w
        text = " ".join(pos[i] for i in sorted(pos))
        doi = norm_doi(v.get("doi") or "")
        if text and doi:
            out[doi] = text
    return out


def classify(entry: dict, abs_map: dict, fulltext_idx: set[str],
             doi_abs: dict) -> dict:
    """对单篇应用 Research-Usable 规则，返回判定明细。"""
    eid = norm_eid(entry.get("eid") or entry.get("scopus_eid") or entry.get("paper_id") or "")
    doi = norm_doi(entry.get("doi") or "")
    title = entry.get("title") or ""
    # abstract：优先 DOI 桥接 scopus_cache，fallback openalex（也按 DOI）
    abstract = abs_map.get(doi) or doi_abs.get(doi) or ""
    has_full = bool(eid.lower() in fulltext_idx or doi in fulltext_idx)

    idv = identity_ok(eid, doi)
    ttv = title_ok(title)
    abv = abstract_ok(abstract)
    usable = idv and ttv and (abv or has_full)

    if not idv:
        reason = "NO_IDENTITY"
    elif not ttv:
        reason = "NO_TITLE"
    elif not abv and not has_full:
        reason = "NO_ABSTRACT_NO_FULLTEXT"
    elif not abv and has_full:
        reason = "FULLTEXT_ONLY"   # ✅ 可用
    else:
        reason = "OK"

    return {
        "eid": eid, "doi": doi, "title": str(title)[:120],
        "identity_valid": idv, "title_available": ttv,
        "abstract_available": abv, "fulltext_available": has_full,
        "usable": usable, "reason": reason,
    }


def load_candidate_db() -> list[dict]:
    """Candidate DB = depth run raw union @1000（每篇保留 eid/doi/title）。"""
    recs = json.load(open(DEPTH_RUN_PATH, encoding="utf-8"))["records"]
    by_eid = {}
    for qid, rs in recs.items():
        for r in rs:
            if r["rank"] > 1000:
                continue
            e = norm_eid(r.get("eid"))
            if e and e not in by_eid:
                by_eid[e] = r
    return list(by_eid.values())


def load_qgs() -> list[dict]:
    bench = json.load(open(BENCH_PATH, encoding="utf-8"))
    return [p for p in bench["papers"] if p.get("scopus_eligibility") == "IN_SCOPUS"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--detail", action="store_true", help="打印 QGS 逐篇判定")
    args = ap.parse_args()

    print("加载数据源（纯离线）...")
    abs_map = load_scopus_abstracts()
    doi_abs = load_openalex_abstracts()
    fulltext_idx = load_fulltext_index()
    print(f"  scopus_cache abstract: {len(abs_map)} | openalex doi-abstract fallback: "
          f"{len(doi_abs)} | 本地 fulltext: {len(fulltext_idx)}")

    candidates = load_candidate_db()
    qgs = load_qgs()

    cand_rows = [classify(c, abs_map, fulltext_idx, doi_abs) for c in candidates]
    qgs_rows = [classify(q, abs_map, fulltext_idx, doi_abs) for q in qgs]

    def summarize(rows, label):
        total = len(rows)
        usable = sum(1 for r in rows if r["usable"])
        rc = Counter(r["reason"] for r in rows)
        return total, usable, rc

    c_total, c_usable, c_rc = summarize(cand_rows, "candidate")
    q_total, q_usable, q_rc = summarize(qgs_rows, "qgs")

    # 交集（按 eid 匹配；QGS 的 usable 集合与 candidate usable 集合）
    cand_usable_eids = {r["eid"] for r in cand_rows if r["usable"] and r["eid"]}
    qgs_usable_eids = {r["eid"] for r in qgs_rows if r["usable"] and r["eid"]}
    rr_usable_num = len(qgs_usable_eids & cand_usable_eids)
    rr_usable = rr_usable_num / max(q_usable, 1)

    # Discovery Candidate Recall（旧基线，全 candidate 口径）
    cand_eids = {r["eid"] for r in cand_rows if r["eid"]}
    qgs_eids = {r["eid"] for r in qgs_rows if r["eid"]}
    rr_disc = len(cand_eids & qgs_eids) / max(q_total, 1)

    print("=" * 70)
    print("Research-Usable Audit（Usable = Identity ∧ Title ∧ (Abstract ∨ FullText)）")
    print("=" * 70)
    print(f"\nA. Candidate DB（depth≤1000 raw union）")
    print(f"  total             = {c_total}")
    print(f"  usable            = {c_usable}  ({c_usable/c_total*100:.1f}%)")
    for reason, n in c_rc.most_common():
        print(f"    {reason:<28} {n:>6}")
    print(f"\nB. QGS v1 B_scopus = {q_total}")
    print(f"  QGS usable        = {q_usable}  ({q_usable/q_total*100:.1f}%)")
    for reason, n in q_rc.most_common():
        print(f"    {reason:<28} {n:>6}")
    print(f"  UsableCoverage_QGS = {q_usable}/{q_total}")

    print(f"\n指标对比：")
    print(f"  Discovery Candidate Recall = {rr_disc*100:.1f}%  ({len(cand_eids & qgs_eids)}/{q_total})")
    print(f"  Research-Usable Recall     = {rr_usable*100:.1f}%  ({rr_usable_num}/{q_usable})")
    print(f"  差距（discovery 找到但不可用）= "
          f"{len(cand_eids & qgs_eids) - rr_usable_num} 篇")

    if args.detail:
        print("\nQGS 逐篇判定:")
        for r in sorted(qgs_rows, key=lambda x: (x["usable"], x["reason"])):
            print(f"  {'✅' if r['usable'] else '❌'} {r['reason']:<28} {r['eid']:<20} "
                  f"{r['title'][:55]}")

    out = {
        "created_at": "2026-08-28",
        "rule": "Usable = IdentityValid ∧ TitleAvailable ∧ (AbstractAvailable ∨ FullTextAvailable)",
        "data_sources": {
            "candidate": "query_family_runs_depth.json raw union @1000",
            "abstract": "scopus_cache.db(immutable) + openalex_cache fallback",
            "fulltext": "本地目录（当前无 → 全局 False）",
        },
        "candidate": {"total": c_total, "usable": c_usable,
                      "reasons": dict(c_rc)},
        "qgs": {"total": q_total, "usable": q_usable,
                "usable_coverage": round(q_usable / q_total, 4),
                "reasons": dict(q_rc)},
        "metrics": {
            "discovery_candidate_recall": round(rr_disc, 4),
            "research_usable_recall": round(rr_usable, 4),
            "found_but_unusable": len(cand_eids & qgs_eids) - rr_usable_num,
        },
        "qgs_detail": qgs_rows,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n✓ 已写: {OUT_PATH}")


if __name__ == "__main__":
    main()
