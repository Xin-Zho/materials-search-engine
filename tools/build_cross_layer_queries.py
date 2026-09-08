#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/build_cross_layer_queries.py — S5 CrossLayerQueryExpansion V2（2026-08-31 用户拍板）。

V1（DEV_FAILED）教训：
  60 actions → 12536 new / saturated 8 / r04 recovery 0/26。失败原因：
    1. term-bag Cartesian 组合无关系约束（interface×amine 无材料意义）；
    2. secondary property 被当主锚点（gelation AND additive manufacturing）；
    3. query 词太 literal，不是 TermFamily（3d print vs 3d printing）。
  结论：不是缺更多词，而是把已有词之间的关系丢掉了。

V2 只改本文件（主架构一行不改）：
  1. term-paper incidence → 轻量 relation graph（论文级共现矩阵 co[x][y]）
  2. SF/PA 组合必须存在 known-relevant 2-hop support：
       BridgeSupport(s,f) = Σ_m min[w(s,m), w(m,f)]，m ∈ 两个中介层
       （SF: S→P→F 或 S→A→F；PA: P→S→A 或 P→F→A）
  3. primary PROPERTY 可直接作 anchor；secondary PROPERTY 必须带 shrinkage core
  4. query 用 TermFamily aliases（corpus 真实词形），不用单 literal
  5. 沿用 specificity / novelty / pilot / QA

纪律（写死）：
  R04 residual may identify the failed transformation type, but MUST NOT supply terms
  to formal S5 query generation——词源 = KnownRelevantKnowledge（relevant papers 文本）。

输入：
  --corpus      known relevant 论文 [{paper_id,title,abstract}]（默认 found_relevant + oa abstract）
  --s4-queries  S4 frozen queries（Novelty 检查）
输出：
  s5_cross_layer_query_candidates.json（candidates + actions 兼容 pilot）
"""
import argparse
import datetime
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T = os.path.join(BASE, "data", "exports", "terminology")
DEFAULT_QUERIES = os.path.join(T, "s4_final_config.json")
DEFAULT_OUT = os.path.join(T, "s5_cross_layer_query_candidates.json")

LAYERS = ["PROPERTY", "STRUCTURE", "FORMULATION", "APPLICATION"]

# ── 四层关键词（与 V1 同源；alias 规范化见 FAMILY_ALIASES）──
LAYER_RULES = {
    "PROPERTY": [
        "cure shrinkage", "polymerization shrinkage", "curing shrink", "residual stress",
        "warpage", "distortion", "dimensional deformation", "volumetric change",
        "shrinkage stress", "gelation", "degree of cure", "post-cure", "residual deformation",
        "stress relaxation", "dimensional stability", "polymerization stress",
    ],
    "STRUCTURE": [
        "fiber", "matrix", "laminate", "network", "interface", "debond", "crosslink",
        "cross-linked", "crosslinked", "reinforced", "fiber/matrix", "interphase",
        "glass fiber", "carbon fiber", "network structure", "multilayer",
    ],
    "FORMULATION": [
        "epoxy", "epoxy resin", "epoxy molding compound", "acrylate", "methacrylate",
        "dimethacrylate", "bis-gma", "urethane acrylate", "monomer", "oligomer",
        "photoinitiator", "filler", "silica", "silica-filled", "bisphenol", "amine",
        "hardener", "curing agent", "thermoset", "thermosetting", "uv-curable",
        "uv curable", "vinyl ester", "thiol-ene", "cationic", "polymer matrix",
        "resin system", "glass particle", "reactive diluent",
    ],
    "APPLICATION": [
        "dental", "dentistry", "restorative", "denture", "tooth", "filling", "adhesive",
        "sealant", "stereolithography", "sla", "3d print", "additive manufacturing",
        "molding compound", "electronics packaging", "packaging", "encapsulation", "insulator",
        "electrical insulation", "coating", "paint", "aircraft", "aerospace", "composite part",
        "composite manufacturing", "optical", "holograph", "data storage", "lithograph",
        "photoresist", "microelectronics", "multi-chip", "pattern", "casting", "mold",
    ],
}
FAMILY_ALIASES = {
    "3d printing": "3d print", "3d print": "3d print",
    "holographic": "holograph", "lithography": "lithograph",
    "curing shrink": "cure shrinkage",
    "cross-linked": "crosslink", "crosslinked": "crosslink",
    "silica-filled": "silica", "glass particle": "silica",
    "thermosetting": "thermoset", "uv curable": "uv-curable",
    "molding compound": "epoxy molding compound",
    "polymer matrix": "matrix", "fiber/matrix": "fiber",
    "network structure": "network", "resin system": "resin system",
    "dentistry": "dental", "restorative": "dental",
    "electrical insulation": "insulator",
}
# 语义同族映射（alias → canonical family；用于 query 展开）
FAMILY_CANON = {
    "3d print": "3d print", "additive manufacturing": "3d print",
    "stereolithography": "3d print", "sla": "3d print",
    "cure shrinkage": "cure shrinkage", "polymerization shrinkage": "polymerization shrinkage",
    "curing shrink": "cure shrinkage",
    "shrinkage stress": "shrinkage stress", "polymerization stress": "shrinkage stress",
    "residual stress": "residual stress", "warpage": "warpage",
    "gelation": "gelation", "stress relaxation": "stress relaxation",
    "deformation": "deformation", "distortion": "deformation",
    "dimensional deformation": "deformation", "dimensional stability": "dimensional stability",
    "dimensional change": "dimensional stability", "volumetric change": "volumetric change",
    "degree of cure": "degree of cure",
    "epoxy": "epoxy", "epoxy resin": "epoxy", "epoxy molding compound": "epoxy molding compound",
    "acrylate": "acrylate", "methacrylate": "methacrylate", "dimethacrylate": "methacrylate",
    "monomer": "monomer", "oligomer": "monomer", "photoinitiator": "photoinitiator",
    "filler": "filler", "silica": "silica",
    "bisphenol": "bisphenol", "amine": "amine", "hardener": "amine",
    "curing agent": "amine", "thermoset": "thermoset", "uv-curable": "uv-curable",
    "thiol-ene": "thiol-ene", "cationic": "cationic",
    "reactive diluent": "reactive diluent", "vinyl ester": "vinyl ester",
    "bis-gma": "bis-gma", "urethane acrylate": "urethane acrylate",
    "dental": "dental", "adhesive": "adhesive", "sealant": "sealant",
    "packaging": "packaging", "electronics packaging": "packaging",
    "encapsulation": "packaging", "insulator": "insulator",
    "coating": "coating", "aircraft": "aircraft", "optical": "optical",
    "holograph": "optical", "lithograph": "lithograph", "photoresist": "lithograph",
    "data storage": "data storage", "microelectronics": "microelectronics",
    "pattern": "pattern", "casting": "casting", "mold": "casting",
    "composite part": "composite part", "composite manufacturing": "composite part",
    "fiber": "fiber", "matrix": "matrix", "laminate": "laminate",
    "network": "network", "interface": "interface", "debond": "debond",
    "crosslink": "crosslink", "reinforced": "reinforced", "interphase": "interface",
    "multilayer": "multilayer",
}

# ── STRUCTURE queryability gate（V3，复用 TermFamily DIRECT/ANCHORED_ONLY/REJECT）──
# V2 教训：crosslink/interface 作检索入口太泛（interface×silica 数万 hits）——SF 漏掉
# Property 层。V3：SF 三元化 P_core×S×F；STRUCTURE 词分级：DIRECT 可裸用（带 anchor 更稳）、
# ANCHORED_ONLY 必须带 shrinkage core anchor、REJECT 非结构词剔除（debond=failure/mechanical）。
STRUCTURE_QUERYABILITY = {
    "crosslink": "ANCHORED_ONLY",   # 需具体化：crosslinked network / network structure / crosslink density
    "interface": "ANCHORED_ONLY",
    "interphase": "ANCHORED_ONLY",
    "network": "ANCHORED_ONLY",
    "fiber": "DIRECT", "matrix": "DIRECT", "laminate": "DIRECT",
    "reinforced": "DIRECT", "multilayer": "DIRECT",
    "debond": "REJECT",             # 非结构词：failure/mechanical phenomenon
}

# ── Primary / Secondary property（搜索主锚点纪律）──
PRIMARY_PROPERTY = {
    "cure shrinkage", "polymerization shrinkage", "shrinkage stress",
    "volumetric change", "dimensional stability",
}
SECONDARY_PROPERTY = {"residual stress", "warpage", "gelation", "stress relaxation",
                      "deformation", "degree of cure"}
# secondary property 必须带 shrinkage core 才能作 PA anchor
SHRINKAGE_CORE = '("shrinkage" OR contraction OR "cure shrink*" OR "polymerization shrink*")'

# ── TermFamily query aliases（corpus 真实词形；轻量，不做无限扩展）──
FAMILY_QUERY_ALIASES = {
    "cure shrinkage": ['"cure shrink*"', '"polymerization shrink*"', '"shrinkage strain*"',
                       '"curing shrink*"', '"shrinkage stress"'],
    "polymerization shrinkage": ['"polymerization shrink*"', '"polymerization-induced shrink*"',
                                 '"shrinkage stress"', '"cure shrink*"'],
    "shrinkage stress": ['"shrinkage stress"', '"polymerization stress"', '"shrinkage strain*"'],
    "residual stress": ['"residual stress"', '"residual deformation"'],
    "warpage": ['warpage', 'warping'],
    "gelation": ['gelation', '"gel point"'],
    "stress relaxation": ['"stress relaxation"'],
    "deformation": ['deformation', 'distortion'],
    "degree of cure": ['"degree of cure"', '"degree of conversion"'],
    "volumetric change": ['"volumetric change"', '"volumetric shrink*"'],
    "dimensional stability": ['"dimensional stability"', '"dimensional change"',
                              '"dimensional accuracy"'],
    "crosslink": ['"crosslinked network"', '"network structure"', '"crosslink density"'],
    "fiber": ['"glass fiber*"', '"carbon fiber*"', 'fiber', 'fibre', 'reinforced'],
    "matrix": ['matrix', '"polymer matrix"'],
    "laminate": ['laminate*', 'multilayer*', 'multilayered'],
    "network": ['"polymer network"', 'network'],
    "interface": ['interface', 'interphase'],
    "debond": ['debond*'],
    "epoxy": ['epoxy'],
    "epoxy molding compound": ['"epoxy molding compound"', 'emc', '"molding compound"'],
    "acrylate": ['acrylate*', '"urethane acrylate"'],
    "methacrylate": ['methacrylate*', 'dimethacrylate'],
    "monomer": ['monomer*', 'oligomer*'],
    "photoinitiator": ['photoinitiator*'],
    "filler": ['filler', 'fillers', '"glass particle*"'],
    "silica": ['silica', 'silica-filled'],
    "bisphenol": ['bisphenol', '"bisphenol-a"'],
    "amine": ['amine*', 'hardener', '"curing agent*"'],
    "thermoset": ['thermoset*'],
    "uv-curable": ['"uv-curable"', '"uv curable"', '"uv-curing"'],
    "thiol-ene": ['"thiol-ene"'],
    "cationic": ['cationic'],
    "reactive diluent": ['"reactive diluent*"'],
    "vinyl ester": ['"vinyl ester"'],
    "3d print": ['"3d print*"', '"additive manufacturing"', 'stereolithograph*'],
    "dental": ['dental', 'dentistry', 'restorative', 'denture'],
    "packaging": ['"electronics packaging"', 'packaging', 'encapsulation',
                  '"molding compound"', 'microelectronic*', '"multi-chip"'],
    "coating": ['coating', 'coatings', 'paint'],
    "adhesive": ['adhesive', 'sealant'],
    "insulator": ['insulator', '"electrical insulation"'],
    "aircraft": ['aircraft', 'aerospace'],
    "optical": ['optical', 'holograph*', '"data storage"'],
    "lithograph": ['lithograph*', 'photoresist'],
    "pattern": ['pattern'],
    "casting": ['casting', 'mold', 'molding'],
    "composite part": ['"composite part*"', '"composite manufacturing"'],
}

GENERIC_BLACKLIST = {
    "resin", "polymer", "composite", "coating", "adhesive", "application", "material",
    "shrinkage", "contraction", "stress", "strain", "mold", "cure", "monomer", "network",
    "filler", "dental", "packaging", "fiber", "matrix", "epoxy", "acrylate", "thermoset",
}
BRIDGE_TYPES = [("SF", "STRUCTURE", "FORMULATION"),
                ("PA", "PROPERTY", "APPLICATION")]


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())


def _load_oa_with_abstract(found_wids: list[str]) -> list[dict]:
    """从 openalex_cache 原始响应加载 found relevant 论文（abstract_inverted_index 重建）。"""
    cache_path = os.path.join(BASE, "data", "cache", "openalex_cache.json")
    cache = json.load(open(cache_path, encoding="utf-8"))
    want = set(found_wids)
    out, seen_ids = [], set()
    for q, resp in cache.items():
        for w in resp.get("results", []):
            wid = (w.get("id") or "").replace("https://openalex.org/", "")
            if wid not in want or wid in seen_ids:
                continue
            seen_ids.add(wid)
            inv = w.get("abstract_inverted_index") or {}
            if inv:
                pos = {}
                for word, idxs in inv.items():
                    for i in idxs:
                        pos[i] = word
                abstract = " ".join(pos[i] for i in sorted(pos)) if pos else ""
            else:
                abstract = ""
            out.append({"paper_id": wid, "title": w.get("title") or "",
                        "abstract": abstract})
    return out


def extract_paper_tags(corpus: list[dict]) -> dict:
    """term-paper incidence：{paper_id: {canonical_family: layer}}（alias/canon 规范化后）。"""
    inc = {}
    for p in corpus:
        text = _norm(f"{p.get('title', '')} {p.get('abstract', '') or ''}")
        pid = p.get("paper_id")
        tags = {}
        for layer, kws in LAYER_RULES.items():
            for kw in kws:
                if kw in text:
                    ckw = FAMILY_ALIASES.get(kw, kw)
                    canon = FAMILY_CANON.get(ckw, ckw)
                    tags[canon] = layer
        if tags:
            inc[pid] = tags
    return inc


def query_part(family: str) -> str:
    al = FAMILY_QUERY_ALIASES.get(family)
    if al:
        return "(" + " OR ".join(al) + ")"
    return f'"{family}"'


def specificity(family: str) -> float:
    if family in GENERIC_BLACKLIST:
        return 0.0
    n_words = len(family.split())
    return 1.0 if n_words >= 3 else (0.7 if n_words == 2 else 0.4)


def novelty_ok(parts: list[str], s4_query_strings: list[str]) -> int:
    """S4 queries 已同时含所有 parts → 0。"""
    for q in s4_query_strings:
        ql = _norm(q)
        if all(pa in ql for pa in parts):
            return 0
    return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="")
    ap.add_argument("--s4-queries", default=DEFAULT_QUERIES)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--min-evidence", type=int, default=2)
    ap.add_argument("--max-candidates", type=int, default=60)
    ap.add_argument("--require-2hop", action="store_true", default=True,
                    help="SF/PA 组合必须存在 known-relevant 2-hop support（默认开）")
    args = ap.parse_args()

    # ── 1. corpus（词源 = KnownRelevantKnowledge，纪律写死）──
    if args.corpus:
        corpus = json.load(open(args.corpus, encoding="utf-8"))
        if isinstance(corpus, dict):
            corpus = corpus.get("papers", corpus.get("labels", []))
        print(f"[corpus] {args.corpus}（n={len(corpus)}）")
    else:
        from search_engine.completeness.universe_builder import build_agent_seen_pool
        pool = build_agent_seen_pool()
        corpus = _load_oa_with_abstract(pool["found_relevant"])
        n_abs = sum(1 for p in corpus if p["abstract"])
        print(f"[corpus] KnownRelevantKnowledge: found_relevant={len(corpus)} "
              f"（abstract {n_abs}/{len(corpus)}）")

    # ── 2. term-paper incidence + relation graph（共现矩阵）──
    inc = extract_paper_tags(corpus)
    paper_tags = {pid: set(tags) for pid, tags in inc.items()}
    # family -> set(paper_ids)
    fam_papers = {}
    for pid, tags in inc.items():
        for fam, layer in tags.items():
            fam_papers.setdefault(fam, {"layer": layer, "papers": set()})["papers"].add(pid)
    # 共现权重 w(x,y) = 同一论文中共现的论文数
    co = {}
    fams = list(fam_papers)
    for i, x in enumerate(fams):
        for y in fams[i + 1:]:
            w = len(fam_papers[x]["papers"] & fam_papers[y]["papers"])
            if w > 0:
                co[(x, y)] = w
                co[(y, x)] = w

    def w(x, y):
        return co.get((x, y), 0)

    by_layer = {L: [(f, len(e["papers"])) for f, e in fam_papers.items()
                    if e["layer"] == L and len(e["papers"]) >= args.min_evidence]
                for L in LAYERS}
    # V3：STRUCTURE queryability gate——REJECT 词剔除；SF 候选记录 queryability
    by_layer["STRUCTURE"] = [(f, e) for f, e in by_layer["STRUCTURE"]
                             if STRUCTURE_QUERYABILITY.get(f, "DIRECT") != "REJECT"]
    print("[layers] " + " | ".join(f"{L}={len(by_layer[L])}" for L in LAYERS))
    print("[structure-qa] " + " | ".join(
        f"{f}={STRUCTURE_QUERYABILITY.get(f, 'DIRECT')}" for f, _ in by_layer["STRUCTURE"]))

    # ── 3. S4 queries（Novelty）──
    qcfg = json.load(open(args.s4_queries, encoding="utf-8"))
    s4_qs = [q["query_string"] for q in qcfg.get("query_actions", [])]

    # ── 4. SF / PA bridge：2-hop support 门槛 ──
    cands = []
    for btype, la, lb in BRIDGE_TYPES:
        # 中介层 = 除 la, lb 外的两层（2-hop 路径用）
        mid_layers = [L for L in LAYERS if L not in (la, lb)]
        for fa, ea in by_layer[la]:
            for fb, eb in by_layer[lb]:
                if fa == fb:
                    continue
                # BridgeSupport = Σ_m min[w(fa,m), w(m,fb)]，m ∈ 中介层
                support2 = 0
                for m in fams:
                    if fam_papers[m]["layer"] not in mid_layers:
                        continue
                    support2 += min(w(fa, m), w(m, fb))
                if args.require_2hop and support2 <= 0:
                    continue
                # PA：secondary property 必须带 shrinkage core；primary 直接 anchor
                if btype == "PA" and la == "PROPERTY":
                    if fa in SECONDARY_PROPERTY:
                        q_a, q_p = query_part(fb), query_part(fa)
                        query = f'TITLE-ABS-KEY({SHRINKAGE_CORE} AND {q_p} AND {q_a})'
                    else:
                        query = f'TITLE-ABS-KEY({query_part(fa)} AND {query_part(fb)})'
                elif btype == "SF":
                    # V3：SF 三元化 P_core × S × F——必须带 shrinkage relevance anchor
                    query = (f'TITLE-ABS-KEY({SHRINKAGE_CORE} AND '
                             f'{query_part(fa)} AND {query_part(fb)})')
                else:
                    query = f'TITLE-ABS-KEY({query_part(fa)} AND {query_part(fb)})'
                novel = novelty_ok([fa, fb], s4_qs)
                if not novel:
                    continue
                sa, sb = specificity(fa), specificity(fb)
                if sa == 0 or sb == 0:
                    continue
                score = (support2 + w(fa, fb)) * ea * eb * sa * sb
                cands.append({
                    "bridge_type": btype,
                    "family_a": fa, "layer_a": la, "evidence_a": ea,
                    "family_b": fb, "layer_b": lb, "evidence_b": eb,
                    "bridge_support_2hop": support2,
                    "direct_cooccurrence": w(fa, fb),
                    "structure_queryability": (STRUCTURE_QUERYABILITY.get(fa, "DIRECT")
                                               if la == "STRUCTURE" else None),
                    "novelty": novel, "specificity_a": round(sa, 2),
                    "specificity_b": round(sb, 2),
                    "bridge_score": round(score, 1),
                    "query_candidate": query,
                })

    cands.sort(key=lambda x: -x["bridge_score"])
    cands = cands[:args.max_candidates]

    from collections import Counter
    bt = Counter(c["bridge_type"] for c in cands)
    print(f"[bridge] 候选 = {len(cands)}（SF={bt['SF']}, PA={bt['PA']}）")
    print("\n=== top candidates ===")
    for c in cands[:20]:
        print(f"  [{c['bridge_type']}] score={c['bridge_score']:>6.1f} "
              f"2hop={c['bridge_support_2hop']:>2} {c['query_candidate'][:75]}")

    out = {
        "version": "s5_cross_layer_query_candidates_v3",
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "policy": "CrossLayerQueryExpansion V3：PA 保持 V2（PASS_TO_QUALITY_QA）；SF 三元化 "
                  "P_core×S×F + STRUCTURE queryability gate（DIRECT/ANCHORED_ONLY/REJECT）——"
                  "V2 教训：relation constraint alone is insufficient",
        "discipline": {
            "term_source": "KnownRelevantKnowledge（relevant papers title+abstract）——"
                           "R04 residual MUST NOT supply terms to formal S5 query generation",
            "relation_rule": "SF/PA 组合必须存在 known-relevant 2-hop support "
                             "（BridgeSupport=Σ_m min[w(s,m),w(m,f)]，m=中介层）；非全笛卡尔积",
            "anchor_rule": "primary PROPERTY 直接 anchor；secondary PROPERTY 必须带 "
                           "shrinkage core（SHRINKAGE_CORE）",
            "family_rule": "query 用 TermFamily aliases（corpus 真实词形），不用单 literal",
            "no_llm": True, "no_setcover": True,
        },
        "min_evidence": args.min_evidence,
        "n_candidates": len(cands),
        "bridge_type_counts": dict(bt),
        "candidates": cands,
        "actions": [{
            "action_id": f"{c['bridge_type']}_{i:03d}", "type": "QUERY_FAMILY",
            "query_string": c["query_candidate"],
            "bridge_type": c["bridge_type"],
            "family_a": c["family_a"], "family_b": c["family_b"],
            "bridge_support_2hop": c["bridge_support_2hop"],
            "bridge_score": c["bridge_score"],
        } for i, c in enumerate(cands, 1)],
        "next": "run_s3_query_pilot.py --new-basis s4 pilot → quality gate → S5 freeze → "
                "fresh R05 paired (Recall(S4|R05) vs Recall(S5|R05))",
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n[OK] {args.out}")


if __name__ == "__main__":
    main()
