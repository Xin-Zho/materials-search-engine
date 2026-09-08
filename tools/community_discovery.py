#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/community_discovery.py — S4 Step 3：COMMUNITY discovery（2026-08-30 用户定）。

候选社区（不预设成立）：dental-measurement / holography-recording / 3DP-lithography。
统一三证据检验 E(C) = (Cohesion, Vocabulary, Undercoverage)：

  1. Cohesion：citation_density（成员互引） + bibliographic_coupling（共享参考文献 Jaccard）
     + semantic_similarity（title token Jaccard）——至少一种强或两种中等
  2. Vocabulary：Enrichment(t,C) = P(t|C)/P(t|¬C)（平滑）；MEASUREMENT/PROBLEM/METHOD/CONTEXT
     富集词（enrichment≥2 且 in_C≥2）为 shared_vocab
  3. Undercoverage：1 − #(community entry terms 在 S3 query/seeds/term-family 中)/#total
     ——S3 query strings + S3 citation seeds titles

verdict 三级（用户定）：
  STRONG   = 三项全部通过（cohesion≥MED ∧ vocab≥MED ∧ undercoverage≥MED，且 ≥2 项 strong）
  MODERATE = 三项中 ≥2 项 ≥MED
  WEAK     = 仅人工主题相似，无系统性证据

★ 纪律（用户定）：community 成立与否**不看"能追回多少 R03 miss"**——那属于 repair pilot
阶段；本步骤只回答"是否存在客观、内部连贯且 Search 系统性低覆盖的研究子群"。

输出：s4_community_discovery.json
  per-community {community_id, members, cohesion{citation_density, bibliographic_coupling,
  semantic_similarity}, shared_vocab[], undercovered_terms[], undercovered_seeds[], verdict}

用法：
  python tools/community_discovery.py [--out <path>]
"""
import argparse
import json
import math
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "tools"))

from citation_reachability_audit import load_oa  # noqa: E402
from term_evidence_extraction import (  # noqa: E402
    classify_slot, _is_fragment, MEASUREMENT_PATTERNS, SLOT_PRIORITY,
)
from classify_residual_misses import _grams, _tokens  # noqa: E402
from term_queryability_gate import GENERIC_BLACKLIST  # noqa: E402
from build_s3_query_v2 import norm_term  # noqa: E402

T = os.path.join(BASE, "data", "exports", "terminology")
MISSES = os.path.join(T, "s4_residual_misses.json")
REACH = os.path.join(T, "s4_citation_reachability.json")
QUERY_SET = os.path.join(T, "s1_final_query_set.json")
DEFAULT_OUT = os.path.join(T, "s4_community_discovery.json")

# 社区定义关键词（启发式成员分配；不预设 verdict）
COMMUNITY_DEF = {
    "dental-measurement": {
        "kw": ["dental", "tooth", "teeth", "restoration", "restorative", "dentin",
               "enamel", "cuspal", "composite resin", "marginal", "occlusal",
               "bulk fill", "bulk-fill"],
        "measurement_required": True,
    },
    "holography-recording": {
        "kw": ["holograph", "grating", "recording", "diffraction", "photonic",
               "data storage", "relief", "refractive"],
        "measurement_required": False,
    },
    "3dp-lithography": {
        "kw": ["stereolithograph", "3d print", "three dimensional printing",
               "vat", "photoresist", "lithograph", "additive manufacturing",
               "laser draw", "laser writing", "microstructur"],
        "measurement_required": False,
    },
}


def _norm(s: str) -> str:
    return " ".join(s.lower().replace("-", " ").split())


def extract_terms(m: dict) -> dict:
    """per-miss 的 eligible 槽词（复用 term_evidence 逻辑，不依赖其落盘）。"""
    text = " ".join(x for x in (m.get("abstract") or "", m.get("title") or "",
                                m.get("oa_title") or "") if x)
    out = {}
    for g in _grams(text):
        if g in GENERIC_BLACKLIST or _is_fragment(g):
            continue
        sl = classify_slot(g)
        if sl:
            out[g] = sl
    return out


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--misses", default=MISSES)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    data = json.load(open(args.misses, encoding="utf-8"))
    misses = [m for m in data["misses"] if m["wid"] != "W7110794929"]
    oa = load_oa()
    wids = [m["wid"] for m in misses]
    wid_set = set(wids)

    # 现有覆盖（S3 query strings + S3 seeds titles）
    qs = set()
    if os.path.exists(QUERY_SET):
        for q in json.load(open(QUERY_SET, encoding="utf-8"))["queries"]:
            qs.add(q["query_string"].lower())
    cfg = os.path.join(T, "s3_final_config.json")
    if os.path.exists(cfg):
        for a in json.load(open(cfg, encoding="utf-8"))["query_actions"]:
            qs.add(a["query_string"].lower())
    seed_titles = set()
    if os.path.exists(REACH):
        for r in json.load(open(REACH, encoding="utf-8"))["per_miss"]:
            for p in r.get("paths", []):
                t = oa.get(p["seed_wid"], {}).get("title")
                if t:
                    seed_titles.add(_norm(t))

    # per-miss terms + refs + title tokens
    miss_terms = {m["wid"]: extract_terms(m) for m in misses}
    miss_refs = {w: set(oa.get(w, {}).get("referenced_works", [])) for w in wids}
    miss_tok = {m["wid"]: set(_tokens(m.get("oa_title") or m.get("title") or ""))
                for m in misses}

    # ── 成员分配（启发式关键词；社区互斥优先 dental-measurement 判测量）──
    members = {}
    for m in misses:
        text = (m.get("oa_title") or m.get("title") or "").lower()
        for cid, cf in COMMUNITY_DEF.items():
            if any(k in text for k in cf["kw"]):
                if cf["measurement_required"]:
                    # dental 社区需有 MEASUREMENT 词才算 measurement 子群
                    if any(p in text for p in MEASUREMENT_PATTERNS):
                        members.setdefault(cid, []).append(m["wid"])
                    else:
                        members.setdefault(cid, []).append(m["wid"])  # 仍计入 dental 社区
                else:
                    members.setdefault(cid, []).append(m["wid"])
                break  # 每个 miss 归第一个命中社区

    print("=" * 78)
    print("S4 Step 3: COMMUNITY discovery（三证据检验，不看 recovery）")
    print("=" * 78)
    table = []
    for cid, ms in members.items():
        mem = sorted(set(ms))
        n = len(mem)
        if n == 0:
            continue
        # 1) cohesion
        edges = 0
        coupling = []
        sim = []
        for i in range(n):
            for j in range(i + 1, n):
                a, b = mem[i], mem[j]
                if miss_refs[a] & miss_refs[b] or (a in miss_refs[b]) or (b in miss_refs[a]):
                    edges += 1
                coupling.append(jaccard(miss_refs[a], miss_refs[b]))
                sim.append(jaccard(miss_tok[a], miss_tok[b]))
        denom = n * (n - 1) / 2 if n > 1 else 1
        cit_dens = edges / denom
        bco = (sum(coupling) / len(coupling)) if coupling else 0.0
        ssim = (sum(sim) / len(sim)) if sim else 0.0
        coh = {"citation_density": round(cit_dens, 3),
               "bibliographic_coupling": round(bco, 3),
               "semantic_similarity": round(ssim, 3)}
        coh_strong = cit_dens >= 0.25 or bco >= 0.35 or ssim >= 0.45
        coh_med = cit_dens >= 0.10 or bco >= 0.15 or ssim >= 0.25
        coh_verdict = "STRONG" if coh_strong else ("MED" if coh_med else "WEAK")

        # 2) vocabulary enrichment（对照 = 37 中非社区成员）
        others = [w for w in wids if w not in mem]
        def count_in(group, term):
            return sum(1 for w in group if term in miss_terms.get(w, {}))
        def enrichment(t):
            pc = (count_in(mem, t) + 0.5) / (n + 1)
            po = (count_in(others, t) + 0.5) / (len(others) + 1)
            return pc / po
        all_terms = {}
        for w in mem:
            for t, sl in miss_terms.get(w, {}).items():
                if sl in ("MEASUREMENT", "PROBLEM", "METHOD", "CONTEXT"):
                    all_terms.setdefault(norm_term(t), set()).add(w)
        shared = []
        for f, ws in all_terms.items():
            e = enrichment(f)
            if e >= 2.0 and len(ws) >= 2:
                shared.append((f, round(e, 1), len(ws)))
        shared.sort(key=lambda x: -x[1])
        vocab_verdict = "STRONG" if len(shared) >= 3 else ("MED" if shared else "WEAK")

        # 3) undercoverage：社区入口词（shared_vocab + MEASUREMENT 词）在 S3 query/seeds 的缺失率
        #    判定（token 集包含，非子串）：f_tokens ⊆ query_tokens 或 ⊆ 某 seed title tokens
        qs_toks = [set(q.split()) for q in qs]
        seed_toks = [set(t.split()) for t in seed_titles]
        entry = list(dict.fromkeys(
            [f for f, _, _ in shared] +
            [norm_term(t) for w in mem for t, sl in miss_terms.get(w, {}).items()
             if sl == "MEASUREMENT"]))
        covered = 0
        under_terms = []
        for f in entry:
            nf_toks = set(_norm(f).split())
            hit = any(nf_toks and nf_toks <= qt for qt in qs_toks) or \
                  any(nf_toks and nf_toks <= st for st in seed_toks)
            if hit:
                covered += 1
            else:
                under_terms.append(f)
        uc = 1 - (covered / len(entry)) if entry else None
        under_verdict = ("NA" if uc is None else
                         "STRONG" if uc >= 0.6 else ("MED" if uc >= 0.3 else "WEAK"))

        # verdict（用户规则）；NA 项不计入通过
        scores = {"cohesion": coh_verdict, "vocab": vocab_verdict,
                  "undercoverage": under_verdict}
        n_med = sum(1 for v in scores.values() if v in ("STRONG", "MED"))
        n_strong = sum(1 for v in scores.values() if v == "STRONG")
        if n_med == 3 and n_strong >= 2:
            verdict = "STRONG"
        elif n_med >= 2:
            verdict = "MODERATE"
        else:
            verdict = "WEAK"

        table.append({
            "community_id": f"C_{cid}", "members": mem, "size": n,
            "cohesion": coh, "cohesion_verdict": coh_verdict,
            "shared_vocab": [f for f, _, _ in shared[:12]],
            "vocab_verdict": vocab_verdict,
            "undercoverage": round(uc, 3) if uc is not None else None,
            "undercovered_terms": under_terms[:15],
            "undercoverage_verdict": under_verdict,
            "verdict": verdict,
        })
        print(f"\n[{cid}] size={n} verdict={verdict}")
        print(f"  cohesion: cit_dens={coh['citation_density']} "
              f"coupling={coh['bibliographic_coupling']} "
              f"semantic={coh['semantic_similarity']} → {coh_verdict}")
        print(f"  shared_vocab（enrichment≥2, inC≥2）: {[f for f, _, _ in shared[:8]]} → {vocab_verdict}")
        ucs = f"{uc:.1%}" if uc is not None else "NA"
        print(f"  undercoverage = {ucs}（入口 {len(entry)}，未覆盖 {len(under_terms)}）→ {under_verdict}")

    print("\n=== 社区证据表 ===")
    print(f"{'community':<24}{'size':>5}{'cohesion':>10}{'vocab':>8}{'under':>8}{'verdict':>10}")
    for t in table:
        print(f"{t['community_id']:<24}{t['size']:>5}{t['cohesion_verdict']:>10}"
              f"{t['vocab_verdict']:>8}{t['undercoverage_verdict']:>8}{t['verdict']:>10}")

    out = {
        "version": "s4_community_discovery_v1",
        "frozen_at": "2026-08-30",
        "development_source": "AUDIT_R03",
        "denominator": "37 canonical",
        "evidence_rule": "E(C)=(Cohesion[cit_dens/coupling/semantic], Vocabulary[enrichment>=2 & inC>=2], "
                         "Undercoverage[1 - entry covered in S3 query/seeds])；"
                         "STRONG=三全过且≥2 strong；MODERATE=≥2 项 MED；WEAK=仅主题相似",
        "discipline": "community 成立不看 R03 recovery（防过拟合）；recovery 属 repair pilot 阶段",
        "communities": table,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n[OK] written: {args.out}")


if __name__ == "__main__":
    main()
