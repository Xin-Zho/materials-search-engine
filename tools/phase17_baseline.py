"""Phase 1.7 autonomous search baseline 冻结 manifest。

生成 data/exports/phase17_baseline.json：
  - records / canonical_miss / extraction_miss / true_open 计数
  - initial_true_open: [(route, mechanism), ...] —— 冻结的 TRUE_OPEN 搜索集
    （loop 启动后不动态改；新发现的 gap 单独记 newly_discovered）
  - extraction_miss_gaps: 每个带最佳候选 paper/evidence（供人工处理，绝不进 loop）

用法:
    python tools/phase17_baseline.py
    python tools/phase17_baseline.py --json-only   # 只写 manifest 不打印全表

loop 用法（读 manifest 冻结 targets）:
    python tools/run_autonomous_loop.py "..." --baseline data/exports/phase17_baseline.json
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from audit_open_gaps import semantic_score  # noqa: E402

from search_engine.knowledge_base import KnowledgeBase  # noqa: E402
from search_engine.route_mechanism_ontology import (  # noqa: E402
    CoverageMatcher, get_mechanisms, CORE_ROUTE_MECHANISMS, compute_gap_coverage,
    mechanism_type, knowledge_status, missing_reason,
)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def classify(all_edges, matcher):
    """每个 OPEN gap → (route, mech, type, verdict, candidates)。

    verdict 判定与 audit_open_gaps 一致：
      CANONICAL_MISS（mech HIGH）/ EXTRACTION_MISS（ev HIGH 或 mech/ev MEDIUM）/ TRUE_OPEN。
    """
    gap_cov = compute_gap_coverage(all_edges)
    out = []
    for core in CORE_ROUTE_MECHANISMS:
        for mech in get_mechanisms(core):
            if gap_cov[(core, mech)]["status"] != "OPEN":
                continue
            candidates = []
            for e in all_edges:
                r = e.canonical_route or e.raw_route or ""
                m = e.canonical_mechanism or e.raw_mechanism or ""
                if not r or not m:
                    continue
                rm = matcher.route_match({r}, core)
                if rm == "NO_MATCH":
                    continue
                if matcher.mechanism_match([m], mech) != "NO_MATCH":
                    continue
                ms, es = semantic_score(m, mech, e.evidence or "")
                candidates.append({
                    "paper_id": e.paper_id, "route": r, "mechanism": m,
                    "evidence": (e.evidence or "")[:120],
                    "mech_score": ms, "ev_score": es, "relation_type": e.relation_type,
                })
            mech_scores = {c["mech_score"] for c in candidates}
            ev_scores = {c["ev_score"] for c in candidates}
            if "HIGH" in mech_scores:
                verdict = "CANONICAL_MISS"
            elif "HIGH" in ev_scores or "MEDIUM" in mech_scores or "MEDIUM" in ev_scores:
                verdict = "EXTRACTION_MISS"
            else:
                verdict = "TRUE_OPEN"
            rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
            candidates.sort(key=lambda c: (rank[c["mech_score"]], rank[c["ev_score"]]))
            out.append({
                "route": core, "mechanism": mech,
                "type": mechanism_type(core, mech),
                "verdict": verdict,
                "candidates": candidates,
            })
    return out


def main():
    ap = argparse.ArgumentParser(description="Phase 1.7 search baseline 冻结 manifest")
    ap.add_argument("--json-only", action="store_true", help="只写 manifest 不打印全表")
    args = ap.parse_args()

    kb = KnowledgeBase()
    records = kb.get_all()
    all_edges = []
    for r in records:
        all_edges.extend(r.route_mechanism_edges)
    kb.close()

    gaps = classify(all_edges, CoverageMatcher())
    cm = [g for g in gaps if g["verdict"] == "CANONICAL_MISS"]
    em = [g for g in gaps if g["verdict"] == "EXTRACTION_MISS"]
    to = [g for g in gaps if g["verdict"] == "TRUE_OPEN"]

    # KNOWLEDGE_STATUS 权威标注（用户定 2026-08-25）：loop 实际输入 =
    #   confirmed_missing 且 missing_reason==SEARCH_GAP 且 type==MECHANISM
    # （EXTRACTION_GAP 走 extractor/人工，hypothesis 走 discovery mode，都不进 completeness）
    search_gaps = [
        [r, m] for r in CORE_ROUTE_MECHANISMS for m in get_mechanisms(r)
        if knowledge_status(r, m) == "confirmed_missing"
        and missing_reason(r, m) == "SEARCH_GAP"
        and mechanism_type(r, m) == "MECHANISM"
    ]

    manifest = {
        "phase": "phase17_search_baseline",
        "records": len(records),
        "edges_total": len(all_edges),
        "canonical_miss": len(cm),
        "extraction_miss": len(em),
        "true_open": len(to),
        "initial_true_open": [[g["route"], g["mechanism"]] for g in to],
        # loop 实际输入（旧启发式口径）：TRUE_OPEN ∩ type==MECHANISM
        "initial_true_open_mechanism": [
            [g["route"], g["mechanism"]] for g in to if g["type"] == "MECHANISM"
        ],
        # loop 实际输入（权威标注口径，优先使用）：SEARCH_GAP ∩ type==MECHANISM
        "initial_search_gaps": search_gaps,
        "extraction_miss_gaps": [
            {"route": g["route"], "mechanism": g["mechanism"], "type": g["type"],
             "best_candidate": g["candidates"][0] if g["candidates"] else None,
             "n_candidates": len(g["candidates"])}
            for g in em
        ],
        "note": "initial_search_gaps（KNOWLEDGE_STATUS/MISSING_REASON 权威口径）是 loop 实际搜索集，"
                "优先于 initial_true_open_mechanism（旧启发式）。loop 启动后不动态改，"
                "新发现 gap 单独记 newly_discovered。EXTRACTION_GAP 与 extraction_miss 绝不进 loop"
                "（evidence 已在 KB，只走 targeted re-extract 或 DIRECT_HUMAN）。",
    }

    os.makedirs("data/exports", exist_ok=True)
    path = "data/exports/phase17_baseline.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print("=" * 76)
    print("Phase 1.7 Search Baseline 冻结")
    print("=" * 76)
    print(f"records          = {manifest['records']}")
    print(f"canonical_miss   = {manifest['canonical_miss']}")
    print(f"extraction_miss  = {manifest['extraction_miss']}   (绝不进 loop)")
    print(f"true_open        = {manifest['true_open']}   (旧启发式审计)")
    print(f"  → loop 实际搜索集 = {len(manifest['initial_search_gaps'])} 个 SEARCH_GAP mechanism"
          f"（KNOWLEDGE_STATUS 权威标注）")
    print("\nSEARCH_GAP MECHANISM gaps（completeness mode 输入）:")
    for r, m in manifest["initial_search_gaps"]:
        print(f"  ✗ {r} × {m}")
    print("\nEXTRACTION_GAP（修 extractor / add_human_edge，不进 loop）:")
    for g in em:
        best = g["candidates"][0] if g["candidates"] else {}
        print(f"  ⚠ {g['route']} × {g['mechanism']}  [{g['type']}]")
        if best:
            print(f"      best: {best['paper_id'][-24:]}  {best['mechanism']}  "
                  f"(mech={best['mech_score']} ev={best['ev_score']})")
            print(f"      ev  : {best['evidence'][:80]}")
    print("\nhypothesis（discovery mode 输入，不在此表——跑 loop --mode discovery）: "
          "AFCT×reversible bond exchange / AFCT×delayed gelation / dual-curing×stress relief")
    print(f"\nmanifest 已存: {path}")
    print("loop 用法: python tools/run_autonomous_loop.py \"...\" --baseline " + path)


if __name__ == "__main__":
    main()
