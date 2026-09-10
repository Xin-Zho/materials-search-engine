# -*- coding: utf-8 -*-
"""P0-A: 统一论文身份层 —— 建 paper_identifiers + identity_conflicts 并回填。

用户 2026-09-10 拍板（docs/2026-09-10-identity-and-event-db-proposal.md）:
  P0-A 只解决一件事 ——
    「一个稳定的 paper_uid 可以拥有 DOI / OpenAlex W-ID / Scopus EID 等多个标识，
      并且任一外部标识不能被两个实体静默认领。」

  明确**不做**（留给后续阶段）:
    preprint/published 关系、SI 与正文关系、title-based 自动合并、
    历史 JSON 迁移、query/retrieval 事件回填、Candidate/KB 状态统一。

设计要点（对应用户三条纠正）:
  1. 唯一约束 = ``PRIMARY KEY (id_type, normalized_value)``，原始值存 ``id_value``。
     DOI 大小写 / ``https://doi.org/`` / W-ID URL 形式在 identity.normalize_identifier 消除。
  2. ``relation_type`` 不进这两张表（版本关系属后续 ``paper_relations``）。
  3. title 同题只产出 ``TITLE_COLLISION_CANDIDATE`` + REVIEW_REQUIRED，绝不自动合并。
  4. 冲突只记录不覆盖。

风险定性（用户要求改写）: **低风险、可回滚、暂不影响现有读路径** —— 不是「零风险」。
  故须先备份、先在副本上验收。本工具默认 dry-run；--apply 前自动备份。

用法:
    python tools/migrate_p0a_identifiers.py                       # dry-run（真库，零写入）
    python tools/migrate_p0a_identifiers.py --apply                # 真库落库（自动备份）
    python tools/migrate_p0a_identifiers.py --db <副本> --apply    # 副本上验收
    python tools/migrate_p0a_identifiers.py --db <副本> --apply --no-backup
"""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from search_engine.identity import plan_backfill  # noqa: E402

DEFAULT_DB = os.path.join(BASE, "data/cache/knowledge_base.db")
OUT_REPORT = os.path.join(BASE, "data/exports/schema/p0a_identity_migration_report.json")

SCHEMA_VERSION = "v2.1-identity-2026-09-10"
RESOLVER_VERSION = "p0a_v1"

# 一次性备份用户要求的「S1-S8 冻结产物不动」锚（v1.0 冻结态，migrate_v2 报告亦锚定）
FROZEN_TABLE_ANCHORS = {"knowledge_records": 216, "route_mechanism_edges": 700}

DDL = [
    # ── 标识符：一个 uid 多行标识；唯一约束落在标准化值上 ──────────────
    """CREATE TABLE IF NOT EXISTS paper_identifiers (
        id_type          TEXT NOT NULL
                         CHECK (id_type IN ('DOI','OPENALEX','SCOPUS_EID','PUBMED','ARXIV','ISBN','URL')),
        normalized_value TEXT NOT NULL,
        paper_uid        TEXT NOT NULL,
        id_value         TEXT NOT NULL,
        source           TEXT NOT NULL,
        confidence       REAL NOT NULL DEFAULT 1.0,
        is_primary       INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0,1)),
        first_seen_run   TEXT,
        created_at       TEXT NOT NULL,
        PRIMARY KEY (id_type, normalized_value)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_pid_uid ON paper_identifiers(paper_uid)",
    "CREATE INDEX IF NOT EXISTS ix_pid_primary ON paper_identifiers(paper_uid, is_primary)",
    # ── 冲突：记录「为什么不能写入」，不覆盖原值 ─────────────────────
    """CREATE TABLE IF NOT EXISTS identity_conflicts (
        conflict_id         INTEGER PRIMARY KEY AUTOINCREMENT,
        id_type             TEXT NOT NULL
                            CHECK (id_type IN ('DOI','OPENALEX','SCOPUS_EID','PUBMED','ARXIV','ISBN','URL','TITLE')),
        normalized_value    TEXT NOT NULL DEFAULT '',
        incoming_paper_uid  TEXT NOT NULL,
        existing_paper_uid  TEXT,
        incoming_value      TEXT,
        existing_value      TEXT,
        conflict_type       TEXT NOT NULL CHECK (conflict_type IN
                              ('IDENTIFIER_ALREADY_OWNED','MISPLACED_IDENTIFIER',
                               'INVALID_IDENTIFIER','TITLE_COLLISION_CANDIDATE',
                               'NO_IDENTIFIER')),
        source              TEXT,
        provenance_json     TEXT,
        detected_at         TEXT NOT NULL,
        resolution_status   TEXT NOT NULL DEFAULT 'UNRESOLVED' CHECK (resolution_status IN
                              ('UNRESOLVED','REVIEW_REQUIRED','RESOLVED_MERGED',
                               'RESOLVED_KEPT','RESOLVED_INVALID')),
        resolution_decision TEXT,
        resolver            TEXT,
        resolver_version    TEXT
    )""",
    # 幂等护栏：同一 (冲突类型, id_type, 值, 认领者) 只落一条 -> 重复运行零新增
    """CREATE UNIQUE INDEX IF NOT EXISTS ux_conflict_dedup ON identity_conflicts(
        conflict_type, id_type, normalized_value, incoming_paper_uid)""",
    "CREATE INDEX IF NOT EXISTS ix_conflict_status ON identity_conflicts(resolution_status)",
    "CREATE INDEX IF NOT EXISTS ix_conflict_type ON identity_conflicts(conflict_type)",
]


def read_papers(db, readonly):
    """读取可回填的 papers 行（含迁移前内容指纹）。"""
    uri = f"file:{db}?mode=ro" if readonly else db
    con = sqlite3.connect(uri, uri=readonly)
    try:
        rows = con.execute(
            "SELECT paper_id, doi, openalex_id, scopus_eid, title FROM papers "
            "ORDER BY paper_id").fetchall()
        anchors = {}
        for t in FROZEN_TABLE_ANCHORS:
            try:
                anchors[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.Error:
                anchors[t] = None
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        return rows, anchors, tables
    finally:
        con.close()


def fingerprint(rows):
    """papers 全行内容指纹：证明迁移未修改任何现有行（含 paper_id）。"""
    h = hashlib.sha256()
    for r in rows:
        h.update(("|".join("" if v is None else str(v) for v in r) + "\n").encode("utf-8"))
    return h.hexdigest()


def backup_db(db):
    """一次性备份（用户要求：对真实库执行 DDL 前先备份）。"""
    cand = db + ".preP0A.db"
    if os.path.exists(cand):
        cand = db + ".preP0A.%s.db" % time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(db, cand)
    return cand


def migrate(db, apply_changes, make_backup, report_path, title_collision=True):
    t0 = time.time()
    print(f"[p0a] schema={SCHEMA_VERSION} db={db}")
    if not os.path.exists(db):
        print(f"[ABORT] 数据库不存在: {db}")
        return 2

    rows, anchors, tables = read_papers(db, readonly=True)
    fp_before = fingerprint(rows)
    print(f"[load] papers={len(rows)} 指纹={fp_before[:16]}… 表数={len(tables)}")
    print(f"[load] 冻结锚: {anchors}")

    print("[plan] 构建回填计划（纯计算，零写入）…")
    plan = plan_backfill(rows, title_collision=title_collision,
                         resolver_version=RESOLVER_VERSION)
    st, terms = plan["stats"], plan["terms"]
    print("[plan] 统一口径: " + json.dumps(terms, ensure_ascii=False))
    print("[plan] 回填统计: " + json.dumps(
        {k: v for k, v in st.items() if k != "identifiers_by_type"}, ensure_ascii=False))

    # ── 预检断言（dry-run 也跑）─────────────────────────────────
    n_primary = sum(1 for c in plan["identifiers"] if c.is_primary)
    uids = {c.paper_uid for c in plan["identifiers"]}
    checks = {
        "papers_rows_stable": len(rows) > 0,
        "no_identifierless_rows": st["rows_without_identifier"] == terms["NO_IDENTIFIER"],
        "identifier_pk_unique": len({c.key for c in plan["identifiers"]}) == len(plan["identifiers"]),
        "conflict_dedup_unique": len({c.dedup_key for c in plan["conflicts"]}) == len(plan["conflicts"]),
        "every_uid_has_identity": st["rows"] - st["rows_without_identifier"] == len(uids),
        "one_primary_per_uid": n_primary == len(uids),
        "reverse_map_complete": len(plan["reverse_map"]) == len(plan["identifiers"]),
        "no_auto_merge": not any(c.resolution_status == "RESOLVED_MERGED"
                                 for c in plan["conflicts"]),
    }
    for name, ok in checks.items():
        print(f"  [check] {name}: {'PASS' if ok else 'FAIL'}")

    if not apply_changes:
        print("\n[dry-run] 未写库（只读打开，零写入）。")
        print(f"[dry-run] 预计插入 identifiers={st['identifiers']} conflicts={st['conflicts']}")
        print("[dry-run] 通过即执行 --apply")
        _write_report(report_path, db, fp_before, anchors, plan, checks,
                      applied=False, backup=None, new_ids=0, new_conf=0,
                      elapsed=time.time() - t0)
        return 0 if all(checks.values()) else 3

    backup = backup_db(db) if make_backup else None
    if backup:
        print(f"[backup] {backup}")

    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
    new_ids = new_conf = 0
    con = sqlite3.connect(db)
    try:
        con.execute("BEGIN IMMEDIATE")
        for stmt in DDL:
            con.execute(stmt)   # 注意：executescript 会隐式 commit，禁用
        cur = con.executemany(
            "INSERT OR IGNORE INTO paper_identifiers (id_type, normalized_value, paper_uid, "
            "id_value, source, confidence, is_primary, first_seen_run, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            [(c.id_type, c.normalized_value, c.paper_uid, c.id_value, c.source,
              c.confidence, c.is_primary, c.first_seen_run, now_iso)
             for c in plan["identifiers"]])
        new_ids = cur.rowcount
        cur = con.executemany(
            "INSERT OR IGNORE INTO identity_conflicts (id_type, normalized_value, "
            "incoming_paper_uid, existing_paper_uid, incoming_value, existing_value, "
            "conflict_type, source, provenance_json, detected_at, resolution_status, "
            "resolution_decision, resolver, resolver_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [c.as_row(now_iso) for c in plan["conflicts"]])
        new_conf = cur.rowcount

        # 迁移后立即复验：papers 未被改动 + 冻结表未变
        after, anchors_after, _ = read_papers(db, readonly=False)
        fp_after = fingerprint(after)
        if fp_after != fp_before or len(after) != len(rows):
            raise RuntimeError("papers 表在迁移中被修改（指纹不一致）—— 回滚")
        for t, n in FROZEN_TABLE_ANCHORS.items():
            if anchors_after.get(t) != n:
                raise RuntimeError(f"冻结表 {t} 行数变化 {n} -> {anchors_after.get(t)} —— 回滚")
        con.commit()
        print(f"[commit] 新增 identifiers={new_ids} conflicts={new_conf}（幂等：重复运行为 0）")
    except Exception as e:
        con.rollback()
        print(f"[ROLLBACK] {e}")
        raise
    finally:
        con.close()

    _write_report(report_path, db, fp_before,
                  dict(list(anchors.items()) + [("after_" + k, v) for k, v in
                                                read_papers(db, True)[1].items()]),
                  plan, checks, applied=True, backup=backup,
                  new_ids=new_ids, new_conf=new_conf, elapsed=time.time() - t0)
    print(f"[done] 报告 -> {report_path}（{time.time() - t0:.1f}s）")
    return 0 if all(checks.values()) else 3


def _write_report(path, db, fp_before, anchors, plan, checks, *, applied, backup,
                  new_ids, new_conf, elapsed):
    st, terms = plan["stats"], plan["terms"]
    rows_after, _anchors_after, _ = read_papers(db, readonly=True)
    fp_after = fingerprint(rows_after)
    mm = plan["uid_prefix_mismatches"]
    report = {
        "schema_version": SCHEMA_VERSION,
        "resolver_version": RESOLVER_VERSION,
        "db": os.path.relpath(db, BASE) if str(db).startswith(BASE) else str(db),
        "applied": applied,
        "backup": os.path.relpath(backup, BASE) if backup and str(backup).startswith(BASE) else backup,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "papers_fingerprint_before": fp_before,
        "papers_fingerprint_after": fp_after,
        "papers_unchanged": fp_after == fp_before,
        "frozen_anchors": {k: v for k, v in anchors.items() if not k.startswith("after_")},
        "terms": terms,
        "terms_definitions": {
            "W_PRIMARY": "paper_id 前缀为 openalex: （主键来源 OpenAlex）",
            "W_TRUE_ONLY": "有有效 W-ID 且无有效 DOI 且无有效 EID",
            "MULTI_ID_WITH_W": "有有效 W-ID 且有其他有效身份（DOI 或 EID）",
            "NO_DOI": "无**有效** DOI（doi 列可能非空但装的是错位的 EID）",
            "EFFECTIVE_DOI_PRIMARY": "前缀为 doi: 且 DOI 值确实有效",
            "UID_PREFIX_MISMATCH": "uid 前缀声明的类型与可用身份不符",
            "注": "全部身份判断基于 normalize 通过的有效值，不是「列非空」",
        },
        "stats": st,
        "inserted": {"identifiers": new_ids, "conflicts": new_conf},
        "uid_prefix_mismatches": {
            "count": len(mm),
            "declared_types": _counter([m["declared"] for m in mm]),
            "effective_types": _counter([m["effective"] for m in mm]),
            "sample": sorted({(m["declared"], m["effective"]) for m in mm}),
        },
        "checks": checks,
        "scope_note": (
            "P0-A 只建立身份层：多标识归属 + 认领唯一性。未做：preprint/published 关系、"
            "SI/正文关系、title 自动合并、历史 JSON 迁移、query/retrieval 事件回填、"
            "Candidate/KB 状态统一。"
        ),
        "known_gaps_deferred": {
            "version_relations": "TITLE_COLLISION_CANDIDATE 冲突行即待建 paper_relations 的输入；"
                                 "本阶段不产出 IDENTICAL/VERSION_OF/ERRATUM_OF/PART_OF 判定",
            "misplaced_columns": "MISPLACED_IDENTIFIER 冲突行记录了「列装错标识」的事实；"
                                 "uid 前缀更正需另立阶段（uid 被 topic_papers 引用，不可就地改）",
            "history_json": "270 个 JSON 事实源未导入（P0-B/P1），导入须记录 "
                            "source_file/file_hash/record_locator/imported_at/importer_version/run_stage",
            "scopus_cache": "713MB 检索缓存不并库（可重建，非权威事实）",
        },
        "elapsed_sec": round(elapsed, 2),
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _counter(items):
    c = {}
    for i in items:
        c[i] = c.get(i, 0) + 1
    return c


def main():
    ap = argparse.ArgumentParser(description="P0-A 统一身份层迁移（默认 dry-run）")
    ap.add_argument("--db", default=DEFAULT_DB, help="目标库（默认真库）")
    ap.add_argument("--apply", action="store_true", help="落库；缺省为 dry-run")
    ap.add_argument("--no-backup", action="store_true", help="跳过自动备份（不建议）")
    ap.add_argument("--no-title-collision", action="store_true",
                    help="不产出 title 同题候选冲突")
    ap.add_argument("--report", default=OUT_REPORT)
    args = ap.parse_args()
    return migrate(args.db, args.apply, not args.no_backup, args.report,
                   title_collision=not args.no_title_collision)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
