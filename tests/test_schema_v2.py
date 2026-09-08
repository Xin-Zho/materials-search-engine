"""P0-1 schema v2 约束测试（全局论文 + 主题关系架构）。

被测对象: tools/migrate_v2_schema.SCHEMA_SQL 建出的表结构约束：
  - papers.doi 唯一（同一论文只存一次）
  - topic_papers PK(topic_id, paper_id)：同一论文允许多主题，label topic-specific
  - relevance_label CHECK(R/U/I)
全部用临时库，不碰 data/cache/knowledge_base.db。
"""

import sqlite3
import sys

import pytest

sys.path.insert(0, "tools")
from migrate_v2_schema import SCHEMA_SQL


@pytest.fixture()
def db(tmp_path):
    con = sqlite3.connect(tmp_path / "t.db")
    con.executescript(SCHEMA_SQL)
    con.commit()
    yield con
    con.close()


def _insert_paper(con, paper_id="doi:10.1/x", doi="10.1/x"):
    con.execute(
        "INSERT INTO papers (paper_id, doi, openalex_id, scopus_eid, created_at) "
        "VALUES (?,?,?,?,1)", (paper_id, doi, None, None))
    con.commit()


def test_paper_doi_unique(db):
    """同一 DOI 不允许在 papers 中重复。"""
    _insert_paper(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO papers (paper_id, doi, created_at) VALUES ('doi:10.1/x2','10.1/x',1)")
        db.commit()
    # 不同 doi 可共存
    _insert_paper(db, paper_id="doi:10.2/y", doi="10.2/y")
    assert db.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 2


def test_same_paper_multi_topic_different_label(db):
    """同一论文允许在两个 topic 下具有不同 relevance label。"""
    for tid in ("photopolymerization_shrinkage", "thermochromic_materials"):
        db.execute("INSERT INTO topics (topic_id, name, created_at) VALUES (?,?,1)", (tid, tid))
    _insert_paper(db)
    db.execute(
        "INSERT INTO topic_papers (topic_id, paper_id, relevance_label, created_at) "
        "VALUES ('photopolymerization_shrinkage','doi:10.1/x','IRRELEVANT',1)")
    db.execute(
        "INSERT INTO topic_papers (topic_id, paper_id, relevance_label, created_at) "
        "VALUES ('thermochromic_materials','doi:10.1/x','RELEVANT',1)")
    db.commit()
    rows = db.execute(
        "SELECT topic_id, relevance_label FROM topic_papers "
        "WHERE paper_id='doi:10.1/x' ORDER BY topic_id").fetchall()
    assert rows == [("photopolymerization_shrinkage", "IRRELEVANT"),
                    ("thermochromic_materials", "RELEVANT")]


def test_topic_paper_pk_no_dup(db):
    """同一 (topic_id, paper_id) 不允许重复行。"""
    _insert_paper(db)
    db.execute(
        "INSERT INTO topic_papers (topic_id, paper_id, relevance_label, created_at) "
        "VALUES ('t','doi:10.1/x','RELEVANT',1)")
    db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO topic_papers (topic_id, paper_id, relevance_label, created_at) "
            "VALUES ('t','doi:10.1/x','UNCERTAIN',1)")
        db.commit()


def test_label_check(db):
    """relevance_label 只允许 R/U/I。"""
    _insert_paper(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO topic_papers (topic_id, paper_id, relevance_label, created_at) "
            "VALUES ('t','doi:10.1/x','MAYBE',1)")
        db.commit()
    # 合法值
    for lab in ("RELEVANT", "UNCERTAIN", "IRRELEVANT"):
        db.execute(
            "INSERT INTO topic_papers (topic_id, paper_id, relevance_label, created_at) "
            "VALUES (?,?,?,1)", ("t", f"doi:10.1/{lab}", lab))
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM topic_papers").fetchone()[0] == 3


def test_paper_identity_channels_columns(db):
    """papers 表保留三身份通道列。"""
    cols = [c[1] for c in db.execute("PRAGMA table_info(papers)").fetchall()]
    for need in ("paper_id", "doi", "openalex_id", "scopus_eid"):
        assert need in cols
