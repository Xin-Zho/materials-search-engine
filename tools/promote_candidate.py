"""Promoter CLI：VALIDATED candidate → PromotionProposal → 人工批准 → APPLY。

流程（用户定 2026-08-26）：
    --plan-only: 输出 proposal（PROPOSED，不写任何东西）
    --approve:   PROPOSED → APPROVED → APPLIED（写 ontology_promotions.json + 候选 PROMOTED）
    --reject:    否决 proposal

APPLY 写入 data/exports/ontology_promotions.json（节点定义 + typed relations + evidence
provenance）；代码层 ontology（CORE_ROUTE_MECHANISMS 等）由人工按 proposal 修改——
Phase 2 不让模型自己扩 ontology。

用法:
    python tools/promote_candidate.py --name "bulk-fill composite formulation" --plan-only
    python tools/promote_candidate.py --name "bulk-fill composite formulation" --approve
    python tools/promote_candidate.py --name "bulk-fill composite formulation" --reject --reason "..."
"""

import argparse
import json
import os
import sys

from search_engine.discovery import DiscoveryCandidate
from search_engine.discovery.promoter import PromotionProposal, PromotionRelation, build_proposal

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

POOL_PATH = "data/exports/phase2_candidates.json"
PROMOTIONS_PATH = "data/exports/ontology_promotions.json"


def _load_pool() -> list[dict]:
    with open(POOL_PATH, encoding="utf-8") as f:
        return json.load(f).get("candidates", [])


def _save_pool(cands: list[dict]):
    with open(POOL_PATH, encoding="utf-8") as f:
        data = json.load(f)
    data["candidates"] = cands
    with open(POOL_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load_promotions() -> list[dict]:
    if os.path.exists(PROMOTIONS_PATH):
        try:
            return json.load(open(PROMOTIONS_PATH, encoding="utf-8"))
        except Exception:
            return []
    return []


def _find_candidate(cands: list[dict], name: str) -> list[dict]:
    n = name.lower()
    return [c for c in cands if n in c["raw_name"].lower() or c["candidate_id"] == name]


def _print_proposal(p: PromotionProposal):
    print("=" * 72)
    print(f"Candidate: {p.candidate_name}  [{p.candidate_type}]")
    print(f"Decision:  {p.action}" + (f"   parent={p.parent_node}" if p.parent_node else ""))
    if p.new_node_name:
        print(f"New node:  {p.new_node_name}  [{p.new_node_type}]")
    print(f"Causal status: {p.causal_status}")
    print(f"Evidence:  target DIRECT {p.direct_target_paper_count} 篇独立论文, "
          f"papers={len(set(p.evidence_papers))}")
    print("Relations（grounding 层：evidence sentence 不能当 node）:")
    for r in p.proposed_relations:
        mark = "✓" if r.writable else ("?" if r.evidence_type == "INFERRED" else "·")
        tgt = r.target_node if r.target_node else "(UNRESOLVED: evidence 仅记录)"
        print(f"  {mark} {r.source_node} --{r.predicate}--> {tgt}"
              f"  [{r.evidence_type}/{r.grounding_status}]")
        for e in r.raw_evidence[:1]:
            print(f"      ev: {e[:80]}")
    for w in p.warnings:
        print(f"⚠ {w}")
    print(f"Node status: {p.node_status}   Relation status: {p.relation_status}")
    n_writable = sum(1 for r in p.proposed_relations if r.writable)
    print(f"可写正式 ontology 的 relations（GROUNDED+DIRECT）: {n_writable}")


def main():
    ap = argparse.ArgumentParser(description="Phase 2.0 promotion（VALIDATED → ontology 提案）")
    ap.add_argument("--name", required=True, help="候选名（需 status=VALIDATED）")
    ap.add_argument("--plan-only", action="store_true", help="只输出 proposal，不写任何东西")
    ap.add_argument("--approve", action="store_true", help="批准并 APPLY（写 ontology_promotions.json）")
    ap.add_argument("--reject", action="store_true", help="否决 proposal")
    ap.add_argument("--reason", default="", help="approve/reject 理由")
    ap.add_argument("--relation", action="append", default=[],
                    help="附加 relation：'predicate:target_node[:evidence_type]'"
                         "（source=candidate，如 affects:polymerization shrinkage:DIRECT；"
                         "target 必须是 node，否则 UNRESOLVED）")
    args = ap.parse_args()

    cands = _load_pool()
    hits = _find_candidate(cands, args.name)
    if not hits:
        print(f"✗ 找不到候选: {args.name}")
        return
    if len(hits) > 1:
        print("匹配到多个，请精确:")
        for c in hits:
            print(f"  {c['candidate_id']}  {c['raw_name']}  [{c['status']}]")
        return
    c = hits[0]

    # 验收 ①：非 VALIDATED 不能 promotion（写操作 approve/reject 严格检查；
    # plan-only 只读——PROMOTED 候选允许重预览 grounding 效果，不写任何东西）
    if c["status"] != "VALIDATED":
        if args.plan_only and c["status"] == "PROMOTED":
            print(f"· 候选已 PROMOTED（{c['status']}）——plan-only 只读重预览 proposal，不写任何东西")
        else:
            print(f"✗ 候选非 VALIDATED（当前 {c['status']}）——不能 promotion")
            return

    # 验收 ②：promoter 不重做验证——只读 verification 结果
    verification = (c.get("provenance") or {}).get("verification") or {}
    extra = []
    for r in args.relation:
        parts = r.split(":")
        if len(parts) < 2:
            print(f"✗ relation 格式应为 predicate:target_node[:evidence_type]（got {r!r}）")
            return
        pred, target = parts[0], parts[1]
        etype = parts[2] if len(parts) > 2 else "DIRECT"
        extra.append(PromotionRelation(source_node=c["raw_name"], predicate=pred,
                                       target_node=target, evidence_type=etype))
    candidate_obj = DiscoveryCandidate(**{k: v for k, v in c.items()
                                          if k in DiscoveryCandidate.__dataclass_fields__})
    proposal = build_proposal(candidate_obj, verification, extra_relations=extra,
                              preview=args.plan_only)
    _print_proposal(proposal)

    if args.reject:
        if proposal.reject(args.reason):
            print(f"\n✓ 已否决: {proposal.candidate_name}（PROPOSED → REJECTED）")
        else:
            print(f"\n✗ 无法否决（状态 {proposal.status}）")
        return

    if args.plan_only:
        if c["status"] == "PROMOTED":
            print("\n[plan-only] 只读重预览完成——候选已 PROMOTED，不可再次 approve；"
                  "本输出仅用于验收 grounding 效果")
        else:
            print("\n[plan-only] 未写任何东西。批准: python tools/promote_candidate.py "
                  f"--name \"{proposal.candidate_name}\" --approve")
        return

    if args.approve:
        if not proposal.approve():
            print(f"\n✗ 无法批准（状态 {proposal.status}）")
            return
        # 验收 ⑥：APPLY 前有 proposal/approval 记录（review_log 已记 APPROVED）
        proposal.apply()
        # 写 ontology_promotions.json（node 与 relation 分开验收：
        # 只有 GROUNDED + DIRECT 的 relation 标记为可写正式 ontology）
        promos = _load_promotions()
        promos.append(proposal.to_dict())
        with open(PROMOTIONS_PATH, "w", encoding="utf-8") as f:
            json.dump(promos, f, ensure_ascii=False, indent=2)
        # 候选池：status → PROMOTED
        c["status"] = "PROMOTED"
        c.setdefault("review_log", []).append({
            "to": "PROMOTED", "action": "PROMOTION_APPLIED",
            "promotion_action": proposal.action,
            "node_status": proposal.node_status,
            "relation_status": proposal.relation_status,
            "by": "promoter",
        })
        c["provenance"]["promotion"] = proposal.to_dict()
        _save_pool(cands)
        print(f"\n✓ APPLIED: {proposal.candidate_name} → {proposal.action}")
        print(f"  node_status={proposal.node_status}  relation_status={proposal.relation_status}")
        print(f"  已写 {PROMOTIONS_PATH}（{len(promos)} 条 promotion 记录）")
        writable = [r for r in proposal.proposed_relations if r.writable]
        unresolved = [r for r in proposal.proposed_relations
                      if r.grounding_status != "GROUNDED" or r.evidence_type != "DIRECT"]
        print(f"  可写正式 ontology（GROUNDED+DIRECT）: {len(writable)} 条")
        for r in writable:
            print(f"    ✓ {r.source_node} --{r.predicate}--> {r.target_node}")
        if unresolved:
            print(f"  ⚠ relation 待 grounding（UNRESOLVED/INFERRED）: {len(unresolved)} 条"
                  f"——只保留记录，不写正式 ontology")
        print(f"  代码层 ontology 修改（CORE_ROUTE_MECHANISMS 等）请人工按 proposal 执行——"
              f"模型不自己扩 ontology")
    else:
        print("\n需指定 --approve 或 --reject 或 --plan-only")


if __name__ == "__main__":
    main()
