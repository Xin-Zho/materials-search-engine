#!/usr/bin/env python3
"""P0-B2.2：Type A 30 条 uid 重映射（列错位 + uid 前缀错标的**同步**修复）。

用户 2026-09-12 裁定（只做这一批，做完即退出 P0）：
  * Type A  30 条  -> **修**（真错误：列名声明一种类型，值形态是另一种）
  * Type B  18 条  -> **不迁移**。性质是 ``identity_policy_drift``（策略差异），
                      不是 corruption。entity_id 与 preferred_identifier 是两个概念：
                      ``paper_uid`` 像 git commit hash（永不变），
                      「最优引用标识」像 branch pointer（可变）——
                      把两者混进 UID 会把稳定标识变成可变量。
  * Type C   3 组  -> **不自动合并**，全部保留为 duplicate candidate（identified / not resolved）

修复前 -> 修复后：

    papers.paper_id    doi:2-s2.0-X       -> scopus:2-s2.0-X
    papers.doi         2-s2.0-X（错位）    -> NULL
    papers.scopus_eid  2-s2.0-X（已正确）  -> 不变

**必须同时改列与 uid**：只清列会留下「前缀声明 DOI、实际身份是 EID」的 uid；
只改 uid 则列里仍是错值。

影响面（已实测，非估计）——4 张表，v1 事实层零触及：

    papers             30 行   paper_id, doi
    paper_identifiers  30 行   paper_uid
    topic_papers       30 行   paper_id（R4 / U26）
    identity_conflicts 30 行   incoming_paper_uid + resolution_status/decision
    knowledge_claims / knowledge_records / route_mechanism_edges  0 行

安全设计：
  * 默认 **dry-run**（只读，零写入）
  * ``--apply`` 前自动备份 ``<db>.preP0B22.db``
  * 单事务；任何断言失败 -> rollback
  * 幂等：第二次运行检出 0 条 -> no-op
  * 迁移后立即验证（Type A 归零 + 引用表行数不变 + owner 正确）

用法：
    .venv/Scripts/python tools/migrate_p0b2_2_type_a.py            # dry-run
    .venv/Scripts/python tools/migrate_p0b2_2_type_a.py --apply    # 执行
输出：
    data/exports/schema/p0b2_2_migration_report.json
"""
from __future__ import annotations

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
sys.path.insert(0, os.path.join(BASE, "tools"))

import audit_identity_report as audit  # noqa: E402
from search_engine import paper_writer as pw  # noqa: E402
from search_engine.identity import ID_TYPES, normalize_identifier  # noqa: E402

DEFAULT_DB = os.path.join(BASE, "data/cache/knowledge_base.db")
DEFAULT_OUT = os.path.join(BASE, "data/exports/schema/p0b2_2_migration_report.json")
MIGRATION_VERSION = "p0b2_2_v1"
BACKUP_SUFFIX = ".preP0B22.db"

# 被重映射的 uid 出现在这四张表的哪些列（少一张就会留下悬空引用）
UID_REF_COLUMNS = (
    ("papers", "paper_id"),
    ("paper_identifiers", "paper_uid"),
    ("topic_papers", "paper_id"),
    ("identity_conflicts", "incoming_paper_uid"),
)
COUNTED_TABLES = ("papers", "topic_papers", "paper_identifiers", "identity_conflicts",
                  "knowledge_claims", "knowledge_records", "route_mechanism_edges")


def _rel(path):
    """相对 BASE 的展示路径。

    ⚠️ Windows 上 ``os.path.relpath`` 跨盘符会抛 ValueError（副本可能在 C:、
    仓库在 D:）—— 迁移工具必须能对任意路径的副本运行，所以这里兜底。
    """
    try:
        return os.path.relpath(path, BASE).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_plan(con):
    """只读：产出 Type A 重映射计划 + 预检（零写入）。"""
    papers = audit._load_papers(con)
    existing_uids = {p["paper_id"] for p in papers}
    plan = []

    for p in papers:
        old_uid = p["paper_id"]
        # ── 1. 找出**列错位**（列名声明一种类型，值形态是另一种）──
        misplaced = []
        for col, declared in audit.COLUMNS:
            raw = p.get(col)
            if raw is None or not str(raw).strip():
                continue
            if normalize_identifier(declared, str(raw)) is not None:
                continue
            look = next((t for t in ID_TYPES if t != declared
                         and normalize_identifier(t, str(raw)) is not None), None)
            misplaced.append({"column": col, "declared_type": declared,
                              "value": str(raw), "looks_like": look})
        if not misplaced:
            continue

        # ── 2. 按**有效值口径**重算 uid（走入口的规则，不特判）──
        meta = {c: p.get(c) for c, _ in audit.COLUMNS}
        meta["title"] = p.get("title")
        meta["year"] = p.get("year")
        claims, _anomalies = pw.classify_metadata(meta)
        new_uid = pw.make_paper_uid(claims=claims, title=p.get("title"),
                                    year=p.get("year"))

        # ── 3. 列值：**只清空被判定错位的列**，其余列原样不动 ──
        cols_after = {c: p.get(c) for c, _ in audit.COLUMNS}
        for m in misplaced:
            cols_after[m["column"]] = None

        ok, declared_type, actual_type = pw.uid_type_matches_value(old_uid)
        plan.append({
            "old_uid": old_uid,
            "new_uid": new_uid,
            "uid_changed": new_uid != old_uid,
            "uid_prefix_was_ok": ok,
            "uid_declared_type": declared_type,
            "uid_actual_type": actual_type,
            "misplaced": misplaced,
            "columns_before": {c: p.get(c) for c, _ in audit.COLUMNS},
            "columns_after": cols_after,
        })

    # ── 预检 ──────────────────────────────────────────────
    new_uids = [r["new_uid"] for r in plan]
    changed = [r for r in plan if r["uid_changed"]]
    checks = {
        "no_new_uid_collides_with_existing": not (
            set(new_uids) & (existing_uids - {r["old_uid"] for r in plan})),
        "new_uids_unique": len(new_uids) == len(set(new_uids)),
        "all_rows_have_new_uid": all(r["new_uid"] for r in plan),
        "all_misplaced_recoverable": all(
            m["looks_like"] for r in plan for m in r["misplaced"]),
        "uid_changed_count_matches": len(changed) == len(plan),
    }
    return plan, checks


def snapshot_counts(con):
    return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in COUNTED_TABLES}


def apply_plan(con, plan, resolver_version=MIGRATION_VERSION, now=None):
    """单事务执行重映射。调用方负责 BEGIN/COMMIT 与 rollback。"""
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(
        float(now if now is not None else time.time())))
    stats = {"papers": 0, "paper_identifiers": 0, "topic_papers": 0,
             "identity_conflicts": 0}

    for r in plan:
        old, new = r["old_uid"], r["new_uid"]
        if not r["uid_changed"]:
            continue
        c = r["columns_after"]

        # 1) 引用表先跟过来（本表无 FK，顺序不影响最终一致性）
        cur = con.execute("UPDATE paper_identifiers SET paper_uid = ? WHERE paper_uid = ?",
                          (new, old))
        stats["paper_identifiers"] += cur.rowcount
        cur = con.execute("UPDATE topic_papers SET paper_id = ? WHERE paper_id = ?",
                          (new, old))
        stats["topic_papers"] += cur.rowcount

        # 2) 冲突记录：换 uid + 落裁决（只记录不覆盖的原则不变 —— 原值仍在
        #    incoming_value/normalized_value 里，此处只更新"指向"与"状态"）
        cur = con.execute(
            "UPDATE identity_conflicts SET incoming_paper_uid = ?, resolution_status = ?, "
            "resolution_decision = ?, resolver = ?, resolver_version = ? "
            "WHERE incoming_paper_uid = ?",
            (new, "RESOLVED_KEPT",
             json.dumps({"action": "column_misplacement_repaired",
                         "cleared_columns": [m["column"] for m in r["misplaced"]],
                         "routed_as": sorted({m["looks_like"] for m in r["misplaced"]
                                              if m["looks_like"]}),
                         "uid": {"from": old, "to": new},
                         "note": "标识本身保留；仅归位到其真实类型的列与 uid 前缀"},
                        ensure_ascii=False),
             pw.RESOLVER_NAME, resolver_version, old))
        stats["identity_conflicts"] += cur.rowcount

        # 3) 最后改 papers 自身（PK 变更）
        cur = con.execute(
            "UPDATE papers SET paper_id = ?, doi = ?, openalex_id = ?, scopus_eid = ? "
            "WHERE paper_id = ?",
            (new, c["doi"], c["openalex_id"], c["scopus_eid"], old))
        stats["papers"] += cur.rowcount

    return stats


def validate(con, before_counts, plan):
    """迁移后验证：Type A 归零 + 引用表行数不变 + owner 指向正确。"""
    after_counts = snapshot_counts(con)
    papers = audit._load_papers(con)
    a = audit.scan_a_column_mismatch(con, papers)
    uids = {p["paper_id"] for p in papers}
    new_uids = [r["new_uid"] for r in plan if r["uid_changed"]]
    old_uids = [r["old_uid"] for r in plan if r["uid_changed"]]

    # 每条新 uid 必须：存在于 papers、且其 SCOPUS_EID 标识行指向它
    owner_ok = True
    for r in plan:
        if not r["uid_changed"]:
            continue
        row = con.execute("SELECT paper_uid FROM paper_identifiers WHERE id_type = ? "
                          "AND normalized_value = ?",
                          ("SCOPUS_EID", r["new_uid"].split(":", 1)[1])).fetchone()
        if row is None or row[0] != r["new_uid"]:
            owner_ok = False
            break

    dangling = {}
    for tbl, col in UID_REF_COLUMNS:
        bad = [x for x in old_uids if con.execute(
            f"SELECT 1 FROM {tbl} WHERE {col} = ?", (x,)).fetchone()]
        dangling[f"{tbl}.{col}"] = len(bad)

    return {
        "counts_before": before_counts,
        "counts_after": after_counts,
        "counts_unchanged": before_counts == after_counts,
        "type_a_after": a["count"],
        "new_uids_present": all(u in uids for u in new_uids),
        "old_uids_fully_gone": all(u not in uids for u in old_uids),
        "identifier_owner_correct": owner_ok,
        "residual_refs_to_old_uid": dangling,
        "conflicts_resolved": con.execute(
            "SELECT COUNT(*) FROM identity_conflicts WHERE resolution_status = ?",
            ("RESOLVED_KEPT",)).fetchone()[0],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--apply", action="store_true", help="执行写入（默认 dry-run）")
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    src_sha = _sha256(args.db)
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row

    plan, pre = build_plan(con)
    before_counts = snapshot_counts(con)

    print(f"[p0b2.2] {MIGRATION_VERSION}  db={_rel(args.db)}  "
          f"sha256={src_sha[:16]}…")
    print(f"\n检出 Type A: {len(plan)} 条（预检 {'OK' if all(pre.values()) else 'FAIL'}）")
    for k, v in pre.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")

    if plan:
        print("\n计划（前 5 条）:")
        for r in plan[:5]:
            cols = ", ".join(f"{m['column']}: {m['declared_type']} <- {m['looks_like']}"
                             for m in r["misplaced"])
            print(f"  {r['old_uid']:<28} -> {r['new_uid']:<28} [{cols}]")

    if not all(pre.values()):
        print("\n[ABORT] 预检失败，拒绝执行")
        return 2

    if not args.apply:
        print(f"\n[dry-run] 未写库。将影响: papers {len(plan)} / "
              f"paper_identifiers {len(plan)} / topic_papers {len(plan)} / "
              f"identity_conflicts {len(plan)}")
        print("[dry-run] 通过即执行 --apply")
        return 0

    if not plan:
        print("\n[ok] 无 Type A 可迁移（幂等：重复运行 = no-op）")
        return 0

    backup = args.db + BACKUP_SUFFIX
    shutil.copy2(args.db, backup)
    print(f"\n[backup] -> {os.path.basename(backup)}  sha256={_sha256(backup)[:16]}…")

    try:
        con.execute("BEGIN")
        stats = apply_plan(con, plan)
        val = validate(con, before_counts, plan)
        checks = {
            "type_a_zero": val["type_a_after"] == 0,
            "counts_unchanged": val["counts_unchanged"],
            "new_uids_present": val["new_uids_present"],
            "old_uids_fully_gone": val["old_uids_fully_gone"],
            "identifier_owner_correct": val["identifier_owner_correct"],
            "no_residual_refs": all(v == 0 for v in
                                    val["residual_refs_to_old_uid"].values()),
            "conflicts_resolved_30": val["conflicts_resolved"] == len(plan),
        }
        if not all(checks.values()):
            con.rollback()
            print("\n[ROLLBACK] 迁移后验证失败：")
            for k, v in checks.items():
                if not v:
                    print(f"  FAIL  {k}")
            print(json.dumps(val, ensure_ascii=False, indent=1)[:1500])
            return 3
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

    print(f"\n[applied] stats={stats}")
    print("验证:")
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    print(f"  引用表行数: {val['counts_after']}")
    print(f"  identity_conflicts RESOLVED_KEPT: {val['conflicts_resolved']}")

    report = {
        "migration_version": MIGRATION_VERSION,
        "migrated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "db": _rel(args.db),
        "source_sha256_before": src_sha,
        "backup": os.path.basename(backup),
        "scope": "Type A only（用户裁定：Type B 不迁移 / Type C 不自动合并）",
        "n_migrated": len([r for r in plan if r["uid_changed"]]),
        "stats": stats,
        "preflight": pre,
        "validation": val,
        "checks": checks,
        "passed": all(checks.values()),
        "plan": plan,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n[{'OK' if report['passed'] else 'FAIL'}] 报告 -> {_rel(args.out)}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
