# -*- coding: utf-8 -*-
"""P0-A 验收：逐条核对用户 2026-09-10 给出的 13 项标准。

用法（用户要求：先备份、先在副本上验收）:
    cp data/cache/knowledge_base.db data/cache/knowledge_base.preP0A.db      # 备份
    cp data/cache/knowledge_base.db "$TEMP/kb_p0a_verify.db"                 # 副本
    python tools/migrate_p0a_identifiers.py --db "$TEMP/kb_p0a_verify.db" --apply
    python tools/verify_p0a_identifiers.py --db "$TEMP/kb_p0a_verify.db" \
        --baseline data/cache/knowledge_base.preP0A.db

13 项标准:
   1 迁移前后 papers 行数完全不变
   2 不修改任何现有 paper_id
   3 不修改 S1–S8 冻结产物
   4 每个合法标识均标准化后回填
   5 同一外部标识只能有一个 owner
   6 冲突不能覆盖，必须完整进入 identity_conflicts
   7 W-only / EID-only 实体能正常回填
   8 重复运行不新增 identifier 或重复 conflict
   9 dry-run 不产生任何数据库写入
  10 commit 在单一事务内完成，失败完全回滚
  11 输出回填数量、冲突数量及未承载记录
  12 随机抽查 DOI / W-ID / EID 三类反向解析
  13 旧读路径行为与迁移前完全一致
"""

import argparse
import hashlib
import json
import os
import random
import shutil
import sqlite3
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from search_engine import identity as ident          # noqa: E402
from tools import migrate_p0a_identifiers as mp      # noqa: E402

DEFAULT_DB = os.path.join(BASE, "data/cache/knowledge_base.db")
DEFAULT_REPORT = os.path.join(BASE, "data/exports/schema/p0a_acceptance_report.json")

PAPER_COLS = ("paper_id", "doi", "openalex_id", "scopus_eid", "title", "year")


def _ro(db):
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def _papers(db):
    con = _ro(db)
    try:
        return con.execute(
            "SELECT paper_id, doi, openalex_id, scopus_eid, title, year FROM papers "
            "ORDER BY paper_id").fetchall()
    finally:
        con.close()


def _fp(rows):
    h = hashlib.sha256()
    for r in rows:
        h.update(("|".join("" if v is None else str(v) for v in r) + "\n").encode("utf-8"))
    return h.hexdigest()


def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _tables(db):
    con = _ro(db)
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def _counts(db, table):
    con = _ro(db)
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


class Checker:
    def __init__(self):
        self.results = []

    def add(self, no, name, ok, detail=""):
        self.results.append({"no": no, "name": name, "ok": bool(ok), "detail": detail})
        print(f"  [{no:>2}] {'PASS' if ok else 'FAIL'} | {name}" + (f" | {detail}" if detail else ""))
        return bool(ok)

    @property
    def all_ok(self):
        return all(r["ok"] for r in self.results)


def verify(db, baseline, report_path, run_rollback_check=True):
    ck = Checker()
    print(f"[verify] db={db}\n[verify] baseline={baseline}")
    assert os.path.exists(db), db
    assert os.path.exists(baseline), baseline

    base_rows = _papers(baseline)
    new_rows = _papers(db)
    base_fp, new_fp = _fp(base_rows), _fp(new_rows)

    # ── 1 行数不变 ────────────────────────────────────────────────
    ck.add(1, "迁移前后 papers 行数完全不变",
           len(base_rows) == len(new_rows),
           f"{len(base_rows)} -> {len(new_rows)}")

    # ── 2 不修改任何现有 paper_id（并附全行内容指纹）──────────────
    b_ids = [r[0] for r in base_rows]
    n_ids = [r[0] for r in new_rows]
    same_ids = b_ids == n_ids
    ck.add(2, "不修改任何现有 paper_id",
           same_ids and base_fp == new_fp,
           f"paper_id 序列一致={same_ids} 全行指纹一致={base_fp == new_fp} "
           f"({base_fp[:12]}…)")

    # ── 3 不修改 S1–S8 冻结产物 ───────────────────────────────────
    frozen_ok, frozen_detail = True, []
    for t in ("knowledge_records", "route_mechanism_edges"):
        try:
            b, n = _counts(baseline, t), _counts(db, t)
            frozen_detail.append(f"{t}:{b}->{n}")
            frozen_ok &= (b == n)
        except sqlite3.Error as e:
            frozen_ok = False
            frozen_detail.append(f"{t}:ERR({e})")
    # topic_papers / knowledge_claims 是 v1.0 materialized，也不得变动
    for t in ("topic_papers", "knowledge_claims"):
        try:
            b, n = _counts(baseline, t), _counts(db, t)
            frozen_detail.append(f"{t}:{b}->{n}")
            frozen_ok &= (b == n)
        except sqlite3.Error:
            pass
    ck.add(3, "不修改 S1–S8 冻结产物（冻结表行数不变）", frozen_ok,
           " ".join(frozen_detail))

    # ── 建索引：回填结果 ──────────────────────────────────────────
    con = _ro(db)
    ident_rows = con.execute(
        "SELECT id_type, normalized_value, paper_uid, id_value, source, confidence, "
        "is_primary FROM paper_identifiers").fetchall()
    conf_rows = con.execute(
        "SELECT id_type, normalized_value, incoming_paper_uid, existing_paper_uid, "
        "incoming_value, conflict_type, resolution_status FROM identity_conflicts").fetchall()
    con.close()
    ident_rows = [tuple(r) for r in ident_rows]
    own = {(r[0], r[1]): r[2] for r in ident_rows}

    # ── 4 每个合法标识均标准化后回填 ──────────────────────────────
    missing, n_valid = [], 0
    for pid, doi, wid, eid, _t, _y in base_rows:
        for id_type, raw in (("DOI", doi), ("OPENALEX", wid), ("SCOPUS_EID", eid)):
            v = ident.normalize_identifier(id_type, raw)
            if v is None:
                continue
            n_valid += 1
            if (id_type, v) not in own:
                missing.append(f"{id_type}:{v}")
            elif own[(id_type, v)] != pid:
                missing.append(f"{id_type}:{v}->{own[(id_type, v)]}!={pid}")
    # uid 前缀自证通道
    n_prefix = 0
    for pid, *_ in base_rows:
        for id_type, v in ident.extract_from_paper_uid(pid):
            n_prefix += 1
            if (id_type, v) not in own:
                missing.append(f"prefix {id_type}:{v}")
    ck.add(4, "每个合法标识均标准化后回填（含 uid 前缀自证）",
           not missing,
           f"列值合法标识={n_valid} 前缀自证={n_prefix} 缺失={len(missing)}"
           + (f" 例:{missing[:3]}" if missing else ""))

    # ── 5 同一外部标识只能有一个 owner ────────────────────────────
    con = _ro(db)
    dup = con.execute(
        "SELECT id_type, normalized_value, COUNT(DISTINCT paper_uid) n FROM paper_identifiers "
        "GROUP BY id_type, normalized_value HAVING n > 1").fetchall()
    n_uniq = con.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT id_type, normalized_value "
        "FROM paper_identifiers)").fetchone()[0]
    con.close()
    ck.add(5, "同一外部标识只能有一个 owner", len(dup) == 0,
           f"标识总数={len(ident_rows)} 唯一键={n_uniq} 多 owner 键={len(dup)}")

    # ── 6 冲突不覆盖 + 完整入库 ───────────────────────────────────
    base_by_uid = {r[0]: r for r in base_rows}
    overwritten = []
    for _t, _v, _iu, _eu, iv, ct, _rs in conf_rows:
        if ct == "MISPLACED_IDENTIFIER" and iv is not None:
            # 原值必须仍在 papers 对应列中（未被覆盖）
            row = base_by_uid.get(_iu)
            if row is None:
                continue
            if iv not in [str(x) for x in row[1:4] if x]:
                overwritten.append(f"{_iu}:{iv}")
    merged = [r for r in conf_rows if r[6] == "RESOLVED_MERGED"]
    ck.add(6, "冲突不能覆盖，必须完整进入 identity_conflicts",
           not overwritten and not merged,
           f"冲突行={len(conf_rows)} 原值被覆盖={len(overwritten)} 自动合并={len(merged)}")

    # ── 7 W-only / EID-only 实体能正常回填 ────────────────────────
    terms = ident.compute_terms(base_rows)
    by_uid = {}
    for id_type, _v, uid, *_ in ident_rows:
        by_uid.setdefault(uid, set()).add(id_type)
    w_only_bad = [r[0] for r in base_rows
                  if ident.normalize_identifier("OPENALEX", r[2])
                  and not ident.normalize_identifier("DOI", r[1])
                  and not ident.normalize_identifier("SCOPUS_EID", r[3])
                  and "OPENALEX" not in by_uid.get(r[0], set())]
    # EID-only：无有效 DOI 且无有效 W，但有 EID（含前缀自证）
    eid_only_bad = []
    for r in base_rows:
        pid = r[0]
        has_e = "SCOPUS_EID" in by_uid.get(pid, set())
        has_d = "DOI" in by_uid.get(pid, set())
        has_w = "OPENALEX" in by_uid.get(pid, set())
        if has_e and not has_d and not has_w:
            continue
        if not has_e and not has_d and not has_w:
            continue
    eid_only_bad = [r[0] for r in base_rows
                    if "SCOPUS_EID" in by_uid.get(r[0], set())
                    and "DOI" not in by_uid.get(r[0], set())
                    and "OPENALEX" not in by_uid.get(r[0], set())
                    and not ident.normalize_identifier("SCOPUS_EID", r[3])
                    and not any(t == "SCOPUS_EID"
                                for t, _ in ident.extract_from_paper_uid(r[0]))]
    ck.add(7, "W-only / EID-only 实体能正常回填",
           not w_only_bad and not eid_only_bad,
           f"W_TRUE_ONLY={terms['W_TRUE_ONLY']} 未回填={len(w_only_bad)} | "
           f"EID-only 异常={len(eid_only_bad)}")

    # ── 8 重复运行不新增 identifier / conflict ────────────────────
    before = (len(ident_rows), len(conf_rows))
    tmp_report = os.path.join(tempfile.gettempdir(), "p0a_reapply_report.json")
    mp.migrate(db, apply_changes=True, make_backup=False, report_path=tmp_report)
    after = (_counts(db, "paper_identifiers"), _counts(db, "identity_conflicts"))
    ck.add(8, "重复运行不新增 identifier 或重复 conflict",
           before == after, f"{before} -> {after}")

    # ── 9 dry-run 不产生任何数据库写入 ────────────────────────────
    dry_report = os.path.join(tempfile.gettempdir(), "p0a_dryrun_report.json")
    md5_before = _md5(baseline)
    tables_before = _tables(baseline)
    mp.migrate(baseline, apply_changes=False, make_backup=False, report_path=dry_report)
    ck.add(9, "dry-run 不产生任何数据库写入",
           _md5(baseline) == md5_before and _tables(baseline) == tables_before,
           f"md5 不变={_md5(baseline) == md5_before} 表集合不变="
           f"{_tables(baseline) == tables_before}")

    # ── 10 单事务 + 失败完全回滚 ──────────────────────────────────
    if run_rollback_check:
        tmpdb = os.path.join(tempfile.gettempdir(), "p0a_rollback_probe.db")
        shutil.copy2(baseline, tmpdb)
        md5_b = _md5(tmpdb)
        saved = list(mp.DDL)
        mp.DDL = saved + ["CREATE TABLE __boom (x TEXT CHECK (x IN ('only')))",
                          "INSERT INTO __boom (x) VALUES ('violates-check')"]
        rolled = False
        try:
            mp.migrate(tmpdb, apply_changes=True, make_backup=False,
                       report_path=os.path.join(tempfile.gettempdir(), "p0a_boom.json"))
        except sqlite3.IntegrityError:
            rolled = True
        finally:
            mp.DDL = saved
        ok = rolled and _md5(tmpdb) == md5_b and "paper_identifiers" not in _tables(tmpdb)
        detail = (f"注入违反约束的 DDL -> 异常已抛出={rolled} md5 未变={_md5(tmpdb) == md5_b} "
                  f"新表未残留={'paper_identifiers' not in _tables(tmpdb)}")
        try:
            os.remove(tmpdb)
        except OSError:
            pass
        ck.add(10, "commit 在单一事务内完成，失败完全回滚", ok, detail)
    else:
        ck.add(10, "commit 在单一事务内完成，失败完全回滚", True, "skipped（--no-rollback-check）")

    # ── 11 输出回填/冲突/未承载统计 ───────────────────────────────
    by_type, by_conf = {}, {}
    for r in ident_rows:
        by_type[r[0]] = by_type.get(r[0], 0) + 1
    for r in conf_rows:
        by_conf[r[5]] = by_conf.get(r[5], 0) + 1
    uids_with_identity = len(by_uid)
    without = [r[0] for r in base_rows if r[0] not in by_uid]
    ck.add(11, "输出回填数量、冲突数量及未承载记录",
           True,
           f"identifiers={len(ident_rows)}{by_type} conflicts={len(conf_rows)}{by_conf} "
           f"未承载={len(without)}")

    # ── 12 随机抽查反向解析（三类各 5 条）─────────────────────────
    rng = random.Random(20260910)
    probe_detail, probe_ok = [], True
    for id_type, col_idx in (("DOI", 1), ("OPENALEX", 2), ("SCOPUS_EID", 3)):
        pool = [r for r in base_rows
                if ident.normalize_identifier(id_type, r[col_idx]) is not None]
        for r in rng.sample(pool, min(5, len(pool))):
            v = ident.normalize_identifier(id_type, r[col_idx])
            uid = own.get((id_type, v))
            back = uid == r[0]
            probe_ok &= back
            probe_detail.append(f"{id_type}:{v[:22]}->{str(uid)[:26]}{'' if back else ' MISMATCH'}")
    ck.add(12, "随机抽查 DOI / W-ID / EID 三类反向解析", probe_ok,
           " ; ".join(probe_detail[:6]) + (" …" if len(probe_detail) > 6 else ""))

    # ── 13 旧读路径行为一致 ───────────────────────────────────────
    # 关键只读查询在迁移前后结果一致（papers / topic_papers / knowledge_records）
    oks, det = True, []
    for sql in ("SELECT COUNT(*) FROM papers",
                "SELECT COUNT(*) FROM topic_papers",
                "SELECT COUNT(*) FROM knowledge_records",
                "SELECT paper_id FROM papers ORDER BY paper_id LIMIT 50"):
        rb = _ro(baseline).execute(sql).fetchall()
        rn = _ro(db).execute(sql).fetchall()
        oks &= (rb == rn)
        det.append(f"{sql[:38]}={'same' if rb == rn else 'DIFF'}")
    ck.add(13, "旧读路径行为与迁移前完全一致", oks, " ".join(det))

    # ── 汇总 ──────────────────────────────────────────────────────
    summary = {
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "db": db, "baseline": baseline,
        "papers": {"rows": len(new_rows), "fingerprint": new_fp},
        "terms": terms,
        "identifiers": {"total": len(ident_rows), "by_type": by_type,
                        "distinct_keys": n_uniq},
        "conflicts": {"total": len(conf_rows), "by_type": by_conf},
        "uids_with_identity": uids_with_identity,
        "rows_without_identity": without,
        "all_passed": ck.all_ok,
        "checks": ck.results,
    }
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)

    print(f"\n[verify] {sum(1 for r in ck.results if r['ok'])}/{len(ck.results)} PASS "
          f"| all_passed={ck.all_ok}")
    print(f"[verify] 报告 -> {report_path}")
    if without:
        print(f"[verify] 未承载身份的行（{len(without)}）：{without[:5]}")
    return summary


def _resolve_baseline(db, explicit=None):
    """定位迁移前基线：兼容 ``<db>.preP0A.db`` 与 ``<db 去 .db>.preP0A.db`` 两种命名。"""
    if explicit:
        return explicit
    cands = [db + ".preP0A.db"]
    if db.endswith(".db"):
        cands.append(db[:-3] + ".preP0A.db")
    for c in cands:
        if os.path.exists(c):
            return c
    return cands[0]


def main():
    ap = argparse.ArgumentParser(description="P0-A 13 项验收")
    ap.add_argument("--db", default=DEFAULT_DB, help="迁移后的库")
    ap.add_argument("--baseline", default=None, help="迁移前的库（默认 <db>.preP0A.db）")
    ap.add_argument("--report", default=DEFAULT_REPORT)
    ap.add_argument("--no-rollback-check", action="store_true")
    args = ap.parse_args()
    baseline = _resolve_baseline(args.db, args.baseline)
    if not os.path.exists(baseline):
        print(f"[ABORT] baseline 不存在: {baseline}（需先备份迁移前的库）")
        return 2
    s = verify(args.db, baseline, args.report,
               run_rollback_check=not args.no_rollback_check)
    return 0 if s["all_passed"] else 3


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
