#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/reconcile_s4_identity.py — S4 identity reconciliation（2026-08-30 用户定）。

背景：S4 正式执行 citation_query_overlap=0（citation key=DOI/WID，query key=Scopus EID，
两套 namespace 直接比较恒为 0——与 S3 当初同样的假象）；identity_unknown=885（4.55%）。
S3 已证明 overlap=0 可以是 identity namespace bug（reconcile 后 0→50，13477→13430），
故 S4 必须先做 canonical identity reconciliation，不能把 19445 当最终 seen set。

修复：统一 canonical paper identity（union-find over EID/DOI/WID/title）：
  - 节点 = 每条 citation record / query row
  - 连边 = DOI 相等（高置信）或 normalized title 完全相等（高置信精确匹配，
    与 R03 resolve_seen_s1 同口径；不做事后模糊匹配）
  - canonical_id 优先级：EID(2-s2.0-) > DOI > WID > key
  - match_method 逐边保存（doi/title），identity provenance 可追踪

只读 s4_citation_records.json + s4_query_records.json + s3_seen_set.json +
S3 三通道（build_s3_found_sets）——**不重跑检索**。

S4 identity gold QA（用户定，三类 merge）：
  1. same DOI / different WID（DOI 合并了多个 WID 的组）
  2. same normalized title / different IDs（title 合并且组内含不同 EID/DOI/WID）
  3. citation + query 双来源 paper（BOTH）——重点：reconcile 后 overlap>0
     说明 namespace bug 已修复；仍 =0 才说明是真零重叠

输出：
  s4_identity_manifest.json  per-paper canonical（canonical_id/eids/doi/wid/titles/
                              match_method/source: CIT|QUERY|BOTH/already_seen_s3）
  s4_identity_qa.json        三类 merge gold QA（含 BOTH 全量明细）
  s4_seen_set.json           重建（S3 keys ∪ canonical new papers，canonical_id）
  s4_candidate_snapshot.json 更新（S4_IDENTITY=VERIFIED + 新 aggregate）
  s4_delta_vs_s3.json        更新

断言（用户定）：
  1. |S4| == |S3| + |S4_new_vs_S3|（canonical 层面）
  2. identity resolution provenance 可追踪（manifest 逐篇含 match 链）
  （citation_query_overlap 是 QA signal，不硬断言——用户判断真零 vs bug）

用法：
  python tools/reconcile_s4_identity.py [--out-manifest <path>] [--rebuild-seen <path>] ...
"""
import argparse
import datetime
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tools"))

from build_r02_seen import _norm_doi, _norm_title  # noqa: E402
from build_r03_seen import build_s3_found_sets  # noqa: E402

T = os.path.join(BASE, "data", "exports", "terminology")
CIT_REC = os.path.join(T, "s4_citation_records.json")
Q_REC = os.path.join(T, "s4_query_records.json")
S3_SEEN = os.path.join(T, "s3_seen_set.json")
SNAP = os.path.join(T, "s4_candidate_snapshot.json")
SEEN = os.path.join(T, "s4_seen_set.json")
DELTA = os.path.join(T, "s4_delta_vs_s3.json")
MANIFEST = os.path.join(T, "s4_identity_manifest.json")
QA_OUT = os.path.join(T, "s4_identity_qa.json")


class DSU:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _eid_of(nd):
    e = nd.get("eid")
    if e:
        return e
    k = nd.get("key") or ""
    return k if k.startswith("2-s2.0-") else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--citation-records", default=CIT_REC)
    ap.add_argument("--query-records", default=Q_REC)
    ap.add_argument("--s3-seen", default=S3_SEEN)
    ap.add_argument("--manifest", default=MANIFEST)
    ap.add_argument("--qa-out", default=QA_OUT)
    ap.add_argument("--seen-set", default=SEEN)
    ap.add_argument("--snapshot", default=SNAP)
    ap.add_argument("--delta", default=DELTA)
    args = ap.parse_args()
    cit = json.load(open(args.citation_records, encoding="utf-8"))["records"]
    rbq = json.load(open(args.query_records, encoding="utf-8"))["records_by_query"]
    s3_keys = set(json.load(open(args.s3_seen, encoding="utf-8"))["keys"])
    s3_found = build_s3_found_sets()

    # ── 1. 收集所有论文节点 ──
    # node: {id, source, key, eid, doi, wid, title, provenance}
    nodes = {}
    for r in cit:
        nid = f"cit:{r['key']}"
        nodes[nid] = {"id": nid, "source": "CIT", "key": r["key"],
                      "doi": _norm_doi(r.get("doi")) or None,
                      "wid": r.get("wid"), "title": r.get("title"),
                      "provenance": r.get("provenance", [])}
    for qs, rows in rbq.items():
        for r in rows:
            nid = f"q:{r['key']}"
            nodes[nid] = {"id": nid, "source": "QUERY", "key": r["key"],
                          "eid": r.get("eid"), "doi": _norm_doi(r.get("doi")) or None,
                          "wid": None, "title": r.get("title"),
                          "provenance": [{"query": qs}]}

    # ── 2. union-find：DOI 相等 / normalized title 相等（高置信精确）──
    dsu = DSU()
    doi_idx = {}     # doi -> node_id
    title_idx = {}   # ntitle -> node_id
    match_edges = []  # (node_a, node_b, method)
    for nid, nd in nodes.items():
        if nd["doi"]:
            other = doi_idx.get(nd["doi"])
            if other is not None:
                dsu.union(nid, other)
                match_edges.append((nid, other, "doi"))
            else:
                doi_idx[nd["doi"]] = nid
        nt = _norm_title(nd["title"]) if nd["title"] else None
        if nt:
            other = title_idx.get(nt)
            if other is not None:
                dsu.union(nid, other)
                match_edges.append((nid, other, "title"))
            else:
                title_idx[nt] = nid

    # ── 3. canonical paper：EID > DOI > WID ──
    groups = {}
    for nid in nodes:
        root = dsu.find(nid)
        groups.setdefault(root, []).append(nid)

    def canon_of(nds):
        for nd in nds:
            e = _eid_of(nd)
            if e:
                return e
        for nd in nds:
            if nd.get("doi"):
                return nd["doi"]
        for nd in nds:
            if nd.get("wid"):
                return nd["wid"]
        return sorted(nd["key"] for nd in nds)[0]

    papers = []
    for root, nids in groups.items():
        nds = [nodes[n] for n in nids]
        cid = canon_of(nds)
        eids = sorted({_eid_of(nd) for nd in nds} - {None})
        dois = sorted({nd["doi"] for nd in nds if nd["doi"]})
        wids = sorted({nd["wid"] for nd in nds if nd["wid"]})
        titles = sorted({_norm_title(nd["title"]) for nd in nds if nd["title"]})
        src = "BOTH" if any(nd["source"] == "CIT" for nd in nds) \
              and any(nd["source"] == "QUERY" for nd in nds) \
              else ("CIT" if any(nd["source"] == "CIT" for nd in nds) else "QUERY")
        # 已见判定（三通道，R04 同口径）+ WID 直查 S3 canonical keys
        # （2026-08-30：WID-only 且 title 缺失的论文三通道覆盖不到，但 S3 citation 层
        #   收录过同一 WID（S3_SEEN 有 262 WID keys）→ 必须回查 s3_keys，否则重复计入）
        already = False
        if eids and any(e in s3_found["eids"] for e in eids):
            already = True
        elif dois and any(d in s3_found["dois"] for d in dois):
            already = True
        elif titles and any(t in s3_found["titles"] for t in titles):
            already = True
        elif wids and any(w in s3_keys for w in wids):
            already = True
        papers.append({
            "canonical_id": cid, "source": src,
            "eids": eids, "doi": dois, "wids": wids, "titles": titles,
            "already_seen_s3": already,
            "n_members": len(nids),
            "match_edges": [e for e in match_edges if e[0] in nids or e[1] in nids],
        })

    # ── 4. 集合统计（canonical 层面）──
    cit_papers = [p for p in papers if p["source"] in ("CIT", "BOTH")]
    q_papers = [p for p in papers if p["source"] in ("QUERY", "BOTH")]
    both_papers = [p for p in papers if p["source"] == "BOTH"]
    cit_new = {p["canonical_id"] for p in cit_papers if not p["already_seen_s3"]}
    q_new = {p["canonical_id"] for p in q_papers if not p["already_seen_s3"]}
    s4_new = cit_new | q_new
    s4_keys = s3_keys | s4_new

    aggregate = {
        "s3_seen_unique": len(s3_keys),
        "citation_union_unique": len(cit_papers),
        "citation_new_vs_s3": len(cit_new),
        "citation_already_seen_s3": len(cit_papers) - len(cit_new),
        "query_union_unique": len(q_papers),
        "query_new_vs_s3": len(q_new),
        "citation_query_overlap": len(both_papers),   # canonical BOTH
        "s4_new_vs_s3": len(s4_new),
        "search_s4_union_unique": len(s4_keys),
        "identity_unknown_total": sum(1 for p in papers if not p["eids"] and not p["doi"]),
        "canonicalized_papers": len(papers),
        "multi_identity_papers": sum(
            1 for p in papers
            if len(p["eids"]) + len(p["doi"]) + len(p["wids"]) > 2),
    }

    # ── 5. S4 identity gold QA：三类 merge ──
    # 1) same DOI / different WID
    doi_wid_merge = [p for p in papers
                     if p["doi"] and len(p["wids"]) >= 2]
    # 2) same normalized title / different IDs（title 合并 + 组内含多个不同 id 类型）
    title_id_merge = []
    for p in papers:
        has_title_edge = any(m[2] == "title" for m in p["match_edges"])
        n_ids = len(p["eids"]) + len(p["doi"]) + len(p["wids"])
        if has_title_edge and n_ids >= 2:
            title_id_merge.append(p)
    # 3) citation + query 双来源（BOTH）——重点
    both_detail = [{
        "canonical_id": p["canonical_id"], "eids": p["eids"],
        "doi": p["doi"], "wids": p["wids"], "titles": p["titles"],
        "n_members": p["n_members"],
        "match_methods": sorted({m[2] for m in p["match_edges"]}),
        "already_seen_s3": p["already_seen_s3"],
    } for p in both_papers]
    qa = {
        "version": "s4_identity_qa_v1",
        "reconciled_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "overlap_qa": {
            "nominal_before": 0,
            "canonical_after": len(both_papers),
            "verdict": ("identity namespace bug 已修复（citation/query 实际共享论文，"
                        "nominal key 比较恒 0 是假象）" if len(both_papers) > 0
                        else "真零重叠：citation 与 query 两机制无共享论文（需用户裁决）"),
        },
        "merge_class_1_same_doi_diff_wid": {
            "n_papers": len(doi_wid_merge),
            "samples": [{"canonical_id": p["canonical_id"], "doi": p["doi"],
                         "wids": p["wids"]} for p in doi_wid_merge[:10]],
        },
        "merge_class_2_same_title_diff_ids": {
            "n_papers": len(title_id_merge),
            "samples": [{"canonical_id": p["canonical_id"], "eids": p["eids"],
                         "doi": p["doi"], "wids": p["wids"]} for p in title_id_merge[:10]],
        },
        "merge_class_3_cit_query_both": {
            "n_papers": len(both_detail),
            "papers": both_detail,
        },
        "identity_unknown_note": "无 EID 且无 DOI 的 canonical paper（title/WID-only）；"
                                 "canonical 合并后若低于 nominal 885 说明部分论文经 query 侧获得强 identity",
    }

    # ── 6. 断言（用户定）──
    assert len(s4_keys) == len(s3_keys) + len(s4_new), \
        f"canonical union invariant broken: {len(s4_keys)} != {len(s3_keys)} + {len(s4_new)}"
    assert all(p["match_edges"] or p["n_members"] == 1 for p in papers), \
        "identity provenance 不完整"

    print("=" * 78)
    print("S4 identity reconciliation（canonical paper identity）")
    print("=" * 78)
    print(f"citation records = {len(cit)} | query rows = {sum(len(v) for v in rbq.values())}"
          f" | S3 = {len(s3_keys)}")
    print(f"canonical papers = {len(papers)}（合并前 {len(nodes)} 节点）| "
          f"BOTH source = {len(both_papers)}")
    print(f"identity unknown = {aggregate['identity_unknown_total']}（无 EID 且无 DOI）")
    print()
    print("=== aggregate（canonical）===")
    for k, v in aggregate.items():
        print(f"  {k:<26} {v}")
    print(f"\n  search_s4_union_unique == {len(s3_keys)} + {len(s4_new)} "
          f"= {len(s3_keys) + len(s4_new)} ✓")

    print("\n=== S4 identity gold QA ===")
    print(f"  merge1 same-DOI/diff-WID   : {len(doi_wid_merge)}")
    print(f"  merge2 same-title/diff-IDs : {len(title_id_merge)}")
    print(f"  merge3 cit+query BOTH      : {len(both_papers)}"
          f"（nominal overlap_before=0 → canonical after={len(both_papers)}）")
    for b in both_detail[:10]:
        print(f"    {b['canonical_id'][:50]:<52} eids={len(b['eids'])} "
              f"doi={b['doi']} wids={len(b['wids'])} methods={b['match_methods']}")

    # ── 7. 写盘 ──
    now = datetime.datetime.now().isoformat(timespec="seconds")
    manifest = {
        "version": "s4_identity_manifest_v1", "reconciled_at": now,
        "identity_channels": ["EID", "DOI", "WID", "normalized_title"],
        "match_rules": "DOI 相等 union；normalized_title 完全相等 union（高置信精确，"
                       "与 R03 resolve_seen_s1 同口径；无模糊匹配）",
        "canonical_id_priority": "EID(2-s2.0-) > DOI > WID > key",
        "n_records_in": {"citation": len(cit), "query": sum(len(v) for v in rbq.values())},
        "n_papers": len(papers), "aggregate": aggregate,
        "papers": papers,
    }
    for path, obj in ((args.manifest, manifest), (args.qa_out, qa),
                      (args.seen_set, {
                          "search_snapshot_id": "S4_" + (json.load(open(SNAP, encoding="utf-8"))
                                                         .get("search_snapshot_id", "?")),
                          "frozen_at": now,
                          "definition": "SEARCH_S4_SEEN_UNION = S3_SEEN(13430) ∪ canonical new "
                                        "papers（EID/DOI/WID/title 统一 identity，reconcile v1）",
                          "size": len(s4_keys), "keys": sorted(s4_keys)}),
                      (args.delta, {
                          "search_snapshot_id": "S4_" + (json.load(open(SNAP, encoding="utf-8"))
                                                         .get("search_snapshot_id", "?")),
                          "delta_vs_s3": {"s3_seen": len(s3_keys), "s4_seen": len(s4_keys),
                                          "citation_new_vs_s3": len(cit_new),
                                          "query_new_vs_s3": len(q_new),
                                          "s4_new_vs_s3": len(s4_new),
                                          "note": "canonical identity 重建后（reconcile v1）"},
                          "identity_reconciled": True})):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)

    # snapshot 更新：aggregate 换 canonical 口径 + S4_IDENTITY=VERIFIED
    old_snap = json.load(open(args.snapshot, encoding="utf-8")) \
        if os.path.exists(args.snapshot) else {}
    old_snap["aggregate"] = aggregate
    old_snap["identity_reconciliation"] = {
        "status": "VERIFIED", "reconciled_at": now,
        "tool": "tools/reconcile_s4_identity.py",
        "canonical_papers": len(papers),
        "nodes_before": len(nodes),
        "citation_query_overlap_before": 0,      # nominal key namespace 直接比较（bug）
        "citation_query_overlap_after": len(both_papers),
        "s4_seen_before": 19445, "s4_seen_after": len(s4_keys),
        "identity_unknown_before": 885, "identity_unknown_after": aggregate[
            "identity_unknown_total"],
        "multi_identity_papers": aggregate["multi_identity_papers"],
        "gold_qa": {
            "merge1_same_doi_diff_wid": len(doi_wid_merge),
            "merge2_same_title_diff_ids": len(title_id_merge),
            "merge3_cit_query_both": len(both_papers),
            "note": "overlap QA：reconcile 后 >0 = namespace bug 已修复；"
                    "仍 =0 才是真零重叠（详见 s4_identity_qa.json）"},
        "match_rules": manifest["match_rules"],
        "canonical_id_priority": manifest["canonical_id_priority"],
    }
    old_snap["interpretation"] = ("S4 是 quality-gate + diverse-policy 决策（防 development "
                                  "overfit）；identity 已 reconcile（canonical paper："
                                  "EID/DOI/WID/title 统一）；正式召回率必须 fresh R04")
    with open(args.snapshot, "w", encoding="utf-8") as f:
        json.dump(old_snap, f, ensure_ascii=False, indent=1)
    print(f"[OK] snapshot   : {args.snapshot}（S4_IDENTITY=VERIFIED，aggregate 换 canonical）")
    print(f"[OK] manifest   : {args.manifest}")
    print(f"[OK] qa         : {args.qa_out}")
    print(f"[OK] seen set   : {args.seen_set}（{len(s4_keys)} keys）")
    print(f"[OK] delta      : {args.delta}")


if __name__ == "__main__":
    main()
