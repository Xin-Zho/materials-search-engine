"""`tools/verify_artifacts.py` 的回归测试（产物溯源核对）。

守的不变量：
  1. **只读**：核对不得改动任何产物字节（脱钩本身就是证据，不能被"顺手修好"）
  2. 四种判定必须分得开：OK / MISMATCH / MISSING / UNRESOLVED / NO_PROVENANCE
  3. 路径解析要同时吃"相对产物目录"和"相对仓库根"两种历史写法
  4. 不确定时不许猜：兜底候选命中多个 -> UNRESOLVED，不当成通过
  5. 退出码只由 MISMATCH / MISSING 决定（"没记录"是历史债，不该让 CI 永远红）
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(BASE, "tools", "verify_artifacts.py")

_spec = importlib.util.spec_from_file_location("verify_artifacts", TOOL)
va = importlib.util.module_from_spec(_spec)
sys.modules["verify_artifacts"] = va
_spec.loader.exec_module(va)


def _write(tmp_path, payload, name="artifact.json"):
    p = tmp_path / name
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False)
    return str(p)


def _target(tmp_path, content=b"x\n"):
    p = tmp_path / "input.jsonl"
    p.write_bytes(content)
    return str(p)


# ══ 1. 基本判定 ════════════════════════════════════════════════════════
def test_ok_when_sha_matches(tmp_path):
    t = _target(tmp_path)
    a = _write(tmp_path, {"inputs": {"concepts_file": t,
                                     "concepts_sha256": va._sha256(t)}})
    r = va.audit_artifact(a)
    assert r["status"] == va.OK
    assert r["checks"][0]["how"].startswith("explicit:")


def test_mismatch_when_input_changed_after_the_fact(tmp_path):
    t = _target(tmp_path)
    a = _write(tmp_path, {"inputs": {"concepts_file": t,
                                     "concepts_sha256": "0" * 64}})
    assert va.audit_artifact(a)["status"] == va.MISMATCH


def test_missing_when_certified_file_is_gone(tmp_path):
    """实测过的脱钩形态：产物认证了一份已不存在的文件。"""
    t = _target(tmp_path)
    a = _write(tmp_path, {"inputs": {"concepts_file": t,
                                     "concepts_sha256": va._sha256(t)}})
    os.remove(t)
    r = va.audit_artifact(a)
    assert r["status"] == va.MISSING
    assert "不存在" in r["checks"][0]["note"]


def test_no_provenance_when_artifact_records_nothing(tmp_path):
    a = _write(tmp_path, {"predictor": "x", "n": 3})
    assert va.audit_artifact(a)["status"] == va.NO_PROVENANCE


def test_unresolved_when_no_path_and_no_convention(tmp_path):
    """没有路径也没有兜底映射 -> 必须说"不确定"，不能当成通过。"""
    a = _write(tmp_path, {"inputs": {"weird_thing_sha256": "a" * 64}})
    r = va.audit_artifact(a)
    assert r["status"] == va.UNRESOLVED
    assert "兜底映射" in r["checks"][0]["note"]


def test_non_json_artifact_does_not_crash(tmp_path):
    p = tmp_path / "broken.json"
    p.write_text("{not json", encoding="utf-8")
    r = va.audit_artifact(str(p))
    assert r["status"] == va.UNRESOLVED
    assert "合法 JSON" in r["checks"][0]["note"]


# ══ 2. 路径解析（两种历史写法都要吃）═══════════════════════════════════
def test_resolve_path_accepts_artifact_relative_and_root_relative(tmp_path):
    sub = tmp_path / "datasets"
    sub.mkdir()
    (sub / "a.jsonl").write_bytes(b"1")
    assert va._resolve_path("a.jsonl", str(sub)) == str(sub / "a.jsonl")
    # 仓库根相对写法：用仓库内真实文件，base 故意指向别处
    # （不要用 os.path.relpath 造这个用例 —— 跨盘符会 ValueError，实测踩过）
    rel = "datasets/photopolymerization_v1/benchmark.yaml"
    assert va._resolve_path(rel, str(sub)) == os.path.join(va.ROOT, rel)


def test_resolve_path_falls_back_to_joined_path_when_absent(tmp_path):
    got = va._resolve_path("nope.jsonl", str(tmp_path))
    assert got == str(tmp_path / "nope.jsonl")


def test_ambiguous_convention_is_not_guessed(monkeypatch):
    """兜底候选命中多个 -> UNRESOLVED（猜错了比不猜更糟）。"""
    two = ["datasets/photopolymerization_v1/benchmark.yaml",
           "datasets/photopolymerization_v1/scope_allowlist.yaml"]
    for rel in two:
        assert os.path.exists(os.path.join(va.ROOT, rel)), "测试前提：两份文件都存在"
    monkeypatch.setitem(va.BY_CONVENTION, "ambiguous_sha256", two)
    p, how, note = va.resolve_target("ambiguous_sha256", {}, BASE)
    assert p is None and how == "ambiguous" and "命中多个" in note


def test_convention_used_when_unique(monkeypatch, tmp_path):
    one = ["datasets/photopolymerization_v1/benchmark.yaml"]
    monkeypatch.setitem(va.BY_CONVENTION, "unique_sha256", one)
    p, how, _ = va.resolve_target("unique_sha256", {}, BASE)
    assert how == "convention" and p.endswith("benchmark.yaml")


# ══ 3. 只读与汇总 ══════════════════════════════════════════════════════
def test_audit_is_read_only(tmp_path):
    t = _target(tmp_path)
    a = _write(tmp_path, {"inputs": {"concepts_file": t,
                                     "concepts_sha256": "0" * 64}})
    before = open(a, "rb").read()
    va.audit_artifact(a)
    assert open(a, "rb").read() == before, "核对改动了产物 —— 那会销毁脱钩证据"


def test_summarize_counts_only_real_problems_as_action():
    results = [{"status": va.OK}, {"status": va.MISMATCH},
               {"status": va.NO_PROVENANCE}, {"status": va.UNRESOLVED},
               {"status": va.MISSING}]
    s = va.summarize(results)
    assert s["needs_action"] == 2          # MISMATCH + MISSING
    assert s[va.OK] == 1 and s[va.NO_PROVENANCE] == 1


def test_scan_reports_all_json_in_dir(tmp_path):
    _write(tmp_path, {"a": 1}, name="one.json")
    _write(tmp_path, {"b": 2}, name="two.json")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    got = va.scan(str(tmp_path))
    assert sorted(os.path.basename(r["file"]) for r in got) == ["one.json", "two.json"]


# ══ 4. 当前仓库的真实状态（已知债务要显式记录，不能被静默忽略）══════════
def test_current_dataset_has_no_new_mismatch():
    """除已知的 validation_report_v1 脱钩外，不应出现新的 MISMATCH。

    已知债务：validation_report_v1.json 认证了一份已被后续写入取代的候选文件
    （时间差 32 秒）。它**保持原样**，因为脱钩本身就是证据。
    """
    known = {"validation_report_v1.json"}
    got = [r for r in va.scan() if r["status"] == va.MISMATCH]
    assert {os.path.basename(r["file"]) for r in got} <= known, (
        "出现了新的溯源脱钩: %s" % [r["file"] for r in got])


def test_edge_artifact_is_fully_self_certifying():
    """边层产物必须三链齐全：输入 concepts / spec / 工具自身。"""
    path = os.path.join(va.DATASET, "edge_new_link_candidates_v2.json")
    if not os.path.exists(path):
        import pytest
        pytest.skip("尚未落盘边层候选清单")
    r = va.audit_artifact(path)
    assert r["status"] == va.OK, r["checks"]
    keys = {c["key"] for c in r["checks"]}
    assert {"inputs.concepts_sha256", "inputs.tool_sha256", "spec_sha256"} <= keys
