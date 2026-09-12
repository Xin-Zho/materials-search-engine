"""P4-1A 抽取与审计的回归测试（**不调用 LLM**、不依赖真数据集）。

覆盖三类风险：
  1. LLM 输出不可信 -> 校验层必须真的清洗（类型/词表/悬空端点/自环/重复）
  2. 协议与产物脱钩 -> prompt 改了就**必须**拒绝运行
  3. 时相污染 -> 审计器必须能真的抓出「用了预测窗口之后才出现的术语」
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(BASE, "tools")
for p in (BASE, TOOLS):
    if p not in sys.path:
        sys.path.insert(0, p)


def _load(name, fname):
    spec = importlib.util.spec_from_file_location(name, os.path.join(TOOLS, fname))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ext = _load("extract_concepts_p4_1a", "extract_concepts_p4_1a.py")
aud = _load("audit_extraction_quality", "audit_extraction_quality.py")


# ══ 1. LLM 输出解析与清洗 ════════════════════════════════════════════════
def test_parse_json_content_strips_markdown_fences():
    """prompt 说了不要围栏，但模型仍可能加 —— 必须能剥掉。"""
    assert ext.parse_json_content('```json\n{"concepts": []}\n```') == {"concepts": []}
    assert ext.parse_json_content('```\n{"a": 1}\n```') == {"a": 1}
    assert ext.parse_json_content('{"a": 1}') == {"a": 1}
    with pytest.raises(Exception):
        ext.parse_json_content("not json at all")


@pytest.mark.parametrize("raw,expect", [
    ("Thiol-Ene Photopolymerization", "thiol-ene photopolymerization"),
    ("  Polymerization   Shrinkage  ", "polymerization shrinkage"),
    ("shrinkage stress.", "shrinkage stress"),
    ("Dental Composite,", "dental composite"),
    ("x", None),                       # 太短
    ("", None),
    ("12345", None),                   # 纯数字
    ("1.5 / 2.0", None),               # 纯数字符号
    (None, None),
    ("a" * 81, None),                  # 过长
])
def test_normalize_concept_name(raw, expect):
    assert ext.normalize_concept_name(raw) == expect


def test_normalize_does_not_merge_synonyms():
    """规范化只做机械处理 —— **不做同义合并**（合并属下游图任务且不可逆）。"""
    a = ext.normalize_concept_name("thiol-ene photopolymerization")
    b = ext.normalize_concept_name("thiol ene photopolymerisation")
    assert a != b


def test_validate_payload_drops_bad_type_and_bad_name():
    payload = {
        "concepts": [
            {"type": "challenge", "name": "shrinkage stress", "evidence": "e"},
            {"type": "nonsense", "name": "should be dropped", "evidence": "e"},
            {"type": "material", "name": "!!", "evidence": "e"},
            "not a dict",
        ],
        "relations": [],
    }
    clean, issues = ext.validate_payload(payload)
    assert [c["name"] for c in clean["concepts"]] == ["shrinkage stress"]
    assert issues["CONCEPT_BAD_TYPE"] == 1
    assert issues["CONCEPT_BAD_NAME"] == 1
    assert issues["CONCEPT_NOT_OBJECT"] == 1


def test_validate_payload_relation_vocabulary_is_enforced_not_dropped():
    """词表外的关系降级为 ``other`` 而**不丢弃** —— 信息不丢，但图可分析。"""
    payload = {
        "concepts": [{"type": "direction", "name": "a x"}, {"type": "challenge", "name": "b y"}],
        "relations": [{"source": "a x", "relation": "revolutionizes", "target": "b y"}],
    }
    clean, issues = ext.validate_payload(payload)
    assert clean["relations"] == [{"source": "a x", "relation": "other",
                                   "target": "b y"}]
    assert issues["RELATION_OUT_OF_VOCAB"] == 1


def test_validate_payload_drops_dangling_and_self_loop_and_dupes():
    payload = {
        "concepts": [{"type": "direction", "name": "a x"},
                     {"type": "challenge", "name": "b y"}],
        "relations": [
            {"source": "a x", "relation": "addresses", "target": "not declared"},
            {"source": "a x", "relation": "addresses", "target": "a x"},
            {"source": "a x", "relation": "addresses", "target": "b y"},
            {"source": "a x", "relation": "addresses", "target": "b y"},
        ],
    }
    clean, issues = ext.validate_payload(payload)
    assert len(clean["relations"]) == 1
    assert issues["RELATION_UNDECLARED_ENDPOINT"] == 1
    assert issues["RELATION_SELF_LOOP"] == 1
    assert issues["RELATION_DUPLICATE"] == 1


def test_validate_payload_dedupes_same_type_same_name():
    payload = {
        "concepts": [{"type": "material", "name": "resin"},
                     {"type": "material", "name": "Resin "},
                     {"type": "challenge", "name": "resin"}],
        "relations": [],
    }
    clean, issues = ext.validate_payload(payload)
    # 同类型同名去重；不同类型同名保留（"resin" 既是材料也是挑战是合法的）
    assert len(clean["concepts"]) == 2
    assert issues["CONCEPT_DUPLICATE"] == 1


def test_validate_payload_rejects_non_object():
    clean, issues = ext.validate_payload(["nope"])
    assert clean is None and issues == {"NOT_AN_OBJECT": 1}


# ══ 2. prompt 装载与协议冻结 ═════════════════════════════════════════════
def test_load_prompt_parses_system_and_user():
    sys_p, tpl = ext.load_prompt()
    assert "concept" in sys_p.lower()
    assert "{title}" in tpl and "{abstract}" in tpl and "{year}" in tpl
    # 关系封闭词表必须写在 system 里（否则模型会自造关系）
    for rel in ext.RELATION_VOCAB:
        assert rel in sys_p, f"prompt 未声明关系 {rel}"


def test_build_user_message_truncates_and_marks():
    tpl = "T:{title}\nY:{year}\nA:{abstract}"
    rec = {"title": "t", "year": 2015, "abstract": "x" * 100}
    out = ext.build_user_message(tpl, rec, abstract_max=40)
    assert len(out) < 200
    assert "[TRUNCATED BY PIPELINE]" in out


def test_prompt_edit_requires_refreeze(tmp_path, monkeypatch):
    """改了 prompt 不重新冻结 -> ``--live`` 必须**拒绝运行**。

    这是本项目「先冻结再跑」纪律的核心：产物必须能追溯到确定的 prompt 版本。
    """
    prompt = tmp_path / "p.md"
    prompt.write_text("## SYSTEM\nS\n## USER\n```\nT:{title} Y:{year} A:{abstract}\n```\n",
                      encoding="utf-8")
    spec = tmp_path / "spec.yaml"
    monkeypatch.setattr(ext, "PROMPT_PATH", prompt)
    monkeypatch.setattr(ext, "SPEC_PATH", spec)

    ext.freeze_spec("m", 0.0, 100, 100, tmp_path / "sample.jsonl")
    assert spec.exists()
    got = ext.assert_spec_fresh()
    assert got["model"] == "m"

    prompt.write_text(prompt.read_text(encoding="utf-8") + "\n# 又改了一句\n",
                      encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        ext.assert_spec_fresh()
    assert "未重新冻结" in str(e.value)


def test_assert_spec_fresh_refuses_when_not_frozen(tmp_path, monkeypatch):
    monkeypatch.setattr(ext, "SPEC_PATH", tmp_path / "missing.yaml")
    monkeypatch.setattr(ext, "PROMPT_PATH",
                        tmp_path / "x.md")
    (tmp_path / "x.md").write_text("## SYSTEM\na\n## USER\n```\n{title}{year}{abstract}\n```",
                                   encoding="utf-8")
    with pytest.raises(SystemExit):
        ext.assert_spec_fresh()


def test_frozen_spec_records_prompt_and_sample_hashes(tmp_path, monkeypatch):
    prompt = tmp_path / "p.md"
    prompt.write_text("## SYSTEM\nS\n## USER\n```\n{title}{year}{abstract}\n```",
                      encoding="utf-8")
    sample = tmp_path / "s.jsonl"
    sample.write_text('{"paper_uid":"x"}\n', encoding="utf-8")
    monkeypatch.setattr(ext, "PROMPT_PATH", prompt)
    monkeypatch.setattr(ext, "SPEC_PATH", tmp_path / "spec.yaml")
    spec = ext.freeze_spec("deepseek-chat", 0.0, 2000, 8000, sample)
    assert len(spec["prompt_sha256"]) == 64
    assert len(spec["sample_sha256"]) == 64
    assert spec["policy"]["eval_side_never_extracted"] is True


# ══ 3. 失败分类（严格分开，不混为一谈）═══════════════════════════════════
def _spec():
    return {"model": "m", "spec_version": "v", "prompt_sha256": "0" * 64,
            "temperature": 0.0, "max_tokens": 10}


def _rec():
    return {"paper_uid": "openalex:W1", "sample_layer": "A", "year": 2010,
            "primary_topic": "t"}


def test_row_classification_separates_failure_types():
    r_ok = ext._row(_rec(), ext.ST_OK, 0, None, _spec(),
                    clean={"concepts": [{"type": "direction", "name": "a b",
                                         "evidence": None}],
                           "relations": [], "note": None})
    assert r_ok["status"] == "OK" and r_ok["n_concepts"] == 1
    for st in (ext.ST_JSON_INVALID, ext.ST_TRUNCATED, ext.ST_API_ERROR,
               ext.ST_SCHEMA_INVALID, ext.ST_INSUFFICIENT):
        row = ext._row(_rec(), st, 0, None, _spec())
        assert row["status"] == st
        assert row["n_concepts"] == 0 and row["concepts"] == []


def test_row_records_usage_and_prompt_version():
    row = ext._row(_rec(), ext.ST_OK, 0, {"prompt_tokens": 10}, _spec())
    assert row["usage"] == {"prompt_tokens": 10}
    assert row["prompt_sha256"] == "0" * 64
    assert row["latency_s"] >= 0


def test_summarize_counts_and_estimates_cost():
    ok = ext._row(_rec(), ext.ST_OK, 0, {"prompt_tokens": 1000,
                                         "completion_tokens": 500}, _spec(),
                  clean={"concepts": [{"type": "material", "name": "resin",
                                       "evidence": None}],
                         "relations": [], "note": None})
    bad = ext._row(_rec(), ext.ST_API_ERROR, 0, None, _spec())
    s = ext.summarize([ok, bad], _spec())
    assert s["coverage"] == 0.5
    assert s["usage"]["total_tokens"] == 1500
    assert s["usage"]["est_cost_usd"] > 0
    assert s["concepts"]["type_totals"] == {"material": 1}


# ══ 4. 审计：三项验收 ════════════════════════════════════════════════════
def _mk_db(path, rows):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE paper_meta (year INTEGER, title TEXT, abstract TEXT, "
                "exclusion_reason TEXT)")
    con.executemany("INSERT INTO paper_meta VALUES (?,?,?,NULL)", rows)
    con.commit()
    con.close()


def test_term_first_year_uses_full_corpus_including_future(tmp_path):
    """首现年索引必须用**全部**论文 —— 只用 TRAIN 就永远发现不了时相污染。"""
    db = tmp_path / "m.db"
    _mk_db(db, [(2015, "quantum computing basics", None),
                (2024, "large language models survey", None)])
    first, years, prefix_min = aud.build_term_first_year(str(db))
    assert first["quantum"] == 2015
    assert first["large"] == 2024
    assert years[2024] == 1
    assert prefix_min["quantu"] == 2015 and prefix_min["large"] == 2024


def test_concept_first_attested_is_max_of_tokens():
    first = {"thiol": 2010, "ene": 2009, "polymerization": 2005}
    y, unknown = aud.concept_first_attested("thiol-ene polymerization", first,
                                            mode="exact")
    assert y == 2010 and unknown == []
    y2, unk2 = aud.concept_first_attested("thiol-ene zzzqqq", first, mode="exact")
    assert y2 is None and unk2 == ["zzzqqq"]


def test_prefix_mode_absorbs_morphological_variants():
    """**真实踩过的度量缺陷**：概念名经规范化后与原文词形不一致
    （balloons->balloon、thixotropic->thixotropy、dithioesters->dithioester），
    精确 token 比较会把它们误报成时相异常 —— 首轮全量 23 条里绝大多数是这么来的。

    前缀模式应把这类假阳性消掉；同时保留「词族内部换代」漏报的代价。
    """
    db_rows = [(2017, "shear thinning and thixotropic properties", None),
               (2017, "hollow structures such as balloons", None),
               (2006, "mediated by dithioesters chain transfer agents", None)]
    import tempfile, os as _os
    with tempfile.TemporaryDirectory() as d:
        p = _os.path.join(d, "m.db")
        _mk_db(p, db_rows)
        first, _y, prefix_min = aud.build_term_first_year(str(p))
    # 精确口径：概念名（单数/名词化）在语料里"没出现过"
    y_exact, _ = aud.concept_first_attested("thixotropy", first, mode="exact")
    assert y_exact is None
    # 前缀口径：能被 thixotropic / balloons / dithioesters 回溯到真实年份
    for name in ("thixotropy", "balloon", "dithioester"):
        y_pf, _ = aud.concept_first_attested(name, first, prefix_min,
                                            mode="prefix")
        assert y_pf == 2017 or y_pf == 2006, f"{name} -> {y_pf}"
        assert y_pf <= aud.CUTOFF_YEAR


def test_audit_detects_anachronism():
    """核心断言：2020 年前的论文抽出 2024 才出现的术语 -> 必须报出来。"""
    first = {"shrinkage": 2001, "quantum": 2018, "transformer": 2024,
             "neural": 2012, "network": 1999, "resin": 2005}
    concepts_rows = [
        {"paper_uid": "p1", "sample_layer": "A", "year": 2015,
         "primary_topic": "t", "status": "OK",
         "concepts": [{"type": "challenge", "name": "shrinkage stress",
                       "evidence": None},
                      {"type": "mechanism", "name": "transformer network",
                       "evidence": None}],
         "relations": [], "n_concepts": 2, "n_relations": 0,
         "validation_issues": {}, "latency_s": 1.0, "usage": None},
    ]
    sample = [{"paper_uid": "p1"}]
    a = aud.audit(concepts_rows, sample, first, {2015: 1},
                  coverage_threshold=0.95)
    t = a["temporal"]
    assert t["anachronism_count"] == 1
    assert t["anachronism_sample"][0]["concept"] == "transformer network"
    assert t["anachronism_sample"][0]["first_attested_exact"] == 2024
    # 双口径必须同时报：exact（严、假阳性多）/ prefix（宽、假阴性多）
    assert t["anachronism_count_exact"] == 1
    assert "anachronism_count_prefix_refined" in t
    # 覆盖率的两个口径分开报
    c = a["coverage"]
    assert c["raw_coverage"] == 1.0 and c["extractable_coverage"] == 1.0
    assert a["consistency"]["complete"] is True


def test_audit_coverage_counts_insufficient_text_separately():
    rows = [
        {"paper_uid": "p1", "year": 2015, "status": "OK", "concepts": [],
         "relations": [], "n_concepts": 0, "n_relations": 0,
         "validation_issues": {}, "latency_s": 1.0, "usage": None,
         "sample_layer": "A", "primary_topic": "t"},
        {"paper_uid": "p2", "year": 2015, "status": "INSUFFICIENT_TEXT",
         "concepts": [], "relations": [], "n_concepts": 0, "n_relations": 0,
         "validation_issues": {}, "latency_s": 1.0, "usage": None,
         "sample_layer": "A", "primary_topic": "t"},
    ]
    a = aud.audit(rows, [{"paper_uid": "p1"}, {"paper_uid": "p2"}], {}, {},
                  coverage_threshold=0.95)
    c = a["coverage"]
    assert c["raw_coverage"] == 0.5
    assert c["extractable_coverage"] == 1.0     # 扣掉文本不足后 100%
    assert c["pass_extractable"] is True


def test_audit_diversity_flags_degenerate_keyword_lists():
    """1000 篇只得到 10 个词 -> 必须 REVIEW（这是「退化成 Google trend」的信号）。"""
    rows = []
    for i in range(10):
        rows.append({"paper_uid": f"p{i}", "year": 2015, "status": "OK",
                     "concepts": [{"type": "material", "name": "resin",
                                   "evidence": None}],
                     "relations": [], "n_concepts": 1, "n_relations": 0,
                     "validation_issues": {}, "latency_s": 1.0, "usage": None,
                     "sample_layer": "A", "primary_topic": "t"})
    a = aud.audit(rows, [], {}, {}, coverage_threshold=0.95)
    d = a["diversity"]
    assert d["verdict"] == "REVIEW"
    assert d["top_concept_share"] == 1.0
    assert d["concepts_per_paper"]["mean"] == 1.0


def test_audit_reports_sample_consistency_gap():
    rows = [{"paper_uid": "p1", "year": 2015, "status": "OK", "concepts": [],
             "relations": [], "n_concepts": 0, "n_relations": 0,
             "validation_issues": {}, "latency_s": 1.0, "usage": None,
             "sample_layer": "A", "primary_topic": "t"}]
    a = aud.audit(rows, [{"paper_uid": "p1"}, {"paper_uid": "p2"}], {}, {},
                  coverage_threshold=0.95)
    assert a["consistency"]["complete"] is False
    assert a["consistency"]["missing_from_concepts"] == 1


# ══ 5. 抽取产物与样本的键一致性（契约）═════════════════════════════════
def test_extraction_spec_is_frozen_and_matches_prompt():
    """committed 的 extraction_spec.yaml 必须与当前 prompt 一致。

    漂移了说明有人改了 prompt 没重新冻结 —— 那会让产物无法溯源到确定的 prompt。
    """
    import hashlib
    spec_path = BASE + "/datasets/photopolymerization_v1/extraction_spec.yaml"
    prompt_path = BASE + "/prompts/p4_1a_concept_extraction_v1.md"
    if not os.path.exists(spec_path):
        pytest.skip("尚未冻结协议")
    import yaml
    spec = yaml.safe_load(open(spec_path, encoding="utf-8"))
    cur = hashlib.sha256(open(prompt_path, "rb").read()).hexdigest()
    assert spec["prompt_sha256"] == cur, "prompt 变了但未重新冻结"


# ══ 6. 样本漂移守卫（扩样本必须重新冻结）════════════════════════════════
def _frozen_env(tmp_path, monkeypatch, sample_text='{"paper_uid":"x"}\n'):
    prompt = tmp_path / "p.md"
    prompt.write_text("## SYSTEM\nS\n## USER\n```\n{title}{year}{abstract}\n```",
                      encoding="utf-8")
    sample = tmp_path / "s.jsonl"
    sample.write_text(sample_text, encoding="utf-8")
    monkeypatch.setattr(ext, "PROMPT_PATH", prompt)
    monkeypatch.setattr(ext, "SPEC_PATH", tmp_path / "spec.yaml")
    ext.freeze_spec("m", 0.0, 100, 100, sample)
    return sample


def test_assert_spec_fresh_refuses_when_sample_changed(tmp_path, monkeypatch):
    """换样本若不重新冻结，必须在 --live 之前被拒。

    原先只校验 prompt：spec 里虽记了 sample_sha256，却没人断言它 ->
    换一个 --sample 就能在"协议已冻结"的名义下抽另一批论文，产物与协议**静默脱钩**。
    """
    sample = _frozen_env(tmp_path, monkeypatch)
    assert ext.assert_spec_fresh(sample_path=sample)["model"] == "m"
    sample.write_text('{"paper_uid":"x"}\n{"paper_uid":"y"}\n', encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        ext.assert_spec_fresh(sample_path=sample)
    msg = str(e.value)
    assert "样本与冻结协议不一致" in msg
    assert "--freeze" in msg


def test_assert_spec_fresh_skips_sample_check_when_not_given(tmp_path, monkeypatch):
    """不给 sample_path 时保持旧行为（只校验 prompt），不破坏既有调用。"""
    sample = _frozen_env(tmp_path, monkeypatch)
    sample.write_text("变了\n", encoding="utf-8")
    assert ext.assert_spec_fresh()["model"] == "m"


def test_freeze_spec_can_write_to_a_separate_file(tmp_path, monkeypatch):
    """扩样本走新 spec 文件，旧 spec 留作历史证据（修订 ≠ 覆盖）。"""
    prompt = tmp_path / "p.md"
    prompt.write_text("## SYSTEM\nS\n## USER\n```\n{title}{year}{abstract}\n```",
                      encoding="utf-8")
    sample = tmp_path / "s.jsonl"
    sample.write_text('{"paper_uid":"x"}\n', encoding="utf-8")
    legacy = tmp_path / "spec_legacy.yaml"
    legacy.write_text("keep-me", encoding="utf-8")
    monkeypatch.setattr(ext, "PROMPT_PATH", prompt)
    monkeypatch.setattr(ext, "SPEC_PATH", legacy, raising=False)
    new_spec = tmp_path / "spec_v2.yaml"
    ext.freeze_spec("m", 0.0, 100, 100, sample, new_spec)
    assert new_spec.exists()
    assert legacy.read_text(encoding="utf-8") == "keep-me"
    assert ext.assert_spec_fresh(new_spec, sample)["sample_sha256"]
