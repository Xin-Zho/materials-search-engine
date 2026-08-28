"""Promotion relation representation repair（用户定 2026-08-26：revision 不覆盖历史）。

场景：candidate 已 PROMOTED（如 bulk-fill），但当时旧 promoter 的 relation grounding
有缺陷（target 是证据句、链扁平化、predicate 语义错误）。node promotion 不撤销，
只修复已发生 promotion 的 **relation representation**——以 revision 追加，旧记录原样保留。

语义关键：
    candidate.status = PROMOTED  完全不变（不是重新 promotion）
    node_promotion    = UNCHANGED（节点提升不受影响）
    relations         = 用当前 grounding 层重建（writable = GROUNDED+DIRECT）
    历史              = 原 promotion 记录保留 + revisions 追加（可审计）

用法:
    python tools/repair_promotion_grounding.py --name "bulk-fill composite formulation" \
        --reason "relation grounding v2: ordered grounding + self-loop removal + predicate typing"
    python tools/repair_promotion_grounding.py --name "..." --plan-only   # 只预览不写
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from search_engine.discovery import DiscoveryCandidate
from search_engine.discovery.promoter import build_proposal

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

POOL_PATH = "data/exports/phase2_candidates.json"
PROMOTIONS_PATH = "data/exports/ontology_promotions.json"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_json(path: str) -> list | dict:
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _find_promotion(promos: list[dict], candidate_id: str) -> dict | None:
    for p in promos:
        if p.get("candidate_id") == candidate_id:
            return p
    return None


def build_grounding_revision(candidate: dict, reason: str) -> dict:
    """用当前 grounding 层重建 proposal → v2 relations（writable = GROUNDED+DIRECT）。

    返回 revision dict（用户 schema）：revision/action/supersedes_revision/
    node_promotion/relations/reason/recorded_at。不写任何文件。
    """
    verification = (candidate.get("provenance") or {}).get("verification") or {}
    cobj = DiscoveryCandidate(**{k: v for k, v in candidate.items()
                                 if k in DiscoveryCandidate.__dataclass_fields__})
    proposal = build_proposal(cobj, verification, preview=True)

    relations = []
    for r in proposal.proposed_relations:
        if not r.writable:          # 只修复"可写正式 ontology"的部分（GROUNDED+DIRECT）
            continue
        relations.append({
            "source": r.source_node,
            "predicate": r.predicate,
            "target": r.target_node,
            "evidence_type": r.evidence_type,
            "paper_ids": r.paper_ids,
            "raw_evidence": r.raw_evidence,
        })

    promo = (candidate.get("provenance") or {}).get("promotion") or {}
    history = (candidate.get("provenance") or {}).get("promotion_history") or []
    prev_revision = max([h.get("version", 1) for h in history] + [1])

    return {
        "revision": prev_revision + 1,
        "action": "GROUNDING_REPAIR",
        "supersedes_revision": prev_revision,
        "node_promotion": {"status": "UNCHANGED"},
        "relations": relations,
        "reason": reason,
        "recorded_at": _now(),
    }


def repair_grounding(cands: list[dict], promos: list[dict], name: str, reason: str,
                     promotion_file_mtime: str = "") -> tuple[dict | None, list[dict], list[str]]:
    """核心逻辑（不写文件）：定位候选 + 生成 revision + 组装新状态。

    返回 (candidate, new_promotions, messages)。candidate 为 None 表示定位失败。
    不变式（用户定）：
      - candidate.status 保持 PROMOTED（不是重新 promotion）
      - 原 promotion 记录保留（revisions 追加，旧 relation 不动）
      - provenance 加 promotion_history（v1 = 原始 PROMOTION，v2 = GROUNDING_REPAIR）
    """
    msgs = []
    hits = [c for c in cands if name.lower() in c.get("raw_name", "").lower()
            or c.get("candidate_id") == name]
    if not hits:
        return None, promos, [f"✗ 找不到候选: {name}"]
    if len(hits) > 1:
        return None, promos, ["匹配到多个，请精确: " +
                              ", ".join(f"{c['candidate_id']} {c['raw_name']}" for c in hits)]
    cand = hits[0]
    if cand.get("status") != "PROMOTED":
        return None, promos, [f"✗ 候选当前 {cand.get('status')}——GROUNDING_REPAIR 只针对已 PROMOTED 候选"]
    if not (cand.get("provenance") or {}).get("promotion"):
        return None, promos, ["✗ 候选无 provenance.promotion 记录（未 promote 过）"]

    promo = _find_promotion(promos, cand["candidate_id"])
    if promo is None:
        return None, promos, [f"✗ ontology_promotions.json 找不到 candidate_id={cand['candidate_id']}"]

    revision = build_grounding_revision(cand, reason)

    # ① 原 promotion 记录保留，revisions 追加（旧 relation 不动）
    promo.setdefault("revisions", []).append(revision)
    # ② provenance 加 promotion_history（v1 原始 + v2 repair）
    prov = cand.setdefault("provenance", {})
    history = prov.setdefault("promotion_history", [])
    if not history:
        history.append({
            "action": "PROMOTION", "version": 1,
            "recorded_at": promotion_file_mtime or "",
        })
    history.append({
        "action": "GROUNDING_REPAIR", "version": revision["revision"],
        "supersedes_revision": revision["supersedes_revision"],
        "reason": reason, "recorded_at": revision["recorded_at"],
    })
    # ③ candidate.status 完全不变
    msgs.append(f"✓ 候选 {cand['raw_name']} status 保持 {cand['status']}（未重新 promotion）")
    msgs.append(f"✓ revision {revision['revision']}（GROUNDING_REPAIR，supersedes "
                f"revision {revision['supersedes_revision']}）已追加；旧 promotion 记录保留")
    msgs.append(f"✓ node_promotion.status = UNCHANGED；relations 重建 "
                f"{len(revision['relations'])} 条（writable = GROUNDED+DIRECT）")
    return cand, promos, msgs


def main():
    ap = argparse.ArgumentParser(description="Phase 2.0 relation representation repair（revision 不覆盖历史）")
    ap.add_argument("--name", required=True, help="候选名（需已 PROMOTED + 有 promotion 记录）")
    ap.add_argument("--reason", required=True, help="repair 理由（审计必填）")
    ap.add_argument("--plan-only", action="store_true", help="只预览 revision，不写文件")
    args = ap.parse_args()

    cands = _load_json(POOL_PATH)
    if isinstance(cands, dict):
        cands = cands.get("candidates", [])
    promos = _load_json(PROMOTIONS_PATH)
    if isinstance(promos, dict):
        promos = promos.get("promotions", [])

    mtime = ""
    if os.path.exists(PROMOTIONS_PATH):
        mtime = datetime.fromtimestamp(os.path.getmtime(PROMOTIONS_PATH),
                                        tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    cand, promos, msgs = repair_grounding(cands, promos, args.name, args.reason,
                                          promotion_file_mtime=mtime)
    for m in msgs:
        print(m)
    if cand is None:
        return

    rev = _find_promotion(promos, cand["candidate_id"]).get("revisions", [])[-1]
    print(f"\nrevision {rev['revision']} relations（GROUNDED+DIRECT）:")
    for r in rev.get("relations", []):
        print(f"  ✓ {r['source']} --{r['predicate']}--> {r['target']}  [{r['evidence_type']}]"
              f"  papers={r['paper_ids']}")

    if args.plan_only:
        print("\n[plan-only] 未写任何东西。确认后去掉 --plan-only 写入 revision。")
        return

    _save_json(POOL_PATH, {"candidates": cands})
    _save_json(PROMOTIONS_PATH, promos)
    print(f"\n✓ 已写 {POOL_PATH}（provenance.promotion_history）")
    print(f"✓ 已写 {PROMOTIONS_PATH}（原记录保留 + revisions 追加）")


if __name__ == "__main__":
    main()
