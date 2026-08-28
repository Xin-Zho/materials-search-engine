"""Phase 2.1 Discovery Controller（用户定 2026-08-26 Phase 2.1a）。

把 Phase 2.0 单次流程稳定串成多轮 discovery loop：

    Round t → scan → merge_pool → canonical_filter → eligible → prioritizer.select
      → 逐候选 verify → tracking 更新 → VALIDATED → approval queue（人工批准）
      → refresh pool/versions → metrics → append DiscoveryRound → stopping check

P1 目标（用户定，很窄）：**证明已有 Phase 2.0 模块能被稳定串成多轮流程，
且不会重复验证、重复 promotion、乱改状态**。不追求发现新知识（那是 2.1b）。

锁死的 invariant（用户定，P1 验收 6 条）：
    ① controller 不能自动 approve——只生成 PENDING 进 approval queue
    ② 单个候选验证失败不整轮失败——记 candidate_errors 后继续
    ③ 同一 round_id 重跑不造成 verification / round manifest 双写（append 幂等 +
       tracking 同轮幂等）
    ④ NEED_MORE_EVIDENCE 验证后按 evidence_signature 判断增益 → 更新 streak
    ⑤ SEARCH_INCONCLUSIVE 第一次可重试；第二次无解 → SEARCH_INCONCLUSIVE_FROZEN
    ⑥ PROMOTED / REJECTED / ADJACENT / ALIAS / EXISTING_KNOWLEDGE 不重新选择
    ⑦ VALIDATED 已有 proposal 的不重复生成（queue 查重）

verify 插拔：verify_fn(candidate_dict) -> verdict str。默认实现只读候选已有的
verification 缓存（不烧 API）；无缓存且未注入 → candidate_error。测试全部注入 mock。
"""

from __future__ import annotations

import json
import os

from .candidate import DiscoveryCandidate, get_tracking, InvalidTransition
from .round_state import (
    DiscoveryRound, kb_version, ontology_version, load_rounds, append_round,
)
from .prioritizer import (
    select, eligible, score_with_penalty, update_tracking_after_verify,
    novelty_score, relevance_score, evidence_score, structural_score, cost_score,
)
from .metrics import compute_round_metrics, format_round_report
from .stopping import should_stop
from .approval_queue import (
    load_queue, save_queue, add_proposal, has_proposal,
    proposal_ids_approved,
)

POOL_PATH = "data/exports/phase2_candidates.json"
ROUNDS_PATH = "data/exports/discovery_rounds.json"
QUEUE_PATH = "data/exports/discovery_approval_queue.json"


class VerifierUnavailableError(RuntimeError):
    """候选无 verification 缓存且未注入 verify_fn（P1 默认不烧 API）。"""


# ── 模块级工具 ──

def _load_pool(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            return data.get("candidates", []) if isinstance(data, dict) else data
    except Exception:
        return []


def _save_pool(cands: list[dict], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"phase": "phase2_candidates", "schema_version": "2.1",
                   "candidates": cands}, f, ensure_ascii=False, indent=2)


def _build_candidate(raw) -> DiscoveryCandidate:
    """RawCandidate → DiscoveryCandidate（scan→type→filter，与 discover_candidates 同逻辑）。"""
    from .typer import type_candidate, domain_relevance_level
    from .canonical_filter import canonical_match, existing_knowledge_match
    cm = canonical_match(raw.raw_name)
    existing = existing_knowledge_match(raw.raw_name)
    if cm:
        status, ctype, match_name = "ALIAS", "ALIAS", cm
    elif existing:
        status, ctype, match_name = "EXISTING_KNOWLEDGE", "EFFECT", existing
    else:
        status, ctype, match_name = "CANDIDATE", type_candidate(raw.raw_name), None
    rel, rel_score = domain_relevance_level(
        raw.raw_name, [x["evidence"] for x in raw.evidence_samples])
    return DiscoveryCandidate.from_raw(
        raw,
        provenance_extra={"relevance_score": rel_score, "typer_rule": ctype,
                          "evidence_samples": raw.evidence_samples},
        candidate_type=ctype, canonical_match=match_name,
        domain_relevance=rel, status=status,
        evidence=[x["evidence"] for x in raw.evidence_samples],
    )


def _default_scan() -> list:
    """默认 scan：真实 KB + scanner（测试可注入 fake）。"""
    from ..knowledge_base import KnowledgeBase
    from .scanner import scan_kb
    kb = KnowledgeBase()
    try:
        return scan_kb(kb)
    finally:
        kb.close()


def _as_candidate_obj(c: dict) -> DiscoveryCandidate:
    return DiscoveryCandidate(**{k: v for k, v in c.items()
                                 if k in DiscoveryCandidate.__dataclass_fields__})


def _est_queries(c: dict) -> float:
    from .prioritizer import estimate_queries
    return estimate_queries(c)


def _est_cost(c: dict) -> float:
    return _est_queries(c) * 0.02   # 估算：~0.02 单位/query（可配置；仅展示）


class DiscoveryController:
    """多轮 discovery controller（P1：稳定性优先，verify/scan 可插拔）。"""

    def __init__(self, pool_path: str = POOL_PATH, rounds_path: str = ROUNDS_PATH,
                 queue_path: str = QUEUE_PATH, top_n: int = 8,
                 max_per_type: dict | None = None, weights: dict | None = None,
                 verify_fn=None, scan_fn=None, build_fn=None):
        self.pool_path = pool_path
        self.rounds_path = rounds_path
        self.queue_path = queue_path
        self.top_n = top_n
        self.max_per_type = max_per_type
        self.weights = weights
        self._verify_fn = verify_fn
        self._scan_fn = scan_fn or _default_scan
        self._build_fn = build_fn or _build_candidate
        self.pool = _load_pool(pool_path)

    # ── 内部工具 ──

    def _scan_and_merge(self) -> tuple[int, int]:
        """scan → type/filter → merge_pool（内存；落盘由 save 统一处理）。

        返回 (candidates_scanned, new_candidates)。
        """
        raws = self._scan_fn()
        built = [self._build_fn(r) for r in raws]
        before = {c["candidate_id"] for c in self.pool}
        from .candidate import merge_pool
        self.pool = merge_pool(self.pool, built)
        after = {c["candidate_id"] for c in self.pool}
        return len(raws), len(after - before)

    def _verify(self, c: dict) -> str:
        """verify：注入的 verify_fn 优先；否则读 verification 缓存（不烧 API）。"""
        if self._verify_fn is not None:
            return self._verify_fn(c)
        v = (c.get("provenance") or {}).get("verification") or {}
        verdict = v.get("verdict")
        if verdict:
            return verdict
        raise VerifierUnavailableError(
            f"{c.get('raw_name')} 无 verification 缓存且未注入 verify_fn（P1 默认不烧 API）")

    def _queue_proposal(self, c: dict, round_id: int, round_: DiscoveryRound) -> None:
        """VALIDATED → build proposal → approval queue（不自动 approve；查重防重复生成）。"""
        queue = load_queue(self.queue_path)
        if has_proposal(queue, c["candidate_id"]):
            return  # invariant ⑦：已有 PENDING/APPROVED proposal 不重复生成
        from .promoter import build_proposal
        verification = (c.get("provenance") or {}).get("verification") or {}
        proposal = build_proposal(_as_candidate_obj(c), verification)
        item = add_proposal(queue, proposal.to_dict(), round_id)
        if item:
            save_queue(queue, self.queue_path)
            round_.proposal_ids_created.append(item["proposal_id"])
            round_.promotions.append(c["raw_name"])

    def _enqueue_unqueued_validated(self, round_id: int, round_: DiscoveryRound) -> None:
        """补录：池里已 VALIDATED 但 queue 无 proposal 的候选 → 本轮入队（不重复验证）。"""
        for c in self.pool:
            if c.get("status") != "VALIDATED":
                continue
            queue = load_queue(self.queue_path)
            if has_proposal(queue, c["candidate_id"]):
                continue
            self._queue_proposal(c, round_id, round_)

    def _handle_verdict(self, c: dict, verdict: str, round_id: int,
                        round_: DiscoveryRound) -> None:
        """verdict 分支：状态推进 + tracking（invariant ④⑤）。"""
        track = get_tracking(c)
        if verdict == "VALIDATED":
            c["status"] = "VALIDATED"
            self._queue_proposal(c, round_id, round_)
        elif verdict == "NEED_MORE_EVIDENCE":
            c["status"] = "NEED_MORE_EVIDENCE"   # 自动重入下一轮（prioritizer 降权处理）
        elif verdict == "SEARCH_INCONCLUSIVE":
            track["search_inconclusive_retries"] = \
                int(track.get("search_inconclusive_retries", 0)) + 1
            if track["search_inconclusive_retries"] >= 2:
                # invariant ⑤：第二次无解 → 冻结（不再自动进 prioritizer）
                c["status"] = "SEARCH_INCONCLUSIVE"
                try:
                    _as_candidate_obj(c).transition("SEARCH_INCONCLUSIVE_FROZEN")
                    c["status"] = "SEARCH_INCONCLUSIVE_FROZEN"
                    round_.verification_results[c["raw_name"]] = "SEARCH_INCONCLUSIVE_FROZEN"
                except InvalidTransition:
                    round_.candidate_errors.append(
                        f"{c.get('raw_name')}: 无法冻结（状态 {c.get('status')}）")
            else:
                c["status"] = "SEARCH_INCONCLUSIVE"

    # ── 只读规划（plan-only 用；绝不写任何文件）──

    def plan_round(self) -> dict:
        """下一轮规划（真正只读）。不修改任何候选/不写任何文件。

        返回：eligible 数 / 排序（含五分量）/ 选中（含预计成本）。
        """
        scanned, new = self._scan_and_merge()
        elig = [c for c in self.pool if eligible(c)]
        scored = sorted(elig, key=lambda c: score_with_penalty(c, self.weights), reverse=True)
        selected = select(self.pool, top_n=self.top_n, max_per_type=self.max_per_type,
                          weights=self.weights)
        sel_keys = {c["candidate_id"] for c in selected}
        ranked = []
        for c in scored:
            ranked.append({
                "raw_name": c["raw_name"],
                "candidate_type": c["candidate_type"],
                "status": c["status"],
                "selected": c["candidate_id"] in sel_keys,
                "score": round(score_with_penalty(c, self.weights), 3),
                "components": {
                    "novelty": round(novelty_score(c), 2),
                    "relevance": round(relevance_score(c), 2),
                    "evidence": round(evidence_score(c), 2),
                    "structural": round(structural_score(c), 2),
                    "cost": round(cost_score(c), 2),
                },
                "est_queries": _est_queries(c),
                "est_cost": round(_est_cost(c), 3),
            })
        return {
            "scanned": scanned,
            "new_candidates": new,
            "eligible": len(elig),
            "selected_count": len(selected),
            "selected": [c["raw_name"] for c in selected],
            "ranked": ranked,
            "est_queries_total": sum(_est_queries(c) for c in selected),
            "est_cost_total": round(sum(_est_cost(c) for c in selected), 3),
            "read_only": True,
        }

    # ── 一轮执行 ──

    def run_round(self, round_id: int | None = None) -> tuple[DiscoveryRound, bool, str | None]:
        """执行一轮完整 loop（见模块 docstring 链路）。返回 (round, stop, stop_reason)。"""
        rid = round_id if round_id is not None else self._next_round_id()
        r = DiscoveryRound(round_id=rid,
                           kb_version_before=kb_version(),
                           ontology_version_before=ontology_version())

        # 1. scan → merge（invariant ⑥：冻结状态由 eligible 排除，不重新选择）
        r.candidates_scanned, r.new_candidates = self._scan_and_merge()

        # 2. eligible + prioritizer.select（硬配额）
        selected = select(self.pool, top_n=self.top_n, max_per_type=self.max_per_type,
                          weights=self.weights)
        r.selected_candidates = [c["raw_name"] for c in selected]

        # 3. 逐候选 verify（invariant ②：单候选失败不整轮失败）
        for c in selected:
            try:
                verdict = self._verify(c)
            except Exception as e:                      # noqa: BLE001
                r.candidate_errors.append(f"{c.get('raw_name')}: {e}")
                continue
            r.verification_results[c["raw_name"]] = verdict
            # invariant ③：同轮重跑不重复计数（last_selected_round == rid → 已处理）
            if get_tracking(c).get("last_selected_round") != rid:
                update_tracking_after_verify(c, rid)
            self._handle_verdict(c, verdict, rid, r)

        # 4. 未入队 VALIDATED 补录（已有 proposal 不重复生成）
        self._enqueue_unqueued_validated(rid, r)

        # 5. 落盘：候选池（status/tracking 变化）+ round manifest
        _save_pool(self.pool, self.pool_path)
        r.proposal_ids_approved = proposal_ids_approved(load_queue(self.queue_path))
        r.kb_version_after = kb_version()
        r.ontology_version_after = ontology_version()
        append_round(r, self.rounds_path)

        # 6. metrics + stopping check
        self._metrics = compute_round_metrics(r)
        rounds = load_rounds(self.rounds_path)
        stop, reason = should_stop(rounds)
        if stop:
            r.stop_reason = reason
            append_round(r, self.rounds_path)   # 幂等更新 stop_reason
        return r, stop, reason

    def _next_round_id(self) -> int:
        rounds = load_rounds(self.rounds_path)
        return (rounds[-1].round_id + 1) if rounds else 1

    def run(self, max_rounds: int) -> list[DiscoveryRound]:
        """连续跑多轮（stop 触发即停）。"""
        results = []
        for _ in range(max_rounds):
            r, stop, reason = self.run_round()
            results.append(r)
            if stop:
                break
        return results

    # ── 报告 ──

    def last_report(self) -> str:
        rounds = load_rounds(self.rounds_path)
        if not rounds:
            return "（无 round 记录）"
        r = rounds[-1]
        return format_round_report(r, compute_round_metrics(r))
