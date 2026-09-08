#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/cross_audit_miss_similarity.py — Step 7 diagnostic（2026-08-31 用户拍板）。

目的（最小增量，不动主架构）：
  判断 R04 的 26 篇 miss 到底属于哪一类：
    A. 已有知识附近没搜到（query coverage gap）→ 只修 query generation / term expansion
    B. citation 强但语义词弱 → 修 citation seed/action policy
    C. 应用跨域导致词断裂 → TermFamily 加 cross-application expansion
    D. 五层普遍弱 → 才考虑新增 discovery channel

方法：26 miss × R04 S4-found relevant(28) 配对关联分析，五层诊断特征：
  PROPERTY    性质/机制（cure shrinkage / residual stress / warpage / gelation ...）
  STRUCTURE   结构（fiber-matrix / laminate / network / debond ...）
  FORMULATION 配方（epoxy / acrylate / dimethacrylate / photoinitiator ...）
  APPLICATION 应用（dental / SLA / packaging / composites / optics ...）
  CITATION    引用距离（direct / 1-hop / 2-hop / disconnected，OpenAlex referenced_works）

输入（全部复用现有产物，不重跑检索）：
  --labels     R04 filled labels（含 title/abstract）
  --misses     data/exports/terminology/r04_misses.json（26）
  found 判定   build_s4_found_sets()（S4_SEEN_SET=19194 canonical 三通道）+ resolve_seen_s1
  citation    data/cache/openalex_cache.json（--skip-citation 时跳过，仅四层语义）

输出：
  r04_miss_found_similarity.json
    每 miss：top3 found（含五层标注 + 聚合分数）
    汇总：HIGH/MED/LOW 分布 + verdict 分支建议

用法：
  python tools/cross_audit_miss_similarity.py --labels <filled.json> [--skip-citation]
"""
import argparse
import datetime
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tools"))
sys.path.insert(0, os.path.join(BASE, "search_engine", "completeness"))

from build_r02_seen import resolve_seen_s1  # noqa: E402
from build_r04_seen import build_s4_found_sets  # noqa: E402
from citation_reachability_audit import load_oa  # noqa: E402

T = os.path.join(BASE, "data", "exports", "terminology")
DEFAULT_MISSES = os.path.join(T, "r04_misses.json")
DEFAULT_OUT = os.path.join(T, "r04_miss_found_similarity.json")
TOP_K = 3

# ── 五层关键词规则（诊断特征，非系统 ontology）──
LAYER_RULES = {
    "PROPERTY": [
        "shrinkage", "contraction", "cure shrink", "residual stress", "warpage",
        "distortion", "deformation", "stress", "strain", "gelation", "gel point",
        "modulus", "dimensional change", "volumetric change", "curing shrink",
        "post-cure", "degree of cure", "glass transition",
    ],
    "STRUCTURE": [
        "fiber", "matrix", "composite", "laminate", "network", "interface",
        "debond", "crosslink", "coating", "film", "layer", "specimen",
        "fiber/matrix", "reinforced", "particle", "nanoparticle", "mold",
    ],
    "FORMULATION": [
        "epoxy", "acrylate", "methacrylate", "dimethacrylate", "monomer",
        "oligomer", "photoinitiator", "resin", "polymer", "filler", "glass",
        "bisphenol", "amine", "curing agent", "thermoset", "thermosetting",
        "uv curable", "uv-curable", "vinyl", "copolymer", "polyurethane",
        "elastomer", "jeffamine", "hardener", "additive",
    ],
    "APPLICATION": [
        "dental", "tooth", "restorative", "denture", "filling", "adhesive",
        "sealant", "sla", "stereolithography", "3d print", "3d printing",
        "additive manufacturing", "packaging", "molding", "encapsulation",
        "insulator", "insulation", "coating", "paint", "aircraft", "aerospace",
        "automotive", "electronics", "electronic", "optical", "holograph",
        "data storage", "chip", "module", "composite part", "casting",
        "pattern", "lithograph", "photoresist",
    ],
}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())


def layer_tags(text: str) -> set:
    """返回命中的层标签（关键词子串匹配）。"""
    t = _norm(text)
    tags = set()
    for layer, kws in LAYER_RULES.items():
        for kw in kws:
            if kw in t:
                tags.add(f"{layer}:{kw}")
    return tags


def layer_overlap(tags_a: set, tags_b: set) -> dict:
    """按层算 Jaccard；无共享 → 0。返回 {layer: 0..1}。"""
    out = {}
    for layer in LAYER_RULES:
        a = {x for x in tags_a if x.startswith(layer + ":")}
        b = {x for x in tags_b if x.startswith(layer + ":")}
        if not a and not b:
            out[layer] = None        # 双方都无该层标签 → 不可比
        else:
            out[layer] = len(a & b) / len(a | b) if (a | b) else 0.0
    return out


def sim_label(v):
    if v is None:
        return "N/A"
    if v >= 0.5:
        return "HIGH"
    if v > 0:
        return "MED"
    return "LOW"


def cite_distance(miss_meta, found_meta, oa):
    """miss ↔ found citation 距离：direct/1-hop/2-hop/disconnected。"""
    mw = miss_meta.get("paper_id") or miss_meta.get("wid")
    fw = found_meta.get("paper_id") or found_meta.get("wid")
    m = oa.get(mw, {})
    f = oa.get(fw, {})
    m_refs = set(m.get("referenced_works", []))
    f_refs = set(f.get("referenced_works", []))
    if mw == fw:
        return "direct"
    if mw in f_refs or fw in m_refs:
        return "1-hop"
    if m_refs & f_refs:
        return "2-hop"
    return "disconnected"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", required=True, help="filled labels 路径（R04/R05 同构）")
    ap.add_argument("--misses", default=DEFAULT_MISSES)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--seen-basis", default="s4", choices=["s4", "s5"],
                    help="found relevant 判定基准：s4（R04 用，build_s4_found_sets）/ "
                         "s5（R05 用，build_s5_found_sets，S5_SEEN=20417 canonical）")
    ap.add_argument("--skip-citation", action="store_true",
                    help="跳过 OpenAlex citation 距离（仅四层语义诊断）")
    args = ap.parse_args()

    # ── 1. found relevant：RELEVANT ∧ Seen_<basis>=TRUE ──
    labs = json.load(open(args.labels, encoding="utf-8"))["labels"]
    if args.seen_basis == "s5":
        from build_r05_seen import build_s5_found_sets
        found_sets = build_s5_found_sets()
        basis_label = "S5"
    else:
        found_sets = build_s4_found_sets()
        basis_label = "S4"
    verdicts = resolve_seen_s1([l["paper_id"] for l in labs], found_sets)
    found = [l for l in labs if l.get("label") == "RELEVANT"
             and verdicts.get(l["paper_id"], {}).get("agent_seen_s1") == "TRUE"]
    misses = json.load(open(args.misses, encoding="utf-8"))["misses"]
    print(f"[found] RELEVANT ∧ Seen_{basis_label}=TRUE = {len(found)}")
    print(f"[miss]  RELEVANT ∧ Seen_{basis_label}=FALSE = {len(misses)}")

    oa = None
    if not args.skip_citation:
        oa = load_oa()
        print(f"[oa] openalex cache = {len(oa)} works")

    # ── 2. 文本标签（title + abstract）──
    def text_of(entry):
        return f"{entry.get('title', '')} {entry.get('abstract', '') or ''}"

    miss_tags = {m["paper_id"]: layer_tags(text_of(m)) for m in misses}
    found_tags = {f["paper_id"]: layer_tags(text_of(f)) for f in found}

    # ── 3. 每 miss 找 top-3 found ──
    results = []
    for m in misses:
        pid = m["paper_id"]
        scored = []
        for f in found:
            fid = f["paper_id"]
            ov = layer_overlap(miss_tags[pid], found_tags[fid])
            # 聚合：四层语义（citation 单独）——只算可比的层
            sem_layers = [v for k, v in ov.items() if k != "CITATION" and v is not None]
            agg = sum(sem_layers) / len(sem_layers) if sem_layers else 0.0
            row = {
                "found_paper_id": fid, "found_title": f.get("title", ""),
                "overlap": {k: (round(v, 3) if v is not None else None)
                            for k, v in ov.items()},
                "sim_labels": {k: sim_label(v) for k, v in ov.items()},
                "agg_semantic": round(agg, 3),
            }
            if oa is not None:
                row["citation"] = cite_distance(m, f, oa)
            scored.append(row)
        scored.sort(key=lambda x: (-x["agg_semantic"], x["citation"]
                                   if oa is not None else ""))
        top = scored[:TOP_K]
        # 该 miss 的最强关联：top1 的层标签 + citation
        t1 = top[0] if top else None
        strong = 0
        if t1:
            strong = sum(1 for k, v in t1["sim_labels"].items()
                         if v == "HIGH")
        results.append({
            "miss_paper_id": pid, "miss_title": m.get("title", ""),
            "miss_year": m.get("year"), "miss_tags": sorted(miss_tags[pid]),
            "nearest": top,
            "top1_strong_layers": strong,
            "verdict": ("NEAR" if (t1 and strong >= 2) else
                        ("CIT_ONLY" if (t1 and oa is not None
                                        and t1.get("citation") in ("1-hop", "2-hop")
                                        and strong == 0) else
                         ("ISOLATED" if (t1 and strong == 0 and
                                         (oa is None or t1.get("citation") == "disconnected"))
                          else "WEAK"))),
        })

    # ── 4. 汇总 ──
    from collections import Counter
    verdict_cnt = Counter(r["verdict"] for r in results)
    near = [r for r in results if r["verdict"] == "NEAR"]
    n_high = sum(1 for r in results if r["top1_strong_layers"] >= 2)
    n_med = sum(1 for r in results if 0 < r["top1_strong_layers"] < 2)
    n_low = len(results) - n_high - n_med

    print("=" * 72)
    print(f"{basis_label} {len(results)} miss × found({len(found)}) 五层关联诊断")
    print("=" * 72)
    print(f"verdict 分布: {dict(verdict_cnt)}")
    print(f"top1 强关联层数: >=2 HIGH: {n_high}/{len(results)} | 0<HIGH<2: {n_med}/{len(results)} | 0: {n_low}/{len(results)}")
    if oa is not None:
        cit_cnt = Counter(r["nearest"][0]["citation"] for r in results)
        print(f"top1 citation 距离: {dict(cit_cnt)}")

    print("\n=== 每 miss（top1 摘要）===")
    for r in results:
        t1 = r["nearest"][0] if r["nearest"] else {}
        print(f"  [{r['verdict']:<7}] {r['miss_title'][:55]}")
        print(f"          → {t1.get('found_title', '?')[:55]}")
        print(f"            PROP={t1.get('sim_labels', {}).get('PROPERTY')} "
              f"STRUC={t1.get('sim_labels', {}).get('STRUCTURE')} "
              f"FORM={t1.get('sim_labels', {}).get('FORMULATION')} "
              f"APP={t1.get('sim_labels', {}).get('APPLICATION')} "
              f"CIT={t1.get('citation', 'SKIP')}")

    # ── 5. 结论分支（用户定）──
    if n_high >= 18:   # 20/26 级别
        branch = ("QUERY_GAP：大部分 miss 与 found 关联强——已知知识够，"
                  "query formulation/expansion 没利用好 → 只修 query generation")
    elif n_high >= 10:
        branch = ("PARTIAL_QUERY_GAP：约半数关联强——优先补 query/term expansion，"
                  "同时检查 citation 弱侧")
    else:
        branch = ("GENUINE_NEW_REGION：多数 miss 关联弱——现有 TERM+citation+community "
                  "缺少发现新区域机制 → 才有理由考虑新 discovery channel")
    print("\n=== 分支建议 ===")
    print(f"  {branch}")

    out = {
        "version": f"r05_miss_found_similarity_v1" if args.seen_basis == "s5"
        else "r04_miss_found_similarity_v1",
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "method": f"{len(results)} miss × {len(found)} {basis_label}-found relevant；五层诊断特征"
                  "（PROPERTY/STRUCTURE/FORMULATION/APPLICATION/CITATION）关键词规则，非系统 ontology",
        "inputs": {"misses": args.misses,
                   "found_definition": f"{basis_label} RELEVANT ∧ Seen_{basis_label}=TRUE",
                   "citation": "openalex_cache" if oa is not None else "SKIPPED"},
        "summary": {
            "n_misses": len(results), "n_found": len(found),
            "verdict_distribution": dict(verdict_cnt),
            "top1_strong_layers_ge2": n_high,
            "top1_strong_layers_1": n_med,
            "top1_strong_layers_0": n_low,
            "branch": branch,
        },
        "results": results,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n[OK] {args.out}")


if __name__ == "__main__":
    main()
