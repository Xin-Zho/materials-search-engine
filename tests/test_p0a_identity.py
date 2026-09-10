# -*- coding: utf-8 -*-
"""P0-A 统一身份层回归测试。

覆盖用户 2026-09-10 验收标准中可单元化的部分：
  1. 迁移前后 papers 行数/内容不变（指纹）         -> test_apply_leaves_papers_untouched
  2. 不修改任何现有 paper_id                       -> 同上（指纹含 paper_id）
  3. 每个合法标识均标准化后回填                     -> test_normalize_* / test_backfill_covers_all_valid
  4. 同一外部标识只能有一个 owner                   -> test_identifier_already_owned_no_overwrite
  5. 冲突不覆盖，完整进入 identity_conflicts        -> 同上 + test_misplaced_not_silently_dropped
  6. W-only / EID-only 实体正常回填                 -> test_w_only_and_eid_only
  7. 重复运行不新增 identifier 或重复 conflict      -> test_apply_is_idempotent
  8. dry-run 零写入                                -> test_dry_run_writes_nothing
  9. 单事务失败完全回滚                            -> test_apply_rolls_back_on_failure
 10. 每 uid 恰好一个 is_primary                     -> test_one_primary_per_uid
 11. title 同题不自动合并                          -> test_title_collision_requires_review
"""

import hashlib
import os
import sqlite3
import sys

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from search_engine import identity as ident  # noqa: E402
from tools import migrate_p0a_identifiers as mp  # noqa: E402

REAL_DB = os.path.join(BASE, "data/cache/knowledge_base.db")


# ── fixtures ─────────────────────────────────────────────────────────
def _mini_db(path, papers):
    """构造 mini knowledge_base（结构对齐真库，行数可控）。"""
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE papers (
            paper_id TEXT PRIMARY KEY, doi TEXT, openalex_id TEXT, scopus_eid TEXT,
            title TEXT, abstract TEXT, year INTEGER, source_json TEXT, created_at REAL);
        CREATE UNIQUE INDEX ux_papers_doi ON papers(doi)
            WHERE doi IS NOT NULL AND doi != '';
        CREATE TABLE knowledge_records (paper_id TEXT);
        CREATE TABLE route_mechanism_edges (paper_id TEXT);
        CREATE TABLE topic_papers (topic_id TEXT, paper_id TEXT);
    """)
    con.executemany(
        "INSERT INTO papers (paper_id, doi, openalex_id, scopus_eid, title) VALUES (?,?,?,?,?)",
        papers)
    con.commit()
    con.close()


@pytest.fixture
def mini(tmp_path, monkeypatch):
    db = str(tmp_path / "mini_kb.db")
    _mini_db(db, [
        # uid 前缀与身份一致的正常行
        ("doi:10.1000/a", "10.1000/a", None, "2-s2.0-1", "Study A"),
        # 双身份（DOI + W）
        ("doi:10.1000/b", "10.1000/b", "W123", None, "Study B"),
        # W-only（无 DOI 无 EID）：一等公民
        ("openalex:W999", None, "W999", None, "Study C"),
        # 错位列：doi 列装 EID，且 eid 列同值承载
        ("doi:2-s2.0-77", "2-s2.0-77", None, "2-s2.0-77", "Study D"),
        # 错位列且无承载：doi 列装 W-ID，openalex 列空 -> 应恢复
        ("doi:10.1000/e", "W555", None, None, "Study E"),
        # EID-only
        ("scopus:2-s2.0-42", None, None, "2-s2.0-42", "Study F"),
        # 完全无身份
        ("raw:no-identity", None, None, None, "Study G"),
    ])
    # mini 库的冻结锚（真库为 216/700）
    monkeypatch.setattr(mp, "FROZEN_TABLE_ANCHORS",
                        {"knowledge_records": 0, "route_mechanism_edges": 0})
    return db


def _rows(db):
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT paper_id, doi, openalex_id, scopus_eid, title "
                           "FROM papers ORDER BY paper_id").fetchall()
    finally:
        con.close()


def _tables(db):
    con = sqlite3.connect(db)
    try:
        return {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def _md5(path):
    return hashlib.md5(open(path, "rb").read()).hexdigest()


# ── 1. 归一化 ────────────────────────────────────────────────────────
@pytest.mark.parametrize("id_type,raw,expected", [
    ("DOI", "10.1000/ABC", "10.1000/abc"),
    ("DOI", "doi:10.1000/abc", "10.1000/abc"),
    ("DOI", "https://doi.org/10.1000/ABC", "10.1000/abc"),
    ("DOI", "http://dx.doi.org/10.1000/abc", "10.1000/abc"),
    ("DOI", "10.1000/abc</div>", "10.1000/abc"),
    ("DOI", " 10.1000/abc ", "10.1000/abc"),
    ("DOI", "not-a-doi", None),
    ("DOI", "", None),
    ("DOI", None, None),
    ("DOI", "nan", None),
    ("OPENALEX", "W12345", "W12345"),
    ("OPENALEX", "w12345", "W12345"),
    ("OPENALEX", "openalex:W12345", "W12345"),
    ("OPENALEX", "https://openalex.org/W12345", "W12345"),
    ("OPENALEX", "https://api.openalex.org/works/W12345", "W12345"),
    ("OPENALEX", "doi:10.1000/a", None),
    ("SCOPUS_EID", "2-s2.0-85056983120", "2-s2.0-85056983120"),
    ("SCOPUS_EID", "scopus:2-s2.0-123", "2-s2.0-123"),
    ("SCOPUS_EID", "2-s2.0-abc", None),
])
def test_normalize_identifier(id_type, raw, expected):
    assert ident.normalize_identifier(id_type, raw) == expected


def test_detect_actual_type_for_misplaced():
    assert ident.detect_actual_type("2-s2.0-123", exclude="DOI") == "SCOPUS_EID"
    assert ident.detect_actual_type("W123", exclude="DOI") == "OPENALEX"
    assert ident.detect_actual_type("garbage", exclude="DOI") is None


# ── 2. 回填覆盖与错位处理 ──────────────────────────────────────────────
def test_backfill_covers_all_valid_identifiers(mini):
    plan = ident.plan_backfill(_rows(mini))
    own = {(c.id_type, c.normalized_value) for c in plan["identifiers"]}
    # 正常列值全部回填
    for expected in [("DOI", "10.1000/a"), ("DOI", "10.1000/b"), ("OPENALEX", "W123"),
                     ("OPENALEX", "W999"), ("SCOPUS_EID", "2-s2.0-1"),
                     ("SCOPUS_EID", "2-s2.0-77"), ("SCOPUS_EID", "2-s2.0-42")]:
        assert expected in own, f"缺少回填 {expected}"
    # 行 E：doi 列装 W555 且无其他承载 -> 按真实类型恢复
    assert ("OPENALEX", "W555") in own
    assert plan["stats"]["misplaced_recovered"] == 1


def test_correct_column_wins_over_recovered(mini):
    """行 D：doi 列（错位）与 scopus_eid 列（正确）同值 -> source 必须是正确列、confidence 1.0。"""
    plan = ident.plan_backfill(_rows(mini))
    c = [x for x in plan["identifiers"]
         if x.key == ("SCOPUS_EID", "2-s2.0-77")][0]
    assert c.source == "papers.scopus_eid"
    assert c.confidence == 1.0


def test_misplaced_not_silently_dropped(mini):
    """错位列必须产出冲突记录，且绝不改写原始列。"""
    plan = ident.plan_backfill(_rows(mini))
    mp_conf = [c for c in plan["conflicts"] if c.conflict_type == "MISPLACED_IDENTIFIER"]
    assert len(mp_conf) == 2  # 行 D + 行 E
    assert {c.incoming_paper_uid for c in mp_conf} == {"doi:2-s2.0-77", "doi:10.1000/e"}
    assert all(c.resolution_status == "REVIEW_REQUIRED" for c in mp_conf)
    # 原值仍在 papers 里（未被覆盖）
    row = [r for r in _rows(mini) if r[0] == "doi:2-s2.0-77"][0]
    assert row[1] == "2-s2.0-77"


def test_w_only_and_eid_only_are_first_class(mini):
    plan = ident.plan_backfill(_rows(mini))
    by_uid = {}
    for c in plan["identifiers"]:
        by_uid.setdefault(c.paper_uid, []).append(c.id_type)
    assert by_uid["openalex:W999"] == ["OPENALEX"]
    assert by_uid["scopus:2-s2.0-42"] == ["SCOPUS_EID"]
    assert plan["terms"]["W_TRUE_ONLY"] == 1
    assert plan["terms"]["NO_IDENTIFIER"] == 1  # raw:no-identity


def test_uid_prefix_mismatch_detected(mini):
    """只有行 D（uid=doi:2-s2.0-77 且无有效 DOI 值）是真正的前缀错配。

    行 E（uid=doi:10.1000/e）虽然 doi 列装的是 W555，但 uid 前缀本身能自证出
    合法 DOI 10.1000/e —— 它的主身份就是 DOI，不算前缀错配（列错位另记 MISPLACED）。
    口径必须与 paper_identifiers 同源，否则就是双口径打架。
    """
    plan = ident.plan_backfill(_rows(mini))
    assert plan["stats"]["uid_prefix_mismatches"] == 1
    assert [m["paper_uid"] for m in plan["uid_prefix_mismatches"]] == ["doi:2-s2.0-77"]
    # 两个口径必须一致
    assert plan["terms"]["UID_PREFIX_MISMATCH"] == plan["stats"]["uid_prefix_mismatches"]


def test_one_primary_per_uid(mini):
    plan = ident.plan_backfill(_rows(mini))
    uids = {c.paper_uid for c in plan["identifiers"]}
    prim = [c for c in plan["identifiers"] if c.is_primary]
    assert len(prim) == len(uids)
    assert len({c.paper_uid for c in prim}) == len(uids)


# ── 3. 认领唯一性 / 冲突不覆盖 ─────────────────────────────────────────
def test_identifier_already_owned_no_overwrite():
    rows = [{"paper_uid": "doi:10.1000/x", "doi": "10.1000/x", "title": "T1"},
            {"paper_uid": "openalex:W1", "openalex_id": "W1", "title": "T2", "doi": "10.1000/x"}]
    plan = ident.plan_backfill(rows)
    owned = [c for c in plan["identifiers"] if c.key == ("DOI", "10.1000/x")]
    assert len(owned) == 1 and owned[0].paper_uid == "doi:10.1000/x"   # 首认领者胜
    conf = [c for c in plan["conflicts"] if c.conflict_type == "IDENTIFIER_ALREADY_OWNED"]
    assert len(conf) == 1
    assert conf[0].incoming_paper_uid == "openalex:W1"
    assert conf[0].existing_paper_uid == "doi:10.1000/x"
    assert conf[0].resolution_status == "UNRESOLVED"


def test_title_collision_requires_review():
    rows = [{"paper_uid": "doi:10.1000/a", "doi": "10.1000/a", "title": "Same Title"},
            {"paper_uid": "doi:10.1000/b", "doi": "10.1000/b", "title": "same title "}]
    plan = ident.plan_backfill(rows)
    tc = [c for c in plan["conflicts"] if c.conflict_type == "TITLE_COLLISION_CANDIDATE"]
    assert len(tc) == 1
    assert tc[0].id_type == "TITLE"
    assert tc[0].resolution_status == "REVIEW_REQUIRED"
    assert tc[0].existing_paper_uid != tc[0].incoming_paper_uid
    # 关键：两个 uid 都保留（绝不自动合并）
    assert {c.paper_uid for c in plan["identifiers"]} == {"doi:10.1000/a", "doi:10.1000/b"}
    assert plan["stats"]["title_collision_groups"] == 1


def test_title_collision_can_be_disabled():
    rows = [{"paper_uid": "doi:10.1000/a", "doi": "10.1000/a", "title": "T"},
            {"paper_uid": "doi:10.1000/b", "doi": "10.1000/b", "title": "T"}]
    plan = ident.plan_backfill(rows, title_collision=False)
    assert not [c for c in plan["conflicts"]
                if c.conflict_type == "TITLE_COLLISION_CANDIDATE"]


# ── 4. 迁移行为：dry-run / 幂等 / 事务 ────────────────────────────────
def test_dry_run_writes_nothing(mini, tmp_path):
    before = _md5(mini)
    rc = mp.migrate(mini, apply_changes=False,
                    make_backup=False,
                    report_path=str(tmp_path / "r.json"))
    assert rc == 0
    assert _md5(mini) == before, "dry-run 改动了数据库文件"
    assert "paper_identifiers" not in _tables(mini)
    assert "identity_conflicts" not in _tables(mini)


def test_apply_creates_tables_and_leaves_papers_untouched(mini, tmp_path):
    rows_before = _rows(mini)
    rc = mp.migrate(mini, apply_changes=True, make_backup=False,
                    report_path=str(tmp_path / "r.json"))
    assert rc == 0
    assert {"paper_identifiers", "identity_conflicts"} <= _tables(mini)
    assert _rows(mini) == rows_before, "papers 内容被改动"
    con = sqlite3.connect(mini)
    n_ids = con.execute("SELECT COUNT(*) FROM paper_identifiers").fetchone()[0]
    n_conf = con.execute("SELECT COUNT(*) FROM identity_conflicts").fetchone()[0]
    n_prim = con.execute("SELECT COUNT(*) FROM paper_identifiers WHERE is_primary=1").fetchone()[0]
    con.close()
    assert n_ids > 0 and n_conf > 0
    assert n_prim == len({r[0] for r in rows_before}) - 1  # 唯一无身份行 raw:no-identity


def test_apply_is_idempotent(mini, tmp_path):
    mp.migrate(mini, apply_changes=True, make_backup=False,
               report_path=str(tmp_path / "r1.json"))
    con = sqlite3.connect(mini)
    a = (con.execute("SELECT COUNT(*) FROM paper_identifiers").fetchone()[0],
         con.execute("SELECT COUNT(*) FROM identity_conflicts").fetchone()[0])
    con.close()
    mp.migrate(mini, apply_changes=True, make_backup=False,
               report_path=str(tmp_path / "r2.json"))
    con = sqlite3.connect(mini)
    b = (con.execute("SELECT COUNT(*) FROM paper_identifiers").fetchone()[0],
         con.execute("SELECT COUNT(*) FROM identity_conflicts").fetchone()[0])
    con.close()
    assert a == b, f"重复运行新增了记录: {a} -> {b}"


def test_apply_rolls_back_on_failure(mini, tmp_path, monkeypatch):
    before = _md5(mini)
    boom = list(mp.DDL) + [
        "CREATE TABLE __boom (x TEXT CHECK (x IN ('only')))",
        "INSERT INTO __boom (x) VALUES ('violates-check')",
    ]
    monkeypatch.setattr(mp, "DDL", boom)
    with pytest.raises(sqlite3.IntegrityError):
        mp.migrate(mini, apply_changes=True, make_backup=False,
                   report_path=str(tmp_path / "r.json"))
    assert _md5(mini) == before, "失败后数据库未完全回滚"
    assert "paper_identifiers" not in _tables(mini), "DDL 未随事务回滚"
    assert "__boom" not in _tables(mini)


def test_apply_makes_backup(mini, tmp_path):
    mp.migrate(mini, apply_changes=True, make_backup=True,
               report_path=str(tmp_path / "r.json"))
    backups = [p for p in os.listdir(os.path.dirname(mini))
               if p.startswith("mini_kb.db.preP0A")]
    assert backups, "未生成备份"


def test_conflict_dedup_unique_index_enforced(mini, tmp_path):
    """幂等护栏落在 DB 层：同 (type,id_type,value,uid) 二次插入被拒。"""
    mp.migrate(mini, apply_changes=True, make_backup=False,
               report_path=str(tmp_path / "r.json"))
    con = sqlite3.connect(mini)
    row = con.execute("SELECT conflict_type, id_type, normalized_value, incoming_paper_uid "
                      "FROM identity_conflicts LIMIT 1").fetchone()
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO identity_conflicts (id_type, normalized_value, "
                    "incoming_paper_uid, conflict_type, detected_at) VALUES (?,?,?,?,'x')",
                    (row[1], row[2], row[3], row[0]))
        con.commit()
    con.close()


# ── 5. 真库只读回归（不写任何东西）─────────────────────────────────────
@pytest.mark.skipif(not os.path.exists(REAL_DB), reason="真库不存在")
def test_real_db_plan_invariants():
    con = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
    rows = con.execute("SELECT paper_id, doi, openalex_id, scopus_eid, title "
                       "FROM papers ORDER BY paper_id").fetchall()
    con.close()
    plan = ident.plan_backfill(rows)
    st, terms = plan["stats"], plan["terms"]
    assert st["rows"] == len(rows) == terms["rows"]
    # 每 uid 恰好一个主身份
    assert st["primary_marked"] == len({c.paper_uid for c in plan["identifiers"]})
    # PK 唯一 + 冲突 dedup 唯一
    assert len({c.key for c in plan["identifiers"]}) == len(plan["identifiers"])
    assert len({c.dedup_key for c in plan["conflicts"]}) == len(plan["conflicts"])
    # 不做任何自动合并
    assert not any(c.resolution_status == "RESOLVED_MERGED" for c in plan["conflicts"])
    # 三空行为 0（每篇至少一个身份通道）
    assert terms["NO_IDENTIFIER"] == st["rows_without_identifier"]
    # 口径自洽
    assert terms["EFFECTIVE_DOI_PRIMARY"] == terms["DOI_PRIMARY"] - terms["UID_PREFIX_MISMATCH"]
    assert terms["with_doi"] + terms["NO_DOI"] == terms["rows"]
