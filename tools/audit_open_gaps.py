"""18 个 OPEN gap 的逐项语义审计：canonical miss vs 真正缺证据。

背景（用户定）：不要凭感觉扩 MECHANISM_CANONICAL（相近词就加 alias 会制造假 coverage）。
先对每个 OPEN gap 做 candidate-edge 审计，分三类：
  A. CANONICAL_MISS            —— 已有直接 evidence，只是机制措辞没对齐 → 修 normalizer
  B. EXTRACTION_MISS —— evidence 已存在但 extractor 没结构化（不是搜索失败、不是真 gap）；
     重抽失败后人工核实补 human_verified edge（DIRECT_HUMAN 单独统计）
                                          → 调 extractor / targeted re-extract
  C. TRUE_OPEN                 —— KB 没有任何直接 evidence → 交给 autonomous loop 去搜

candidate edge = route 匹配（DIRECT/INHERITED）但 mechanism 未匹配的 edge
（论文确实在讲这个 route，但 edge 的 mechanism 措辞与 checklist 机制没对齐）。

semantic_score（本地启发式，不调 LLM）：
  HIGH   —— 术语归一化可达（token 高度重叠 / 后缀差异 / canonical 映射）
  MEDIUM —— 部分重叠，需人工确认
  LOW    —— 语义不同，绝不能 canonical（如 "NIR-to-UV upconversion" ≠ "reduced polymerizable fraction"）

用法:
    python tools/audit_open_gaps.py [--route AFCT] [--verbose]
"""

import argparse
import sys

from search_engine.knowledge_base import KnowledgeBase
from search_engine.route_mechanism_ontology import (
    CoverageMatcher, get_mechanisms, CORE_ROUTE_MECHANISMS, compute_gap_coverage,
    MECHANISM_CANONICAL, _norm_mech,
)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

_STOP = {"the", "a", "an", "of", "in", "for", "and", "on", "with", "via", "by", "to", "at", "from",
         "during", "via", "its", "their"}

_TYPE_TAG = {"MECHANISM": "M", "ROUTE_PROPERTY": "P", "EFFECT": "E"}


def _norm_tokens(s: str) -> set[str]:
    s = _norm_mech(s).replace("-", " ").replace("/", " ").replace("(", " ").replace(")", " ")
    return {t for t in s.split() if t and t not in _STOP}


def _mech_semantic(edge_mech: str, target_mech: str) -> str:
    """edge.mechanism 与 target 的语义接近度：HIGH / MEDIUM / LOW。"""
    a, b = _norm_tokens(edge_mech), _norm_tokens(target_mech)
    if not a or not b:
        return "LOW"
    # canonical 映射可达（stress relaxation → stress relaxation 等）
    ec = MECHANISM_CANONICAL.get(_norm_mech(edge_mech), "").lower()
    tc = MECHANISM_CANONICAL.get(_norm_mech(target_mech), "").lower()
    if ec and tc and ec == tc:
        return "HIGH"
    if a == b:
        return "HIGH"
    inter = a & b
    jac = len(inter) / len(a | b) if (a | b) else 0.0
    if jac >= 0.6:
        return "HIGH"
    # 子串包含（"step-growth polymerization mechanism" ⊃ "step-growth polymerization"）
    en, tn = _norm_mech(edge_mech), _norm_mech(target_mech)
    if (en and tn) and (en in tn or tn in en):
        return "HIGH" if jac >= 0.5 else "MEDIUM"
    if jac >= 0.3:
        return "MEDIUM"
    return "LOW"


def _evidence_semantic(evidence: str, target_mech: str) -> str:
    """evidence 文本是否在讲 target mechanism：覆盖 target 核心 token 的比例。

    例：evidence "Thiol-ene proceeds via a step-growth mechanism, which delays gelation"
    对 target "step-growth polymerization" —— target 核心词 step/growth 都在 →
    HIGH/MEDIUM。这识别"evidence 支持目标但 edge.mechanism 抽成了别的"（B 类）。

    严格性（用户原则）：单 token 重叠（如 evidence 只有 "reduced" 而 target 是
    "reduced polymerizable fraction"）不算——"reduced shrinkage" 绝不能支撑
    "reduced polymerizable fraction"（effect ≠ mechanism）。要求至少 2 个核心词重叠。
    """
    b = _norm_tokens(target_mech)
    if not b or not evidence:
        return "LOW"
    ev = _norm_tokens(evidence)
    inter = ev & b
    cover = len(inter) / len(b)
    if cover >= 0.66 and len(inter) >= 2:
        return "HIGH"
    if cover >= 0.5 and len(inter) >= 2:
        return "MEDIUM"
    return "LOW"


def semantic_score(edge_mech: str, target_mech: str, evidence: str = "") -> tuple[str, str]:
    """→ (mech_score, ev_score)。mech_score 判断 A 类（normalizer 能救）；
    ev_score 判断 B 类（evidence 支持但 extractor 没抽对）。"""
    return _mech_semantic(edge_mech, target_mech), _evidence_semantic(evidence, target_mech)


def main():
    ap = argparse.ArgumentParser(description="OPEN gap 逐项语义审计（A/B/C 分类）")
    ap.add_argument("--route", default="", help="只看指定 core route")
    ap.add_argument("--verbose", action="store_true", help="打印候选 edge 的完整 evidence")
    args = ap.parse_args()

    kb = KnowledgeBase()
    records = kb.get_all()
    all_edges = []
    for rec in records:
        all_edges.extend(rec.route_mechanism_edges)
    matcher = CoverageMatcher()

    gap_cov = compute_gap_coverage(all_edges)
    open_gaps = [(r, m) for (r, m), info in gap_cov.items() if info["status"] == "OPEN"]
    if args.route:
        open_gaps = [(r, m) for r, m in open_gaps if r == args.route]

    print("=" * 80)
    print(f"OPEN gap 语义审计  (OPEN={len(open_gaps)} / checklist={len(gap_cov)})")
    print("=" * 80)

    counts = {"CANONICAL_MISS": 0, "EXTRACTION_MISS": 0, "TRUE_OPEN": 0}

    for gap_route, gap_mech in sorted(open_gaps):
        gtype = gap_cov[(gap_route, gap_mech)]["type"]
        tag = _TYPE_TAG.get(gtype, "?")
        print(f"\n{'─' * 80}\nGAP: {gap_route} × {gap_mech}   [{tag} {gtype}]")

        # 候选 edges：route 匹配（DIRECT/INHERITED）但 mechanism 未匹配
        candidates = []
        for e in all_edges:
            r = e.canonical_route or e.raw_route or ""
            m = e.canonical_mechanism or e.raw_mechanism or ""
            if not r or not m:
                continue
            rm = matcher.route_match({r}, gap_route)
            if rm == "NO_MATCH":
                continue
            mm = matcher.mechanism_match([m], gap_mech)
            if mm != "NO_MATCH":
                continue  # 已匹配的不会出现在 OPEN gap
            ms, es = semantic_score(m, gap_mech, e.evidence or "")
            candidates.append({
                "edge": e, "route_match": rm,
                "mech_score": ms, "ev_score": es,
            })

        if not candidates:
            print("  candidate edges: (无 —— route 匹配的 edge 都没有)")
            verdict = "TRUE_OPEN"
            counts[verdict] += 1
            print(f"  verdict: {verdict}   (KB 无该 route 的 edge)")
            continue

        # 按 score 排序（mech HIGH > ev HIGH > MEDIUM > LOW）
        rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        candidates.sort(key=lambda c: (rank[c["mech_score"]], rank[c["ev_score"]]))
        for i, c in enumerate(candidates[:8], 1):
            e = c["edge"]
            r = e.canonical_route or e.raw_route or ""
            m = e.canonical_mechanism or e.raw_mechanism or ""
            print(f"  {i}. [{e.paper_id[-22:]}] {r} → {m}   [{c['route_match']}, "
                  f"{e.relation_type}, conf={e.confidence:.2f}]  mech={c['mech_score']} ev={c['ev_score']}")
            if args.verbose and e.evidence:
                print(f"     evidence: {e.evidence[:100]}")
            else:
                print(f"     evidence: {(e.evidence or '')[:70]}")
        if len(candidates) > 8:
            print(f"     ... 另有 {len(candidates) - 8} 条候选")

        # verdict：A（mech 归一可达）> B（evidence 支持但 edge 抽错）> C（无证据）
        mech_scores = {c["mech_score"] for c in candidates}
        ev_scores = {c["ev_score"] for c in candidates}
        if "HIGH" in mech_scores:
            verdict = "CANONICAL_MISS"
            best = next(c for c in candidates if c["mech_score"] == "HIGH")
            best_m = best["edge"].canonical_mechanism or best["edge"].raw_mechanism or ""
            print(f"  verdict: {verdict}")
            print(f"  suggest: \"{best_m}\" → \"{gap_mech}\"  (补 MECHANISM_CANONICAL)")
        elif "HIGH" in ev_scores or "MEDIUM" in mech_scores or "MEDIUM" in ev_scores:
            verdict = "EXTRACTION_MISS"
            print(f"  verdict: {verdict}")
            print(f"  reason : evidence 已存在但 extractor 没结构化（不是搜索失败、不是真 gap）；"
                  f"重抽失败后人工核实可补 human_verified edge（DIRECT_HUMAN 单独统计）")
        else:
            verdict = "TRUE_OPEN"
            print(f"  verdict: {verdict}")
            print(f"  reason : 无 candidate edge 描述 \"{gap_mech}\"（措辞语义不同，不能 canonical）")
        counts[verdict] += 1

    print("\n" + "=" * 80)
    print("汇总")
    print("=" * 80)
    for v, n in counts.items():
        print(f"  {v:<34} {n}")
    print("\n边界（用户定）：EXTRACTION_MISS ≠ TRUE_OPEN——evidence 已存在但 extractor 没结构化"
          "是 extraction failure，不是搜索失败。重抽失败 → 人工核实补 human_verified edge"
          "（DIRECT_HUMAN 单独统计），绝不混回搜索 gap。")
    print("下一步：只修 CANONICAL_MISS（补 MECHANISM_CANONICAL）；EXTRACTION_MISS 做"
          "targeted re-extract 或人工补 edge；TRUE_OPEN 才进 autonomous loop。")
    kb.close()


if __name__ == "__main__":
    main()
