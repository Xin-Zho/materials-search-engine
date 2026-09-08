"""晋升对账不变式测试（tools/audit_promotion_ledger.py）。

覆盖：
1. 跨轮次标签合并（取更优 R > U）与 doi/eid 元数据桥接
2. 原因码判定规则（u_deferred / no_abstract / identity_unresolved）
3. 端到端三方恒等：R/U 判定 = KB 收录 + 待补队列；新发现无着落 → strict 退出码 1，处置后放行
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

import audit_promotion_ledger as ledger  # noqa: E402


def test_collect_judged_merges_best_label(tmp_path):
    (tmp_path / "c1.json").write_text(json.dumps({
        "papers": [{"eid": "2-s2.0-x", "doi": "10.1/A", "abstract": "abs"}]}), encoding="utf-8")
    (tmp_path / "l1.json").write_text(json.dumps({"labels": {"2-s2.0-x": "RELEVANT"}}), encoding="utf-8")
    (tmp_path / "l2.json").write_text(json.dumps({"labels": {"2-s2.0-x": "UNCERTAIN"}}), encoding="utf-8")
    orig_sources, orig_corpora = ledger.QA_SOURCES, ledger.CORPORA
    ledger.QA_SOURCES = [("A", tmp_path / "l1.json"), ("B", tmp_path / "l2.json")]
    ledger.CORPORA = [tmp_path / "c1.json"]
    try:
        judged = ledger.collect_judged()
    finally:
        ledger.QA_SOURCES, ledger.CORPORA = orig_sources, orig_corpora
    assert judged["2-s2.0-x"]["label"] == "RELEVANT"
    assert judged["2-s2.0-x"]["doi"] == "10.1/a"
    assert judged["2-s2.0-x"]["has_abstract"] is True
    assert judged["2-s2.0-x"]["stages"] == {"A": "RELEVANT", "B": "UNCERTAIN"}


def test_reason_code():
    assert ledger.reason_code({"label": "UNCERTAIN"}) == "u_deferred"
    assert ledger.reason_code({"label": "RELEVANT", "has_abstract": False}) == "no_abstract"
    assert ledger.reason_code({"label": "RELEVANT", "has_abstract": True}) == "identity_unresolved"


@pytest.fixture()
def ledger_env(tmp_path, monkeypatch):
    term = tmp_path / "term"
    term.mkdir()
    corpus = term / "s6_qa_corpus.json"
    labels = term / "s6_qa_labels.json"
    corpus.write_text(json.dumps({"papers": [
        {"eid": "E-KB", "doi": "10.1/in-kb", "abstract": "a"},
        {"eid": "E-MISS", "doi": "10.1/missing", "abstract": ""},
        {"eid": "E-U", "doi": "10.1/uncertain", "abstract": "a"},
    ]}), encoding="utf-8")
    labels.write_text(json.dumps({"labels": {
        "E-KB": "RELEVANT", "E-MISS": "RELEVANT", "E-U": "UNCERTAIN"}}), encoding="utf-8")
    db = tmp_path / "kb.db"
    con = sqlite3.connect(db)
    con.execute("create table papers (paper_id, doi, openalex_id, scopus_eid)")
    con.execute("insert into papers values ('openalex:W1', '10.1/IN-KB', 'W1', 'E-KB')")
    con.commit()
    con.close()

    monkeypatch.setattr(ledger, "TERM", term)
    monkeypatch.setattr(ledger, "KB_DB", db)
    monkeypatch.setattr(ledger, "QUEUE_PATH", term / "promotion_pending_queue.json")
    monkeypatch.setattr(ledger, "QA_SOURCES", [("S6", labels)])
    monkeypatch.setattr(ledger, "CORPORA", [corpus])
    return term / "promotion_pending_queue.json"


class _argv:
    """临时替换 sys.argv 的上下文管理器。"""

    def __init__(self, argv):
        self.argv = argv
        self._orig = None

    def __enter__(self):
        self._orig = sys.argv
        sys.argv = self.argv
        return self

    def __exit__(self, *exc):
        sys.argv = self._orig
        return False


def _run_main(*extra: str) -> int:
    argv = ["audit_promotion_ledger.py", *extra]
    with contextlib.redirect_stdout(io.StringIO()), _argv(argv):
        try:
            return ledger.main()
        except SystemExit as e:
            return int(e.code or 0)


def test_invariant_end_to_end(ledger_env):
    assert _run_main() == 0
    queue = json.loads(ledger_env.read_text(encoding="utf-8"))
    assert queue["counts"] == {"judged_RU": 3, "in_kb": 1, "dispositioned": 0,
                               "queued": 2, "newly_queued": 2}
    assert queue["entries"]["E-MISS"]["reason_code"] == "no_abstract"
    assert queue["entries"]["E-U"]["reason_code"] == "u_deferred"


def test_invariant_strict_cycle(ledger_env):
    # 首跑：2 条新发现无着落 → strict 退出码 1（漏水报警）
    assert _run_main("--strict") == 1
    # 复跑（条目已在队列带原因码）→ 无新发现，strict 放行
    assert _run_main("--strict") == 0
    # 已收录论文不得留在队列
    queue = json.loads(ledger_env.read_text(encoding="utf-8"))
    assert "E-KB" not in queue["entries"]
