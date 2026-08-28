"""Phase 2.0 candidate 发现 CLI：scan → type → filter → Candidate Pool。

调用 search_engine.discovery 模块（DiscoveryCandidate 统一结构）：
    scan_kb（高召回）→ canonical_match（ALIAS）→ existing_knowledge_match（EXISTING_KNOWLEDGE）
    → type_candidate（9 类）→ domain_relevance_level → DiscoveryCandidate 池

输出 data/exports/phase2_candidates.json（含状态机/升格规则/每候选 8+ 字段）。

用法:
    python tools/discover_candidates.py [--min-papers 2] [--top 30] [--dry-run]
"""

import argparse
import json
import os
import sys

from search_engine.knowledge_base import KnowledgeBase
from search_engine.discovery import (
    scan_kb, type_candidate, domain_relevance_level,
    canonical_match, existing_knowledge_match,
    DiscoveryCandidate, CANDIDATE_TYPES, STATUS_FLOW, PROMOTION_RULES,
    can_verify, verification_priority, merge_pool,
)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

POOL_PATH = "data/exports/phase2_candidates.json"


def _build_candidate(raw) -> DiscoveryCandidate:
    """RawCandidate → DiscoveryCandidate（含 typing + filter 分类）。"""
    cm = canonical_match(raw.raw_name)
    existing = existing_knowledge_match(raw.raw_name)
    if cm:
        status, ctype = "ALIAS", "ALIAS"
        match_name = cm
    elif existing:
        status, ctype = "EXISTING_KNOWLEDGE", "EFFECT"
        match_name = existing
    else:
        status, ctype = "CANDIDATE", type_candidate(raw.raw_name)
        match_name = None
    rel, rel_score = domain_relevance_level(
        raw.raw_name, [x["evidence"] for x in raw.evidence_samples])
    return DiscoveryCandidate.from_raw(
        raw,
        provenance_extra={
            "relevance_score": rel_score,
            "typer_rule": ctype,
            "evidence_samples": raw.evidence_samples,
        },
        candidate_type=ctype,
        canonical_match=match_name,
        domain_relevance=rel,
        status=status,
        evidence=[x["evidence"] for x in raw.evidence_samples],
    )


def merge_pool_old(new_raws) -> list[dict]:
    """merge 封装：旧池 JSON + 新扫描 RawCandidate → 合并后候选 dict 列表。"""
    old_cands = load_old_pool()
    new_cands = [_build_candidate(raw) for raw in new_raws]
    return merge_pool(old_cands, new_cands)


def load_old_pool() -> list[dict]:
    if os.path.exists(POOL_PATH):
        try:
            with open(POOL_PATH, encoding="utf-8") as f:
                return json.load(f).get("candidates", [])
        except Exception:
            return []
    return []


def build_pool(kb, min_papers: int) -> list[DiscoveryCandidate]:
    """scan + type + filter → 候选池，并与旧池按 candidate_id merge（persistence）。"""
    raws = scan_kb(kb)
    if os.path.exists(POOL_PATH):
        merged = merge_pool_old(raws)
        return [DiscoveryCandidate(**{k: v for k, v in c.items()}) for c in merged]
    pool: list[DiscoveryCandidate] = []
    for raw in raws:
        pool.append(_build_candidate(raw))
    return pool


def main():
    ap = argparse.ArgumentParser(description="Phase 2.0 candidate 发现与分类（DiscoveryCandidate）")
    ap.add_argument("--min-papers", type=int, default=2, help="展示最低支持论文数（默认 2）")
    ap.add_argument("--top", type=int, default=30, help="每类最多显示数（默认 30）")
    ap.add_argument("--dry-run", action="store_true", help="只打印不写文件")
    args = ap.parse_args()

    kb = KnowledgeBase()
    pool = build_pool(kb, args.min_papers)
    kb.close()

    # 分桶
    buckets: dict[str, list[DiscoveryCandidate]] = {}
    for c in pool:
        key = c.status if c.status in ("ALIAS", "EXISTING_KNOWLEDGE") else c.candidate_type
        buckets.setdefault(key, []).append(c)

    n_true = sum(1 for c in pool if c.status == "CANDIDATE")
    n_alias = sum(1 for c in pool if c.status == "ALIAS")
    n_existing = sum(1 for c in pool if c.status == "EXISTING_KNOWLEDGE")
    print("=" * 78)
    print(f"Phase 2.0 Candidate Pool（raw={len(pool)}  alias={n_alias}  "
          f"existing_knowledge={n_existing}  candidate={n_true}）")
    print(f"状态机: RAW → TYPED → {{ALIAS, IRRELEVANT, EXISTING_KNOWLEDGE, CANDIDATE}} "
          f"→ VERIFYING → {{REJECTED, ADJACENT, NEED_MORE_EVIDENCE, VALIDATED}} → PROMOTED")
    print("=" * 78)

    order = [t for t in CANDIDATE_TYPES if t not in ("ALIAS", "UNKNOWN")] + ["UNKNOWN"]
    for t in order:
        entries = sorted(buckets.get(t, []), key=lambda c: -c.independent_paper_count)
        entries = [e for e in entries if e.independent_paper_count >= args.min_papers]
        if not entries:
            continue
        print(f"\n=== {t}（{len(entries)} 个，≥{args.min_papers} 篇）===")
        for e in entries[: args.top]:
            print(f"  [{e.independent_paper_count:>2} 篇 {e.domain_relevance:<7}] {e.raw_name[:60]}")
            if e.evidence:
                print(f"        {e.source_papers[0][-24:]}  {e.evidence[0][:75]}")

    print(f"\n=== ALIAS 清洗（{n_alias} 个，不算 discovery）===")
    for e in sorted(buckets.get("ALIAS", []), key=lambda c: -c.independent_paper_count)[:10]:
        print(f"  [{e.independent_paper_count:>2} 篇] {e.raw_name[:50]}  → canonical_match={e.canonical_match}")
    print(f"\n=== EXISTING_KNOWLEDGE（{n_existing} 个——字符串不同但 ontology 已表达）===")
    for e in sorted(buckets.get("EXISTING_KNOWLEDGE", []), key=lambda c: -c.independent_paper_count)[:8]:
        print(f"  [{e.independent_paper_count:>2} 篇] {e.raw_name[:50]}  ≈ {e.canonical_match}（≠，不合并）")

    # 自动验证优先池（shortlist）：verification_priority = can_verify + ≥2 篇 + rel≥MEDIUM
    # 只用于"值得优先验证"，不是入口门槛也不是 promotion 预检（用户定 2026-08-26：入口宽出口严）
    prio = [e for e in pool if verification_priority(e)]
    print(f"\n=== 自动验证优先池（can_verify + ≥{max(2, args.min_papers)} 篇 + rel≥MEDIUM，"
          f"{len(prio)} 个值得优先验证）===")
    for e in sorted(prio, key=lambda c: (-c.independent_paper_count,
                                         c.domain_relevance == "HIGH"))[:8]:
        print(f"  [{e.candidate_type:<22}] {e.raw_name[:55]}  "
              f"({e.independent_paper_count} 篇, {e.domain_relevance})")
    # 入口门槛单独展示：can_verify 允许进 VERIFYING 的全部（含 human_seed / rel=UNKNOWN）
    entry_ok = [e for e in pool if can_verify(e) and e.status == "CANDIDATE"]
    print(f"\n验证入口（can_verify，{len(entry_ok)} 个可进 VERIFYING——含 seed/rel=UNKNOWN，"
          f"不等同优先池）:")
    for e in sorted(entry_ok, key=lambda c: -c.independent_paper_count)[:5]:
        print(f"  [{e.candidate_type:<22}] {e.raw_name[:55]}  "
              f"({e.independent_paper_count} 篇, {e.domain_relevance}, {e.source})")

    print("\n升格条件（PROMOTION_RULES，出口严）: " + "; ".join(PROMOTION_RULES.values()))

    if not args.dry_run:
        os.makedirs("data/exports", exist_ok=True)
        path = "data/exports/phase2_candidates.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "phase": "phase2_candidates",
                "schema_version": "2.0",
                "status_flow": STATUS_FLOW,
                "promotion_rules": PROMOTION_RULES,
                "candidates": [c.to_dict() for c in pool],
            }, f, ensure_ascii=False, indent=2)
        print(f"\n候选池已存: {path}（{len(pool)} 条 DiscoveryCandidate）")
        print("review: python tools/review_candidates.py --status PROMOTED --name ...")
    else:
        print("\n[dry-run] 未写文件。正式运行: python tools/discover_candidates.py")


if __name__ == "__main__":
    main()
