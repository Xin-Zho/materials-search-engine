"""Phase 2.1 科学语义审计：离线打印 closed loops 完整证据链（无 LLM，无 OpenAlex）。

用户定（2026-08-27）：工程闭环已通（promotion→query→NEW paper→relevance→edge→
candidate），只差最后确认——**闭环上的 relation 方向是否科学正确**。本工具把每条
closed loop 的 paper_id / raw evidence / source / predicate / target / evidence_type
/ candidate 全部打印，供人工逐条判断 subject/object 是否反、predicate 对不对、
candidate 是否真新。

**方向修正**（重要）：edge 数据模型是 raw_route → raw_mechanism（route=主语/条件，
mechanism=宾语/结果，见 knowledge_extractor 契约）。但 build_trace 显示层
（src=raw_mechanism, tgt=raw_route）把方向打反——曾导致 "increased modulus --direct-->
filler loading" 的假象，实际存储是 "filler loading --direct--> increased modulus"。
本工具按 **route → mechanism** 正确方向打印。

join 键：edges.provenance.query_id 是旧碰撞格式（bulk composite f），registry 已修成
md5——统一用 query_text 关联（两边都全）。
"""

import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPORTS = os.path.join(BASE, "data", "exports")


def _load(name: str):
    path = os.path.join(EXPORTS, name)
    if not os.path.exists(path):
        print(f"✗ 缺少 {path}（先跑 repair_staging_join.py + run_staging_pipeline.py）")
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def canonical_identity(c: dict) -> str:
    """canonical identity：canonical_name > canonical_match > raw_name（小写归一）。"""
    return (c.get("canonical_name") or c.get("canonical_match")
            or c.get("raw_name", "")).lower().strip()


def main():
    edges = _load("discovery_edges.json")["edges"]
    staging = _load("discovery_staging.json")["papers"]
    prov = _load("discovery_paper_provenance.json")
    reg = _load("discovery_query_registry.json")
    cands = _load("phase2_candidates.json")["candidates"]

    st_by_paper = {p["paper_id"]: p for p in staging}
    reg_by_text = {r.get("query_text"): r for r in reg}
    prov_by_paper: dict[str, list] = {}
    for r in prov:
        prov_by_paper.setdefault(r.get("paper_id", ""), []).append(r)
    prov_papers = set(prov_by_paper)

    # 旧池近似 = source_papers 不在 discovery provenance 的候选（canonical 判定基线）
    old_canons = {canonical_identity(c) for c in cands
                  if not (c.get("source_papers")
                          and c["source_papers"][0] in prov_papers)}

    # 候选 → closed loop：source_papers[0] 是 discovery 论文 且 relevance 非 IRRELEVANT
    loops: list[dict] = []
    for c in cands:
        sp = c.get("source_papers") or []
        if not sp or sp[0] not in prov_papers:
            continue
        paper_id = sp[0]
        st = st_by_paper.get(paper_id, {})
        rel = st.get("relevance_status", "")
        if rel not in ("RELEVANT", "UNCERTAIN"):
            continue
        provs = prov_by_paper.get(paper_id, [])
        if not provs:
            continue
        p0 = provs[0]
        qrec = reg_by_text.get(p0.get("query_text", ""), {})
        # 该候选关联的 edges（raw_mechanism 或 raw_route 命中候选名）
        cn = c["raw_name"].lower()
        cand_edges = [e for e in edges
                      if e.get("paper_id") == paper_id
                      and (cn in (e.get("raw_mechanism") or "").lower()
                           or cn in (e.get("raw_route") or "").lower())]
        loops.append({
            "candidate": c,
            "paper_id": paper_id,
            "relevance": rel,
            "promoted_node": p0.get("promoted_node", ""),
            "promotion_id": p0.get("promotion_id", ""),
            "query_text": qrec.get("query_text", p0.get("query_text", "")),
            "query_family": qrec.get("query_family", p0.get("query_family", "")),
            "query_id": qrec.get("query_id", p0.get("query_id", "")),
            "edges": cand_edges,
            "canonical_seen_before": canonical_identity(c) in old_canons,
        })

    loops.sort(key=lambda x: (x["canonical_seen_before"],
                              x["candidate"]["raw_name"].lower()))
    complete = [x for x in loops if x["edges"]]
    incomplete = [x for x in loops if not x["edges"]]

    print("=" * 60)
    print(f"Closed Loop 审计（离线，无 LLM/OpenAlex）")
    print(f"  候选总数      {len(loops)}")
    print(f"  有 edge 证据   {len(complete)}")
    print(f"  无 edge 证据   {len(incomplete)}（INCOMPLETE_TRACE，不计）")
    print(f"  方向说明      edge 按 raw_route → raw_mechanism 打印（修正 build_trace 显示反转）")
    print("=" * 60)

    n = 0
    for x in complete:
        n += 1
        c = x["candidate"]
        print()
        print("=" * 60)
        print(f"Closed Loop {n}")
        print("=" * 60)
        print(f"origin:")
        print(f"  {x['promoted_node']}（promotion_id={x['promotion_id']}）")
        print()
        print(f"query:")
        print(f"  {x['query_text']}  [{x['query_family']}]  ({x['query_id']})")
        print()
        print(f"paper:")
        print(f"  {x['paper_id']}")
        print()
        print(f"relevance:")
        print(f"  {x['relevance']}")
        for e in x["edges"]:
            print()
            print(f"raw evidence:")
            print(f"  \"{e.get('evidence', '')}\"")
            print()
            print(f"relation:")
            print(f"  {e.get('raw_route')}")
            print(f"  --{e.get('relation_type')}-->")
            print(f"  {e.get('raw_mechanism')}")
        print()
        print(f"candidate:")
        print(f"  {c['raw_name']}  [{c.get('candidate_type')}]")
        print(f"  canonical: {canonical_identity(c)}")
        print(f"  canonical_seen_before: {x['canonical_seen_before']}")
        print(f"  source_paper: {x['paper_id']}")

    if incomplete:
        print()
        print("-" * 60)
        print(f"无 edge 证据的候选（{len(incomplete)}，trace 不完整）:")
        for x in incomplete:
            c = x["candidate"]
            print(f"  {c['raw_name']}  [{c.get('candidate_type')}]  paper={x['paper_id']}")


if __name__ == "__main__":
    main()
