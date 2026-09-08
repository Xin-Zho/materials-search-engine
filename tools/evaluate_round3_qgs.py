"""tools/evaluate_round3_qgs.py — v2.1.5 Round3 打开 QGS 的正式评估（用户 2026-08-29 定稿）。

QGS-blind 阶段已闭环（execution 16/16、identity PASS、union new 280、usable 277、
断言全过）→ ROUND3_DEPTH500_RETRIEVAL = VALID → 本工具**第一次打开 QGS**。

只算三组指标（用户冻结，不混）：
  1. G2R_all       = |R_Round3 ∩ G_8| / 8             整个 discovery→retrieval 链
     G2R_community = |R_Round3 ∩ G_8[bridge≥2]| / 6  被 community representation
                                                      接住后的 Evidence→Query 转换率
     （8 篇里 2 篇 bridge=1 未进入 term community）
     逐篇：round3_retrieved / matched_query_ids / best_rank
  2. NewQGS_R3     = (QGS_usable ∩ R_Round3) - R_pre-R3
     R_pre-R3 = R_depth1000(old) ∪ R_Round1(TC_006/TC_015)   ← set union，不重算
  3. RR_usable     = |QGS_usable ∩ R_≤Round3| / 105
     保留 Before Round1 / After Round1 / After Round3 / Round3 delta

身份匹配：EID 精确优先 + DOI 精确 fallback（QGS 有 scopus_eid + doi）。

输入（数据契约）：
  data/exports/round3_depth500_retrieval.json    R_Round3
  data/exports/research_usability_audit.json     QGS_usable（qgs_detail）
  data/exports/pc_001_external_qgs_v1.json       QGS 全集（year）
  data/exports/citation_bridge_hop2.json（或 %TEMP%）  G_8 + bridge_count
  data/exports/query_family_runs_depth.json      R_depth（old）
  data/exports/community_round1_retrieval.json   R_Round1（TC_006/TC_015）

用法：
  python tools/evaluate_round3_qgs.py [--out data/exports/round3_qgs_evaluation.json]
"""
import argparse
import json
import os
import sys
from collections import defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tools"))

from evaluate_hop2_round2 import (load_graph_visible_qgs, load_old_eids,  # noqa: E402
                                  norm_doi, norm_eid)

R3_PATH = os.path.join(BASE, "data", "exports", "round3_depth500_retrieval.json")
AUDIT_PATH = os.path.join(BASE, "data", "exports", "research_usability_audit.json")
BENCH_PATH = os.path.join(BASE, "data", "exports", "pc_001_external_qgs_v1.json")
DEPTH_RUN_PATH = os.path.join(BASE, "data", "exports", "query_family_runs_depth.json")
R1_PATH = os.path.join(BASE, "data", "exports", "community_round1_retrieval.json")
HOP2_PATH = os.path.join(BASE, "data", "exports", "citation_bridge_hop2.json")
OUT_PATH = os.path.join(BASE, "data", "exports", "round3_qgs_evaluation.json")

QGS_TOTAL = 105   # QGS usable（冻结分母）


def load_qgs_all() -> dict[str, dict]:
    """QGS_usable 全集 → {eid: {doi, title, year}}（105 篇，audit usable + bench year）。"""
    audit = json.load(open(AUDIT_PATH, encoding="utf-8"))
    bench = json.load(open(BENCH_PATH, encoding="utf-8"))
    year_by_eid = {norm_eid(p.get("scopus_eid")): p.get("year") for p in bench["papers"]}
    out = {}
    for d in audit["qgs_detail"]:
        e = norm_eid(d.get("eid"))
        if d.get("usable") and e:
            out[e] = {"doi": norm_doi(d.get("doi")), "title": d.get("title") or "",
                      "year": year_by_eid.get(e)}
    return out


def load_r3() -> dict:
    """R_Round3 → (eids, dois, by_eid, by_doi)。by_*: identity -> {query_ids, min_rank}。"""
    data = json.load(open(R3_PATH, encoding="utf-8"))
    eids: set[str] = set()
    dois: set[str] = set()
    by_eid: dict[str, dict] = {}
    by_doi: dict[str, dict] = {}
    for cid, spec in data.get("communities", {}).items():
        for q in spec.get("queries", []):
            qid = q.get("query_id", "?")
            for rec in q.get("records", []):
                e = norm_eid(rec.get("eid"))
                d = norm_doi(rec.get("doi"))
                rank = rec.get("rank")
                if e:
                    eids.add(e)
                    m = by_eid.setdefault(e, {"query_ids": [], "min_rank": None})
                    if qid not in m["query_ids"]:
                        m["query_ids"].append(qid)
                    if rank and (m["min_rank"] is None or rank < m["min_rank"]):
                        m["min_rank"] = rank
                if d:
                    dois.add(d)
                    m = by_doi.setdefault(d, {"query_ids": [], "min_rank": None})
                    if qid not in m["query_ids"]:
                        m["query_ids"].append(qid)
                    if rank and (m["min_rank"] is None or rank < m["min_rank"]):
                        m["min_rank"] = rank
    return {"eids": eids, "dois": dois, "by_eid": by_eid, "by_doi": by_doi}


def load_r_pre_r3() -> dict:
    """R_pre-R3 = R_depth ∪ R_Round1 → (eids, dois)。"""
    eids: set[str] = set()
    dois: set[str] = set()
    # R_depth（old，depth run）
    records = json.load(open(DEPTH_RUN_PATH, encoding="utf-8"))["records"]
    for rs in records.values():
        for r in rs:
            if r.get("eid"):
                eids.add(r["eid"].strip())
            d = norm_doi(r.get("doi"))
            if d:
                dois.add(d)
    # R_Round1（TC_006 / TC_015）
    r1 = json.load(open(R1_PATH, encoding="utf-8"))
    for cid, spec in r1.get("communities", {}).items():
        for q in spec.get("queries", []):
            for rec in q.get("records", []):
                e = norm_eid(rec.get("eid"))
                d = norm_doi(rec.get("doi"))
                if e:
                    eids.add(e)
                if d:
                    dois.add(d)
    return {"eids": eids, "dois": dois}


def match(rec: dict, r3: dict) -> dict | None:
    e = rec["eid"]
    d = rec["doi"]
    if e and e in r3["by_eid"]:
        return {"matched_by": "eid", **r3["by_eid"][e]}
    if d and d in r3["by_doi"]:
        return {"matched_by": "doi", **r3["by_doi"][d]}
    return None


def in_set(rec: dict, s: dict) -> bool:
    return (rec["eid"] and rec["eid"] in s["eids"]) or \
           (rec["doi"] and rec["doi"] in s["dois"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=OUT_PATH)
    args = ap.parse_args()

    qgs_all = load_qgs_all()                       # 105 usable
    gv = load_graph_visible_qgs()                  # 8 graph-visible missing
    r3 = load_r3()
    pre = load_r_pre_r3()

    print(f"QGS_usable = {len(qgs_all)} | G_8 = {len(gv)} | "
          f"R_Round3 eids={len(r3['eids'])} | R_pre-R3 eids={len(pre['eids'])}")

    # ── 1. G2R 逐篇 ──
    per_paper = []
    for e, m in sorted(gv.items(), key=lambda kv: -kv[1]["bridge_count"]):
        rec = {"eid": e, "doi": m["doi"]}
        hit = match(rec, r3)
        per_paper.append({
            "eid": e, "title": m["title"][:70], "year": m["year"],
            "bridge_count": m["bridge_count"],
            "graph_visible": True,
            "round3_retrieved": bool(hit),
            "matched_by": hit["matched_by"] if hit else None,
            "matched_query_ids": hit["query_ids"] if hit else [],
            "best_rank": hit["min_rank"] if hit else None,
        })
    g8 = [p for p in per_paper if p["round3_retrieved"]]
    g2r_all = len(g8) / len(gv)
    g6 = [p for p in per_paper if p["bridge_count"] >= 2]
    g6_hit = [p for p in g6 if p["round3_retrieved"]]
    g2r_comm = len(g6_hit) / len(g6) if g6 else None

    # ── 2. NewQGS_R3（去重口径）──
    qgs_r3 = [e for e, m in qgs_all.items()
              if in_set({"eid": e, "doi": m["doi"]}, r3)]
    newly = [e for e in qgs_r3 if not in_set({"eid": e, "doi": qgs_all[e]["doi"]}, pre)]
    new_qgs_r3 = [{"eid": e, "doi": qgs_all[e]["doi"],
                   "title": qgs_all[e]["title"][:70],
                   "year": qgs_all[e]["year"],
                   "matched_query_ids": match({"eid": e, "doi": qgs_all[e]["doi"]}, r3)["query_ids"]
                   if match({"eid": e, "doi": qgs_all[e]["doi"]}, r3) else []}
                  for e in newly]

    # ── 3. RR_usable ──
    def overlap(s: dict) -> list[str]:
        return [e for e, m in qgs_all.items() if in_set({"eid": e, "doi": m["doi"]}, s)]

    r_depth = load_old_eids()
    r_depth_set = {"eids": r_depth,
                   "dois": set()}   # depth run 只有 eid 集合够（load_old_eids 是 eids）
    rr_before_r1 = overlap(r_depth_set)
    rr_after_r1 = [e for e in qgs_all if in_set({"eid": e, "doi": qgs_all[e]["doi"]}, pre)]
    rr_after_r3 = [e for e in qgs_all
                   if in_set({"eid": e, "doi": qgs_all[e]["doi"]}, r3)
                   or in_set({"eid": e, "doi": qgs_all[e]["doi"]}, pre)]

    print(f"\n{'='*80}")
    print("Round3 QGS 正式评估（QGS 已打开）")
    print(f"{'='*80}")
    print(f"\n[1] G2R_all = {len(g8)}/{len(gv)} = {g2r_all*100:.1f}%"
          f" | G2R_community(bridge>=2) = {len(g6_hit)}/{len(g6)}"
          f" = {g2r_comm*100:.1f}%")
    print(f"    逐篇:")
    for p in per_paper:
        st = "RETRIEVED" if p["round3_retrieved"] else "NOT_RETRIEVED"
        print(f"    {'OK' if p['round3_retrieved'] else '--'} bridge={p['bridge_count']} "
              f"({p['year']}) [{st}] {p['title']}")
        if p["round3_retrieved"]:
            print(f"        by={p['matched_by']} rank={p['best_rank']} "
                  f"queries={p['matched_query_ids']}")
    print(f"\n[2] NewQGS_R3（QGS_usable ∩ R_Round3 - R_pre-R3）= {len(newly)} 篇")
    for p in new_qgs_r3:
        print(f"    + {p['year']} {p['title']} (q={p['matched_query_ids']})")
    print(f"\n[3] RR_usable:")
    print(f"    Before Round1 = {len(rr_before_r1)}/{QGS_TOTAL}"
          f" = {len(rr_before_r1)/QGS_TOTAL*100:.1f}%")
    print(f"    After Round1  = {len(rr_after_r1)}/{QGS_TOTAL}"
          f" = {len(rr_after_r1)/QGS_TOTAL*100:.1f}%")
    print(f"    After Round3  = {len(rr_after_r3)}/{QGS_TOTAL}"
          f" = {len(rr_after_r3)/QGS_TOTAL*100:.1f}%")
    print(f"    Round3 delta  = +{len(rr_after_r3)-len(rr_after_r1)} papers / "
          f"+{(len(rr_after_r3)-len(rr_after_r1))/QGS_TOTAL*100:.1f} pp")

    out = {
        "version": "round3_qgs_evaluation_v1",
        "qgs_opened_at": "2026-08-29",
        "g2r": {
            "all": {"num": len(g8), "denom": len(gv), "rate": round(g2r_all, 4)},
            "community_represented": {"num": len(g6_hit), "denom": len(g6),
                                      "rate": round(g2r_comm, 4) if g2r_comm else None,
                                      "criterion": "bridge_count >= 2（8 篇里 2 篇 bridge=1 未进入 term community）"},
            "per_paper": per_paper,
        },
        "new_qgs_r3": {"count": len(newly), "papers": new_qgs_r3,
                       "definition": "QGS_usable ∩ R_Round3 - (R_depth ∪ R_Round1)"},
        "rr_usable": {
            "denominator": QGS_TOTAL,
            "before_round1": {"num": len(rr_before_r1),
                              "rate": round(len(rr_before_r1)/QGS_TOTAL, 4)},
            "after_round1": {"num": len(rr_after_r1),
                             "rate": round(len(rr_after_r1)/QGS_TOTAL, 4)},
            "after_round3": {"num": len(rr_after_r3),
                             "rate": round(len(rr_after_r3)/QGS_TOTAL, 4)},
            "round3_delta_papers": len(rr_after_r3) - len(rr_after_r1),
            "round3_delta_pp": round((len(rr_after_r3)-len(rr_after_r1))/QGS_TOTAL*100, 2),
        },
        "note": "G2R 分母仍为 8（不因 Round3 新增 280 篇改变实验定义）；"
                "QGS regression 只作诊断",
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n[OK] 已写: {args.out}")


if __name__ == "__main__":
    main()
