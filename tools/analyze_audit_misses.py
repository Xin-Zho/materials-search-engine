"""tools/analyze_audit_misses.py — v3.0 Completeness Auditor 主链入口。

主链（用户 2026-08-29 定稿，Repair-first）：
  Audit（Phase A：独立抽样 + labels + miss-rate/CI）
    -> Miss Detection（build_audit_misses 自动导出 RELEVANT ∧ agent_seen=false）
    -> Miss Expansion Agent（漏检论文自身 = 搜索线索：term mining + 用途分类 /
       backward citation / forward citation / community / historical language）
    -> Repair Search 候选（DRY_RUN，不执行）

Failure Classifier 降级为辅助记录/解释层（Repair first, classification for
auditability），不再作为主链必经门。

统计纪律（写死原则）：
  - 漏检论文用于修复后 = 开发数据；下一次证明效果必须用新的独立 Audit Round 样本
  - Phase A 的 miss-rate / CI 不被 Phase B 改写
  - 核心曲线 = p_hat_miss^(t) + 95% Upper Bound 持续下降（不是 QGS 百分比）

misses 来源（--mode）：
  audit  Phase A AuditRecord（labels 必须 COMPLETED，否则明确报错）
  gold   QGS usable gold（独立人工构建，非概率抽样；miss_rate/CI 标注 N/A）
  demo   4 篇 postmortem（pipeline 验证）

输出（四组）：
  1. Audit statistics（Phase A 原值）
  2. Miss Expansion（每篇：术语用途分类 / backward / historical / query 候选）
  3. Evidence coverage（告诉下一步最值得补哪类数据）
  4. Repair Search 候选（DRY_RUN）+ 附带 classification 解释记录
"""
import argparse
import json
import math
import os
import sys
from collections import Counter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "search_engine", "audit"))

from miss_analyzer import AuditContext, analyze_miss, build_audit_misses  # noqa: E402
from miss_expansion import expand_miss                                     # noqa: E402
from failure_classifier import classify                                    # noqa: E402
from repair_planner import build_repair_plan                               # noqa: E402

OUT_DIAG = os.path.join(BASE, "data", "exports", "audit_miss_diagnostics.json")
OUT_PLAN = os.path.join(BASE, "data", "exports", "audit_repair_plan.json")

_DEMO_MISSES = [
    {"paper_id": "QGS_1999_flowable", "audit_label": "RELEVANT", "agent_seen": False,
     "title": "Polymerization shrinkage and elasticity of flowable composites and fillers",
     "year": 1999, "doi": "10.1016/s0109-5641(99)00022-6",
     "eid": "2-s2.0-0033098184", "wid": "W1968790752", "abstract": ""},
    {"paper_id": "QGS_1987_methacrylate", "audit_label": "RELEVANT", "agent_seen": False,
     "title": "Polymerization shrinkage of methacrylate esters",
     "year": 1987, "doi": "10.1016/0142-9612(87)90030-5",
     "eid": "2-s2.0-0023121831", "wid": "W2075818050", "abstract": ""},
    {"paper_id": "QGS_1998_light", "audit_label": "RELEVANT", "agent_seen": False,
     "title": "Do Dental Composites Always Shrink Toward the Light?",
     "year": 1998, "doi": "10.1177/00220345980770060801",
     "eid": "2-s2.0-0032223611", "wid": "W2008672090", "abstract": ""},
    {"paper_id": "QGS_1997_microfilled", "audit_label": "RELEVANT", "agent_seen": False,
     "title": "Polymerization shrinkage of microfilled composites determined by laser scanning",
     "year": 1997, "doi": "10.1016/s0142-9612(96)00171-8",
     "eid": "2-s2.0-0031104955", "wid": "W1985310578", "abstract": ""},
]


def wilson_upper(n: int, m: int, z: float = 1.645) -> float:
    if n <= 0:
        return 0.0
    p = m / n
    denom = 1 + z * z / n
    center = p + z * z / (2 * n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return min((center + half) / denom, 1.0)


def evidence_coverage(diag: list[dict], n: int) -> dict:
    def cnt(pred):
        return sum(1 for d in diag if pred(d))

    return {
        "identity_resolved": f"{cnt(lambda d: d['identity_evidence']['resolved'])}/{n}",
        "query_evidence_matched": f"{cnt(lambda d: bool(d['query_evidence']['matched_existing_queries']))}/{n}",
        "citation_evidence_data": f"{cnt(lambda d: d['citation_evidence'].get('_data_available'))}/{n}",
        "citation_positive": f"{cnt(lambda d: d['citation_evidence']['known_bridge_node'] or (d['citation_evidence']['backward_links_to_found'] or 0) > 0)}/{n}",
        "community_evidence": f"{cnt(lambda d: bool(d['community_evidence']['community_ids']))}/{n}",
        "term_evidence": f"{cnt(lambda d: d['term_evidence']['n_shared'] + d['term_evidence']['n_novel'] > 0)}/{n}",
        "term_novel": f"{cnt(lambda d: d['term_evidence']['n_novel'] > 0)}/{n}",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["gold", "audit", "demo"], default="gold")
    ap.add_argument("--audit-round", default=None,
                    help="audit 模式：Phase A audit_id（labels 必须 COMPLETED）")
    ap.add_argument("--misses", default=None, help="直接喂 misses JSON（兼容手动）")
    ap.add_argument("--sample-size", type=int, default=None)
    ap.add_argument("--out-diag", default=OUT_DIAG)
    ap.add_argument("--out-plan", default=OUT_PLAN)
    args = ap.parse_args()

    ctx = AuditContext()

    # ── Miss Detection ──
    audit_stats = None
    if args.misses:
        misses = json.load(open(args.misses, encoding="utf-8"))
        mode_note = f"manual misses file: {args.misses}"
    elif args.mode == "demo":
        misses = list(_DEMO_MISSES)
        mode_note = "demo（v2.2-A postmortem 4 篇，QGS-guided 非独立审计）"
    elif args.mode == "gold":
        misses, mode_note = build_audit_misses(ctx, mode="gold")
    else:
        misses, mode_note = build_audit_misses(ctx, mode="audit",
                                               audit_id=args.audit_round)
        from search_engine.completeness.audit import find_audit
        a = find_audit(args.audit_round)
        audit_stats = {
            "audit_round": a.audit_id, "status": a.status,
            "sample_size": a.sample_size, "m": a.m,
            "estimated_miss_rate": round(a.m / a.sample_size, 4) if a.m is not None else None,
            "ci_upper_95": getattr(a, "ci_upper_95", None),
            "recall_lcb": getattr(a, "recall_lcb", None),
            "note": "Phase A 原始统计，Phase B 不改写",
        }

    print(f"[misses] n={len(misses)} | {mode_note}")
    print(f"registries: found_eids={len(ctx.found_eids)} | exec_queries="
          f"{len(ctx.query_depth)} | communities={len(ctx.comm_terms)} | "
          f"terms={len(ctx.terms)} | bridge={len(ctx.bridge)}")

    # ── Miss Analyzer（证据） + Classifier（辅助记录） + Miss Expansion（主链）──
    diag = []
    for miss in misses:
        ev = analyze_miss(miss, ctx)
        ev["classification"] = classify(ev)          # 辅助记录（降级层）
        ev["expansion"] = expand_miss(ev, ctx)       # 主链：expansion
        diag.append(ev)

    # 打印（主链优先：expansion；classification 一行辅助）
    for ev in diag:
        ex = ev["expansion"]
        cl = ev["classification"]
        print(f"\n[miss] {ev['paper_id']} ({ev.get('year')})")
        print(f"  evidence: query_contains={ev['query_evidence']['query_set_contains_target']} "
              f"bridge={ev['citation_evidence']['known_bridge_node']} "
              f"community={ev['community_evidence']['represented']}")
        tc = ex["term_candidates"]
        hist = [t["surface"] for t in ex["historical_terms"]][:6]
        byc = Counter(t["primary"] for t in tc)
        qc = ex["query_candidates"]
        print(f"  term mining: {len(tc)} 短语 | 用途分类 {dict(byc)} | "
              f"historical={hist}")
        print(f"  backward: {len(ex['backward_candidates'])} 新引用论文 | "
              f"forward: {ex['forward_candidates']['available']}")
        print(f"  query 候选(DRY_RUN): {len(qc)} 条 | "
              f"例: {qc[0]['query'] if qc else '-'}")
        print(f"  [explanation] {cl['primary_failure']} conf={cl['confidence']} "
              f"sec={cl['secondary_failures']}")

    # ── 四组输出 ──
    if audit_stats is None:
        n = args.sample_size or len(misses)
        audit_stats = {
            "audit_round": args.audit_round or "GOLD_QGS",
            "status": "GOLD_NON_PROBABILISTIC",
            "sample_size": n, "m": len(misses),
            "estimated_miss_rate": round(len(misses) / n, 4) if n else None,
            "ci_upper_95": round(wilson_upper(n, len(misses)), 4) if n else None,
            "note": "gold/demo 模式非概率抽样——miss_rate/CI 仅示意，无统计意义",
        }

    # Expansion 类型统计（修复类型：Repair first）
    exp_types = Counter()
    for ev in diag:
        ex = ev["expansion"]
        if ex["term_candidates"]:
            exp_types["term"] += 1
        if ex["backward_candidates"]:
            exp_types["backward_citation"] += 1
        if ex["historical_terms"]:
            exp_types["historical_language"] += 1
        if ex["community_evidence"]["known_bridge_node"]:
            exp_types["bridge_node"] += 1
    query_candidates_total = sum(len(ev["expansion"]["query_candidates"])
                                 for ev in diag)

    primary_dist = Counter(d["classification"]["primary_failure"] for d in diag)
    coverage = evidence_coverage(diag, len(diag))

    # Repair Search 候选（DRY_RUN）
    repair_plan = {
        "audit_round": audit_stats["audit_round"],
        "repair_mode": "DRY_RUN",
        "repair_version": None,
        "miss_count": len(diag),
        "expansion_type_summary": dict(exp_types),
        "query_candidates_total": query_candidates_total,
        "classification_summary": dict(primary_dist),
        "recommended_expansions": [
            {"type": "term_mining", "triggered_by": exp_types.get("term", 0)},
            {"type": "backward_citation", "triggered_by": exp_types.get("backward_citation", 0)},
            {"type": "historical_language", "triggered_by": exp_types.get("historical_language", 0)},
            {"type": "bridge_node_community", "triggered_by": exp_types.get("bridge_node", 0)},
        ],
        "note": "DRY-RUN ONLY：不自动执行任何 Repair/query；漏检论文用于修复后即成为"
                "开发数据，下一轮效果必须用新的独立 Audit Round 样本验证"
                "（统计纪律：p_hat + CI_upper 双下降才是趋于完备）",
    }

    print(f"\n{'='*78}")
    print(f"[1] Audit statistics: {json.dumps(audit_stats, ensure_ascii=False)}")
    print(f"[2] Miss Expansion 类型: {dict(exp_types)} | "
          f"query 候选(DRY_RUN)={query_candidates_total}")
    print(f"[3] Evidence coverage:")
    for k, v in coverage.items():
        print(f"    {k:<24} {v}")
    print(f"[4] Repair Search (DRY_RUN):")
    for r in repair_plan["recommended_expansions"]:
        print(f"    {r['type']:<24} x{r['triggered_by']}")
    print(f"    [explanation] classification: {dict(primary_dist)}")

    out_diag = {
        "version": "v30_miss_diagnostics",
        "mode": args.mode,
        "misses_source": mode_note,
        "audit_statistics": audit_stats,
        "expansion_type_summary": dict(exp_types),
        "evidence_coverage": coverage,
        "classification_summary": dict(primary_dist),
        "misses": [{
            "paper_id": d["paper_id"], "year": d.get("year"), "title": d.get("title"),
            "evidence": {k: d[k] for k in ("identity_evidence", "query_evidence",
                                           "citation_evidence", "community_evidence",
                                           "term_evidence", "execution_evidence")},
            "classification": d["classification"],
            "expansion": d["expansion"],
        } for d in diag],
    }
    with open(args.out_diag, "w", encoding="utf-8") as f:
        json.dump(out_diag, f, ensure_ascii=False, indent=1)
    repair_plan["audit_statistics"] = audit_stats
    with open(args.out_plan, "w", encoding="utf-8") as f:
        json.dump(repair_plan, f, ensure_ascii=False, indent=1)
    print(f"\n[OK] diagnostics: {args.out_diag}")
    print(f"[OK] repair plan:  {args.out_plan}")


if __name__ == "__main__":
    main()
