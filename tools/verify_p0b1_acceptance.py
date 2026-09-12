#!/usr/bin/env python3
"""P0-B1 验收器：在**真库副本**上验证「唯一身份写入入口」的 8 项验收标准。

零副作用：绝不触碰 data/cache/knowledge_base.db，只在临时副本上操作。

验收标准（用户 2026-09-11 裁决）
--------------------------------
  A1 所有新增论文写入都有唯一生产者
  A2 新写入中 UID_PREFIX_MISMATCH = 0
  A3 EID 不可能进入有效 DOI 列
  A4 相同 DOI/W-ID/EID 不会创建第二个 UID
  A5 W-only 与 EID-only 能正常成为一等实体
  A6 冲突只记录、不覆盖
  A7 旧 331 个 UID 及其 topic_papers 引用完全不变
  A8 重复导入幂等

驱动数据：tests/fixtures/p0b1_misplaced_30.json
         （30 条真实「EID 写进 DOI 列」错位模式，从真库抽取后冻结）

用法：
  python tools/verify_p0b1_acceptance.py                      # 副本上跑（默认）
  python tools/verify_p0b1_acceptance.py --report <path>      # 指定报告路径
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from search_engine import paper_writer as pw  # noqa: E402

DEFAULT_DB = os.path.join(BASE, "data/cache/knowledge_base.db")
FIXTURE = os.path.join(BASE, "tests/fixtures/p0b1_misplaced_30.json")
DEFAULT_REPORT = os.path.join(BASE, "data/exports/schema/p0b1_acceptance_report.json")

def _rel(path):
    """展示用相对路径。

    ⚠️ Windows 上 ``os.path.relpath`` **跨盘符会抛 ValueError**（副本可能在 C:、
    仓库在 D:）。所有接受 --db/--out 的工具都必须能对**任意路径的副本**运行，
    否则「先在副本上验证」这个安全惯例就无法执行。
    """
    try:
        return os.path.relpath(path, BASE).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


RESULTS = []


def check(idx, name, passed, detail=""):
    RESULTS.append({"id": idx, "name": name, "passed": bool(passed), "detail": detail})
    print(f"  [{idx:>2}] {'PASS' if passed else 'FAIL'} | {name}"
          + (f" | {detail}" if detail else ""))
    return passed


def fp(con, table, order="1"):
    rows = con.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()
    return hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()


def count(con, table):
    return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def load_fixture():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def load_30_rows(con, fixture):
    """按 fixture 定位真库中这 30 条论文 —— 用 **EID 值**匹配，跨 uid 变更稳定。

    P0-B2.2 之前用 ``existing_paper_uid`` 匹配（uid = ``doi:2-s2.0-*``）。
    迁移把 uid 修正为 ``scopus:2-s2.0-*`` 之后，按 uid 匹配得到**空集** ——
    而空集会让后续所有检查在"零行"上通过（**假绿**）。
    因此改用 fixture 里冻结的 ``true_identifier_value``（EID，迁移不变）。
    """
    eids = [r["true_identifier_value"] for r in fixture["records"]]
    q = (f"SELECT paper_uid FROM paper_identifiers WHERE id_type = 'SCOPUS_EID' "
         f"AND normalized_value IN ({','.join('?' * len(eids))})")
    return {r[0] for r in con.execute(q, eids)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DEFAULT_DB, help="源真库（只读复制，绝不改写）")
    ap.add_argument("--report", default=DEFAULT_REPORT)
    args = ap.parse_args()

    src_sha = hashlib.sha256(open(args.db, "rb").read()).hexdigest()
    fixture = load_fixture()
    print(f"[p0b1] 源库 {_rel(args.db)} sha256={src_sha[:16]}…")
    print(f"[p0b1] fixture n={fixture['provenance']['n']} "
          f"sha256={fixture['provenance'].get('fixture_sha256', '')[:16]}…")

    tmpdir = tempfile.mkdtemp(prefix="p0b1_verify_")
    work_db = os.path.join(tmpdir, "kb_copy.db")
    shutil.copy2(args.db, work_db)
    empty_db = os.path.join(tmpdir, "kb_empty.db")

    con = sqlite3.connect(work_db)
    con.execute("PRAGMA journal_mode=MEMORY")

    # ── 基线快照（真库副本）─────────────────────────────
    before = {
        "papers": fp(con, "papers", "paper_id"),
        "topic_papers": fp(con, "topic_papers", "paper_id"),
        "paper_identifiers": fp(con, "paper_identifiers", "id_type, normalized_value"),
        "n_papers": count(con, "papers"),
        "n_tp": count(con, "topic_papers"),
        "n_ids": count(con, "paper_identifiers"),
        "n_cf": count(con, "identity_conflicts"),
        "uids": {r[0] for r in con.execute("SELECT paper_id FROM papers")},
    }
    present30 = load_30_rows(con, fixture)
    print(f"[p0b1] 基线: papers={before['n_papers']} topic_papers={before['n_tp']} "
          f"identifiers={before['n_ids']} conflicts={before['n_cf']} "
          f"| fixture 命中 {len(present30)}/30")
    print()

    # ── 防"空集假绿"（P0-B2.2 实测教训）────────────────────
    # 本验收器的多数检查是「对 present30 这批行做 X」。一旦定位函数返回空集，
    # 所有检查都会在零行上"通过" —— P0-B2.2 把 uid 从 doi:2-s2.0-* 修正为
    # scopus:2-s2.0-* 之后，按 uid 匹配曾让这里静默变成 0/30 却报 16/16 PASS。
    # 所以把「命中数」本身升为一条断言。
    check(0, "fixture 命中 30/30（防空集假绿）", len(present30) == 30,
          f"命中 {len(present30)}/30；若为 0 则其后各检查均无意义")

    # ══ A1 唯一生产者 ═══════════════════════════════════
    # P0-B1b：扫描改用 **AST** 而非文本匹配。理由不是洁癖 ——
    # 迁移脚本的头注释必须逐字引用旧代码（``con.execute("insert into papers ...")``）
    # 才能说明「这里曾经错在哪」，文本扫描会把这种解释性注释当成新的直写点，
    # 守卫于是被迫加豁免，最终失去意义。
    print("[A1] 唯一生产者")
    import ast
    import re
    pat = re.compile(r"INSERT\s+(OR\s+\w+\s+)?INTO\s+papers\b", re.I)

    def _docstring_ids(tree):
        ids = set()
        owners = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        for node in ast.walk(tree):
            if not isinstance(node, owners):
                continue
            body = getattr(node, "body", None) or []
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                ids.add(id(body[0].value))
        return ids

    def _sql_writers(dirpath):
        hits = set()
        for root, dirs, files in os.walk(dirpath):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for fn in sorted(files):
                if not fn.endswith(".py"):
                    continue
                p = os.path.join(root, fn)
                try:
                    tree = ast.parse(open(p, encoding="utf-8", errors="ignore").read())
                except SyntaxError:
                    continue
                doc_ids = _docstring_ids(tree)
                for node in ast.walk(tree):
                    if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                            and id(node) not in doc_ids and pat.search(node.value)):
                        hits.add(_rel(p))
                        break
        return hits

    prod_hits = _sql_writers(os.path.join(BASE, "search_engine"))
    tool_hits = _sql_writers(os.path.join(BASE, "tools"))
    check(1, "A1 事实层 papers 唯一生产写入者 = paper_writer.py",
          prod_hits == {"search_engine/paper_writer.py", "search_engine/cache.py"},
          f"search_engine/ 命中 {sorted(prod_hits)}（cache.py 写引擎缓存库，非事实层）")
    check(2, "A1 tools/ 直写点已归零（W1/W2 全部迁移到入口）",
          tool_hits == set(),
          f"tools/ 命中 {sorted(tool_hits) if tool_hits else '（空）'}")

    # ══ A4 / A6 / A7 / A8：真库副本上重放 30 条真实输入 ═════
    print("\n[A4/A6/A7/A8] 在真库副本上重放那 30 条真实错位输入")
    statuses = {}
    first_pass_conflicts = 0
    for r in fixture["records"]:
        out = pw.resolve_or_create_paper(con, r["replay_metadata"], r["replay_source"],
                                         first_seen_run="p0b1_verify")
        statuses[out.status] = statuses.get(out.status, 0) + 1
        first_pass_conflicts += out.created_rows.get("conflicts", 0)
    con.commit()

    replayed_ids = {u for (u,) in con.execute("SELECT paper_id FROM papers")}
    check(3, "A4 同 EID 不创建第二个 UID（30 条全部复用旧 uid）",
          statuses.get(pw.STATUS_REUSED) == 30 and replayed_ids == before["uids"],
          f"statuses={statuses}；新增 uid {len(replayed_ids - before['uids'])} 个")

    check(4, "A7 旧 331 个 UID 完全不变",
          replayed_ids == before["uids"] and fp(con, "papers", "paper_id") == before["papers"],
          f"papers 行数 {before['n_papers']} -> {count(con, 'papers')}")

    check(5, "A7 topic_papers 引用完全不变",
          fp(con, "topic_papers", "paper_id") == before["topic_papers"],
          f"topic_papers {before['n_tp']} -> {count(con, 'topic_papers')}")

    check(6, "A6 冲突只记录、不覆盖（既有 identifier 一行未改）",
          fp(con, "paper_identifiers", "id_type, normalized_value") == before["paper_identifiers"],
          f"identifiers {before['n_ids']} -> {count(con, 'paper_identifiers')}")

    # 冲突必须落表（30 条 MISPLACED 已由 P0-A 记录 -> 本次幂等不新增）
    n_mis_cf = con.execute(
        "SELECT COUNT(*) FROM identity_conflicts WHERE conflict_type='MISPLACED_IDENTIFIER'"
    ).fetchone()[0]
    check(7, "A6 错位事实在冲突表中可查（MISPLACED_IDENTIFIER）",
          n_mis_cf >= 30 and first_pass_conflicts == 0,
          f"MISPLACED 行 {n_mis_cf}；本次新增冲突 {first_pass_conflicts}（应为 0，P0-A 已记录）")

    # ══ A8 幂等：第二次重放应完全无变化 ═══════════════════
    snap = {t: fp(con, t, o) for t, o in (
        ("papers", "paper_id"), ("topic_papers", "paper_id"),
        ("paper_identifiers", "id_type, normalized_value"), ("identity_conflicts", "1"))}
    for r in fixture["records"]:
        pw.resolve_or_create_paper(con, r["replay_metadata"], r["replay_source"],
                                   first_seen_run="p0b1_verify")
    con.commit()
    idem = all(fp(con, t, o) == snap[t] for t, o in (
        ("papers", "paper_id"), ("topic_papers", "paper_id"),
        ("paper_identifiers", "id_type, normalized_value"), ("identity_conflicts", "1")))
    check(8, "A8 重复导入幂等（四张表逐位相同）", idem)

    check(9, "A7 源真库零改动（只操作副本）",
          hashlib.sha256(open(args.db, "rb").read()).hexdigest() == src_sha)

    # ══ A2 / A3：空库上重放 -> 新写入的检验 ════════════════
    print("\n[A2/A3] 在空库上重放 30 条（检验**新写入**的形态）")
    # 注意：con 保持打开，A5 还要用它读真库副本
    shutil.copy2(work_db, empty_db)          # 借结构
    e = sqlite3.connect(empty_db)
    for t in ("papers", "paper_identifiers", "identity_conflicts", "topic_papers"):
        e.execute(f"DELETE FROM {t}")
    e.commit()
    for r in fixture["records"]:
        pw.resolve_or_create_paper(e, r["replay_metadata"], r["replay_source"],
                                   first_seen_run="p0b1_verify")
    e.commit()

    new_uids = [u for (u,) in e.execute("SELECT paper_id FROM papers")]
    bad_prefix = [(u, pw.uid_type_matches_value(u)[1]) for u in new_uids
                  if not pw.uid_type_matches_value(u)[0]]
    check(10, "A2 新写入 UID_PREFIX_MISMATCH = 0", bad_prefix == [],
          f"新建 {len(new_uids)} 个 uid；错配 {len(bad_prefix)}")
    check(11, "A2 新 uid 形态正确（scopus:<EID>）",
          bool(new_uids) and all(u.startswith("scopus:2-s2.0-") for u in new_uids))

    n_eid_in_doi = e.execute(
        "SELECT COUNT(*) FROM papers WHERE doi LIKE '2-s2.0-%'").fetchone()[0]
    n_doi_any = e.execute(
        "SELECT COUNT(*) FROM papers WHERE doi IS NOT NULL AND doi != ''").fetchone()[0]
    check(12, "A3 EID 不可能进入有效 DOI 列", n_eid_in_doi == 0 and n_doi_any == 0,
          f"doi 列形态为 EID 的行 {n_eid_in_doi}；任何 DOI 值 {n_doi_any}（应全 0）")

    n_eid_col = e.execute(
        "SELECT COUNT(*) FROM papers WHERE scopus_eid LIKE '2-s2.0-%'").fetchone()[0]
    check(13, "A3 EID 被正确归属到 scopus_eid 列", n_eid_col == 30, f"scopus_eid 命中 {n_eid_col}/30")

    # ══ A5：W-only / EID-only 一等实体（真库副本上按既有行验证）══
    print("\n[A5] W-only / EID-only 一等实体")
    w_only = [r[0] for r in con.execute(
        "SELECT paper_id FROM papers WHERE openalex_id IS NOT NULL AND openalex_id != '' "
        "AND (doi IS NULL OR doi = '')")]
    eid_only = [r[0] for r in con.execute(
        "SELECT paper_id FROM papers WHERE scopus_eid IS NOT NULL AND scopus_eid != '' "
        "AND (doi IS NULL OR doi = '') AND (openalex_id IS NULL OR openalex_id = '')")]

    def _all_resolvable(uids):
        if not uids:
            return False, []
        bad = [u for u in uids
               if pw.find_paper_uid(con, u.split(":", 1)[1]) != u]
        return (not bad), bad

    w_ok, w_bad = _all_resolvable(w_only)
    e_ok, e_bad = _all_resolvable(eid_only)
    check(14, "A5 W-only 可反查为 uid（一等实体）", w_ok and len(w_only) > 0,
          f"W-only {len(w_only)} 条；不可反查 {len(w_bad)}")
    check(15, "A5 EID-only 可反查为 uid（一等实体）", e_ok and len(eid_only) > 0,
          f"EID-only {len(eid_only)} 条；不可反查 {len(e_bad)}")

    # 重放期间除冲突表外不应有任何增长
    tp_delta = count(con, "topic_papers") - before["n_tp"]
    check(16, "A6/A7 重放期间 topic_papers 零变化", tp_delta == 0, f"delta={tp_delta}")

    # ══ 汇总 ════════════════════════════════════════════
    passed = sum(1 for r in RESULTS if r["passed"])
    total = len(RESULTS)
    print(f"\n[p0b1] {passed}/{total} PASS | all_passed={passed == total}")

    report = {
        "role": "P0-B1 统一身份写入入口验收报告",
        "verifier_version": pw.RESOLVER_VERSION,
        "db": _rel(args.db),
        "db_sha256": src_sha,
        "fixture": _rel(FIXTURE),
        "fixture_sha256": fixture["provenance"].get("fixture_sha256"),
        "baseline": {k: v for k, v in before.items() if k != "uids"},
        "summary": {"passed": passed, "total": total, "all_passed": passed == total},
        "checks": RESULTS,
    }
    os.makedirs(os.path.dirname(args.report), exist_ok=True)
    with open(args.report, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"[p0b1] 报告 -> {_rel(args.report)}")

    e.close()
    con.close()
    shutil.rmtree(tmpdir, ignore_errors=True)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
