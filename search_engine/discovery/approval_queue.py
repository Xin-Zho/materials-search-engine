"""Phase 2.1 approval queue（用户定 2026-08-26）：待人工批准 proposal 独立存储。

不把"待批准 proposal"混进 round state——DiscoveryRound 只记 proposal_ids_created /
proposal_ids_approved；queue 单独维护 data/exports/discovery_approval_queue.json。

每条：
    candidate_id / proposal_id / created_round / candidate_type / action /
    status(PENDING/APPROVED/REJECTED) + proposal（build_proposal 完整 dict，审计用）

规则（用户定）：
    - controller 不能自动 approve——只能生成 PENDING
    - --approve-queue 逐条明确批准，不要一键全批准
    - 同 candidate 已有 PENDING/APPROVED proposal → 不重复入队（防每轮重复生成）
"""

from __future__ import annotations

import json
import os

QUEUE_PATH = "data/exports/discovery_approval_queue.json"


def load_queue(path: str = QUEUE_PATH) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_queue(items: list[dict], path: str = QUEUE_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def has_proposal(queue: list[dict], candidate_id: str) -> bool:
    """候选是否已有有效 proposal（PENDING/APPROVED）——防重复生成（用户 invariant ⑦）。"""
    return any(q.get("candidate_id") == candidate_id
               and q.get("status") in ("PENDING", "APPROVED")
               for q in queue)


def add_proposal(queue: list[dict], proposal: dict, round_id: int) -> dict | None:
    """候选的 proposal 入队（status=PENDING）。已有 → 返回 None（不重复入队）。

    proposal：PromotionProposal.to_dict()（含 candidate_id / candidate_type / action）。
    proposal_id：f"{candidate_id}::{round_id}"（稳定、可重放、可追溯）。
    """
    cid = proposal.get("candidate_id")
    if not cid:
        return None
    if has_proposal(queue, cid):
        return None
    item = {
        "candidate_id": cid,
        "raw_name": proposal.get("candidate_name", ""),   # 审计可读（candidate_id 是 hash）
        "proposal_id": f"{cid}::{round_id}",
        "created_round": round_id,
        "candidate_type": proposal.get("candidate_type"),
        "action": proposal.get("action"),
        "status": "PENDING",
        "proposal": proposal,
    }
    queue.append(item)
    return item


def list_queue(queue: list[dict], status: str | None = None) -> list[dict]:
    if status:
        return [q for q in queue if q.get("status") == status]
    return list(queue)


def approve_item(queue: list[dict], proposal_id: str,
                 approver: str = "human") -> tuple[bool, str]:
    """逐条批准（用户定：PENDING → APPROVED，不自动 approve）。"""
    for q in queue:
        if q.get("proposal_id") == proposal_id:
            if q["status"] != "PENDING":
                return False, f"{proposal_id} 当前 {q['status']}（只批准 PENDING）"
            q["status"] = "APPROVED"
            q["approved_by"] = approver
            return True, f"{proposal_id} 已批准（{q['action']} / {q['candidate_type']}）"
    return False, f"找不到 proposal_id: {proposal_id}"


def reject_item(queue: list[dict], proposal_id: str,
                reason: str = "") -> tuple[bool, str]:
    """逐条拒绝（PENDING → REJECTED，reason 记录）。"""
    for q in queue:
        if q.get("proposal_id") == proposal_id:
            if q["status"] != "PENDING":
                return False, f"{proposal_id} 当前 {q['status']}（只拒绝 PENDING）"
            q["status"] = "REJECTED"
            q["reject_reason"] = reason
            return True, f"{proposal_id} 已拒绝"
    return False, f"找不到 proposal_id: {proposal_id}"


def proposal_ids_created(queue: list[dict]) -> list[str]:
    return [q.get("proposal_id", "") for q in queue if q.get("proposal_id")]


def proposal_ids_approved(queue: list[dict]) -> list[str]:
    return [q.get("proposal_id", "") for q in queue if q.get("status") == "APPROVED"]
