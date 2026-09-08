"""tools/run_citation_hop2.py — v2.1 第二跳 Citation Expansion（用户 2026-08-28 拍板）。

第一跳（citation_bridge_v1：40 RELEVANT → 850 neighbors → TC_006/TC_015 → Scopus
retrieval → 80 new usable papers）已证明能扩库但主要还在 dental 边界。第二跳目标：
看 citation graph 是否真正向外走（holography/UV-NIL 等跨社区语言区）。

设计（用户冻结）：
  seed        = Round1 新增的 80 篇 usable papers（不是 126 全量；46 篇 unusable
                留在 Discovery Index 作中间节点，不当 seed）
  hop         = 2（固定，本轮不无限递归——先量化 hop1→hop2 边际收益再设计 Stop）
  Bridge_2(p) = #{ Round1 new usable seeds citing p }
  ParentCommunityCount(p) = 同篇被几个 promoted community（TC_006/TC_015）指向
  seen flags  = seen_in_hop0（40 RELEVANT）/ seen_in_hop1（850 v1 neighbors）/
                already_in_candidate_db（depth run + Round1 检索全集）
  HopNovelty_2 = 此前从未见过的 W-id / hop2 unique neighbors（跨社区强度）
  DBNovelty_2  = 不在 Candidate DB 的 / hop2 unique neighbors（对新库的增量）

输出：data/exports/citation_bridge_hop2.json（复用 discover()，不重写 bridge 逻辑）
"""
import argparse
import json
import os
import sqlite3
import sys
from collections import Counter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tools"))
sys.path.insert(0, os.path.join(BASE, "search_engine", "discovery"))

import citation_bridge as cb
import audit_research_usability as audit

RETRIEVAL_PATH = os.path.join(BASE, "data", "exports", "community_round1_retrieval.json")
EVAL_PATH = os.path.join(BASE, "data", "exports", "community_round1_evaluation.json")
DEPTH_RUN_PATH = os.path.join(BASE, "data", "exports", "query_family_runs_depth.json")
BRIDGE_V1_PATH = os.path.join(BASE, "data", "exports", "citation_bridge_v1.json")
ENRICHED_PATH = os.path.join(BASE, "data", "cache", "openalex_neighbors_enriched.json")
RELEVANCE_PATH = os.path.join(BASE, "data", "exports", "round1_seed_relevance.json")
IDENTITIES_PATH = os.path.join(BASE, "data", "exports", "round1_seed_identities.json")
OUT_PATH = os.path.join(BASE, "data", "exports", "citation_bridge_hop2.json")
SCOPUS_CACHE = os.path.join(BASE, "data", "cache", "scopus_cache.db")


def load_hop2_seeds() -> list[tuple[str, str, list[str]]]:
    """正式 hop2 seeds（用户 2026-08-28 定稿）：

    Seed_hop2 = New ∩ Usable ∩ Relevant ∩ Resolved
      Relevant : Seed Relevance Gate 三态（round1_seed_relevance.json，RELEVANT 64 篇）
      Resolved : identity resolution（round1_seed_identities.json，64/64 W-id）
    返回 [(wid, doi, communities)]。
    """
    identities = json.load(open(IDENTITIES_PATH, encoding="utf-8"))["seeds"]
    new_usable = load_round1_new_usable()
    doi2comm = {norm_doi(e["doi"]): e["communities"] for e in new_usable}
    seeds = [(r["openalex_id"], norm_doi(r["doi"]),
              doi2comm.get(norm_doi(r["doi"]), []))
             for r in identities if r["openalex_id"]]
    return seeds


def load_scopus_abstracts() -> dict[str, str]:
    """DOI → abstract（scopus_cache immutable 只读；paper_id 是 scopus:{DOI}）。"""
    out = {}
    con = sqlite3.connect(f"file:{SCOPUS_CACHE}?mode=ro&immutable=1", uri=True)
    for pid, j in con.execute("SELECT paper_id, normalized_json FROM papers"):
        nj = json.loads(j)
        a = (nj.get("abstract") or "").strip()
        doi = cb.norm_doi(nj.get("doi") or pid.replace("scopus:", ""))
        if len(a) >= audit.ABSTRACT_MIN and doi:
            out.setdefault(doi, a)
    con.close()
    return out


def norm_eid(e: str) -> str:
    return (e or "").strip()


def norm_doi(d: str) -> str:
    return (d or "").strip().lower()


def load_round1_new_usable() -> list[dict]:
    """Round1 新增且 Research-Usable 的论文 [{eid, doi, title, communities}]。

    new = retrieval unique − depth run old（eid 级）；usable 用同一把尺子
    （Identity ∧ Title ∧ Abstract，abstract 源 scopus_cache DOI 桥）。
    """
    records = json.load(open(DEPTH_RUN_PATH, encoding="utf-8"))["records"]
    old = {r["eid"].strip() for rs in records.values() for r in rs if r.get("eid")}

    retr = json.load(open(RETRIEVAL_PATH, encoding="utf-8"))["communities"]
    abs_map = load_scopus_abstracts()
    by_eid: dict[str, dict] = {}
    for cid, c in retr.items():
        for q in c["queries"]:
            for r in q["records"]:
                eid = norm_eid(r.get("eid"))
                if not eid:
                    continue
                ent = by_eid.setdefault(eid, {
                    "eid": eid, "doi": cb.norm_doi(r.get("doi") or ""),
                    "title": r.get("title") or "", "communities": []})
                ent["title"] = ent["title"] or r.get("title") or ""
                if cid not in ent["communities"]:
                    ent["communities"].append(cid)
    new_usable = []
    for eid, ent in by_eid.items():
        if eid in old:
            continue                       # 不是新增
        abstract = abs_map.get(ent["doi"]) or ""
        if not (ent["doi"] or eid) or not ent["title"]:
            continue
        if len(abstract) < audit.ABSTRACT_MIN:
            continue                       # unusable（无 abstract 无全文）
        new_usable.append(ent)
    return new_usable


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-json", action="store_true")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--out", default=OUT_PATH, help="JSON 输出路径（默认 data/exports/citation_bridge_hop2.json）")
    args = ap.parse_args()

    bridge = cb.CitationBridge()
    bridge.load_openalex()

    # ── seed：正式 hop2 seeds = RELEVANT(64) ∩ resolved W-id(64) ──
    seeds = load_hop2_seeds()
    seed_wids = [s[0] for s in seeds]
    parent_map = {w: comms for w, _, comms in seeds}
    rel = json.load(open(RELEVANCE_PATH, encoding="utf-8"))
    print(f"hop2 seeds = RELEVANT {len(seed_wids)} 篇 "
          f"（SeedPurity={rel['seed_purity']}, IdentityCoverage=1.0）")

    # ── 第二跳：discover（backward）──
    result = bridge.discover(seed_wids, parent_map=parent_map)
    neighbors = result["neighbors"]

    # ── seen-before flags ──
    hop0 = set(bridge.seeds)                      # 40 RELEVANT（staging）
    hop1 = set()                                  # 850 v1 neighbors
    if os.path.exists(BRIDGE_V1_PATH):
        hop1 = {c["wid"] for c in json.load(open(BRIDGE_V1_PATH, encoding="utf-8"))["candidates"]}
    # Candidate DB（含 Round1 检索）：known_wids + seed wids
    bridge.load_known_wids()
    db_wids = set(bridge.known_wids) | set(seed_wids)

    # 元数据：works + enriched（第一跳 enrich 的 850 篇可复用）
    enriched = {}
    if os.path.exists(ENRICHED_PATH):
        enriched = json.load(open(ENRICHED_PATH, encoding="utf-8"))

    for n, nb in neighbors.items():
        w = bridge.works.get(n) or enriched.get(n) or {}
        nb["title"] = (w.get("title") or "")[:120]
        nb["year"] = w.get("year")
        nb["doi"] = w.get("doi") or ""
        nb["parent_papers"] = sorted(nb.pop("citing_seeds"))
        nb["parent_community_count"] = len(nb.get("parent_communities", []))
        nb["seen_in_hop0"] = n in hop0
        nb["seen_in_hop1"] = n in hop1
        nb["already_in_candidate_db"] = n in db_wids
        if n in set(seed_wids):
            nb["class"] = "SEED_ITSELF"
        elif nb["seen_in_hop0"]:
            nb["class"] = "ALREADY_RELEVANT"
        elif nb["already_in_candidate_db"]:
            nb["class"] = "ALREADY_RETRIEVED"
        else:
            nb["class"] = "NEW_NEIGHBOR"

    # ── 扩张指标（用户定）──
    total = len(neighbors)
    never_seen = sum(1 for nb in neighbors.values()
                     if not nb["seen_in_hop0"] and not nb["seen_in_hop1"]
                     and nb["class"] != "SEED_ITSELF")
    not_in_db = sum(1 for nb in neighbors.values() if not nb["already_in_candidate_db"])
    hop_novelty = never_seen / total if total else 0
    db_novelty = not_in_db / total if total else 0
    dist = Counter(v["count"] for v in neighbors.values())
    cls = Counter(v["class"] for v in neighbors.values())
    pcc = Counter(v["parent_community_count"] for v in neighbors.values())

    print("\n" + "=" * 72)
    print(f"Citation Bridge HOP 2（backward, OpenAlex cache only）")
    print(f"seeds = {len(seed_wids)}（with references = {result['seed_with_refs']}）")
    print("=" * 72)
    print(f"unique referenced neighbors      = {total}")
    print(f"bridge_2 >= 2                    = {sum(1 for v in neighbors.values() if v['count'] >= 2)}")
    print(f"bridge_2 >= 3                    = {sum(1 for v in neighbors.values() if v['count'] >= 3)}")
    print(f"ParentCommunityCount 分布        = {dict(sorted(pcc.items()))}")
    print(f"  （≥2 = 同时被 TC_006+TC_015 指向 = 跨边界结构节点: "
          f"{sum(1 for v in neighbors.values() if v['parent_community_count'] >= 2)}）")
    print(f"seen_in_hop0 = {sum(1 for v in neighbors.values() if v['seen_in_hop0'])}"
          f" | seen_in_hop1 = {sum(1 for v in neighbors.values() if v['seen_in_hop1'])}")
    print(f"class: {dict(cls)}")
    print(f"\n扩张指标（用户定）：")
    print(f"  HopNovelty_2（此前从未见过）  = {hop_novelty*100:.1f}%  ({never_seen}/{total})")
    print(f"  DBNovelty_2（不在 Candidate）  = {db_novelty*100:.1f}%  ({not_in_db}/{total})")
    print(f"  bridge_count 分布: {dict(sorted(dist.items()))}")
    print(f"\nTop {args.top}（bridge_2 desc，跨 community 优先标注 ★）:")
    for n, v in sorted(neighbors.items(), key=lambda x: (-x[1]["count"],
                                                         -x[1]["parent_community_count"]))[:args.top]:
        star = "★" if v["parent_community_count"] >= 2 else " "
        print(f"  {star}{v['count']:>3} {v['class']:<17} {str(v['year']):>5} "
              f"pc={v['parent_community_count']} {n:<14} {v['title'][:48]}")

    if not args.no_json:
        out = {
            "version": "hop2_backward",
            "created_at": "2026-08-28",
            "hop": 2,
            "source_round": 1,
            "data_source": "openalex_cache.json only（no network）",
            "seeds": {"relevant_resolved": len(seed_wids),
                      "with_refs": result["seed_with_refs"]},
            "summary": {
                "unique_neighbors": total,
                "bridge_ge2": sum(1 for v in neighbors.values() if v["count"] >= 2),
                "bridge_ge3": sum(1 for v in neighbors.values() if v["count"] >= 3),
                "parent_community_counts": dict(pcc),
                "cross_boundary_nodes": sum(1 for v in neighbors.values()
                                            if v["parent_community_count"] >= 2),
                "seen_in_hop0": sum(1 for v in neighbors.values() if v["seen_in_hop0"]),
                "seen_in_hop1": sum(1 for v in neighbors.values() if v["seen_in_hop1"]),
                "never_seen": never_seen,
                "not_in_candidate_db": not_in_db,
                "hop_novelty": round(hop_novelty, 4),
                "db_novelty": round(db_novelty, 4),
                "bridge_distribution": dict(dist),
                "class": dict(cls),
            },
            "candidates": [
                {"wid": n, "title": v["title"], "year": v["year"], "doi": v["doi"],
                 "bridge_count": v["count"], "parent_papers": v["parent_papers"],
                 "parent_communities": v.get("parent_communities", []),
                 "parent_community_count": v["parent_community_count"],
                 "seen_in_hop0": v["seen_in_hop0"], "seen_in_hop1": v["seen_in_hop1"],
                 "already_in_candidate_db": v["already_in_candidate_db"],
                 "class": v["class"]}
                for n, v in neighbors.items()],
        }
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print(f"\n✓ 已写: {args.out}")


if __name__ == "__main__":
    main()
