"""R06 处置引擎测试（tools/disposition_r06_funnel.py）。

覆盖规则引擎的三个分支：R1 外部判 R、R2 KEEP 簇 U 晋升、R4 证据不足挂起；
以及 --apply 的 KB 写入（papers + topic_papers、幂等、快照）。
"""
from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import disposition_r06_funnel as disp  # noqa: E402


class _argv:
    def __init__(self, argv):
        self.argv, self._orig = argv, None

    def __enter__(self):
        self._orig = sys.argv
        sys.argv = self.argv
        return self

    def __exit__(self, *exc):
        sys.argv = self._orig
        return False


def _run(*extra):
    argv = ["disposition_r06_funnel.py", *extra]
    with contextlib.redirect_stdout(io.StringIO()), _argv(argv):
        try:
            return disp.main()
        except SystemExit as e:
            return int(e.code or 0)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    term = tmp_path / "term"
    term.mkdir()
    # 队列：1 条 U 在 KEEP 簇、1 条 U 在 FAIL 簇、1 条 no_abstract
    (term / "promotion_pending_queue.json").write_text(json.dumps({"entries": {
        "2-s2.0-keep": {"label": "UNCERTAIN", "doi": None, "stages": {"S7": "UNCERTAIN"},
                        "reason_code": "u_deferred"},
        "2-s2.0-fail": {"label": "UNCERTAIN", "doi": None, "stages": {"S7": "UNCERTAIN"},
                        "reason_code": "u_deferred"},
        "2-s2.0-noab": {"label": "RELEVANT", "doi": "10.1/noab", "stages": {"S7": "RELEVANT"},
                        "reason_code": "no_abstract"},
    }}), encoding="utf-8")
    # 19 篇外部判 R（示意 2 篇）
    (term / "r06_retrieved_relevant_detail.json").write_text(json.dumps([
        {"paper_id": "W111", "doi": "10.1/w111", "in_kb": True, "title": "in kb"},
        {"paper_id": "W222", "doi": "10.1/w222", "in_kb": False, "title": "ext R 1"},
        {"paper_id": "W333", "doi": "10.1/w333", "in_kb": False, "title": "ext R 2"},
    ]), encoding="utf-8")
    (term / "s7_candidate_set.json").write_text(json.dumps({"papers": [
        {"key": "2-s2.0-keep", "cluster_id": "C-001", "title": "keep u"},
        {"key": "2-s2.0-fail", "cluster_id": "C-002", "title": "fail u"},
        {"key": "2-s2.0-noab", "cluster_id": "C-001", "title": "no abstract"},
    ]}), encoding="utf-8")
    (term / "s7_community_verdict.json").write_text(json.dumps({"clusters": {
        "C-001": {"cluster_id": "C-001", "decision": "KEEP"},
        "C-002": {"cluster_id": "C-002", "decision": "FAIL"},
    }}), encoding="utf-8")
    # 空摘要缓存
    (tmp_path / "openalex_cache.json").write_text("{}", encoding="utf-8")
    # KB
    db = tmp_path / "kb.db"
    con = sqlite3.connect(db)
    con.executescript("""
    create table papers (paper_id, doi, openalex_id, scopus_eid, title, abstract, source_json, created_at);
    create table topic_papers (topic_id, paper_id, relevance_label, label_source, promotion_status,
                               first_seen_run, evidence_json, created_at);
    """)
    con.commit()
    con.close()

    monkeypatch.setattr(disp, "TERM", term)
    monkeypatch.setattr(disp, "QUEUE_PATH", term / "promotion_pending_queue.json")
    monkeypatch.setattr(disp, "DETAIL_PATH", term / "r06_retrieved_relevant_detail.json")
    monkeypatch.setattr(disp, "CANDIDATE_PATH", term / "s7_candidate_set.json")
    monkeypatch.setattr(disp, "VERDICT_PATH", term / "s7_community_verdict.json")
    monkeypatch.setattr(disp, "LOG_PATH", term / "r06_disposition_log.json")
    monkeypatch.setattr(disp, "KB_DB", db)
    monkeypatch.setattr(disp, "OA_CACHE", tmp_path / "openalex_cache.json")
    return {"db": db, "log": term / "r06_disposition_log.json"}


def test_rules_dryrun(env):
    assert _run() == 0
    log = json.loads(env["log"].read_text(encoding="utf-8"))
    props = {p["key"]: p for p in log["entries"][-1]["proposals"]}
    assert props["W222"]["rule"] == "R1_external_audit_R" and props["W222"]["action"] == "promote"
    assert props["2-s2.0-keep"]["rule"] == "R2_keep_cluster_u" and props["2-s2.0-keep"]["action"] == "promote_tentative"
    assert props["2-s2.0-fail"]["rule"] == "R4_defer" and props["2-s2.0-fail"]["action"] == "defer"
    assert props["2-s2.0-noab"]["rule"] == "R4_defer"  # 补不到摘要且无外部判定
    # dry-run 不写 KB
    con = sqlite3.connect(env["db"])
    assert con.execute("select count(*) from papers").fetchone()[0] == 0
    con.close()


def test_apply_writes_kb_idempotent(env):
    assert _run("--apply") == 0
    con = sqlite3.connect(env["db"])
    assert con.execute("select count(*) from papers").fetchone()[0] == 3  # W222 W333 + keep_u
    row = con.execute(
        "select relevance_label, promotion_status, label_source from topic_papers"
        " where paper_id = 'scopus:2-s2.0-keep'").fetchone()
    assert row == ("UNCERTAIN", "promoted_tentative_u", "U_PROMOTION_RULE_V1")
    con.close()
    # 幂等：再 apply 不重复写
    assert _run("--apply") == 0
    con = sqlite3.connect(env["db"])
    assert con.execute("select count(*) from papers").fetchone()[0] == 3
    con.close()
