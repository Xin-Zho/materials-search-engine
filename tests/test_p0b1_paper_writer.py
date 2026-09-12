# -*- coding: utf-8 -*-
"""P0-B1 统一身份写入入口回归测试。

核心：用真库里那 **30 条真实「EID 写进 DOI 列」错位模式**（tests/fixtures/p0b1_misplaced_30.json，
从 data/cache/knowledge_base.db 抽取后冻结）驱动，验证 P0-B1 的 8 项验收标准：

  A1 唯一生产者        -> test_single_writer_guard / test_only_paper_writer_inserts
  A2 新写入前缀错配=0  -> test_fresh_replay_never_creates_bad_prefix
  A3 EID 不进 DOI 列   -> test_fresh_replay_eid_never_enters_doi_column
  A4 同标识不造第二个uid -> test_replay_reuses_existing_uid
  A5 W-only/EID-only   -> test_w_only_and_eid_only_are_first_class
  A6 冲突只记录不覆盖   -> test_conflict_never_overwrites / test_multi_owner_no_merge
  A7 旧 331 uid 不变    -> test_replay_leaves_papers_and_topic_papers_untouched
  A8 重复导入幂等      -> test_replay_is_idempotent / test_fresh_replay_is_idempotent
"""

import hashlib
import json
import os
import sqlite3
import sys

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from search_engine import paper_writer as pw  # noqa: E402

FIXTURE = os.path.join(BASE, "tests", "fixtures", "p0b1_misplaced_30.json")

SCHEMA = """
CREATE TABLE papers (
    paper_id TEXT PRIMARY KEY, doi TEXT, openalex_id TEXT, scopus_eid TEXT,
    title TEXT, abstract TEXT, year INTEGER, source_json TEXT, created_at REAL);
CREATE UNIQUE INDEX ux_papers_doi ON papers(doi)
    WHERE doi IS NOT NULL AND doi != '';
CREATE TABLE paper_identifiers (
    id_type TEXT NOT NULL CHECK (id_type IN
        ('DOI','OPENALEX','SCOPUS_EID','PUBMED','ARXIV','ISBN','URL')),
    normalized_value TEXT NOT NULL, paper_uid TEXT NOT NULL, id_value TEXT NOT NULL,
    source TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 1.0,
    is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0,1)),
    first_seen_run TEXT, created_at TEXT NOT NULL,
    PRIMARY KEY (id_type, normalized_value));
CREATE TABLE identity_conflicts (
    conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
    id_type TEXT NOT NULL CHECK (id_type IN
        ('DOI','OPENALEX','SCOPUS_EID','PUBMED','ARXIV','ISBN','URL','TITLE')),
    normalized_value TEXT NOT NULL DEFAULT '', incoming_paper_uid TEXT NOT NULL,
    existing_paper_uid TEXT, incoming_value TEXT, existing_value TEXT,
    conflict_type TEXT NOT NULL CHECK (conflict_type IN
        ('IDENTIFIER_ALREADY_OWNED','MISPLACED_IDENTIFIER','INVALID_IDENTIFIER',
         'TITLE_COLLISION_CANDIDATE','NO_IDENTIFIER')),
    source TEXT, provenance_json TEXT, detected_at TEXT NOT NULL,
    resolution_status TEXT NOT NULL DEFAULT 'UNRESOLVED' CHECK (resolution_status IN
        ('UNRESOLVED','REVIEW_REQUIRED','RESOLVED_MERGED','RESOLVED_KEPT','RESOLVED_INVALID')),
    resolution_decision TEXT, resolver TEXT, resolver_version TEXT);
CREATE TABLE topic_papers (
    topic_id TEXT NOT NULL, paper_id TEXT NOT NULL, relevance_label TEXT,
    label_source TEXT, promotion_status TEXT, first_seen_run TEXT,
    evidence_json TEXT, created_at REAL, PRIMARY KEY (topic_id, paper_id));
"""


# ── fixtures ─────────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def misplaced30():
    """30 条真实错位记录（冻结 fixture，禁止改写）。"""
    with open(FIXTURE, encoding="utf-8") as f:
        payload = json.load(f)
    assert payload["provenance"]["n"] == 30, "fixture 必须恰好 30 条"
    return payload


@pytest.fixture
def empty_db(tmp_path):
    """空 mini KB（结构对齐真库）。"""
    con = sqlite3.connect(str(tmp_path / "kb.db"))
    con.executescript(SCHEMA)
    con.commit()
    yield con
    con.close()


@pytest.fixture
def replayed_db(tmp_path, misplaced30):
    """已回填的 mini KB：30 条真实错位行 + 其 SCOPUS_EID 身份（uid 前缀错），
    外加一条 topic_papers 引用（用于验证 A7）。"""
    con = sqlite3.connect(str(tmp_path / "kb_replayed.db"))
    con.executescript(SCHEMA)
    for i, r in enumerate(misplaced30["records"]):
        con.execute(
            "INSERT INTO papers (paper_id, doi, openalex_id, scopus_eid, title, "
            "source_json, created_at) VALUES (?,?,?,?,?,?,?)",
            (r["existing_paper_uid"], r["misplaced_doi_column_value"], None,
             r["true_identifier_value"], r["title"],
             json.dumps({"sources": ["s8_catalog"]}), 1788839006.3926337))
        # 回填：EID 按真实类型落 SCOPUS_EID；doi 列值非法 DOI -> 无 DOI 身份
        con.execute(
            "INSERT INTO paper_identifiers (id_type, normalized_value, paper_uid, "
            "id_value, source, confidence, is_primary, first_seen_run, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("SCOPUS_EID", r["true_identifier_value"], r["existing_paper_uid"],
             r["true_identifier_value"], "papers.scopus_eid", 1.0, 1,
             "p0a_backfill", "2026-09-10 00:00:00"))
        con.execute(
            "INSERT INTO topic_papers (topic_id, paper_id, relevance_label, created_at) "
            "VALUES (?,?,?,?)",
            ("photopolymerization_shrinkage", r["existing_paper_uid"],
             "UNCERTAIN" if i % 2 else "RELEVANT", 1788839006.3926337))
    con.commit()
    yield con
    con.close()


def _fingerprint(con, table, cols="*", order="1"):
    rows = con.execute(f"SELECT {cols} FROM {table} ORDER BY {order}").fetchall()
    return hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()


def _count(con, table):
    return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ══ A3 / A2：新写入中 EID 绝不进 DOI 列，且不生成 doi: 前缀 ══════════
def test_fresh_replay_eid_never_enters_doi_column(empty_db, misplaced30):
    """A3：把 30 条真实输入喂给入口（空库）-> doi 列必须全空。"""
    for r in misplaced30["records"]:
        out = pw.resolve_or_create_paper(empty_db, r["replay_metadata"],
                                        r["replay_source"])
        assert out.status == pw.STATUS_CREATED, out.note
    empty_db.commit()

    n_eid_in_doi = empty_db.execute(
        "SELECT COUNT(*) FROM papers WHERE doi LIKE '2-s2.0-%'").fetchone()[0]
    assert n_eid_in_doi == 0, "EID 进入了 doi 列 —— R3 被破坏"

    n_doi_nonnull = empty_db.execute(
        "SELECT COUNT(*) FROM papers WHERE doi IS NOT NULL AND doi != ''").fetchone()[0]
    assert n_doi_nonnull == 0, "EID 输入不得在任何行产生 DOI 值"

    # 真实类型被正确归属
    assert _count(empty_db, "papers") == 30
    n_eid_col = empty_db.execute(
        "SELECT COUNT(*) FROM papers WHERE scopus_eid LIKE '2-s2.0-%'").fetchone()[0]
    assert n_eid_col == 30


def test_fresh_replay_never_creates_bad_prefix(empty_db, misplaced30):
    """A2：新写入 UID_PREFIX_MISMATCH 必须为 0。"""
    for r in misplaced30["records"]:
        pw.resolve_or_create_paper(empty_db, r["replay_metadata"], r["replay_source"])
    empty_db.commit()

    bad = []
    for (uid,) in empty_db.execute("SELECT paper_id FROM papers"):
        ok, declared, actual = pw.uid_type_matches_value(uid)
        if not ok:
            bad.append((uid, declared, actual))
    assert bad == [], f"新写入出现前缀错配：{bad[:3]}"

    # 生成形态必须是 scopus:<EID>
    uids = {u for (u,) in empty_db.execute("SELECT paper_id FROM papers")}
    assert all(u.startswith("scopus:2-s2.0-") for u in uids)


# ══ A4 / A7：重放既有 30 条 -> 复用旧 uid，不造第二个，旧数据不变 ══════
def test_replay_reuses_existing_uid(replayed_db, misplaced30):
    """A4：同 EID 已存在（uid 前缀错）-> 必须复用，绝不新建 scopus: 副本。"""
    for r in misplaced30["records"]:
        out = pw.resolve_or_create_paper(replayed_db, r["replay_metadata"],
                                        r["replay_source"])
        assert out.status == pw.STATUS_REUSED, f"{r['existing_paper_uid']} -> {out.status}"
        assert out.paper_uid == r["existing_paper_uid"], (
            f"应复用 {r['existing_paper_uid']}，实际 {out.paper_uid}")
    replayed_db.commit()

    assert _count(replayed_db, "papers") == 30, "重放必须零新建"
    n_scopus_uid = replayed_db.execute(
        "SELECT COUNT(*) FROM papers WHERE paper_id LIKE 'scopus:%'").fetchone()[0]
    assert n_scopus_uid == 0, "同 EID 不应产生第二个 uid"


def test_replay_leaves_papers_and_topic_papers_untouched(replayed_db, misplaced30):
    """A7：旧 uid 及其 topic_papers 引用逐行不变。"""
    before_papers = _fingerprint(replayed_db, "papers", order="paper_id")
    before_uid_set = {u for (u,) in replayed_db.execute("SELECT paper_id FROM papers")}
    before_tp = _fingerprint(replayed_db, "topic_papers", order="paper_id")
    before_ids = _fingerprint(
        replayed_db, "paper_identifiers", order="id_type, normalized_value")

    for r in misplaced30["records"]:
        pw.resolve_or_create_paper(replayed_db, r["replay_metadata"], r["replay_source"])
    replayed_db.commit()

    assert _fingerprint(replayed_db, "papers", order="paper_id") == before_papers
    assert {u for (u,) in replayed_db.execute("SELECT paper_id FROM papers")} == before_uid_set
    assert _fingerprint(replayed_db, "topic_papers", order="paper_id") == before_tp
    assert _fingerprint(
        replayed_db, "paper_identifiers", order="id_type, normalized_value") == before_ids
    assert _count(replayed_db, "papers") == 30


# ══ A6：冲突只记录、不覆盖 ══════════════════════════════════════════
def test_conflict_records_misplaced_without_overwriting(replayed_db, misplaced30):
    """A6：错位必须落 MISPLACED_IDENTIFIER，且保留原始输入、不改写任何既有值。"""
    r = misplaced30["records"][0]
    out = pw.resolve_or_create_paper(replayed_db, r["replay_metadata"], r["replay_source"])
    replayed_db.commit()

    assert out.misplaced and out.misplaced[0]["kind"] == "MISPLACED"
    a = out.misplaced[0]
    assert a["declared_column"] == "doi"
    assert a["declared_type"] == "DOI"
    assert a["actual_type"] == "SCOPUS_EID"
    assert a["raw_value"] == r["misplaced_doi_column_value"]

    row = replayed_db.execute(
        "SELECT id_type, normalized_value, conflict_type, incoming_value, "
        "provenance_json, resolution_status, resolver_version "
        "FROM identity_conflicts WHERE incoming_paper_uid = ? "
        "AND conflict_type = 'MISPLACED_IDENTIFIER'", (r["existing_paper_uid"],)).fetchone()
    assert row is not None, "错位未落冲突表"
    prov = json.loads(row[4])
    assert prov["written_to_declared_column"] is False
    assert prov["raw_input_preserved"] is True
    assert prov["routed_to"] == r["true_identifier_value"]
    assert row[5] == "REVIEW_REQUIRED"
    assert row[6] == pw.RESOLVER_VERSION


def test_schema_pk_prevents_second_owner(replayed_db, misplaced30):
    """A4/A6：`paper_identifiers` 的 PK `(id_type, normalized_value)` **结构性地**
    使「一个外部标识被两个 uid 认领」不可能存在。

    因此 `IDENTIFIER_ALREADY_OWNED` 在真库中不是靠流程纪律避免，而是靠 schema 拒绝。
    入口里的 multi-owner 分支是 defense-in-depth（例如未来导入外部标识层时）。
    """
    eid = misplaced30["records"][0]["true_identifier_value"]
    with pytest.raises(sqlite3.IntegrityError):
        replayed_db.execute(
            "INSERT INTO paper_identifiers (id_type, normalized_value, paper_uid, id_value, "
            "source, confidence, is_primary, created_at) VALUES (?,?,?,?,?,?,?,?)",
            ("SCOPUS_EID", eid, "doi:10.9999/dup", eid, "papers.scopus_eid", 1.0, 1,
             "2026-09-11 00:00:00"))
    replayed_db.rollback()

    owners = pw.lookup_owners(replayed_db, "SCOPUS_EID", eid)
    assert len(owners) == 1, "PK 未能阻止第二 owner"


def test_metadata_spanning_two_uids_requires_review(replayed_db, misplaced30):
    """A6：一次写入的多个标识分别指向**不同**既有论文 -> 这是合并请求，必须人工裁决。

    入口必须拒绝自动合并：返回 CONFLICT_REVIEW、零新建、既有行不变。
    """
    eid = misplaced30["records"][0]["true_identifier_value"]
    old_uid = misplaced30["records"][0]["existing_paper_uid"]
    # 另造一篇有 DOI 的论文
    replayed_db.execute(
        "INSERT INTO papers (paper_id, doi, title, created_at) VALUES (?,?,?,?)",
        ("doi:10.5555/x", "10.5555/x", "Other paper", 1.0))
    replayed_db.execute(
        "INSERT INTO paper_identifiers (id_type, normalized_value, paper_uid, id_value, "
        "source, confidence, is_primary, created_at) VALUES (?,?,?,?,?,?,?,?)",
        ("DOI", "10.5555/x", "doi:10.5555/x", "10.5555/x", "papers.doi", 1.0, 1,
         "2026-09-11 00:00:00"))
    replayed_db.commit()

    before = _fingerprint(replayed_db, "papers", order="paper_id")
    before_n = _count(replayed_db, "papers")

    out = pw.resolve_or_create_paper(
        replayed_db, {"doi": "10.5555/x", "scopus_eid": eid, "title": "Merge?"}, "test")
    replayed_db.commit()

    assert out.status == pw.STATUS_CONFLICT_REVIEW
    assert out.paper_uid is None, "不得自动选定 uid"
    assert "merged=0" in out.note
    assert _count(replayed_db, "papers") == before_n, "不得新建行"
    assert _fingerprint(replayed_db, "papers", order="paper_id") == before, "既有行被改写"
    assert any(c.conflict_type == "IDENTIFIER_ALREADY_OWNED" for c in out.conflicts)
    # 既有 uid 保持原样（前缀错的也不修）
    owners = pw.lookup_owners(replayed_db, "SCOPUS_EID", eid)
    assert owners == [old_uid]


def test_conflict_never_overwrites_existing_value(replayed_db, misplaced30):
    """A6：冲突行只新增，既有 papers 三列一行都不改。"""
    cols_before = replayed_db.execute(
        "SELECT paper_id, doi, openalex_id, scopus_eid FROM papers ORDER BY paper_id"
    ).fetchall()
    for r in misplaced30["records"]:
        pw.resolve_or_create_paper(replayed_db, r["replay_metadata"], r["replay_source"])
    replayed_db.commit()
    cols_after = replayed_db.execute(
        "SELECT paper_id, doi, openalex_id, scopus_eid FROM papers ORDER BY paper_id"
    ).fetchall()
    assert cols_before == cols_after


# ══ A8：幂等 ═══════════════════════════════════════════════════════
def test_replay_is_idempotent(replayed_db, misplaced30):
    """A8：重复导入两次 -> 三张表逐位相同。"""
    for r in misplaced30["records"]:
        pw.resolve_or_create_paper(replayed_db, r["replay_metadata"], r["replay_source"])
    replayed_db.commit()
    snap = {t: _fingerprint(replayed_db, t, order="1")
            for t in ("papers", "paper_identifiers", "identity_conflicts")}

    for r in misplaced30["records"]:
        pw.resolve_or_create_paper(replayed_db, r["replay_metadata"], r["replay_source"])
    replayed_db.commit()
    for t in snap:
        assert _fingerprint(replayed_db, t, order="1") == snap[t], f"{t} 不幂等"


def test_fresh_replay_is_idempotent(empty_db, misplaced30):
    """A8：空库连续两次同输入 -> 不新增行、不新增冲突。"""
    for r in misplaced30["records"]:
        pw.resolve_or_create_paper(empty_db, r["replay_metadata"], r["replay_source"])
    empty_db.commit()
    n_papers = _count(empty_db, "papers")
    n_ids = _count(empty_db, "paper_identifiers")
    n_cf = _count(empty_db, "identity_conflicts")
    assert (n_papers, n_ids, n_cf) == (30, 30, 30)

    for r in misplaced30["records"]:
        pw.resolve_or_create_paper(empty_db, r["replay_metadata"], r["replay_source"])
    empty_db.commit()

    assert _count(empty_db, "papers") == n_papers == 30
    assert _count(empty_db, "paper_identifiers") == n_ids == 30
    assert _count(empty_db, "identity_conflicts") == n_cf == 30


# ══ A5：W-only / EID-only 是一等实体 ═══════════════════════════════
def test_w_only_and_eid_only_are_first_class(empty_db):
    """A5：只有 W-ID 或只有 EID 的论文必须能正常成为一等实体。"""
    out_w = pw.resolve_or_create_paper(
        empty_db, {"openalex_id": "https://openalex.org/W1234567890",
                   "title": "W only"}, "test")
    out_e = pw.resolve_or_create_paper(
        empty_db, {"scopus_eid": "2-s2.0-85000000001", "title": "EID only"}, "test")
    empty_db.commit()

    assert out_w.status == pw.STATUS_CREATED
    assert out_w.paper_uid == "openalex:W1234567890"
    assert out_e.status == pw.STATUS_CREATED
    assert out_e.paper_uid == "scopus:2-s2.0-85000000001"

    for uid in (out_w.paper_uid, out_e.paper_uid):
        assert pw.paper_exists(empty_db, uid)
        owners = pw.lookup_owners(empty_db, *pw.resolve_identifier(
            uid.split(":", 1)[1]))
        assert owners == [uid], f"{uid} 不能被反查 —— 不是一等实体"
    # 各恰好一个 primary
    primaries = empty_db.execute(
        "SELECT paper_uid, COUNT(*) FROM paper_identifiers WHERE is_primary = 1 "
        "GROUP BY paper_uid").fetchall()
    assert sorted(primaries) == sorted([(out_w.paper_uid, 1), (out_e.paper_uid, 1)])


def test_title_only_never_borrows_external_namespace(empty_db):
    """无标识 -> local:<hash>，绝不写 openalex:/scopus: 命名空间。"""
    out = pw.resolve_or_create_paper(empty_db, {"title": "No identifier at all"}, "test")
    empty_db.commit()
    assert out.status == pw.STATUS_CREATED
    assert out.paper_uid.startswith("local:")
    assert not out.paper_uid.startswith(("openalex:", "scopus:", "doi:"))
    assert any(c.conflict_type == "NO_IDENTIFIER" for c in out.conflicts)


# ══ R2/R7：识别优先 + 统一查询 ═════════════════════════════════════
def test_resolve_identifier_trusts_value_not_field_name():
    """R2：字段名不构成事实。"""
    assert pw.resolve_identifier("10.1002/app.12345", "DOI") == ("DOI", "10.1002/app.12345")
    # 声明 DOI，实际是 EID -> 按 EID 归属
    assert pw.resolve_identifier("2-s2.0-12345678", "DOI") == ("SCOPUS_EID", "2-s2.0-12345678")
    # 声明 DOI，实际是 W-ID
    assert pw.resolve_identifier("W1234567890", "DOI") == ("OPENALEX", "W1234567890")
    # 完全不可识别
    assert pw.resolve_identifier("garbage string", "DOI") is None


def test_find_paper_uid_no_prefix_concatenation(replayed_db, misplaced30):
    """R7：任意原始值可反查 uid，调用方无需自己拼 doi:/scopus:。"""
    r = misplaced30["records"][0]
    assert pw.find_paper_uid(replayed_db, r["true_identifier_value"]) == \
        r["existing_paper_uid"]
    assert pw.find_paper_uid(replayed_db, r["misplaced_doi_column_value"]) == \
        r["existing_paper_uid"]
    assert pw.find_paper_uid(replayed_db, "2-s2.0-00000000000") is None


def test_make_paper_uid_is_deterministic_and_priority_ordered():
    c_doi = pw.IdentifierClaim("DOI", "10.1/x", "10.1/x", None, "s")
    c_w = pw.IdentifierClaim("OPENALEX", "W1", "W1", None, "s")
    c_eid = pw.IdentifierClaim("SCOPUS_EID", "2-s2.0-1", "2-s2.0-1", None, "s")
    assert pw.make_paper_uid(claims=[c_doi, c_w, c_eid]) == "doi:10.1/x"
    assert pw.make_paper_uid(claims=[c_w, c_eid]) == "openalex:W1"
    assert pw.make_paper_uid(claims=[c_eid]) == "scopus:2-s2.0-1"
    # 顺序无关（deterministic）
    assert pw.make_paper_uid(claims=[c_eid, c_doi]) == pw.make_paper_uid(claims=[c_doi, c_eid])
    # title-only 稳定
    a = pw.make_paper_uid(title="Hello World", year=2020)
    b = pw.make_paper_uid(title="  hello   world ", year=2020)
    assert a == b


def test_source_is_mandatory(empty_db):
    """无溯源的写入必须被拒绝。"""
    with pytest.raises(ValueError):
        pw.resolve_or_create_paper(empty_db, {"doi": "10.1/x"}, "")


def test_dry_run_writes_nothing(empty_db, misplaced30):
    before = _fingerprint(empty_db, "papers", order="1")
    for r in misplaced30["records"]:
        out = pw.resolve_or_create_paper(empty_db, r["replay_metadata"],
                                        r["replay_source"], dry_run=True)
        assert out.status == pw.STATUS_DRY_RUN
    empty_db.commit()
    assert _fingerprint(empty_db, "papers", order="1") == before
    assert _count(empty_db, "papers") == 0
    assert _count(empty_db, "paper_identifiers") == 0


def test_primary_survives_uid_prefix_mismatch(replayed_db, misplaced30):
    """既有 uid 前缀错（doi:2-s2.0-*）时，primary 应落在可用身份上（SCOPUS_EID）。"""
    r = misplaced30["records"][0]
    out = pw.resolve_or_create_paper(replayed_db, r["replay_metadata"], r["replay_source"])
    assert out.primary_type == "SCOPUS_EID"


def test_misplaced_conflict_uses_declared_type_not_actual_type(replayed_db, misplaced30):
    """口径契约：`identity_conflicts.id_type` = **声明类型**，不是真实类型。

    P0-A 对那 30 条写的是 `id_type='DOI'` + `normalized_value=<EID>` +
    `provenance.looks_like='SCOPUS_EID'`。入口必须同口径，否则：
      * `dedup_key`（含 id_type）匹配不上 -> 同一事实被记两次（30 变 60）
      * 跨阶段审计无法按 id_type 聚合
    本测试把这个约定钉死。
    """
    r = misplaced30["records"][0]
    out = pw.resolve_or_create_paper(replayed_db, r["replay_metadata"], r["replay_source"])
    replayed_db.commit()

    cf = [c for c in out.conflicts if c.conflict_type == "MISPLACED_IDENTIFIER"]
    assert len(cf) == 1
    assert cf[0].id_type == "DOI", "id_type 必须是声明类型（问题所在列），不是真实类型"
    assert cf[0].normalized_value == r["misplaced_doi_column_value"]
    prov = json.loads(cf[0].provenance_json)
    assert prov["looks_like"] == "SCOPUS_EID"
    assert prov["routed_to"] == r["true_identifier_value"]
    assert prov["written_to_declared_column"] is False


def test_conflict_dedup_matches_p0a_convention(tmp_path, misplaced30):
    """口径契约的**后果验证**：按 P0-A 口径预置冲突行 -> 入口重放不得重复记录。

    这是「同一事实在审计中不会从 30 条变成 60 条」的机械保证。
    """
    db = sqlite3.connect(str(tmp_path / "kb_p0a.db"))
    db.executescript(SCHEMA)
    for r in misplaced30["records"]:
        db.execute("INSERT INTO papers (paper_id, doi, scopus_eid, title, created_at) "
                   "VALUES (?,?,?,?,?)",
                   (r["existing_paper_uid"], r["misplaced_doi_column_value"],
                    r["true_identifier_value"], r["title"], 1.0))
        db.execute(
            "INSERT INTO paper_identifiers (id_type, normalized_value, paper_uid, id_value, "
            "source, confidence, is_primary, created_at) VALUES (?,?,?,?,?,?,?,?)",
            ("SCOPUS_EID", r["true_identifier_value"], r["existing_paper_uid"],
             r["true_identifier_value"], "papers.scopus_eid", 1.0, 1, "2026-09-10 00:00:00"))
        # ⬇ 完全按 P0-A 的冻结写法预置冲突
        db.execute(
            "INSERT INTO identity_conflicts (id_type, normalized_value, incoming_paper_uid, "
            "incoming_value, conflict_type, source, provenance_json, detected_at, "
            "resolution_status, resolver, resolver_version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("DOI", r["misplaced_doi_column_value"], r["existing_paper_uid"],
             r["misplaced_doi_column_value"], "MISPLACED_IDENTIFIER", "papers.doi",
             json.dumps({"looks_like": "SCOPUS_EID", "recovered": False}),
             "2026-09-10T16:52:05", "REVIEW_REQUIRED", "p0a_v1", "p0a_v1"))
    db.commit()
    n_before = _count(db, "identity_conflicts")

    for r in misplaced30["records"]:
        out = pw.resolve_or_create_paper(db, r["replay_metadata"], r["replay_source"])
        assert out.created_rows.get("conflicts", 0) == 0, "重复记录了 P0-A 已有的事实"
    db.commit()

    assert _count(db, "identity_conflicts") == n_before == 30, "30 条不得膨胀为 60 条"
    db.close()


# ══ 唯一生产者守卫已迁移（P0-B1b） ═════════════════════════════════
# 这里原先用**文本扫描**检查 ``INSERT INTO papers``。P0-B1b 把它整体移到了
# ``tests/test_p0b1b_static_guards.py``（G1），并改用 AST，原因有两条实证：
#   1. 文本扫描会误伤「解释旧缺陷」的注释与正则常量本身 ——
#      本段下方的守卫常量、以及 tools/verify_p0b1_acceptance.py 里的检测模式
#      都被它报成过违规，逼着白名单越加越长；
#   2. B1b 之后 tools/ 的直写点已**归零**（W1/W2 全部迁移到入口），
#      守卫的期望值从「清单只许缩短」变成「必须为空」。
# 下面保留的是 G1 覆盖不到的**命名陷阱**守卫（两张同名 papers 表，不同库）。

# 引擎缓存层另有同名 papers 表（scopus_cache.db），schema 完全不同。
# ⚠️ 命名陷阱：两库同名不同义 —— 直写守卫必须按「库」而非「表名」界定。
ENGINE_CACHE_PAPERS_WRITERS = {"search_engine/cache.py"}


def test_engine_cache_writer_is_confined_to_cache_db():
    """命名陷阱守卫：search_engine/cache.py 的 papers 写入必须只落在引擎缓存库。

    它绝不引用 knowledge_base.db —— 否则同名表会被误当成事实层。
    """
    for rel in ENGINE_CACHE_PAPERS_WRITERS:
        with open(os.path.join(BASE, rel), encoding="utf-8", errors="ignore") as f:
            src = f.read()
        assert "knowledge_base.db" not in src, f"{rel} 不应触碰事实层 DB"
        assert "scopus_cache.db" in src, f"{rel} 应显式指向引擎缓存库"


def test_facts_layer_and_cache_layer_papers_are_different_tables():
    """固化「同名不同义」事实：两库的 papers 表 schema 不同。"""
    cache_src = open(os.path.join(BASE, "search_engine", "cache.py"),
                     encoding="utf-8").read()
    # 缓存层：paper_id + normalized_json + retrieved_at（3 列）
    assert "normalized_json TEXT NOT NULL" in cache_src
    # 事实层（move 到 identity 层的 DDL）含 scopus_eid / openalex_id
    ident_src = open(os.path.join(BASE, "tools", "migrate_p0a_identifiers.py"),
                     encoding="utf-8").read()
    assert "scopus_eid" in ident_src
