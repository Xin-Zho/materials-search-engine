"""P0-B1b 写路径收口测试：权限分离、关系后置、唯一命名出口。

本文件覆盖 ``search_engine/paper_writer.py`` 在 B1b 新增的两个入口，以及
``search_engine/identity.py`` 新增的命名工具 —— 它们的共同主题是
**把「谁能写什么」和「谁能拼什么」变成可测的约束**，而不是靠约定。

    resolve_or_create_paper   身份（唯一生产者）
    backfill_paper_fields     内容（受限：只允许 title/abstract/year）
    register_topic_paper      关系（后置于实体）

静态守卫（G1..G4）在 ``tests/test_p0b1b_static_guards.py``；
重放差异的机器证明在 ``tools/verify_p0b1b_uid_namespace.py``。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "tools"))

import migrate_p0a_identifiers as p0a  # noqa: E402
from search_engine import paper_writer as pw  # noqa: E402
from search_engine.identity import (  # noqa: E402
    make_canonical_uid,
    normalize_identifier,
    scopus_cache_key,
    scopus_cache_key_value,
    uid_id_type,
)

DOI = "10.1234/abc.def"
EID = "2-s2.0-123456789"


@pytest.fixture()
def db(tmp_path):
    con = sqlite3.connect(tmp_path / "kb.db")
    con.executescript("""
        CREATE TABLE papers (paper_id TEXT PRIMARY KEY, doi TEXT, openalex_id TEXT,
                             scopus_eid TEXT, title TEXT, abstract TEXT, year INTEGER,
                             source_json TEXT, created_at REAL);
        CREATE TABLE topic_papers (topic_id TEXT, paper_id TEXT, relevance_label TEXT,
                                   label_source TEXT, promotion_status TEXT,
                                   first_seen_run TEXT, evidence_json TEXT,
                                   created_at REAL, PRIMARY KEY (topic_id, paper_id));
    """)
    # 身份层两表：复用 P0-A 的权威 DDL（不在此复写，避免两套 schema 漂移）
    con.executescript(";\n".join(p0a.DDL) + ";")
    yield con
    con.close()


def _count(con, table):
    return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ══ backfill_paper_fields：内容入口，绝不触碰身份 ═══════════════════════

@pytest.mark.parametrize("col", ["paper_id", "doi", "openalex_id", "scopus_eid",
                                 "source_json", "created_at"])
def test_backfill_refuses_identity_columns(db, col):
    """身份/溯源列必须**显式报错**，而不是静默忽略。

    静默忽略会把「越权意图」藏起来 —— 而「静默接受一个看起来像 DOI 的 EID」
    正是那 30 条错位得以发生的方式。
    """
    out = pw.resolve_or_create_paper(db, {"doi": DOI, "title": "t"}, "test")
    db.commit()
    with pytest.raises(ValueError, match="拒绝改写身份"):
        pw.backfill_paper_fields(db, out.paper_uid, {col: "x"}, "test")


def test_backfill_refuses_unknown_column(db):
    out = pw.resolve_or_create_paper(db, {"doi": DOI, "title": "t"}, "test")
    db.commit()
    with pytest.raises(ValueError, match="不认识列"):
        pw.backfill_paper_fields(db, out.paper_uid, {"authors": "x"}, "test")


def test_backfill_default_never_overwrites(db):
    """默认 only_if_empty：既有非空值不被覆盖（与「冲突只记录不覆盖」同源）。"""
    out = pw.resolve_or_create_paper(
        db, {"doi": DOI, "title": "old", "abstract": "ORIGINAL"}, "test")
    db.commit()
    res = pw.backfill_paper_fields(db, out.paper_uid, {"abstract": "NEW"}, "test")
    db.commit()
    assert res[0].status == pw.BACKFILL_NOT_EMPTY
    assert not res[0].applied
    got = db.execute("SELECT abstract FROM papers WHERE paper_id = ?",
                     (out.paper_uid,)).fetchone()[0]
    assert got == "ORIGINAL"


def test_backfill_fills_empty_field(db):
    out = pw.resolve_or_create_paper(db, {"doi": DOI, "title": "t"}, "test")
    db.commit()
    assert db.execute("SELECT abstract FROM papers WHERE paper_id = ?",
                      (out.paper_uid,)).fetchone()[0] == ""
    res = pw.backfill_paper_fields(db, out.paper_uid, {"abstract": "FILLED"}, "test")
    db.commit()
    assert res[0].status == pw.BACKFILL_APPLIED
    assert db.execute("SELECT abstract FROM papers WHERE paper_id = ?",
                      (out.paper_uid,)).fetchone()[0] == "FILLED"


def test_backfill_does_not_create_missing_paper(db):
    """论文不存在时**不新建** —— 身份只能由 resolve_or_create_paper 产生（R1）。"""
    res = pw.backfill_paper_fields(db, "doi:10.9999/nope", {"abstract": "x"}, "test")
    db.commit()
    assert res[0].status == pw.BACKFILL_MISSING
    assert _count(db, "papers") == 0


def test_backfill_dry_run_writes_nothing(db):
    out = pw.resolve_or_create_paper(db, {"doi": DOI, "title": "t"}, "test")
    db.commit()
    res = pw.backfill_paper_fields(db, out.paper_uid, {"abstract": "X"}, "test",
                                   dry_run=True)
    assert res[0].status == pw.BACKFILL_DRY_RUN
    assert db.execute("SELECT abstract FROM papers WHERE paper_id = ?",
                      (out.paper_uid,)).fetchone()[0] == ""


def test_backfill_requires_source(db):
    out = pw.resolve_or_create_paper(db, {"doi": DOI, "title": "t"}, "test")
    db.commit()
    with pytest.raises(ValueError, match="source 必填"):
        pw.backfill_paper_fields(db, out.paper_uid, {"abstract": "x"}, "")


# ══ register_topic_paper：关系后置于实体 ══════════════════════════════

def test_topic_paper_requires_paper_to_exist(db):
    """关系不得先于实体 —— 否则等于在入口之外开了第二个身份生产者。"""
    with pytest.raises(ValueError, match="关系不得先于实体"):
        pw.register_topic_paper(db, "pc_001", "doi:10.9999/nope", "RELEVANT",
                                source="test")


def test_topic_paper_idempotent_and_never_overwrites_label(db):
    out = pw.resolve_or_create_paper(db, {"doi": DOI, "title": "t"}, "test")
    db.commit()
    a = pw.register_topic_paper(db, "pc_001", out.paper_uid, "RELEVANT",
                                source="test", label_source="QA")
    db.commit()
    assert a.status == pw.TOPIC_PAPER_CREATED
    b = pw.register_topic_paper(db, "pc_001", out.paper_uid, "IRRELEVANT",
                                source="test", label_source="OTHER")
    db.commit()
    assert b.status == pw.TOPIC_PAPER_REUSED
    assert b.note == "existing label preserved (no overwrite)"
    row = db.execute("SELECT relevance_label, label_source FROM topic_papers"
                     " WHERE topic_id = ? AND paper_id = ?",
                     ("pc_001", out.paper_uid)).fetchone()
    assert row == ("RELEVANT", "QA")
    assert _count(db, "topic_papers") == 1


def test_topic_paper_refresh_evidence_only(db):
    out = pw.resolve_or_create_paper(db, {"doi": DOI, "title": "t"}, "test")
    pw.register_topic_paper(db, "pc_001", out.paper_uid, "RELEVANT",
                            source="test", evidence={"v": 1})
    db.commit()
    c = pw.register_topic_paper(db, "pc_001", out.paper_uid, "RELEVANT",
                                source="test", evidence={"v": 2},
                                refresh_evidence=True)
    db.commit()
    assert c.status == pw.TOPIC_PAPER_REFRESHED and c.evidence_refreshed
    import json
    ev = db.execute("SELECT evidence_json FROM topic_papers WHERE topic_id = ?"
                    " AND paper_id = ?", ("pc_001", out.paper_uid)).fetchone()[0]
    assert json.loads(ev) == {"v": 2}


def test_topic_paper_decodes_evidence_dict(db):
    out = pw.resolve_or_create_paper(db, {"doi": DOI, "title": "t"}, "test")
    import json
    pw.register_topic_paper(db, "pc_001", out.paper_uid, "UNCERTAIN", source="test",
                            evidence={"rule": "R2", "nested": {"a": 1}})
    db.commit()
    ev = db.execute("SELECT evidence_json FROM topic_papers WHERE topic_id = ?"
                    " AND paper_id = ?", ("pc_001", out.paper_uid)).fetchone()[0]
    assert json.loads(ev) == {"rule": "R2", "nested": {"a": 1}}


def test_topic_paper_rejects_bad_label(db):
    out = pw.resolve_or_create_paper(db, {"doi": DOI, "title": "t"}, "test")
    db.commit()
    with pytest.raises(ValueError, match="relevance_label 非法"):
        pw.register_topic_paper(db, "pc_001", out.paper_uid, "MAYBE", source="test")


def test_topic_paper_dry_run_writes_nothing(db):
    out = pw.resolve_or_create_paper(db, {"doi": DOI, "title": "t"}, "test")
    db.commit()
    r = pw.register_topic_paper(db, "pc_001", out.paper_uid, "RELEVANT",
                                source="test", dry_run=True)
    assert r.status == pw.TOPIC_PAPER_DRY_RUN
    assert _count(db, "topic_papers") == 0


# ══ 命名出口 ═══════════════════════════════════════════════════════════

def test_make_canonical_uid_prefers_doi():
    assert make_canonical_uid(doi=DOI, scopus_eid=EID) == f"doi:{DOI}"
    assert make_canonical_uid(scopus_eid=EID) == f"scopus:{EID}"
    assert make_canonical_uid(openalex_id="W123") == "openalex:W123"


def test_make_canonical_uid_local_fallback_never_borrows_namespace():
    """无任何有效标识 -> local:<hash>，绝不借用外部命名空间。

    旧写法 ``f"doi:{doi}" if doi else paper_id`` 的 ``else`` 分支会把
    ``scopus:<标题前80字>`` 带进 KB 事实层 —— 与那 30 条同源。
    """
    uid = make_canonical_uid(title="Some Paper Title", year=2024)
    assert uid_id_type(uid) is None
    assert uid.partition(":")[0] == "local"
    # 非法形态的「DOI」同样不能借到 doi: 命名空间
    bad = make_canonical_uid(doi="2-s2.0-123456789")
    assert uid_id_type(bad) is None


def test_normalize_openalex_strips_stacked_prefixes():
    """回归：KB 历史数据存在 ``openalex:https://openalex.org/W...`` 叠加前缀。

    单次剥离会漏掉第二层，于是这条记录被判为「无任何标识」并落到 local:<hash>,
    与它真实拥有的 W-ID 脱钩 —— P0-B1b 实测这是重放差异中除 30 条之外的唯一一条。
    """
    for raw in ("openalex:https://openalex.org/W4406102006",
                "https://openalex.org/W4406102006",
                "openalex:W4406102006",
                "W4406102006",
                "w4406102006",
                "https://api.openalex.org/works/W4406102006"):
        assert normalize_identifier("OPENALEX", raw) == "W4406102006", raw
    assert normalize_identifier("OPENALEX", "Wabc") is None


def test_scopus_cache_key_round_trip():
    k = scopus_cache_key("10.1234/ABC")
    assert k == "scopus:10.1234/abc"
    assert scopus_cache_key_value(k) == "10.1234/abc"
    # 反解不能做形态校验（缓存键的值可能是任意串，包括 DOI 或空）
    assert scopus_cache_key_value("scopus:not-an-eid") == "not-an-eid"
    assert scopus_cache_key_value("no-prefix") == "no-prefix"


def test_uid_id_type_reads_authoritative_prefix_table():
    assert uid_id_type("doi:10.1234/abc") == "DOI"
    assert uid_id_type("scopus:2-s2.0-1") == "SCOPUS_EID"
    assert uid_id_type("openalex:W1") == "OPENALEX"
    assert uid_id_type("local:deadbeef") is None      # 本地命名空间无外部类型
    assert uid_id_type("weird:x") is None
    assert uid_id_type(None) is None
