#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/citation_2hop_reachability_audit.py — S4 Step 0.5 + Step 1（2026-08-30 用户定）。

Step 0.5  residual canonical identity reconciliation（轻量，先于 2-hop）：
  s4_residual 38 篇内部按 normalized DOI / normalized title 查 duplicate/alias
  → residual_canonical_count。**R03 audit record 保持原样**；重复论文在 diagnosis 中标
  INDEX_IDENTITY=True + duplicate_canonical_group（审计事实与开发修复事实分开）。
  （已知案例：W7110794929 vs W4416134588 = 同 SU-8 论文，.s001 补充材料记录。）

Step 1  citation reachability audit（1-hop + restricted 2-hop，reachability 诊断而非 expansion）：
  对每个 residual miss m，求 d(m, S)——到 high-confidence relevant seed 集的 citation 最短距离：
    0 = 已 seen（理论上 residual 不应出现；出现=identity 问题）
    1 = 1-hop reachable
    2 = 2-hop reachable
    >2 / unreachable
  seed 集 S（R03 口径）：KB relevant ∩ S3 seen ∪ R03 RELEVANT ∧ Seen_S3=TRUE。
  2-hop 输出 paths[{seed_wid, bridge_wid, direction_1, direction_2}] + bridge quality
  （n_paths / n_seeds / n_bridges / bridge_relevance / bridge_specificity）。
  方向（QA 口径）：BACKWARD = target 引用 source；FORWARD = source 引用 target。
  形态：
    A seed→b→m   (F,F)   b 被 seed 引用且 b 引用 m（b=m 的引用者）
    B seed→b←m   (F,B)   b 被 seed 引用且 m 引用 b（bibliographic coupling）
    C b→seed,b→m (B,B)   b 同时引用 seed 与 m（co-cited）
    D m→b→seed   (B,B)   m 引用 b，b 引用 seed（链）
  gate：
    2H_STRONG = ≥2 independent seeds/paths 或存在 topic-relevant bridge
    2H_WEAK   = 仅有 generic/high-degree bridge
    2H_NONE   = 无 2-hop path

用法：
  python tools/citation_2hop_reachability_audit.py --r03-labels <R03 filled.json>
      [--out <path>] [--identity-only]
"""
import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "tools"))

from build_r03_seen import build_s3_found_sets  # noqa: E402
from build_r02_seen import resolve_seen_s1, _norm_doi, _norm_title  # noqa: E402
from citation_reachability_audit import load_oa  # noqa: E402

T = os.path.join(BASE, "data", "exports", "terminology")
MISSES = os.path.join(T, "s4_residual_misses.json")
DEFAULT_OUT = os.path.join(T, "s4_citation_reachability.json")
UNIVERSES = os.path.join(BASE, "data", "exports", "completeness_universes.json")
DEFAULT_LABELS = os.path.join(BASE, "data", "exports", "completeness_labels",
                              "pc_001__20260830011713_filled.json")

# bridge topic-relevant 核心词（判断 bridge 是否与 shrinkage 领域相关）
CORE_VOCAB = ("shrinkage", "shrink", "contraction", "polymerization",
              "photopolymer", "photocure", "photocur", "curing", "resin",
              "monomer", "dental", "composite", "acrylate", "methacrylate",
              "polymer", "volumetric", "stress", "deformation", "stereolithograph",
              "holograph", "dilatometer", "linometer")


def build_s3_seeds(labels_path: str, oa: dict) -> tuple[set, int]:
    """S（R03 口径 high-conf seeds）= KB relevant ∩ S3 seen ∪ R03 RELEVANT ∧ Seen_S3=TRUE。"""
    found_s3 = build_s3_found_sets()
    labs = json.load(open(labels_path, encoding="utf-8"))["labels"]
    rel = [l for l in labs if l.get("label") == "RELEVANT"]
    verdicts = resolve_seen_s1([l["paper_id"] for l in rel], found_s3)
    seed = {l["paper_id"] for l in rel
            if verdicts.get(l["paper_id"], {}).get("agent_seen_s1") == "TRUE"}
    n_r03 = len(seed)
    snaps = json.load(open(UNIVERSES, encoding="utf-8"))
    r03_uni = next((u for u in snaps
                    if u["universe_id"] == "pc_001-2026-08-30T011713"), None)
    if r03_uni:
        found = set(r03_uni.get("source_breakdown", {}).get("found_relevant", []))
        kb_v = resolve_seen_s1(list(found), found_s3)
        for wid in found:
            if kb_v.get(wid, {}).get("agent_seen_s1") == "TRUE":
                seed.add(wid)
    return seed, n_r03


def identity_reconcile(misses: list[dict]) -> dict:
    """Step 0.5：38 篇内部 duplicate/alias（normalized DOI / normalized title）。"""
    seen_doi, seen_title = {}, {}
    groups = []
    assigned = set()
    for m in misses:
        wid = m["wid"]
        doi = _norm_doi(m.get("doi"))
        nt = _norm_title(m.get("title") or m.get("oa_title") or "")
        hit = None
        if doi and doi in seen_doi:
            hit = seen_doi[doi]
        elif nt and nt in seen_title:
            hit = seen_title[nt]
        if hit is None:
            groups.append([wid])
            g = groups[-1]
            if doi:
                seen_doi[doi] = g
            if nt:
                seen_title[nt] = g
        else:
            hit.append(wid)
            if doi:
                seen_doi.setdefault(doi, hit)
            if nt:
                seen_title.setdefault(nt, hit)
    dup_groups = [g for g in groups if len(g) > 1]
    n_canonical = len(groups)
    return {
        "residual_raw_count": len(misses),
        "residual_canonical_count": n_canonical,
        "duplicate_groups": dup_groups,
        "n_duplicate_groups": len(dup_groups),
        "index_identity_flags": [{"wids": g, "label": "INDEX_IDENTITY",
                                  "reason": "duplicate/alias in S4 residual"} for g in dup_groups],
    }


def reachability(misses: list[dict], seed: set, oa: dict) -> list[dict]:
    """Step 1：per-miss d(m,S) + 1-hop/2-hop paths + bridge quality。"""
    # 索引
    seed_forward = {s: set(oa.get(s, {}).get("referenced_works", [])) for s in seed}
    b_owners = {}   # bridge -> set(seeds that reference bridge)
    for s, refs in seed_forward.items():
        for b in refs:
            b_owners.setdefault(b, set()).add(s)
    seed_set = set(seed)
    all_refs = {w: set(m.get("referenced_works", []))
                for w, m in oa.items() if m.get("referenced_works")}

    results = []
    for m in misses:
        wid = m["wid"]
        m_refs = all_refs.get(wid, set())
        # m 的引用者（谁引用 m）——全 oa 遍历
        m_citers = [w for w, refs in all_refs.items() if wid in refs]

        # 1-hop
        d1_back = sorted(m_refs & seed_set)
        d1_fwd = sorted(s for s in seed if wid in seed_forward[s])
        hop1 = bool(d1_back or d1_fwd)

        # 2-hop paths
        paths = []
        # 形态 A（seed→b→m, F,F）：b 引用 m 且 b 被 seed 引用
        for b in m_citers:
            if b in b_owners:
                for s in b_owners[b]:
                    paths.append({"seed_wid": s, "bridge_wid": b,
                                  "direction_1": "FORWARD", "direction_2": "FORWARD",
                                  "form": "A"})
        # 形态 B（seed→b←m, F,B）：m 引用 b 且 b 被 seed 引用
        for b in (m_refs & set(b_owners.keys())):
            for s in b_owners[b]:
                paths.append({"seed_wid": s, "bridge_wid": b,
                              "direction_1": "FORWARD", "direction_2": "BACKWARD",
                              "form": "B"})
        # 形态 C（b→seed,b→m, B,B）：b 引用 m 且 b 引用 seed
        for b in m_citers:
            b_refs = all_refs.get(b, set())
            hit = b_refs & seed_set
            for s in hit:
                paths.append({"seed_wid": s, "bridge_wid": b,
                              "direction_1": "BACKWARD", "direction_2": "BACKWARD",
                              "form": "C"})
        # 形态 D（m→b→seed, B,B 链）：m 引用 b 且 b 引用 seed
        for b in m_refs:
            b_refs = all_refs.get(b, set())
            hit = b_refs & seed_set
            for s in hit:
                paths.append({"seed_wid": s, "bridge_wid": b,
                              "direction_1": "BACKWARD", "direction_2": "BACKWARD",
                              "form": "D"})

        # 去重（同 (seed, bridge) 多形态只留一条，优先非 D）
        seen_pair = set()
        uniq = []
        for p in paths:
            k = (p["seed_wid"], p["bridge_wid"])
            if k in seen_pair:
                continue
            seen_pair.add(k)
            uniq.append(p)
        paths = uniq

        # bridge quality
        n_seeds = len({p["seed_wid"] for p in paths})
        n_bridges = len({p["bridge_wid"] for p in paths})
        bridge_info = {}
        for p in paths:
            b = p["bridge_wid"]
            bm = oa.get(b, {})
            bt = (bm.get("title") or "").lower()
            b_refs = all_refs.get(b, set())
            in_deg = sum(1 for w, refs in all_refs.items() if b in refs)
            rel = any(v in bt for v in CORE_VOCAB)
            bridge_info[b] = {"title": bm.get("title"), "topic_relevant": rel,
                              "indegree": in_deg, "outdegree": len(b_refs)}
        n_rel_bridge = sum(1 for v in bridge_info.values() if v["topic_relevant"])
        n_gen_bridge = n_bridges - n_rel_bridge

        # gate
        if not paths:
            gate = "2H_NONE"
        elif n_seeds >= 2 or n_rel_bridge >= 1:
            gate = "2H_STRONG"
        else:
            gate = "2H_WEAK"

        d = 0 if (m.get("seen_s3") == "TRUE") else (1 if hop1 else (2 if paths else 99))
        results.append({
            "miss_wid": wid,
            "title": m.get("oa_title") or m.get("title"),
            "distance": d,
            "citation_1hop": hop1,
            "citation_2hop": bool(paths),
            "gate": gate,
            "n_paths": len(paths),
            "n_seeds": n_seeds,
            "n_bridges": n_bridges,
            "n_topic_relevant_bridges": n_rel_bridge,
            "n_generic_bridges": n_gen_bridge,
            "paths": paths[:20],
            "bridges": bridge_info,
            "hop1_backward": d1_back[:10], "hop1_forward": d1_fwd[:10],
        })
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--misses", default=MISSES)
    ap.add_argument("--r03-labels", default=DEFAULT_LABELS,
                    help="R03 filled labels（默认项目路径；未 copy 时用 Downloads 路径）")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--identity-only", action="store_true",
                    help="只跑 Step 0.5 identity reconciliation")
    args = ap.parse_args()

    if not os.path.exists(args.r03_labels):
        print(f"[FATAL] 缺 R03 labels: {args.r03_labels}")
        sys.exit(2)
    data = json.load(open(args.misses, encoding="utf-8"))
    misses = data["misses"]
    oa = load_oa()

    # ── Step 0.5 ──
    ident = identity_reconcile(misses)
    print("=" * 78)
    print("S4 Step 0.5: residual canonical identity reconciliation")
    print("=" * 78)
    print(f"residual raw = {ident['residual_raw_count']} → canonical = "
          f"{ident['residual_canonical_count']} | duplicate groups = "
          f"{ident['n_duplicate_groups']}")
    for g in ident["duplicate_groups"]:
        print(f"  DUP: {g}")
    if args.identity_only:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"version": "s4_identity_v1",
                       "identity": ident}, f, ensure_ascii=False, indent=1)
        print(f"[OK] identity-only written: {args.out}")
        return

    # ── Step 1 ──
    seed, n_r03 = build_s3_seeds(args.r03_labels, oa)
    print(f"\nS3 high-conf seeds = {len(seed)}（R03 seen relevant {n_r03} + KB）")
    results = reachability(misses, seed, oa)

    from collections import Counter
    dist = Counter(r["distance"] for r in results)
    gates = Counter(r["gate"] for r in results)
    print("\n" + "=" * 78)
    print("S4 Step 1: citation reachability（1-hop + restricted 2-hop）")
    print("=" * 78)
    print(f"distance 分布: {dict(sorted(dist.items()))}（99=unreachable）")
    print(f"2-hop gate: {dict(gates)}")
    print(f"\n{'miss':<16}{'d':>4}{'1h':>4}{'2h':>4}{'gate':<10}{'paths':>6}{'seeds':>6}"
          f"{'relB':>6}")
    for r in results:
        print(f"{r['miss_wid']:<16}{r['distance']:>4}{str(r['citation_1hop']):>4}"
              f"{str(r['citation_2hop']):>4}{r['gate']:<10}{r['n_paths']:>6}"
              f"{r['n_seeds']:>6}{r['n_topic_relevant_bridges']:>6}  "
              f"{(r['title'] or '')[:44]}")

    out = {
        "version": "s4_citation_reachability_v1",
        "frozen_at": "2026-08-30",
        "development_source": "AUDIT_R03",
        "step05_identity": ident,
        "seed_definition": "KB relevant ∩ S3 seen ∪ R03 RELEVANT ∧ Seen_S3=TRUE",
        "n_seeds": len(seed),
        "distance_distribution": dict(dist),
        "gate_distribution": dict(gates),
        "per_miss": results,
        "note": "reachability 诊断非 expansion；2-hop 用 openalex cache 覆盖（已知限制）；"
                "gate: STRONG=≥2 seeds 或 topic-relevant bridge；WEAK=仅 generic bridge",
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n[OK] written: {args.out}")


if __name__ == "__main__":
    main()
