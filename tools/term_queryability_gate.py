"""tools/term_queryability_gate.py — S3 Stage B：TERM discriminative Queryability gate（2026-08-29 用户定）。

问题：classify_residual_misses 的 MissSupport≥2 → 699 词太宽（generic phrase 污染）。
证据强度 ≠ query 质量（同 R01 学到的教训）。

三层 gate（用户定）：
  L1 generic blacklist：直接排掉 glass transition / thermal stability / mechanical
     properties / room temperature / commercially available / significantly higher /
     statistical analysis / magnified image / shows promise / mechanical stability 等
     （即使 MissSupport=20 也不能进 query）
  L2 必须与 shrinkage discovery 语义关联（进分类槽才算）
  L3 强制分类槽 PROBLEM / METHOD / REACTION / MATERIAL / CONTEXT
     优先级：PROBLEM > METHOD > REACTION > MATERIAL > CONTEXT

输出：
  s3_term_gate.json
    discriminative_terms：{category: [term...]}（通过三层 gate）
    per_miss：{wid, repairable_terms{category:[]}, labels}
    四格表（--citation-audit 提供 Stage A 的 citation-repairable）：
      citation-only / term-only / both / neither

用法：
  python tools/term_queryability_gate.py --misses <s3_residual_misses.json>
      [--citation-audit <s3_citation_audit.json>] [--out <path>]
"""
import argparse
import json
import os
import re
import sys
from collections import Counter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "tools"))

from classify_residual_misses import _grams, CORE_TERMS, STOPWORDS  # noqa: E402

DEFAULT_MISSES = os.path.join(BASE, "data", "exports", "terminology",
                              "s3_residual_misses.json")
DEFAULT_OUT = os.path.join(BASE, "data", "exports", "terminology",
                           "s3_term_gate.json")

# L1 generic blacklist（用户 10 词 + 扩充）——即使高 MissSupport 也不进 query
GENERIC_BLACKLIST = {
    "glass transition", "glass transition temperature", "transition temperature",
    "thermal stability", "mechanical properties", "mechanical stability",
    "room temperature", "commercially available", "significantly higher",
    "significantly lower", "statistical analysis", "magnified image",
    "shows promise", "promising", "excellent", "superior", "good agreement",
    "good correlation", "good mechanical", "high mechanical",
    "well known", "well established", "in situ", "in vivo", "in vitro",
    "scanning electron", "scanning electron microscopy", "energy dispersive",
    "x ray", "x ray diffraction", "differential scanning", "thermogravimetric",
    "fourier transform", "infrared spectroscopy", "atomic force", "transmission electron",
    "field emission", "weight loss", "weight gain", "mass loss", "particle size",
    "average particle", "mean value", "standard deviation", "one way anova",
    "two way anova", "p value", "significance", "significant difference",
    "control group", "experimental group", "specimens", "samples were",
    "each specimen", "all specimens", "performed", "carried out", "used as",
    "as received", "as prepared", "as shown", "as compared", "compared with",
    "compared to", "consistent with", "in agreement", "in accordance",
    "can be attributed", "may be due", "is attributed", "was found",
    "it was observed", "the results", "these results", "our results",
    "predicting", "indicative", "simulation", "simulated", "self writing",
    "self-writing", "overview", "comparative", "comparison of", "investigation of",
}

# L3 分类槽关键词（L2 语义关联通过它实现——命中任一类即"与 shrinkage discovery 关联"）
CATEGORY_PATTERNS = {
    "PROBLEM": [
        "contraction", "volumetric change", "volume change", "dimensional change",
        "dimensional stability", "dimensional accuracy", "deformation", "distortion",
        "warpage", "stress relaxation", "internal stress", "shrinkage stress",
        "polymerization stress", "curing stress", "contraction stress", "stress development",
        "post cure shrinkage", "post-cure shrinkage", "expansion", "postcure",
        "crack", "cracking", "delamination", "debonding", "marginal gap",
        "marginal adaptation", "gap formation", "microleakage", "crazing",
    ],
    "METHOD": [
        "pycnometer", "gas pycnometer", "dilatometer", "dilatometry",
        "interferometric", "interferometry", "fiber optic", "compliance",
        "instrument compliance", "calorimetry", "rheometry", "rheometer",
        "profilometer", "profilometry", "strain gauge", "strain gage",
        "transducer", "video imaging", "shadowgraph", "moire", "speckle",
        "digital image correlation", "volumetric measurement", "measuring device",
        "measurement method", "test method", "test setup", "experimental setup",
        "tensile test", "flexural test", "three point bending", "nanoindentation",
        "dynamic mechanical", "thermomechanical", "photoelastic",
    ],
    "REACTION": [
        "chain transfer", "addition fragmentation", "fragmentation",
        "ring opening", "ring-opening", "cationic", "anionic", "radical",
        "thiol ene", "thiol-ene", "click chemistry", "photoinitiator",
        "initiation", "kinetics", "polymerization kinetics", "conversion",
        "double bond", "network formation", "crosslink", "crosslinking",
        "crosslinked", "oligomer", "oligomers", "copolymer", "copolymerization",
        "bulk polymerization", "solution polymerization", "emulsion polymerization",
        "atom transfer", "reversible addition", "living polymerization",
    ],
    "MATERIAL": [
        "spiroorthocarbonate", "orthocarbonate", "spiro", "oxetane", "oxirane",
        "epoxy", "epoxide", "urethane", "udma", "bis-gma", "bisgma", "tegdma",
        "tegma", "dimethacrylate", "methacrylic", "vinyl ether", "vinyl ester",
        "acrylate monomer", "filler", "nanofiller", "nanocomposite", "nanoparticle",
        "silica", "zirconia", "alumina", "glass fiber", "carbon nanotube",
        "fumed silica", "inorganic filler", "organic filler", "composite material",
    ],
    "CONTEXT": [
        "dental", "restorative", "restoration", "adhesive", "dentine", "dentin",
        "enamel", "holograph", "holographic", "recording", "photonic", "optical",
        "lithograph", "stereolithograph", "3d printing", "three dimensional printing",
        "additive manufacturing", "two photon", "two-photon", "laser writing",
        "ceramic", "coating", "membrane", "packaging", "microelectronic",
        "microelectronic", "optoelectronic", "photoresist", "nanoimprint",
    ],
}
CATEGORY_PRIORITY = ["PROBLEM", "METHOD", "REACTION", "MATERIAL", "CONTEXT"]


def classify_term(t: str) -> str | None:
    """L2+L3：命中任一分类槽 → 该类别；否则 None（不进 repair candidate）。"""
    for cat in CATEGORY_PRIORITY:
        if any(p in t for p in CATEGORY_PATTERNS[cat]):
            return cat
    return None


def main():
    ap = argparse.ArgumentParser(description="S3 Stage B TERM discriminative gate")
    ap.add_argument("--misses", default=DEFAULT_MISSES)
    ap.add_argument("--citation-audit", default="",
                    help="Stage A 输出（可选）：四格表需要 citation-repairable 判定")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--min-support", type=int, default=2)
    args = ap.parse_args()

    if not os.path.exists(args.misses):
        print(f"[FATAL] 缺 {args.misses}")
        sys.exit(2)
    data = json.load(open(args.misses, encoding="utf-8"))
    misses = data["misses"]

    # 提取候选词（复用 classify 的 _grams，MissSupport≥min_support）
    miss_grams = {}
    support = Counter()
    for r in misses:
        text = " ".join([r.get("oa_title") or r.get("title") or "",
                         r.get("abstract") or ""])
        gs = set(_grams(text))
        miss_grams[r["wid"]] = gs
        for g in gs:
            support[g] += 1
    cand = {g for g, c in support.items() if c >= args.min_support}

    # L1：generic blacklist 排除
    after_l1 = {g for g in cand if g not in GENERIC_BLACKLIST}
    # L2+L3：分类槽
    per_cat = {c: [] for c in CATEGORY_PRIORITY}
    term_cat = {}
    for g in sorted(after_l1):
        cat = classify_term(g)
        if cat:
            per_cat[cat].append(g)
            term_cat[g] = cat

    # per-miss repairable terms
    per_miss = []
    for r in misses:
        gs = miss_grams.get(r["wid"], set())
        rep = {c: [] for c in CATEGORY_PRIORITY}
        for g in gs & set(term_cat):
            rep[term_cat[g]].append(g)
        per_miss.append({
            "wid": r["wid"],
            "title": r.get("oa_title") or r.get("title"),
            "repairable_terms": {c: sorted(v) for c, v in rep.items() if v},
            "n_repairable_terms": sum(len(v) for v in rep.values()),
            "has_term_repair": bool(any(rep.values())),
        })

    # 四格表（Stage A 集成）
    cit_rep = set()
    if args.citation_audit and os.path.exists(args.citation_audit):
        ca = json.load(open(args.citation_audit, encoding="utf-8"))
        cit_rep = {p["wid"] for p in ca.get("per_miss", [])
                   if p.get("citation_positive_high_conf")}
    table = {"citation_only": 0, "term_only": 0, "both": 0, "neither": 0}
    for p in per_miss:
        c = p["wid"] in cit_rep
        t = p["has_term_repair"]
        key = ("both" if c and t else "citation_only" if c
               else "term_only" if t else "neither")
        table[key] += 1

    out = {
        "version": "s3_term_gate_v1", "frozen_at": "2026-08-29",
        "definition": "TERM 三层 gate：L1 generic blacklist → L2 语义关联 → "
                      "L3 分类槽（PROBLEM/METHOD/REACTION/MATERIAL/CONTEXT，"
                      "优先级 PROBLEM>METHOD>REACTION>MATERIAL>CONTEXT）",
        "stats": {
            "raw_candidates_ms2": len(cand),
            "after_l1_blacklist": len(after_l1),
            "after_l2l3_semantic_gate": sum(len(v) for v in per_cat.values()),
            "n_miss_with_term_repair": sum(1 for p in per_miss if p["has_term_repair"]),
        },
        "discriminative_terms": per_cat,
        "per_miss": per_miss,
        "four_way_table": table,
        "note": "四格表需要 Stage A（--citation-audit）；term-only/both 中按类别"
                "聚类生成 query family，不再按单篇生成",
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print(f"=== TERM gate 统计 ===")
    print(f"  原始候选（MissSupport≥2）      = {len(cand)}")
    print(f"  L1 blacklist 后               = {len(after_l1)}")
    print(f"  L2+L3 语义 gate 后             = {sum(len(v) for v in per_cat.values())}")
    print(f"  有 term-repair 的 miss         = {sum(1 for p in per_miss if p['has_term_repair'])}/{len(per_miss)}")
    for c in CATEGORY_PRIORITY:
        print(f"    {c:<10} {len(per_cat[c]):>3} 词")
    print(f"\n=== 四格表（citation 用 Stage A high-conf）===")
    for k, v in table.items():
        print(f"  {k:<15} {v}")
    print(f"\n[OK] {args.out}")


if __name__ == "__main__":
    main()
