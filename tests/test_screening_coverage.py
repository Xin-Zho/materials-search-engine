"""筛选覆盖台账测试（tools/audit_screening_coverage.py）。"""
from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import audit_screening_coverage as cov  # noqa: E402


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


@pytest.fixture()
def cov_env(tmp_path, monkeypatch):
    term = tmp_path / "term"
    term.mkdir()
    (term / "s6_seen_set.json").write_text(json.dumps(
        {"keys": ["10.1/a", "10.1/b", "2-s2.0-x", "W123", "W456"]}), encoding="utf-8")
    (term / "s6_qa_corpus.json").write_text(json.dumps({"papers": [
        {"eid": "2-s2.0-x", "doi": "10.1/A"}]}), encoding="utf-8")
    labels_dir = tmp_path / "labels"
    labels_dir.mkdir()
    ext = labels_dir / "pc_001__X_filled.json"
    ext.write_text(json.dumps([{"paper_id": "W123", "doi": "10.1/ext", "label": "RELEVANT"}]),
                   encoding="utf-8")
    monkeypatch.setattr(cov, "TERM", term)
    monkeypatch.setattr(cov, "QA_CORPORA", [term / "s6_qa_corpus.json"])
    monkeypatch.setattr(cov, "SEEN_PATH", term / "s6_seen_set.json")
    monkeypatch.setattr(cov, "EXTERNAL_LABELS", ext)
    monkeypatch.setattr(cov, "LEDGER_PATH", term / "screening_coverage_ledger.json")
    return term / "screening_coverage_ledger.json"


def _run(*extra):
    argv = ["audit_screening_coverage.py", *extra]
    with contextlib.redirect_stdout(io.StringIO()), _argv(argv):
        try:
            return cov.main()
        except SystemExit as e:
            return int(e.code or 0)


def test_coverage_ledger_invariant(cov_env):
    assert _run() == 0
    led = json.loads(cov_env.read_text(encoding="utf-8"))
    # screened = {10.1/a(doi+eid 双键), 2-s2.0-x}；external 充抵 W123；unscreened = 10.1/b, W456
    assert led["counts"]["seen"] == 5
    assert led["counts"]["screened_qa"] == 2
    assert led["counts"]["screened_external_offset"] == 1
    assert led["counts"]["unscreened"] == 2
    assert led["counts"]["coverage_rate"] == 0.6
    assert led["violations"]["overlap"] == 0


def test_strict_below_min_rate(cov_env):
    assert _run("--strict", "--min-rate", "0.9") == 1
    assert _run("--strict", "--min-rate", "0.5") == 0
