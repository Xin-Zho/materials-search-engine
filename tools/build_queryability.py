"""tools/build_queryability.py — v3.0 Queryability v1（2026-08-29 用户设计定稿）。

两层结构（用户定，不搞加权 score）：
  L1 Queryability Gate：Q(t) ∈ {DIRECT, ANCHOR, REJECT}（确定性过滤，非连续 score）
  L2 Conditional Coverage：greedy 按 ΔCoverage(t|S) 选（Cost=1，等价 set cover；
      pilot 后再把 Cost 换成 query cost / candidate explosion）

输入：data/exports/terminology/repair_term_candidates.json（113 eligible）
输出：data/exports/terminology/queryability_gate.json
      data/exports/terminology/queryability_greedy.json
      data/exports/terminology/long_tail_repair.json
      （控制台打印两条曲线对比：support_order vs greedy）

DIRECT 判定（复现用户全部例子）：
  term 含 {shrinkage, contraction, shrink} 或含 token "photopolymerization"
  → vat/cationic photopolymerization、volume shrinkage、shrinkage stress、low shrinkage DIRECT
REJECT（确定性，可审计，每条带 reason）：
  FRAGMENT：含 "via" / 首 token 功能词 / 尾 token 功能词(含 high) / 单 token 长度<=3
  GENERIC ：泛词黑名单（resin/composite/mechanical properties/...——证据强≠query 好，
            不因 MissSupport 高救泛词）
ANCHOR query template（anchor 由 term_type 决定，用户例子映射）：
  MATERIAL/REACTION/CONTEXT → TITLE-ABS-KEY(shrinkage AND "term")
  METHOD/PROBLEM/UNKNOWN    → TITLE-ABS-KEY("polymerization shrinkage" AND "term")
DIRECT query：TITLE-ABS-KEY("term")
"""
import argparse
import json
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_IN = os.path.join(BASE, "data", "exports", "terminology",
                          "repair_term_candidates.json")
OUT_DIR = os.path.join(BASE, "data", "exports", "terminology")

MISS_TOTAL = 95

# ── L1 Gate 规则 ──────────────────────────────────────────
# 泛词黑名单（GENERIC；第一版保守清单，可审计调整；不因 MissSupport 高而放行）
GENERIC = {
    "resin", "composite", "polymer", "monomer", "material", "materials",
    "curing", "cure", "fabrication", "performance", "strength", "temperature",
    "pressure", "structure", "application", "preparation", "evaluation",
    "measurement", "characterization", "determination", "simulation", "adhesion",
    "mechanical properties", "high performance", "high strength",
    "three dimensional", "additive manufacturing", "dental applications",
    "dental restorative", "real time", "distortion", "deformation", "resolution",
    "stability", "behavior", "study", "analysis", "effect", "properties",
    "polymerization",     # ≈ anchor 问题域同义，检索必爆（v2.1 intensity/cavity 教训）
    "photopolymerization",  # 同上：太泛，不能作扩展词（DIRECT 应由具体变体承担）
}

LEAD_FUNC = {"with", "of", "and", "the", "for", "in", "on", "at", "to", "from",
             "via", "based", "containing", "using"}
TAIL_FUNC = {"via", "with", "of", "and", "the", "for", "in", "on", "at", "to",
             "from", "using", "high"}

DIRECT_SEM = {"shrinkage", "contraction", "shrink"}     # 直接检索信号
DIRECT_POLY = "photopolymerization"                      # vat/cationic/... 全家族

ANCHOR_BY_TYPE = {   # 用户例子映射（2026-08-29）
    "MATERIAL": "shrinkage",
    "REACTION": "shrinkage",
    "CONTEXT": "shrinkage",
    "METHOD": '"polymerization shrinkage"',
    "PROBLEM": '"polymerization shrinkage"',
    "UNKNOWN": '"polymerization shrinkage"',
}


def gate(term: str, term_types: list[str]) -> dict:
    """返回 {q: DIRECT|ANCHOR|REJECT, reason: str|None, query: str}。"""
    toks = term.split()
    # FRAGMENT 规则
    if "via" in toks:
        return {"q": "REJECT", "reason": "FRAGMENT:含 via（截断证据）"}
    if toks and toks[0] in LEAD_FUNC:
        return {"q": "REJECT", "reason": f"FRAGMENT:首 token 功能词 {toks[0]}"}
    if toks and toks[-1] in TAIL_FUNC:
        return {"q": "REJECT", "reason": f"FRAGMENT:尾 token 功能词 {toks[-1]}"}
    if len(toks) == 1 and len(term) <= 3:
        return {"q": "REJECT", "reason": f"FRAGMENT:单 token 长度<=3（{term}）"}
    # GENERIC 规则
    if term in GENERIC:
        return {"q": "REJECT", "reason": "GENERIC:泛词（证据强≠query 好）"}
    # DIRECT vs ANCHOR
    if any(s in toks for s in DIRECT_SEM) or DIRECT_POLY in toks:
        return {"q": "DIRECT",
                "query": f'TITLE-ABS-KEY("{term}")'}
    anchor = ANCHOR_BY_TYPE.get(term_types[0] if term_types else "UNKNOWN",
                                '"polymerization shrinkage"')
    return {"q": "ANCHOR",
            "query": f'TITLE-ABS-KEY({anchor} AND "{term}")'}


def greedy_coverage(pool: list[dict]) -> tuple[list[dict], set[str]]:
    """greedy：每轮选 ΔCoverage(t|S) 最大（tie: miss_support DESC）。
    Cost=1（用户第一版；pilot 后换真实 cost）。"""
    covered: set[str] = set()
    remaining = [dict(f) for f in pool]
    selected: list[dict] = []
    while remaining:
        best, best_gain = None, 0
        for f in remaining:
            gain = len(set(f["miss_ids"]) - covered)
            if gain > best_gain:
                best, best_gain = f, gain
        if best is None or best_gain <= 0:
            break
        selected.append(best)
        covered |= set(best["miss_ids"])
        remaining.remove(best)
        best["marginal_gain"] = best_gain
    return selected, covered


def _all_95_miss_ids(input_path: str) -> set[str]:
    """95 篇 miss 全集（从 audit_miss_diagnostics.json 的 misses 取）。"""
    diag = os.path.join(BASE, "data", "exports", "audit_miss_diagnostics.json")
    d = json.load(open(diag, encoding="utf-8"))
    return {m["paper_id"] for m in d["misses"]}


def main():
    ap = argparse.ArgumentParser(description="Queryability v1（Gate + Conditional Coverage）")
    ap.add_argument("--input", default=DEFAULT_IN)
    ap.add_argument("--out-dir", default=OUT_DIR)
    args = ap.parse_args()

    data = json.load(open(args.input, encoding="utf-8"))
    cands = data["candidates"]

    # ── L1 Gate ──
    gated = []
    for f in cands:
        g = gate(f["canonical_term"], f["term_types"])
        gated.append({**f, "queryability": g})
    n_dir = sum(1 for f in gated if f["queryability"]["q"] == "DIRECT")
    n_anc = sum(1 for f in gated if f["queryability"]["q"] == "ANCHOR")
    n_rej = sum(1 for f in gated if f["queryability"]["q"] == "REJECT")
    print(f"Gate: DIRECT={n_dir} ANCHOR={n_anc} REJECT={n_rej}（共 {len(gated)}）")
    from collections import Counter
    print("  REJECT reasons:", dict(Counter(f["queryability"]["reason"] for f in gated
                                            if f["queryability"]["q"] == "REJECT")))

    # ── L2 greedy（通过 Gate 的池）──
    pool = [f for f in gated if f["queryability"]["q"] in ("DIRECT", "ANCHOR")]
    selected, covered = greedy_coverage(pool)
    uncovered = MISS_TOTAL - len(covered)
    print(f"\nGreedy 选择: {len(selected)} 条 | 覆盖 {len(covered)}/95 | "
          f"uncovered {uncovered}")

    # ── 两条曲线对比 ──
    ks = [1, 2, 3, 5, 10, 15, 20, 30, 50]
    print(f"\n{'k':>4} {'support_order':>13} {'greedy':>7} {'marginal':>8}  selected_term")
    # support_order：按 miss_support 排序（Gate 通过池内）
    sup_ordered = sorted(pool, key=lambda f: -f["miss_support"])
    sup_covered = set()
    greedy_cov = set()
    sup_map = {}
    for i, f in enumerate(sup_ordered, 1):
        sup_covered |= set(f["miss_ids"])
        sup_map[i] = len(sup_covered)
    gmap = {}
    gsel = {}
    for i, f in enumerate(selected, 1):
        greedy_cov |= set(f["miss_ids"])
        gmap[i] = len(greedy_cov)
        gsel[i] = f["canonical_term"]
    for k in ks:
        sc = sup_map.get(k, sup_map[max(sup_map)] if sup_map else 0)
        gc = gmap.get(k, gmap[max(gmap)] if gmap else 0)
        mg = selected[k - 1]["marginal_gain"] if k <= len(selected) else 0
        term = gsel.get(k, "")
        print(f"{k:>4} {sc:>5}/95={sc/MISS_TOTAL*100:5.1f}% {gc:>3}/95={gc/MISS_TOTAL*100:5.1f}% "
              f"{mg:>8}  {term[:40]}")

    # ── LONG_TAIL（完整两类）──
    all_miss = set()
    for f in cands:
        all_miss |= set(f["miss_ids"])
    # 95 全集 = 候选池 miss 并集 ∪ 无候选 miss（term 全 support=1 或无 eligible）
    total_miss_ids = all_miss | set(long_tail if False else [])
    no_candidate = [m for m in _all_95_miss_ids(args.input) if m not in all_miss]
    pool_uncovered = sorted(all_miss - covered)
    print(f"\nLONG_TAIL_REPAIR: {len(no_candidate) + len(pool_uncovered)} 篇"
          f"（无候选 {len(no_candidate)} + 候选未覆盖 {len(pool_uncovered)}）")
    print("  支路（用户定）：1. citation enrichment  2. singleton-term rescue")
    print(f"    无候选（{len(no_candidate)}）: {no_candidate}")
    print(f"    候选未覆盖（{len(pool_uncovered)}）: {pool_uncovered}")

    # ── 输出 ──
    os.makedirs(args.out_dir, exist_ok=True)
    with open(os.path.join(args.out_dir, "queryability_gate.json"), "w",
              encoding="utf-8") as f:
        json.dump({"version": "queryability_v1", "gate": "DIRECT/ANCHOR/REJECT",
                   "note": "确定性规则：FRAGMENT(含via/首尾功能词/单token<=3) + "
                           "GENERIC(泛词黑名单)；DIRECT=含shrinkage/contraction/shrink "
                           "或 photopolymerization token",
                   "candidates": gated}, f, ensure_ascii=False, indent=1)
    with open(os.path.join(args.out_dir, "queryability_greedy.json"), "w",
              encoding="utf-8") as f:
        json.dump({"version": "queryability_v1", "utility": "DeltaCoverage/Cost, Cost=1",
                   "coverage": {"covered": len(covered), "total": MISS_TOTAL,
                                "uncovered": len(pool_uncovered) + len(no_candidate),
                                "no_candidate": len(no_candidate),
                                "pool_uncovered": len(pool_uncovered)},
                   "selected": [{k: f[k] for k in ("family_id", "canonical_term",
                                                   "miss_support", "marginal_gain",
                                                   "queryability")}
                                for f in selected]}, f, ensure_ascii=False, indent=1)
    with open(os.path.join(args.out_dir, "long_tail_repair.json"), "w",
              encoding="utf-8") as f:
        json.dump({"no_candidate_miss_ids": no_candidate,
                   "pool_uncovered_miss_ids": pool_uncovered,
                   "repair_branches": ["citation enrichment", "singleton-term rescue"],
                   "note": "不为此破坏 Gate（不强行 95/95）"}, f,
                  ensure_ascii=False, indent=1)
    print(f"\n[OK] 输出: {args.out_dir}")


if __name__ == "__main__":
    main()
