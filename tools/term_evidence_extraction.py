#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/term_evidence_extraction.py — S4 Step 2：TERM evidence（2026-08-30 用户定）。

目标（用户定）：**不是找更多 term 生成 query**，而是判断 citation 死区 7 篇 + 部分
citation 可达仍漏的论文，是否存在稳定、可泛化的"undercovered language families"。

口径：**37 canonical papers 为分母**（W7110794929 = W4416134588 duplicate，跳过/并入）。

输出 per-term：
  term / term_family / term_type / miss_support / existing_search_coverage /
  community_specificity / novelty
类型槽：PROBLEM / METHOD / REACTION / MATERIAL / CONTEXT + **MEASUREMENT（新槽）**
  （linometer / µCT shrinkage vector / cuspal deflection / dilatometer / instrument
   compliance 等在 S3 被 METHOD/CONTEXT 混掉，S4 单列）

E_term(t) = (MissSupport, Undercoverage, Specificity)
  Eligible(t) = MissSupport≥2 ∧ Undercoverage>0 ∧ Specificity≥medium
              ∨ singleton ∧ Specificity=high（37 篇太少，niche entry 允许 singleton high）

重点 QA：7 个 citation-unreachable canonical misses 单独输出 term 证据。

用法：
  python tools/term_evidence_extraction.py [--out <path>]
"""
import argparse
import json
import os
import sys
from collections import Counter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "tools"))

from classify_residual_misses import _grams, _tokens, _norm_w  # noqa: E402
from build_s3_query_v2 import norm_term  # noqa: E402
from term_queryability_gate import GENERIC_BLACKLIST  # noqa: E402

T = os.path.join(BASE, "data", "exports", "terminology")
MISSES = os.path.join(T, "s4_residual_misses.json")
REACH = os.path.join(T, "s4_citation_reachability.json")
QUERY_SET = os.path.join(T, "s1_final_query_set.json")
DEFAULT_OUT = os.path.join(T, "s4_term_evidence.json")

# ── MEASUREMENT 新槽（S4 用户定：linometer/cuspal deflection/µCT 等）──
MEASUREMENT_PATTERNS = [
    "linometer", "dilatometer", "dilatometry", "pycnometer", "gas pycnometer",
    "interferomet", "fiber optic", "instrument compliance", "compliance",
    "cuspal deflection", "cuspal strain", "shrinkage vector", "shrinkage vectors",
    "micro computed tomography", "micro ct", "micro-focus x-ray", "x-ray ct",
    "x ray ct", "digital image correlation", "video imaging", "volumetric shrinkage analyzer",
    "bonded disc", "density method", "measuring microscope", "strain gage", "strain gauge",
    "displacement field", "strain field", "deflection", "elastic registration",
    "block matching", "vector spline", "swelling", "transducer", "profilomet",
]

# 复用的分类槽（S3 gate），MEASUREMENT 优先判定
from term_queryability_gate import CATEGORY_PATTERNS, classify_term  # noqa: E402

SLOT_PRIORITY = ["MEASUREMENT", "PROBLEM", "METHOD", "REACTION", "MATERIAL", "CONTEXT"]

# be/助动词 + 限定词碎片（"contraction pattern are" / "block matching following" 类）
FRAGMENT_TAIL = {"was", "were", "is", "are", "be", "been", "being", "can", "could",
                 "would", "should", "will", "may", "might", "must", "had", "has",
                 "have", "having", "do", "does", "did", "the", "a", "an", "this",
                 "these", "those", "that", "following", "along", "using", "during",
                 "under", "over", "within", "without"}


def _is_fragment(t: str) -> bool:
    """n>=2 时首尾 token 为功能词 → 截断碎片。"""
    toks = t.split()
    if len(toks) >= 2:
        if toks[0] in FRAGMENT_TAIL or toks[-1] in FRAGMENT_TAIL:
            return True
    return False


def classify_slot(t: str) -> str | None:
    if any(p in t for p in MEASUREMENT_PATTERNS):
        return "MEASUREMENT"
    return classify_term(t)


def load_existing_coverage() -> dict:
    """existing search coverage：S0-S3 全部 query strings + S3 222 seeds titles。"""
    qs = set()
    if os.path.exists(QUERY_SET):
        for q in json.load(open(QUERY_SET, encoding="utf-8"))["queries"]:
            qs.add(q["query_string"].lower())
    # S1/S2/S3 query 字符串（s3 query_actions 也纳入）
    cfg = os.path.join(T, "s3_final_config.json")
    if os.path.exists(cfg):
        for a in json.load(open(cfg, encoding="utf-8"))["query_actions"]:
            qs.add(a["query_string"].lower())
    # S3 citation seeds 的 title（222，R03 口径）——用 reachability 输出的 seeds 近似
    seeds_titles = set()
    from citation_reachability_audit import load_oa
    oa = load_oa()
    # 用 S3 citation records 的 seed wids（16）∪ reachability per_miss 的 path seeds
    seeds = set()
    if os.path.exists(REACH):
        for r in json.load(open(REACH, encoding="utf-8"))["per_miss"]:
            for p in r.get("paths", []):
                seeds.add(p["seed_wid"])
    for w in seeds:
        t = oa.get(w, {}).get("title")
        if t:
            seeds_titles.add(_norm(t))
    return {"query_strings": qs, "seed_titles": seeds_titles}


def _norm(s: str) -> str:
    return " ".join(s.lower().replace("-", " ").split())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--misses", default=MISSES)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    data = json.load(open(args.misses, encoding="utf-8"))
    misses = data["misses"]
    # 37 canonical：跳过 duplicate W7110794929（= W4416134588）
    canonical = [m for m in misses if m["wid"] != "W7110794929"]
    print(f"canonical residuals = {len(canonical)}（分母；raw {len(misses)}）")

    reach = {}
    if os.path.exists(REACH):
        for r in json.load(open(REACH, encoding="utf-8"))["per_miss"]:
            reach[r["miss_wid"]] = r
    cov = load_existing_coverage()

    # 1) per-miss n-gram → slot
    miss_terms = {}   # wid -> {term: slot}
    for m in canonical:
        text = " ".join(x for x in (m.get("abstract") or "", m.get("title") or "",
                                    m.get("oa_title") or "") if x)
        slots = {}
        for g in _grams(text):
            if g in GENERIC_BLACKLIST or _is_fragment(g):
                continue
            sl = classify_slot(g)
            if sl:
                slots[g] = sl
        miss_terms[m["wid"]] = slots

    # 2) per-term 聚合（family 归并）
    fam = {}      # family -> {members, slots:Counter, miss_wids}
    for wid, slots in miss_terms.items():
        for term, sl in slots.items():
            f = norm_term(term)
            e = fam.setdefault(f, {"members": set(), "slots": Counter(), "miss_wids": set()})
            e["members"].add(term)
            e["slots"][sl] += 1
            e["miss_wids"].add(wid)

    # 3) E_term：MissSupport / Undercoverage / Specificity
    terms_out = []
    for f, e in fam.items():
        ms = len(e["miss_wids"])
        slot = e["slots"].most_common(1)[0][0]
        # undercoverage：family 的任何 member 不在 S3 query strings 且不在 seed titles
        under = True
        for mem in e["members"]:
            nm = _norm(mem)
            if any(nm in q for q in cov["query_strings"]) or \
               nm in cov["seed_titles"]:
                under = False
                break
        # specificity：MEASUREMENT=very_high；PROBLEM=high；其余分类槽=medium
        if slot == "MEASUREMENT":
            spec = "very_high"
        elif slot == "PROBLEM":
            spec = "high"
        elif slot in ("METHOD", "REACTION", "MATERIAL", "CONTEXT"):
            spec = "medium"
        else:
            spec = "low"
        # Eligible（用户定）：sup>=2 ∧ undercovered ∧ spec>=medium；
        # singleton 仅 very_high（MEASUREMENT niche entry——37 篇太少，不卡死 support≥2）
        eligible = (ms >= 2 and under and spec in ("high", "very_high", "medium")) or \
                   (ms == 1 and spec == "very_high" and under)
        terms_out.append({
            "term_family": f, "members": sorted(e["members"]),
            "term_type": slot, "miss_support": ms,
            "undercovered": under,
            "specificity": spec,
            "novelty": "NEW" if under else "COVERED",
            "eligible": eligible,
            "miss_wids": sorted(e["miss_wids"]),
        })
    terms_out.sort(key=lambda x: (-x["miss_support"], x["specificity"]))

    n_el = sum(1 for t in terms_out if t["eligible"])
    print(f"提取 term families = {len(terms_out)} | eligible = {n_el}")
    print(f"\n{'family':<36}{'type':<12}{'sup':>4}{'under':>6}{'spec':<8}eligible")
    for t in terms_out:
        if t["eligible"] or t["miss_support"] >= 2:
            print(f"{t['term_family'][:36]:<36}{t['term_type']:<12}{t['miss_support']:>4}"
                  f"{str(t['undercovered']):>6}{t['specificity']:<8}{t['eligible']}")

    # 4) unreachable 7 的 QA（重点）
    un_reach = [m for m in canonical if reach.get(m["wid"], {}).get("distance") == 99]
    print(f"\n=== citation-unreachable canonical {len(un_reach)} 的 TERM 证据 ===")
    qa7 = []
    for m in un_reach:
        slots = miss_terms.get(m["wid"], {})
        by_slot = {}
        for term, sl in slots.items():
            by_slot.setdefault(sl, []).append(term)
        qa7.append({"wid": m["wid"], "title": (m.get("oa_title") or m.get("title"))[:60],
                    "terms_by_slot": {k: v[:6] for k, v in by_slot.items()}})
        print(f"  {m['wid']} | {(m.get('oa_title') or m.get('title'))[:56]}")
        for sl in SLOT_PRIORITY:
            if sl in by_slot:
                print(f"      [{sl}] {', '.join(by_slot[sl][:6])}")

    out = {
        "version": "s4_term_evidence_v1",
        "frozen_at": "2026-08-30",
        "development_source": "AUDIT_R03",
        "denominator": "37 canonical papers（raw 38 - W7110794929 duplicate）",
        "target": "undercovered language families（非 query 生成）",
        "eligible_rule": "MissSupport>=2 ∧ Undercovered ∧ Spec>=medium ∨ singleton ∧ Spec=high",
        "slot_priority": SLOT_PRIORITY,
        "n_terms": len(terms_out), "n_eligible": n_el,
        "terms": terms_out,
        "unreachable_7_qa": qa7,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n[OK] written: {args.out}")


if __name__ == "__main__":
    main()
