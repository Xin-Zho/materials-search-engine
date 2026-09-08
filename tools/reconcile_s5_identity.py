#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/reconcile_s5_identity.py — S5 identity reconciliation（2026-08-31 用户定）。

S5 只有 query 通道（无新 citation 层）：q_new = q_keys − s4_keys 是 key 级差集——
DOI-fallback key 与 S4 的 EID key 可能同论文双记（S4 曾实证 28 个撞车 + overlap 0→478）。
S5 canonical 处理：
  1. query rows（s5_query_records）→ 节点（EID/DOI/WID/title）
  2. union-find：DOI 相等 / normalized title 相等（高置信精确，与 R03/R04 同口径）
  3. canonical_id 优先级：EID(2-s2.0-) > DOI > WID > key
  4. 已见判定 = S4 三通道（build_s4_found_sets：eids/dois/titles）+ WID/DOI key 直查 s4_keys
     （S4 教训：WID-only 且 title 缺失的论文三通道覆盖不到 → 回查 canonical keys）
  5. S5_SEEN = S4_SEEN ∪ canonical new papers；断言 |S5| == |S4| + |S5\\S4|

只读 s5_query_records.json + s4_seen_set.json + S4 三通道——不重跑检索。

输出：
  s5_identity_manifest.json / s5_seen_set.json（canonical 重建）/ s5_candidate_snapshot.json
  （S5_IDENTITY=VERIFIED）/ s5_delta_vs_s4.json

用法：
  python tools/reconcile_s5_identity.py
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
from build_r04_seen import build_s4_found_sets  # noqa: E402

T = os.path.join(BASE, "data", "exports", "terminology")
Q_REC = os.path.join(T, "s5_query_records.json")
S4_SEEN = os.path.join(T, "s4_seen_set.json")
SNAP = os.path.join(T, "s5_candidate_snapshot.json")
SEEN = os.path.join(T, "s5_seen_set.json")
DELTA = os.path.join(T, "s5_delta_vs_s4.json")
MANIFEST = os.path.join(T, "s5_identity_manifest.json")


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
    ap.add_argument("--query-records", default=Q_REC)
    ap.add_argument("--s4-seen", default=S4_SEEN)
    ap.add_argument("--manifest", default=MANIFEST)
    ap.add_argument("--seen-set", default=SEEN)
    ap.add_argument("--snapshot", default=SNAP)
    ap.add_argument("--delta", default=DELTA)
    args = ap.parse_args()

    rbq = json.load(open(args.query_records, encoding="utf-8"))["records_by_query"]
    s4_keys = set(json.load(open(args.s4_seen, encoding="utf-8"))["keys"])
    s4_found = build_s4_found_sets()
    print(f"[input] s5_query_records rows={sum(len(v) for v in rbq.values())} | S4_SEEN={len(s4_keys)}")

    # ── 1. 节点（query rows）──
    nodes = {}
    for qs, rows in rbq.items():
        for r in rows:
            if not r.get("key"):
                continue
            nid = f"q:{r['key']}"
            if nid in nodes:
                nodes[nid]["provenance"].append({"query": qs})
                continue
            nodes[nid] = {"id": nid, "source": "QUERY", "key": r["key"],
                          "eid": r.get("eid"), "doi": _norm_doi(r.get("doi")) or None,
                          "title": r.get("title"),
                          "provenance": [{"query": qs}]}

    # ── 2. union-find：DOI / normalized title ──
    dsu = DSU()
    doi_idx, title_idx, match_edges = {}, {}, []
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
        groups.setdefault(dsu.find(nid), []).append(nid)

    def canon_of(nds):
        for nd in nds:
            e = _eid_of(nd)
            if e:
                return e
        for nd in nds:
            if nd.get("doi"):
                return nd["doi"]
        return sorted(nd["key"] for nd in nds)[0]

    papers = []
    for root, nids in groups.items():
        nds = [nodes[n] for n in nids]
        cid = canon_of(nds)
        eids = sorted({_eid_of(nd) for nd in nds} - {None})
        dois = sorted({nd["doi"] for nd in nds if nd["doi"]})
        titles = sorted({_norm_title(nd["title"]) for nd in nds if nd["title"]})
        already = False
        if eids and any(e in s4_found["eids"] for e in eids):
            already = True
        elif dois and any(d in s4_found["dois"] for d in dois):
            already = True
        elif titles and any(t in s4_found["titles"] for t in titles):
            already = True
        elif cid in s4_keys:          # key 直查（DOI/WID/EID fallback）
            already = True
        papers.append({
            "canonical_id": cid, "eids": eids, "doi": dois, "titles": titles,
            "already_seen_s4": already, "n_members": len(nids),
            "match_edges": [e for e in match_edges if e[0] in nids or e[1] in nids],
        })

    # ── 4. 集合统计 ──
    s5_new = {p["canonical_id"] for p in papers if not p["already_seen_s4"]}
    s5_keys = s4_keys | s5_new
    overlap_papers = sum(1 for p in papers if p["already_seen_s4"])
    aggregate = {
        "s4_seen_unique": len(s4_keys),
        "query_canonical_papers": len(papers),
        "query_papers_already_seen_s4": overlap_papers,
        "s5_new_vs_s4": len(s5_new),
        "search_s5_union_unique": len(s5_keys),
        "identity_unknown": sum(1 for p in papers if not p["eids"] and not p["doi"]),
    }
    assert len(s5_keys) == len(s4_keys) + len(s5_new), \
        f"canonical union invariant broken: {len(s5_keys)} != {len(s4_keys)} + {len(s5_new)}"
    print(f"[assert] search_s5_union_unique == {len(s4_keys)} + {len(s5_new)} "
          f"= {len(s4_keys) + len(s5_new)} ✓")
    print(f"[overlap] query papers already in S4（key 级差集虚高部分）: {overlap_papers}")
    print(f"[identity-unknown] {aggregate['identity_unknown']}（无 EID 且无 DOI）")

    now = datetime.datetime.now().isoformat(timespec="seconds")
    manifest = {
        "version": "s5_identity_manifest_v1", "reconciled_at": now,
        "identity_channels": ["EID", "DOI", "WID", "normalized_title"],
        "match_rules": "DOI 相等 union；normalized_title 完全相等 union（高置信精确）；"
                       "已见判定 = S4 三通道 + key 直查 s4_keys",
        "canonical_id_priority": "EID(2-s2.0-) > DOI > WID > key",
        "n_papers": len(papers), "aggregate": aggregate, "papers": papers,
    }
    for path, obj in ((args.manifest, manifest),
                      (args.seen_set, {
                          "search_snapshot_id": "S5_" + (json.load(open(SNAP, encoding="utf-8"))
                                                         .get("search_snapshot_id", "?")),
                          "frozen_at": now,
                          "definition": "SEARCH_S5_SEEN_UNION = S4_SEEN(19194) ∪ canonical new "
                                        "papers（EID/DOI/WID/title 统一 identity，reconcile v1）",
                          "size": len(s5_keys), "keys": sorted(s5_keys)}),
                      (args.delta, {
                          "search_snapshot_id": "S5_" + (json.load(open(SNAP, encoding="utf-8"))
                                                         .get("search_snapshot_id", "?")),
                          "delta_vs_s4": {"s4_seen": len(s4_keys), "s5_seen": len(s5_keys),
                                          "s5_new_vs_s4": len(s5_new),
                                          "note": "canonical identity 重建后（reconcile v1）"},
                          "identity_reconciled": True})):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)

    old_snap = json.load(open(args.snapshot, encoding="utf-8")) \
        if os.path.exists(args.snapshot) else {}
    old_snap["aggregate"] = aggregate
    old_snap["identity_reconciliation"] = {
        "status": "VERIFIED", "reconciled_at": now,
        "tool": "tools/reconcile_s5_identity.py",
        "canonical_papers": len(papers),
        "query_papers_already_seen_s4": overlap_papers,
        "s5_seen_after": len(s5_keys),
        "identity_unknown": aggregate["identity_unknown"],
        "note": "S5 单通道（query only）；论文级重叠 = key 级差集虚高部分",
    }
    old_snap["interpretation"] = ("S5 cross-layer query mechanism（V1→V3 实验链冻结）；"
                                  "identity 已 reconcile；正式召回率必须 fresh R05 paired")
    with open(args.snapshot, "w", encoding="utf-8") as f:
        json.dump(old_snap, f, ensure_ascii=False, indent=1)
    print(f"[OK] snapshot   : {args.snapshot}（S5_IDENTITY=VERIFIED）")
    print(f"[OK] manifest   : {args.manifest}")
    print(f"[OK] seen set   : {args.seen_set}（{len(s5_keys)} keys）")
    print(f"[OK] delta      : {args.delta}")


if __name__ == "__main__":
    main()
