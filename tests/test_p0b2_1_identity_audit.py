"""P0-B2.1 身份审计器测试（``tools/audit_identity_report.py``）。

审计器是**决策输入**，不是一次性脚本 —— 它报出来的数字会直接决定 B2.2 迁移哪批
uid、影响面多大。所以它本身必须有回归测试，且必须能在**合成库**上跑
（不依赖真库快照，否则真库一变测试就失去意义）。

覆盖：
  * 每类异常各造一个最小样本 -> 断言计数与分类正确
  * Type C 的三种模式识别（SI / preprint / 多注册机构）
  * 跨世代口径：v1 表的 paper_id **不得**被拿去 v2 域里比对（那是域不同，不是坏数据）
  * 不变式在正常库上全 PASS
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "tools"))

import audit_identity_report as audit  # noqa: E402
import migrate_p0a_identifiers as p0a  # noqa: E402

SCHEMA = """
CREATE TABLE papers (paper_id TEXT PRIMARY KEY, doi TEXT, openalex_id TEXT,
                     scopus_eid TEXT, title TEXT, abstract TEXT, year INTEGER,
                     source_json TEXT, created_at REAL);
CREATE TABLE topic_papers (topic_id TEXT, paper_id TEXT, relevance_label TEXT,
                           label_source TEXT, promotion_status TEXT,
                           first_seen_run TEXT, evidence_json TEXT, created_at REAL);
CREATE TABLE knowledge_claims (paper_id TEXT, topic_id TEXT, claim_type TEXT,
                               material TEXT, mechanism TEXT, property TEXT,
                               evidence TEXT, confidence REAL, payload_json TEXT,
                               source_record TEXT);
CREATE TABLE route_mechanism_edges (paper_id TEXT, raw_route TEXT, raw_mechanism TEXT);
CREATE TABLE knowledge_records (paper_id TEXT PRIMARY KEY, record_json TEXT,
                                extractor_version TEXT, confidence REAL);
"""


@pytest.fixture()
def db(tmp_path):
    con = sqlite3.connect(tmp_path / "kb.db")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.executescript(";\n".join(p0a.DDL) + ";")
    yield con
    con.close()


def add_paper(con, uid, *, doi=None, wid=None, eid=None, title="T", year=None,
              sources=None):
    con.execute(
        "INSERT INTO papers (paper_id, doi, openalex_id, scopus_eid, title, year, "
        "source_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (uid, doi, wid, eid, title, year,
         json.dumps({"sources": sources or ["test"]}, ensure_ascii=False), 1.0))


def add_ident(con, id_type, norm, uid, primary=1):
    con.execute(
        "INSERT INTO paper_identifiers (id_type, normalized_value, paper_uid, id_value, "
        "source, confidence, is_primary, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (id_type, norm, uid, norm, "test", 1.0, primary, "2026-01-01 00:00:00"))


def run(con):
    """跑完整审计，返回 5 类异常 + 不变式。"""
    papers = audit._load_papers(con)
    ident = audit._build_ident_index(con)
    v1 = audit._build_v1_index(con, ident)
    return (
        audit.scan_a_column_mismatch(con, papers, v1),
        audit.scan_b_primary_drift(con, papers, v1),
        audit.scan_c_duplicate_entity(con, papers, v1),
        audit.scan_d_reference_integrity(con, papers),
        audit.scan_e_knowledge_records(con, ident, v1),
        audit.check_invariants(con, papers),
    )


# ══ Type A：标识出现在错误的列 ══════════════════════════════════════

def test_type_a_detects_eid_in_doi_column(db):
    uid = "doi:2-s2.0-123456789"
    add_paper(db, uid, doi="2-s2.0-123456789", eid="2-s2.0-123456789")
    db.commit()
    a, *_ = run(db)
    assert a["count"] == 1
    rec = a["records"][0]
    assert rec["declared_column"] == "doi"
    assert rec["declared_type"] == "DOI"
    assert rec["looks_like"] == "SCOPUS_EID"
    assert rec["recoverable"] is True
    # 同一批在 uid 侧的投影
    assert a["uid_projection_count"] == 1
    assert a["breakdown"] == {"doi<-SCOPUS_EID": 1}


def test_type_a_ignores_legal_columns(db):
    add_paper(db, "doi:10.1234/x", doi="10.1234/x", wid="W1", eid="2-s2.0-1")
    db.commit()
    a, *_ = run(db)
    assert a["count"] == 0


def test_type_a_marks_unrecognizable_value(db):
    add_paper(db, "local:abc", doi="not-an-identifier")
    db.commit()
    a, *_ = run(db)
    assert a["count"] == 1
    assert a["records"][0]["looks_like"] is None
    assert a["records"][0]["recoverable"] is False


# ══ Type B：主身份选择漂移 ══════════════════════════════════════════

def test_type_b_detects_w_primary_when_doi_available(db):
    add_paper(db, "openalex:W1", doi="10.1234/x", wid="W1")
    db.commit()
    _, b, *_ = run(db)
    assert b["count"] == 1
    assert b["records"][0]["declared_type"] == "OPENALEX"
    assert b["records"][0]["best_available"] == "DOI"
    assert b["uid_value_form_all_legal"] is True


def test_type_b_excludes_prefix_mismatch_and_counts_it_separately(db):
    """Type B 只含**形态合法**者；前缀不符的属于 A 的 uid 投影，不重复计数。"""
    add_paper(db, "openalex:W1", doi="10.1234/x", wid="W1")           # B
    add_paper(db, "doi:2-s2.0-123456789", doi="2-s2.0-123456789",     # A 投影
              eid="2-s2.0-123456789")
    db.commit()
    a, b, *_ = run(db)
    assert a["count"] == 1 and b["count"] == 1
    assert b["cross_check_prefix_mismatch_n"] == 1
    assert b["cross_check_prefix_mismatch_n"] == a["count"]


def test_type_b_ignores_optimal_primary(db):
    add_paper(db, "doi:10.1234/x", doi="10.1234/x", wid="W1")
    db.commit()
    _, b, *_ = run(db)
    assert b["count"] == 0


# ══ Type C：同题多 uid（候选 + 模式识别） ═══════════════════════════

def test_type_c_groups_same_title(db):
    add_paper(db, "doi:10.1234/a", doi="10.1234/a", title="Same Title Here")
    add_paper(db, "doi:10.1234/b", doi="10.1234/b", title="same  TITLE, here!")
    db.commit()
    _, _, c, *_ = run(db)
    assert c["count"] == 1 and c["n_papers_involved"] == 2
    assert c["status"] == "CANDIDATE"
    assert c["by_pattern"] == {"MANUAL_REVIEW": 1}


def test_type_c_classifies_supporting_information(db):
    add_paper(db, "doi:10.1021/x", doi="10.1021/x", title="Paper X")
    add_paper(db, "doi:10.1021/x.s001", doi="10.1021/x.s001", title="Paper X")
    db.commit()
    _, _, c, *_ = run(db)
    assert c["by_pattern"] == {"SUPPORTING_INFORMATION": 1}
    assert "附件" in c["groups"][0]["suggested_action"]


def test_type_c_classifies_preprint_as_keep_separate(db):
    add_paper(db, "doi:10.1002/x", doi="10.1002/x", title="Paper Y")
    add_paper(db, "doi:10.2139/ssrn.4918808", doi="10.2139/ssrn.4918808",
              title="Paper Y")
    db.commit()
    _, _, c, *_ = run(db)
    assert c["by_pattern"] == {"PREPRINT_VS_PUBLISHED": 1}
    assert "不合并" in c["groups"][0]["suggested_action"]


def test_type_c_classifies_multi_registrant(db):
    add_paper(db, "doi:10.1002/x", doi="10.1002/x", title="Paper Z")
    add_paper(db, "doi:10.34726/3441", doi="10.34726/3441", title="Paper Z")
    db.commit()
    _, _, c, *_ = run(db)
    assert c["by_pattern"] == {"MULTI_REGISTRANT": 1}


def test_type_c_never_auto_merges(db):
    """P0-A 铁律：同题只产候选。审计器不得改任何数据。"""
    add_paper(db, "doi:10.1234/a", doi="10.1234/a", title="Same")
    add_paper(db, "doi:10.1234/b", doi="10.1234/b", title="Same")
    db.commit()
    before = db.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    run(db)
    assert db.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == before
    assert c_status(db) == "CANDIDATE"


def c_status(con):
    papers = audit._load_papers(con)
    return audit.scan_c_duplicate_entity(con, papers)["status"]


# ══ Type D：引用完整性 ══════════════════════════════════════════════

def test_type_d_detects_dangling_v2_ref(db):
    add_paper(db, "doi:10.1234/a", doi="10.1234/a")
    db.execute("INSERT INTO topic_papers (topic_id, paper_id) VALUES ('t','doi:10.9999/ghost')")
    db.commit()
    _, _, _, d, _, _ = run(db)
    assert d["dangling_refs"]["topic_papers"]["dangling"] == 1
    assert d["count"] >= 1


def test_type_d_v1_tables_are_not_in_v2_scope(db):
    """跨世代口径：v1 表的 paper_id 不参与 v2 悬空比对。"""
    add_paper(db, "doi:10.1234/a", doi="10.1234/a")
    add_ident(db, "DOI", "10.1234/a", "doi:10.1234/a")
    # v1 形态的记录键（v2 域里找不到，但这**不是**悬空）
    db.execute("INSERT INTO knowledge_records (paper_id, record_json) VALUES (?,?)",
               ("scopus:10.1234/a", json.dumps({"doi": "10.1234/a"})))
    db.execute("INSERT INTO route_mechanism_edges (paper_id) VALUES ('scopus:10.1234/a')")
    db.commit()
    _, _, _, d, e, _ = run(db)
    assert "knowledge_records" not in d["dangling_refs"]
    assert "route_mechanism_edges" not in d["dangling_refs"]
    # 但可映射率必须为 100%（经 identity 反解，不是字符串比对）
    assert e["cross_generation_mapping"]["mapped"] == 1
    assert e["cross_generation_mapping"]["rate"] == 1.0
    assert e["cross_generation_mapping_edges"]["mapped"] == 1


def test_type_d_flags_identifier_without_single_primary(db):
    add_paper(db, "doi:10.1234/a", doi="10.1234/a")
    db.execute(
        "INSERT INTO paper_identifiers (id_type, normalized_value, paper_uid, id_value,"
        " source, confidence, is_primary, created_at) VALUES ('DOI','10.1234/a',"
        "'doi:10.1234/a','10.1234/a','t',1.0,0,'2026-01-01 00:00:00')")
    db.commit()
    _, _, _, d, _, _ = run(db)
    assert d["identifiers_without_exactly_one_primary"]["count"] == 1


# ══ Type E：v1 事实层污染 ═══════════════════════════════════════════

def test_type_e_detects_malformed_record_doi(db):
    """这正是 P0-B1 记录的「近失事件」形态：doi 字段装着 EID。"""
    add_paper(db, "doi:10.1234/a", doi="10.1234/a")
    add_ident(db, "DOI", "10.1234/a", "doi:10.1234/a")
    db.execute("INSERT INTO knowledge_records (paper_id, record_json) VALUES (?,?)",
               ("doi:10.1234/a",
                json.dumps({"doi": "2-s2.0-123456789",
                            "canonical_paper_id": "doi:2-s2.0-123456789"})))
    db.commit()
    _, _, _, _, e, _ = run(db)
    assert e["contamination"]["record_json_doi_malformed"] == 1
    assert e["contamination"]["canonical_paper_id_malformed"] == 1


def test_type_e_reports_unmapped_records(db):
    add_paper(db, "doi:10.1234/a", doi="10.1234/a")
    db.execute("INSERT INTO knowledge_records (paper_id, record_json) VALUES (?,?)",
               ("scopus:2-s2.0-999999999", json.dumps({"doi": ""})))
    db.commit()
    _, _, _, _, e, _ = run(db)
    assert e["cross_generation_mapping"]["mapped"] == 0
    assert e["cross_generation_mapping"]["unmapped"] == 1


# ══ 不变式 ══════════════════════════════════════════════════════════

def test_invariants_pass_on_clean_db(db):
    add_paper(db, "doi:10.1234/a", doi="10.1234/a", wid="W1")
    add_paper(db, "openalex:W2", wid="W2")
    add_ident(db, "OPENALEX", "W1", "doi:10.1234/a", primary=0)  # 次身份
    add_ident(db, "DOI", "10.1234/a", "doi:10.1234/a")
    add_ident(db, "OPENALEX", "W2", "openalex:W2")
    db.execute("INSERT INTO topic_papers (topic_id, paper_id) VALUES ('t','doi:10.1234/a')")
    db.commit()
    *_, inv = run(db)
    assert all(inv.values()), inv


def test_invariants_flag_dangling_refs(db):
    add_paper(db, "doi:10.1234/a", doi="10.1234/a")
    add_ident(db, "DOI", "10.1234/a", "doi:10.1234/a")
    db.execute("INSERT INTO knowledge_claims (paper_id) VALUES ('doi:10.9999/ghost')")
    db.commit()
    *_, inv = run(db)
    assert inv["no_dangling_v2_refs"] is False


# ══ 命名出口纪律：审计器不得自己拼前缀 ═══════════════════════════════

def test_audit_tool_uses_uid_id_type_not_startswith():
    """审计器判断 uid 类型必须走 identity.uid_id_type（唯一查询出口）。

    手写 ``uid.startswith("doi:")`` 会让前缀命名空间失去单一权威表，
    也会被 static guard G4 抓住。
    """
    src = (BASE / "tools" / "audit_identity_report.py").read_text(encoding="utf-8")
    assert "startswith(\"doi:\")" not in src
    assert "startswith('doi:')" not in src
    assert "uid_id_type" in src
