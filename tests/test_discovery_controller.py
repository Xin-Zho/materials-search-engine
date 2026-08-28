"""Phase 2.1 P1 测试：controller 多轮稳定性（用户验收 6 条全覆盖）。

mock 3 轮场景（用户定）：
    Round 1: A → VALIDATED, B → NEED_MORE_EVIDENCE, C → SEARCH_INCONCLUSIVE
    Round 2: B → NEED_MORE（无新证据）, C → 第二次 SEARCH_INCONCLUSIVE → FROZEN, D → VALIDATED
    Round 3: B 降权重入, C 不再出现, A/D 已有 proposal 不重复验证

断言：A/D proposal_count==1、C FROZEN、B streak>=1 且 score 下降、
      rounds==3、round_id 唯一、manifest 可重放、plan-only 真正只读。
"""

import json
import hashlib
from collections import defaultdict

import pytest

from search_engine.discovery.candidate import RawCandidate, DiscoveryCandidate
from search_engine.discovery.controller import DiscoveryController
from search_engine.discovery.prioritizer import score_candidate, score_with_penalty, get_tracking
from search_engine.discovery.round_state import load_rounds
from search_engine.discovery.approval_queue import load_queue


# ── fixture：tmp 工作区 + mock scan/build/verify ──

TYPES = {"A": "MECHANISM", "B": "MECHANISM", "C": "FORMULATION_STRATEGY",
         "D": "SUB_ROUTE", "E": "EFFECT"}


def _mk_verify(verdict_map):
    """mock verify：按候选名 + 调用次数返回 verdict。verdict_map: name -> list(按调用序)"""
    calls = defaultdict(int)
    def verify(c):
        name = c["raw_name"]
        calls[name] += 1
        seq = verdict_map.get(name)
        if seq is None:
            return "REJECTED"
        return seq[min(calls[name] - 1, len(seq) - 1)]
    return verify


def _mk_scan(names=("A", "B", "C", "D")):
    def scan():
        return [RawCandidate(raw_name=n, kind="mechanism",
                             paper_ids={f"W{i}"}, edge_count=3,
                             evidence_samples=[]) for i, n in enumerate(names)]
    return scan


def _mk_build():
    def build(raw):
        return DiscoveryCandidate.from_raw(
            raw, candidate_type=TYPES.get(raw.raw_name, "EFFECT"),
            status="CANDIDATE", domain_relevance="HIGH",
            provenance_extra={"evidence_samples": []})
    return build


@pytest.fixture
def workdir(tmp_path):
    pool = tmp_path / "pool.json"
    rounds = tmp_path / "rounds.json"
    queue = tmp_path / "queue.json"
    pool.write_text(json.dumps({"candidates": []}), encoding="utf-8")
    rounds.write_text("[]", encoding="utf-8")
    queue.write_text("[]", encoding="utf-8")
    return tmp_path, pool, rounds, queue


def _ctrl(workdir, verify_fn, scan_fn=None, build_fn=None):
    tmp_path, pool, rounds, queue = workdir
    return DiscoveryController(pool_path=str(pool), rounds_path=str(rounds),
                               queue_path=str(queue),
                               verify_fn=verify_fn, scan_fn=scan_fn or _mk_scan(),
                               build_fn=build_fn or _mk_build())


def _file_digest(path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


# ── 验收 ①⑤：mock 连续 3 轮（用户场景全覆盖）──

def test_three_round_mock_loop(workdir):
    tmp_path, pool_f, rounds_f, queue_f = workdir
    verdicts = {
        "A": ["VALIDATED"],                    # round1 一次即过
        "B": ["NEED_MORE_EVIDENCE", "NEED_MORE_EVIDENCE", "NEED_MORE_EVIDENCE"],
        "C": ["SEARCH_INCONCLUSIVE", "SEARCH_INCONCLUSIVE"],   # 第一次失败→重试→第二次→FROZEN
        "D": ["NEED_MORE_EVIDENCE", "VALIDATED"],  # round1 证据不足，round2 重入通过
    }
    ctl = _ctrl(workdir, _mk_verify(verdicts))
    results = ctl.run(max_rounds=3)

    # ① 连续 3 轮
    assert len(results) == 3
    rounds = load_rounds(str(rounds_f))
    assert [r.round_id for r in rounds] == [1, 2, 3]          # round_id 唯一
    # round2 中 C 的 verdict 记为 FROZEN
    assert results[1].verification_results.get("C") == "SEARCH_INCONCLUSIVE_FROZEN"

    # ⑤ A/D proposal 各 1 条（不重复生成）
    queue = load_queue(str(queue_f))
    a_items = [q for q in queue if q.get("raw_name") == "A"
               and q["status"] == "PENDING"]
    d_items = [q for q in queue if q.get("raw_name") == "D"
               and q["status"] == "PENDING"]
    assert len(a_items) == 1
    assert len(d_items) == 1

    # ⑤ C 冻结，不再出现
    pool = json.loads(pool_f.read_text(encoding="utf-8"))["candidates"]
    c_by_name = {c["raw_name"]: c for c in pool}
    assert c_by_name["C"]["status"] == "SEARCH_INCONCLUSIVE_FROZEN"
    assert results[2].selected_candidates.count("C") == 0

    # B 无新增证据 → streak 增长 + score 降权
    b = c_by_name["B"]
    assert b["provenance"]["tracking"]["no_evidence_gain_streak"] >= 1
    s_now = score_with_penalty(b)
    b_no_streak = json.loads(json.dumps(b))
    b_no_streak["provenance"]["tracking"]["no_evidence_gain_streak"] = 0
    assert s_now < score_with_penalty(b_no_streak)
    assert score_candidate(b) == score_candidate(b_no_streak)  # 原始分相同，penalty 造成差异

    # manifest 可重放：每轮有 selected/verification/versions
    for r in rounds:
        assert r.selected_candidates
        assert r.verification_results
        assert r.kb_version_before and r.ontology_version_before


def test_validated_not_reselected_after_queue(workdir):
    """A 已 VALIDATED 且有 proposal → 后续轮不再被 select / 不再验证 / 不重复入队。"""
    tmp_path, pool_f, rounds_f, queue_f = workdir
    verdicts = {"A": ["VALIDATED", "VALIDATED"], "B": ["VALIDATED"]}
    ctl = _ctrl(workdir, _mk_verify(verdicts))
    results = ctl.run(max_rounds=3)

    queue = load_queue(str(queue_f))
    a_items = [q for q in queue if q.get("raw_name") == "A"]
    assert len(a_items) == 1                     # 只入队一次
    assert a_items[0]["status"] == "PENDING"

    # A 只在 round1 被选（round2/3 不在 selected 列表）
    assert results[0].selected_candidates.count("A") == 1
    assert "A" not in results[1].selected_candidates
    assert "A" not in results[2].selected_candidates
    # A 的 verification_attempts 只有 1（同轮幂等 + 后续不重选）
    pool = json.loads(pool_f.read_text(encoding="utf-8"))["candidates"]
    a = next(c for c in pool if c["raw_name"] == "A")
    assert a["provenance"]["tracking"]["verification_attempts"] == 1


# ── 验收 ②：单候选失败不整轮失败 ──

def test_candidate_error_does_not_fail_round(workdir):
    tmp_path, pool_f, rounds_f, queue_f = workdir
    def verify(c):
        if c["raw_name"] == "B":
            raise RuntimeError("boom")
        return "VALIDATED"
    ctl = _ctrl(workdir, verify, scan_fn=_mk_scan(("A", "B")))
    results = ctl.run(max_rounds=1)

    r = results[0]
    assert any("B" in e and "boom" in e for e in r.candidate_errors)
    assert "A" in r.verification_results          # 其他候选继续
    assert "B" in r.selected_candidates           # B 被选了但失败了


# ── 验收 ③：同 round_id 重跑幂等 ──

def test_rerun_same_round_idempotent(workdir):
    tmp_path, pool_f, rounds_f, queue_f = workdir
    verdicts = {"A": ["VALIDATED"], "B": ["NEED_MORE_EVIDENCE"]}
    ctl = _ctrl(workdir, _mk_verify(verdicts))
    ctl.run_round(round_id=1)
    ctl.run_round(round_id=1)                     # 同 id 重跑

    rounds = load_rounds(str(rounds_f))
    assert [r.round_id for r in rounds] == [1]    # manifest 不双写
    pool = json.loads(pool_f.read_text(encoding="utf-8"))["candidates"]
    b = next(c for c in pool if c["raw_name"] == "B")
    assert b["provenance"]["tracking"]["verification_attempts"] == 1  # 不重复计数
    queue = load_queue(str(queue_f))
    assert len([q for q in queue if q.get("raw_name") == "A"]) == 1   # 不重复入队


# ── 验收 ④：NEED_MORE 自动重入 + 无增益 streak 增长 ──

def test_need_more_reenrolled_and_streak(workdir):
    tmp_path, pool_f, rounds_f, queue_f = workdir
    verdicts = {"A": ["NEED_MORE_EVIDENCE"]}
    ctl = _ctrl(workdir, _mk_verify(verdicts))
    ctl.run_round(round_id=1)
    pool = json.loads(pool_f.read_text(encoding="utf-8"))["candidates"]
    a = next(c for c in pool if c["raw_name"] == "A")
    assert a["status"] == "NEED_MORE_EVIDENCE"
    # 无新证据 → streak=1（第二轮验证后）
    ctl.run_round(round_id=2)
    pool = json.loads(pool_f.read_text(encoding="utf-8"))["candidates"]
    a = next(c for c in pool if c["raw_name"] == "A")
    assert a["provenance"]["tracking"]["no_evidence_gain_streak"] == 1
    assert "A" in load_rounds(str(rounds_f))[1].selected_candidates  # 自动重入


# ── 验收 ⑥：plan-only 真正只读 ──

def test_plan_only_truly_read_only(workdir):
    tmp_path, pool_f, rounds_f, queue_f = workdir
    # 预置一个候选，验证 plan-only 不改它的 status / retry
    pool = [{
        "candidate_id": "B", "raw_name": "B",
        "candidate_type": "MECHANISM", "status": "CANDIDATE",
        "domain_relevance": "HIGH", "independent_paper_count": 3,
        "source": "scanner", "canonical_match": None,
        "provenance": {"tracking": {"search_inconclusive_retries": 0}},
    }]
    pool_f.write_text(json.dumps({"candidates": pool}), encoding="utf-8")

    d_before = {p.name: _file_digest(p) for p in (pool_f, rounds_f, queue_f)}
    ctl = _ctrl(workdir, _mk_verify({}), scan_fn=_mk_scan(()))   # 空扫描，B 全来自预置池
    plan = ctl.plan_round()

    assert plan["read_only"] is True
    assert plan["eligible"] == 1
    assert plan["selected"] == ["B"]
    assert plan["ranked"][0]["components"].keys() == {
        "novelty", "relevance", "evidence", "structural", "cost"}
    assert "est_queries" in plan["ranked"][0] and "est_cost" in plan["ranked"][0]

    # 磁盘零变化（status / retry_count / verify run / manifest / queue 全不动）
    for p in (pool_f, rounds_f, queue_f):
        assert _file_digest(p) == d_before[p.name], f"{p.name} 被 plan-only 修改了"


def test_plan_only_does_not_touch_verification_cache(workdir):
    """plan-only 即使候选有 verification 缓存也不写任何东西（与缓存同文件 → 覆盖断言）。"""
    tmp_path, pool_f, rounds_f, queue_f = workdir
    pool = [{
        "candidate_id": "A", "raw_name": "A",
        "candidate_type": "MECHANISM", "status": "VALIDATED",
        "domain_relevance": "HIGH", "independent_paper_count": 4,
        "source": "scanner", "canonical_match": None,
        "provenance": {"verification": {"verdict": "VALIDATED",
                                        "causal_chain": []}},
    }]
    pool_f.write_text(json.dumps({"candidates": pool}), encoding="utf-8")
    before = pool_f.read_bytes()
    ctl = _ctrl(workdir, _mk_verify({}), scan_fn=_mk_scan(("A",)))
    ctl.plan_round()
    assert pool_f.read_bytes() == before


# ── 补录：已 VALIDATED 但无 proposal 的候选 ──

def test_unqueued_validated_gets_enqueued(workdir):
    """池里已有 VALIDATED（如人工 verify 的）但 queue 无 proposal → 本轮补录入队。"""
    tmp_path, pool_f, rounds_f, queue_f = workdir
    pool = [{
        "candidate_id": "X", "raw_name": "X",
        "candidate_type": "MECHANISM", "status": "VALIDATED",
        "domain_relevance": "HIGH", "independent_paper_count": 4,
        "source": "scanner", "canonical_match": None,
        "provenance": {"verification": {"verdict": "VALIDATED",
                                        "causal_chain": [],
                                        "direct_target_paper_count": 3}},
    }]
    pool_f.write_text(json.dumps({"candidates": pool}), encoding="utf-8")
    ctl = _ctrl(workdir, _mk_verify({}), scan_fn=_mk_scan(()))   # 空扫描
    r, _, _ = ctl.run_round(round_id=1)

    queue = load_queue(str(queue_f))
    assert len([q for q in queue if q["candidate_id"] == "X"]) == 1
    assert r.proposal_ids_created == [f"X::{1}"] or len(r.proposal_ids_created) == 1


def test_approve_queue_item_by_item(workdir):
    """approval 逐条：只批 PENDING；重复批准拒绝。"""
    tmp_path, pool_f, rounds_f, queue_f = workdir
    from search_engine.discovery.approval_queue import save_queue, approve_item, reject_item
    queue = [{"candidate_id": "A", "proposal_id": "A::1", "created_round": 1,
              "candidate_type": "MECHANISM", "action": "NEW_TOP_LEVEL_NODE",
              "status": "PENDING", "proposal": {}}]
    save_queue(queue, str(queue_f))
    q = load_queue(str(queue_f))
    ok, msg = approve_item(q, "A::1")
    assert ok
    ok2, _ = approve_item(q, "A::1")          # 已 APPROVED 不能再批
    assert not ok2
    assert q[0]["status"] == "APPROVED"
