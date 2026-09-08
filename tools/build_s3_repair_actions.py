"""tools/build_s3_repair_actions.py — S3 Repair Action Construction（2026-08-29 用户定）。

把 64 篇有 repair evidence 的 residual miss 拆成两类 action：
  QUERY  action —— 从 gate 后的 term families（96 词）生成 query family
  CITATION action —— 从 high-conf seeds 做 1-hop citation expansion（按 seed 聚合）

统一结构（用户定）：
  {action_id, type: QUERY|CITATION, source, query_string?, covered_miss_ids,
   estimated_cost, development_source: AUDIT_R02}

query formulation（Queryability v1.1 规则延续）：
  自带 shrinkage/contraction/stress 语义的词（PROBLEM 类核心）→ DIRECT 裸搜 "term"
  其余（METHOD/REACTION/MATERIAL/CONTEXT）→ anchor: TITLE-ABS-KEY("term" AND shrinkage)

cost（用户定）：
  QUERY  —— pilot 前未知（None）；pilot 后填 total_hits / new_vs_S2
  CITATION —— #RetrievedCitationCandidates（seed 的 1-hop 出边数，backward=referenced_works 数，
              forward=从 openalex cache 统计引用 seed 的 works 数）

输出：data/exports/terminology/s3_repair_actions.json
  {actions: [...], summary: {n_query, n_citation, union_coverage, per_category}}

用法：
  python tools/build_s3_repair_actions.py [--term-gate <s3_term_gate.json>]
      [--citation-audit <s3_citation_audit.json>] [--out <path>]
"""
import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_GATE = os.path.join(BASE, "data", "exports", "terminology", "s3_term_gate.json")
DEFAULT_CIT = os.path.join(BASE, "data", "exports", "terminology", "s3_citation_audit.json")
DEFAULT_OUT = os.path.join(BASE, "data", "exports", "terminology",
                           "s3_repair_actions.json")
OA_CACHE = os.path.join(BASE, "data", "cache", "openalex_cache.json")

# 自带 shrinkage/problem 语义 → 可 DIRECT 裸搜（Queryability v1.1）
SHRINK_SEMANTIC = ("shrink", "contraction", "stress", "strain", "expansion",
                   "deformation", "distortion", "warpage", "gap", "crack",
                   "dimensional", "volumetric")

CATEGORY_PRIORITY = ["PROBLEM", "METHOD", "REACTION", "MATERIAL", "CONTEXT"]


def query_for(term: str) -> str:
    """v1.1 规则：自带 shrinkage 语义 → DIRECT；否则 anchor AND shrinkage。"""
    if any(s in term for s in SHRINK_SEMANTIC):
        return f'TITLE-ABS-KEY("{term}")'
    return f'TITLE-ABS-KEY("{term}" AND shrinkage)'


def citation_cost(seed_wid: str, oa: dict, forward_count: dict) -> int:
    """#RetrievedCitationCandidates = backward(seed 引用数) + forward(引用 seed 数)。"""
    m = oa.get(seed_wid, {})
    return len(m.get("referenced_works", [])) + forward_count.get(seed_wid, 0)


def main():
    ap = argparse.ArgumentParser(description="S3 Repair Action Construction")
    ap.add_argument("--term-gate", default=DEFAULT_GATE)
    ap.add_argument("--citation-audit", default=DEFAULT_CIT)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    gate = json.load(open(args.gate_path if hasattr(args, "gate_path") else args.term_gate,
                          encoding="utf-8"))
    cit = json.load(open(args.citation_audit, encoding="utf-8"))

    # ── QUERY actions：每个 gate 词一个 action ──
    miss_terms = {p["wid"]: p.get("repairable_terms", {}) for p in gate["per_miss"]}
    query_actions = []
    for cat in CATEGORY_PRIORITY:
        for term in gate["discriminative_terms"].get(cat, []):
            covered = [wid for wid, rep in miss_terms.items()
                       if term in rep.get(cat, [])]
            query_actions.append({
                "action_id": f"QUERY_{len(query_actions) + 1:03d}",
                "type": "QUERY",
                "source": f"{cat}:{term}",
                "query_string": query_for(term),
                "covered_miss_ids": sorted(covered),
                "estimated_cost": None,   # pilot 后填 total_hits / new_vs_S2
                "development_source": "AUDIT_R02",
            })

    # ── CITATION actions：按 seed 聚合（每个 high-conf seed 一个 action）──
    oa = {}
    forward_count = {}
    if os.path.exists(OA_CACHE):
        cache = json.load(open(OA_CACHE, encoding="utf-8"))
        for q, resp in cache.items():
            for w in resp.get("results", []):
                wid = (w.get("id") or "").replace("https://openalex.org/", "")
                if wid and wid not in oa:
                    oa[wid] = {"referenced_works": [r.replace("https://openalex.org/", "")
                                                    for r in (w.get("referenced_works") or [])]}
                if wid:
                    for r in (w.get("referenced_works") or []):
                        rw = r.replace("https://openalex.org/", "")
                        forward_count[rw] = forward_count.get(rw, 0) + 1

    seed2miss = {}
    seed_info = {}
    for p in cit.get("per_miss", []):
        if not p.get("citation_positive_high_conf"):
            continue
        for sw in p.get("linked_seed_wids", []):
            seed2miss.setdefault(sw, set()).add(p["wid"])
            seed_info[sw] = {"direction": p.get("direction")}
    citation_actions = []
    for sw, miss_set in sorted(seed2miss.items()):
        citation_actions.append({
            "action_id": f"CIT_{len(citation_actions) + 1:03d}",
            "type": "CITATION",
            "source": f"seed:{sw}",
            "direction": seed_info[sw].get("direction"),
            "covered_miss_ids": sorted(miss_set),
            "estimated_cost": citation_cost(sw, oa, forward_count),
            "development_source": "AUDIT_R02",
        })

    actions = query_actions + citation_actions
    q_cov = set()
    c_cov = set()
    for a in actions:
        (q_cov if a["type"] == "QUERY" else c_cov).update(a["covered_miss_ids"])
    summary = {
        "n_query_actions": len(query_actions),
        "n_citation_actions": len(citation_actions),
        "query_union_coverage": len(q_cov),
        "citation_union_coverage": len(c_cov),
        "union_coverage": len(q_cov | c_cov),
        "n_residual_total": gate.get("n_misses", cit.get("n_residual", 70)),
        "per_category": {c: sum(1 for a in query_actions
                                if a["source"].startswith(c))
                         for c in CATEGORY_PRIORITY},
    }
    out = {
        "version": "s3_repair_actions_v1", "frozen_at": "2026-08-29",
        "development_source": "AUDIT_R02",
        "note": "QUERY cost 待 pilot（total_hits/new_vs_S2/residual_recovered）；"
                "CITATION cost = #1-hop candidates（backward+forward）",
        "summary": summary,
        "actions": actions,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"=== S3 Repair Actions ===")
    print(f"  QUERY actions     = {len(query_actions)}（覆盖 {len(q_cov)} miss）")
    print(f"  CITATION actions  = {len(citation_actions)}（覆盖 {len(c_cov)} miss）")
    print(f"  union coverage    = {len(q_cov | c_cov)}")
    print(f"  per-category      = {summary['per_category']}")
    print(f"[OK] {args.out}")


if __name__ == "__main__":
    main()
