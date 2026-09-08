"""tools/evaluate_hop2_round2.py — STEP 7 正式评估：G2R + GQ1-6 逐层归因（用户 2026-08-28 定稿）。

输入：community_hop2_round2_retrieval.json（Scopus 执行结果，用户跑）
     + QGS bench + research_usability_audit.json + depth run（R_old）
输出：data/exports/community_hop2_round2_evaluation.json

指标（用户冻结）：
  MCG_008         = |R_008 − R_old|                       marginal candidate gain
  UsableMCG_008   = 新增且 usable
  RetrievalNovelty_008 = |R_008 − R_old| / |R_008|
  G2R             = #graph-visible missing QGS 被正式检索回 / 8（exact identity：EID/DOI）
  分层：GraphVisible → community represented → slot represented → query generated
        → Scopus result set contains → within export depth

失败归因（GQ1-6，对 8 篇 graph-visible QGS 中未 retrieval 的）：
  GQ1 COMMUNITY_TO_SLOT_GAP       在社区 supporting papers 但关键语言没进 slot
  GQ2 SLOT_TO_QUERY_GAP           进 slot 但没生成 query
  GQ3 QUERY_SET_GAP               query 执行但 Scopus 结果集不含该论文
  GQ4 EXPORT_DEPTH_GAP            结果集含但 rank > depth
  GQ5 IDENTITY_ERROR              identity 匹配失败（EID/DOI 变体）
  GQ6 LANGUAGE_UNDISCRIMINATIVE   社区语言太泛（与 registry/泛词重叠），query 无区分度
                                  ——v2.1.1 central≠expansion 机制要解决的核心
"""
import argparse
import json
import os
import sys
from collections import Counter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tools"))

RETRIEVAL_PATH = os.path.join(BASE, "data", "exports", "community_hop2_round2_retrieval.json")
QUERIES_PATH = os.path.join(BASE, "data", "exports", "community_hop2_round2_queries.json")
DEPTH_RUN_PATH = os.path.join(BASE, "data", "exports", "query_family_runs_depth.json")
BENCH_PATH = os.path.join(BASE, "data", "exports", "pc_001_external_qgs_v1.json")
AUDIT_PATH = os.path.join(BASE, "data", "exports", "research_usability_audit.json")
HOP2_PATH = os.path.join(BASE, "data", "exports", "citation_bridge_hop2.json")
OUT_PATH = os.path.join(BASE, "data", "exports", "community_hop2_round2_evaluation.json")

GRAPH_VISIBLE = 8   # hop2 正式 GraphRecall_QGS 分子（roadmap 冻结）


def norm_eid(e: str) -> str:
    return (e or "").strip()


def norm_doi(d: str) -> str:
    return (d or "").strip().lower().replace("https://doi.org/", "").replace("http://doi.org/", "")


def load_old_eids() -> set[str]:
    records = json.load(open(DEPTH_RUN_PATH, encoding="utf-8"))["records"]
    return {r["eid"].strip() for rs in records.values() for r in rs if r.get("eid")}


def load_qgs_meta() -> dict[str, dict]:
    """missing usable QGS → {eid: {doi, title, year}}（22 篇里 8 篇 graph-visible）。"""
    audit = json.load(open(AUDIT_PATH, encoding="utf-8"))
    qgs_usable = {d["eid"] for d in audit["qgs_detail"] if d["usable"] and d["eid"]}
    old = load_old_eids()
    missing = qgs_usable - old
    bench = json.load(open(BENCH_PATH, encoding="utf-8"))
    out = {}
    for p in bench["papers"]:
        e = norm_eid(p.get("scopus_eid"))
        if e in missing:
            out[e] = {"doi": norm_doi(p.get("doi")), "title": p.get("title") or "",
                      "year": p.get("year")}
    return out


def load_graph_visible_qgs() -> dict[str, dict]:
    """8 篇 graph-visible QGS（hop2 邻居 ∩ missing usable）。"""
    missing = load_qgs_meta()
    # hop2 JSON 可能在 data/exports 或系统 Temp（沙箱写入限制期间）
    hop2_path = HOP2_PATH
    if not os.path.exists(hop2_path):
        alt = os.path.join(os.environ.get("TEMP", "/tmp"), "citation_bridge_hop2.json")
        if os.path.exists(alt):
            hop2_path = alt
    bridge2 = json.load(open(hop2_path, encoding="utf-8"))["candidates"]
    gv = {}
    for c in bridge2:
        doi = norm_doi(c.get("doi"))
        for e, m in missing.items():
            if doi and doi == m["doi"]:
                gv[e] = dict(m, bridge_count=c["bridge_count"])
    return gv


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--retrieval", default=RETRIEVAL_PATH)
    ap.add_argument("--out", default=OUT_PATH)
    args = ap.parse_args()

    if not os.path.exists(args.retrieval):
        print(f"❌ 未找到 retrieval: {args.retrieval}\n请先执行 Scopus:\n"
              f"  python tools/run_community_round1.py --queries "
              f"data/exports/community_hop2_round2_queries.json "
              f"--out data/exports/community_hop2_round2_retrieval.json")
        return

    retr = json.load(open(args.retrieval, encoding="utf-8"))
    tc8 = retr["communities"]["TC_008"]
    queries = tc8["queries"]

    # R_008 unique（eid/doi）
    uniq: dict[str, dict] = {}
    for q in queries:
        for rec in q["records"]:
            e = norm_eid(rec.get("eid"))
            if e and e not in uniq:
                uniq[e] = rec
    rc = set(uniq.keys())
    old = load_old_eids()
    new = rc - old
    mcg = len(new)
    ret_nov = mcg / len(rc) if rc else 0.0

    # usable（abstract 源 scopus_cache，DOI 桥）
    from audit_research_usability import load_scopus_abstracts
    abs_map = load_scopus_abstracts()
    def usable(rec) -> bool:
        return bool(norm_doi(rec.get("doi"))) and bool(rec.get("title")) \
            and len(abs_map.get(norm_doi(rec.get("doi")), "")) >= 40
    usable_rc = {e for e in rc if usable(uniq[e])}
    usable_new = {e for e in new if usable(uniq[e])}

    # ── G2R：8 篇 graph-visible QGS exact identity 匹配 ──
    gv = load_graph_visible_qgs()
    gv_by_doi = {m["doi"]: (e, m) for e, m in gv.items()}
    rec_by_doi = {norm_doi(rec.get("doi")): rec for rec in uniq.values()}
    rec_by_eid = {e: rec for e, rec in uniq.items()}

    per_paper = []
    n_retrieved = 0
    for e, m in sorted(gv.items(), key=lambda kv: -kv[1]["bridge_count"]):
        rec = rec_by_eid.get(e) or (rec_by_doi.get(m["doi"]) if m["doi"] else None)
        if rec:
            n_retrieved += 1
            per_paper.append({"eid": e, "title": m["title"][:60], "year": m["year"],
                              "bridge_count": m["bridge_count"],
                              "status": "RETRIEVED", "rank": rec.get("rank"),
                              "gq": None})
        else:
            per_paper.append({"eid": e, "title": m["title"][:60], "year": m["year"],
                              "bridge_count": m["bridge_count"],
                              "status": "NOT_RETRIEVED", "rank": None,
                              "gq": "GQ3_QUERY_SET_GAP" if m["doi"] else "GQ5_IDENTITY_ERROR"})
    g2r = n_retrieved / GRAPH_VISIBLE

    print("=" * 78)
    print("STEP 7 正式评估（TC_008，QGS-BLIND frozen queries）")
    print("=" * 78)
    print(f"retrieved_unique={len(rc)} | new={mcg} | RetrievalNovelty={ret_nov*100:.1f}%")
    print(f"  usable: retrieved={len(usable_rc)} | MCG_usable={len(usable_new)}")
    print(f"\nG2R = {n_retrieved}/{GRAPH_VISIBLE} = {g2r*100:.1f}%"
          f"（graph-visible missing QGS 被正式检索回）")
    print(f"  候选逐篇:")
    for p in per_paper:
        print(f"    {'✅' if p['status']=='RETRIEVED' else '❌'} bridge={p['bridge_count']} "
              f"({p['year']}) {p['title']}")
    print(f"\n注：GQ1/GQ2 需对照 term_community/promoter 数据（8 篇均在 community "
          f"supporting papers，GQ1 预期 0）；GQ4 需 rank>depth 证据；"
          f"GQ6 判定词面重叠（v2.1.1 核心）")

    out = {
        "version": "hop2_round2_eval", "created_at": "2026-08-28",
        "metrics": {"retrieved_unique": len(rc), "new_MCG": mcg,
                    "retrieval_novelty": round(ret_nov, 4),
                    "retrieved_usable": len(usable_rc),
                    "usable_MCG": len(usable_new),
                    "g2r": round(g2r, 4), "g2r_num": n_retrieved, "g2r_denom": GRAPH_VISIBLE},
        "per_paper": per_paper,
        "note": "G2R 是首次量化的 Evidence→Query 转译效率；QGS regression 只作诊断",
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n✓ 已写: {args.out}")


if __name__ == "__main__":
    main()
