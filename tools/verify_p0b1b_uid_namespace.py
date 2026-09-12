#!/usr/bin/env python3
"""P0-B1b 验收：uid 命名空间收口后的「重放差异 = 已知缺陷集」证明。

本工具回答一个必须被机械回答的问题：
    **把迁移脚本改走唯一身份入口之后，重建出来的库与现库差在哪？**

答案必须是「差异恰好等于已知缺陷集合，且差异方向是从错到对」，而不是
「看起来差不多」。因此本工具做**集合代数**，不做抽样目测。

两个差异集，**性质完全不同，不可混为一谈**：

  DELTA-A  UID_PREFIX_MISMATCH（真错配）
           ``doi:2-s2.0-*`` -> ``scopus:2-s2.0-*``
           uid 前缀声明的类型与值的真实形态不符。P0-A 已冻结记录，30 条。
           判据：同一集合的两种形态（去掉前缀后值集合相等）。

  DELTA-B  PRIMARY_CHOICE_INCONSISTENT（规则差异，非形态错误）
           ``openalex:W*`` -> ``doi:*``
           前缀与值形态**都合法**，只是旧写入者按 key 形态硬编码前缀，
           而新规则（make_paper_uid）按 PRIMARY_PRIORITY 取 DOI 优先。
           这 18 条**不违反** uid_type_matches_value，因此不属于 P0-A 的
           30 条错配；它是一处需要用户裁定的口径不一致（见报告 notes）。

安全：全程只读真库。运行前后校验 sha256 不变。
用法：
    .venv/Scripts/python tools/verify_p0b1b_uid_namespace.py
输出：
    data/exports/schema/p0b1b_uid_namespace_report.json
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tools"))

from search_engine import paper_writer as pw  # noqa: E402
from search_engine.identity import uid_id_type  # noqa: E402

KB = os.path.join(BASE, "data/cache/knowledge_base.db")
OUT = os.path.join(BASE, "data/exports/schema/p0b1b_uid_namespace_report.json")

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


VERIFIER_VERSION = "p0b1b_uid_ns_v1"

# 已知冻结量（P0-A / P0-B1 已记录）；差异必须**恰好等于**它们，多一条少一条都算失败。
# DELTA-A 的期望值**不写死**：它恒等于「当前库里 Type A 的数量」。
#   * P0-B2.2 之前：库里有 30 条列错位 -> 重放会纠正 30 条（差异 30）
#   * P0-B2.2 之后：库里 Type A = 0     -> 重放无差异（差异 0）
# 写死 30 会让本验收器在缺陷修好后变成"必须失败"，从而失去长期价值。
EXPECTED_DELTA_A_LEGACY = 30   # 仅作为历史锚：P0-B2.2 前的冻结量
EXPECTED_DELTA_B = 18


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_tool(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(BASE, "tools", name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _current_type_a_count():
    """当前库的 Type A 数量（经 audit_identity_report，唯一判定口径）。

    DELTA-A 的期望值由它派生：DELTA-A 恒等于「重放会纠正的条数」，
    而重放只纠正 Type A —— 所以两者必须相等，且随库状态自适应。
    """
    import audit_identity_report as audit
    con = sqlite3.connect(f"file:{KB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    n = audit.scan_a_column_mismatch(con, audit._load_papers(con))["count"]
    con.close()
    return n


def delta_a_from_w1():
    """W1（migrate_v2_schema）重放规划 uid  vs  现库 uid。"""
    w1 = _load_tool("migrate_v2_schema")
    kb, catalog, _r06 = w1.load_sources()
    wm, dm = w1.build_cache_meta()
    ents, order = w1.build_entities(kb, catalog, wm, dm)
    plan = {w1.canonical_paper_id(ents[k]) for k in order}

    con = sqlite3.connect(f"file:{KB}?mode=ro", uri=True)
    old_all = {r[0] for r in con.execute("SELECT paper_id FROM papers")}
    w2_rows = {r[0] for r in con.execute(
        "SELECT paper_id FROM papers WHERE source_json LIKE '%r06_disposition%'")}
    con.close()

    only_old = (old_all - plan) - w2_rows          # 现库有、重放无（除去 W2 的 19 行）
    only_plan = plan - old_all
    # 判据：同一集合的两种形态（剥掉前缀后值集合相等）
    same_set = ({u.split(":", 1)[1] for u in only_old}
                == {u.split(":", 1)[1] for u in only_plan})
    return {
        "plan_n": len(plan),
        "db_n": len(old_all),
        "db_w2_origin_n": len(w2_rows),
        "shared_n": len(plan & old_all),
        "only_in_db": sorted(only_old),
        "only_in_plan": sorted(only_plan),
        "delta_n": len(only_old),
        "same_identifier_set_two_forms": same_set,
        "bad_prefix_side": {p: sum(1 for u in only_old if u.split(":", 1)[0] == p)
                            for p in ("doi", "openalex", "scopus", "local")},
        "good_prefix_side": {p: sum(1 for u in only_plan if u.split(":", 1)[0] == p)
                             for p in ("doi", "openalex", "scopus", "local")},
    }


def delta_b_from_w2():
    """W2（disposition_r06_funnel）重放规划 uid  vs  现库 W2 行 uid。"""
    w2 = _load_tool("disposition_r06_funnel")
    # 让 dry-run 侧写日志落到临时目录，避免污染仓库 append-only 日志
    tmp = tempfile.mkdtemp(prefix="p0b1b_verify_")
    w2.LOG_PATH = type(w2.LOG_PATH)(os.path.join(tmp, "log.json"))

    ev = w2.build_evidence()
    queue = w2.load_json(w2.QUEUE_PATH)
    proposals = []
    for p in w2.load_json(w2.DETAIL_PATH):
        if p["in_kb"]:
            continue
        proposals.append({"key": p["paper_id"], "kind": "external_R_unscreened",
                          "rule": "R1_external_audit_R", "action": "promote",
                          "label": "RELEVANT", "doi": p.get("doi"),
                          "title": p.get("title", "")})
    for eid, entry in sorted(queue["entries"].items()):
        v = w2.rule_evaluate(eid, entry, ev)
        proposals.append({"key": eid, "kind": "queue_entry", **v,
                          "doi": entry.get("doi"),
                          "title": ev[3].get(eid, {}).get("title", "")})
    to_write = [p for p in proposals if p["action"].startswith("promote")]

    diffs, same = [], 0
    for p in to_write:
        key = p["key"]
        doi = p.get("doi") or ""
        meta = {"doi": doi or None, "title": p.get("title", "")}
        if key.startswith("W"):
            meta["openalex_id"] = key
        else:
            meta["scopus_eid"] = key
        claims, _ = pw.classify_metadata(meta)
        plan_uid = pw.make_paper_uid(claims=claims, title=meta["title"])

        con = sqlite3.connect(f"file:{KB}?mode=ro", uri=True)
        row = con.execute("SELECT paper_id FROM papers WHERE source_json LIKE "
                          "'%r06_disposition%' AND paper_id LIKE ?",
                          ("%" + key,)).fetchone()
        con.close()
        if row and row[0] == plan_uid:
            same += 1
        elif row:
            diffs.append({"key": key, "in_db": row[0], "plan": plan_uid,
                          "has_doi": bool(doi)})
    return {"to_write_n": len(to_write), "same_n": same, "delta_n": len(diffs),
            "delta": diffs}


def main():
    before = _sha256(KB)
    print(f"[verify] {VERIFIER_VERSION}  (真库只读, sha256={before[:16]}…)")

    expected_a = _current_type_a_count()
    print(f"[verify] 当前库 Type A = {expected_a}（DELTA-A 的期望值由它派生，"
          f"不写死）")

    a = delta_a_from_w1()
    print(f"\n[DELTA-A] W1 重放  plan={a['plan_n']}  db={a['db_n']}"
          f"  shared={a['shared_n']}")
    print(f"          差异 {a['delta_n']} 条  "
          f"（现库侧前缀 {a['bad_prefix_side']} -> 重放侧 {a['good_prefix_side']}）")
    print(f"          同一标识集合的两种形态: {a['same_identifier_set_two_forms']}")

    b = delta_b_from_w2()
    print(f"\n[DELTA-B] W2 重放  to_write={b['to_write_n']}  一致={b['same_n']}"
          f"  差异={b['delta_n']} 条")
    for d in b["delta"][:5]:
        print(f"          {d['key']:<14} {d['in_db']}  ->  {d['plan']}")

    checks = {
        "A_delta_eq_current_type_a": a["delta_n"] == expected_a,
        "A_same_identifier_set": a["same_identifier_set_two_forms"] is True,
        "A_bad_side_all_doi_prefix":
            a["bad_prefix_side"].get("doi", 0) == expected_a,
        "A_good_side_all_scopus_prefix":
            a["good_prefix_side"].get("scopus", 0) == expected_a,
        "A_no_collateral_change":
            a["shared_n"] == a["plan_n"] - expected_a,
        "B_delta_eq_known_18": b["delta_n"] == EXPECTED_DELTA_B,
        "B_all_are_doi_upgrades":
            all(uid_id_type(d["plan"]) == "DOI"
                and uid_id_type(d["in_db"]) == "OPENALEX"
                for d in b["delta"]),
    }
    print("\n[checks]")
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")

    after = _sha256(KB)
    checks["source_db_unchanged"] = (before == after)
    print(f"  {'PASS' if checks['source_db_unchanged'] else 'FAIL'}  source_db_unchanged")

    report = {
        "verifier_version": VERIFIER_VERSION,
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source_db": {"path": _rel(KB),
                      "sha256": before, "unchanged": before == after},
        "delta_a_uid_prefix_mismatch": a,
        "delta_b_primary_choice_inconsistent": b,
        "expected": {"delta_a": expected_a, "delta_b": EXPECTED_DELTA_B,
                     "delta_a_legacy_anchor": EXPECTED_DELTA_A_LEGACY},
        "checks": checks,
        "passed": all(checks.values()),
        "notes": [
            "DELTA-A 的性质：uid 前缀声明类型与值真实形态不符（真错配）。"
            "P0-A 已冻结为 30 条 UID_PREFIX_MISMATCH，处置属 P0-B2（uid 重映射）。",
            "DELTA-B 的性质：前缀与值形态**都合法**，旧写入者按 key 形态硬编码前缀，"
            "新规则按 PRIMARY_PRIORITY 取 DOI 优先。**不属于** P0-A 的错配定义，"
            "是一处需要用户裁定的口径不一致（保留 W 为主 vs 统一 DOI 优先）。",
            "两者都不是本阶段回归：真库经入口 REUSE 路径保护，uid 不变；"
            "本报告只量化「从零重建会差多少」，用于证明收口的副作用是**有界且已知**的。",
        ],
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n[{'OK' if report['passed'] else 'FAIL'}] -> "
          f"{_rel(OUT)}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
