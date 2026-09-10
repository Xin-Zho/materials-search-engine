# -*- coding: utf-8 -*-
"""P0-A 缺口量化：身份层落地后，还剩哪些问题 P0-A 解决不了。

用户 2026-09-10 要求：「完成后不要立刻上 P0-B，先证明新的两张表确实有写入者、
可反向解析全部已知身份，并量化仍无法解决的版本关系与历史 JSON 缺口。」

本工具量化三类缺口（只读，不写库）:
  A. 写入者证据   —— paper_identifiers / identity_conflicts 的实际行数、来源分布
  B. 反向解析覆盖 —— 从 papers 全量身份反查表，覆盖率必须 100%
  C. 未解决缺口   ——
       C1 版本关系：同题组（TITLE_COLLISION_CANDIDATE）与 preprint/正式版候选
       C2 历史 JSON：S1–S8 各阶段 JSON 中的唯一身份，多少未进身份层
       C3 错位列    ：MISPLACED_IDENTIFIER（需 uid 前缀更正，但 uid 被引用不可就地改）

用法:
    python tools/audit_identity_gaps.py --db data/cache/knowledge_base.db
"""

import argparse
import glob
import json
import os
import re
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from search_engine import identity as ident  # noqa: E402

DEFAULT_DB = os.path.join(BASE, "data/cache/knowledge_base.db")
DEFAULT_REPORT = os.path.join(BASE, "data/exports/schema/p0a_gap_audit_report.json")

# JSON 中常见的身份字段名（大小写不敏感）
FIELD_MAP = {
    "doi": "DOI",
    "openalex_id": "OPENALEX", "openalex": "OPENALEX", "wid": "OPENALEX",
    "w_id": "OPENALEX", "openalex_work_id": "OPENALEX",
    "scopus_eid": "SCOPUS_EID", "eid": "SCOPUS_EID", "scopus": "SCOPUS_EID",
}
# 复合字段（值本身可能是 DOI/W/EID 任一种）
MIXED_FIELDS = ("key", "paper_key", "paper_id", "uid", "id", "canonical_id")
# preprint 特征（用于版本关系候选）
PREPRINT_MARKERS = ("ssrn", "chemrxiv", "biorxiv", "medrxiv", "researchsquare",
                    "10.26434", "10.2139", "preprint", "10.22541")


def _stage_of(fname):
    """按文件名判定阶段（用于把「未导入」拆成检索命中 vs 已判定）。"""
    f = fname.lower()
    m = re.match(r"^s(\d)", f)
    if m:
        return f"S{m.group(1)}"
    if f.startswith(("r0", "audit")):
        return "AUDIT"
    if f.startswith("qgs"):
        return "S7"
    if "citation" in f:
        return "BRIDGE"
    if "candidate" in f or f.startswith("discovery"):
        return "CANDIDATE"
    return "OTHER"


def _scan_json(path):
    """从一个 JSON 文件收集身份（返回 (ids, n_objects_with_id)）。"""
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception:
        return set(), 0
    out = set()
    n_obj = 0
    stack = [(d, 0)]
    while stack:
        o, depth = stack.pop()
        if depth > 6:
            continue
        if isinstance(o, dict):
            hit = False
            for k, v in o.items():
                if not isinstance(v, str) or not v.strip():
                    continue
                kl = k.lower()
                t = FIELD_MAP.get(kl)
                if t is None and kl in MIXED_FIELDS:
                    t = ident.detect_actual_type(v)
                if t is None:
                    continue
                nv = ident.normalize_identifier(t, v)
                if nv:
                    out.add((t, nv))
                    hit = True
            if hit:
                n_obj += 1
            for v in o.values():
                if isinstance(v, (dict, list)):
                    stack.append((v, depth + 1))
        elif isinstance(o, list):
            for v in o:
                if isinstance(v, (dict, list)):
                    stack.append((v, depth + 1))
    return out, n_obj


def audit(db, report_path, json_glob="data/exports/**/*.json", max_files=400):
    print(f"[audit] db={db}")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)

    # ── A. 写入者证据 ─────────────────────────────────────────────
    try:
        id_rows = con.execute(
            "SELECT id_type, source, confidence, is_primary FROM paper_identifiers").fetchall()
        conf_rows = con.execute(
            "SELECT conflict_type, resolution_status FROM identity_conflicts").fetchall()
    except sqlite3.Error as e:
        print(f"[ABORT] 身份表未建立（先跑 migrate_p0a_identifiers.py --apply）: {e}")
        return 2
    by_source, by_type, by_conf, by_status = {}, {}, {}, {}
    for t, s, _c, p in id_rows:
        by_type[t] = by_type.get(t, 0) + 1
        by_source[s] = by_source.get(s, 0) + 1
    for ct, rs in conf_rows:
        by_conf[ct] = by_conf.get(ct, 0) + 1
        by_status[rs] = by_status.get(rs, 0) + 1

    # ── B. 反向解析覆盖 ───────────────────────────────────────────
    papers = con.execute(
        "SELECT paper_id, doi, openalex_id, scopus_eid, title FROM papers "
        "ORDER BY paper_id").fetchall()
    own = {(t, v): u for t, v, u in con.execute(
        "SELECT id_type, normalized_value, paper_uid FROM paper_identifiers")}
    missing, n_valid = [], 0
    for pid, doi, wid, eid, _t in papers:
        cands = [(t, ident.normalize_identifier(t, raw))
                 for t, raw in (("DOI", doi), ("OPENALEX", wid), ("SCOPUS_EID", eid))]
        cands += ident.extract_from_paper_uid(pid)
        for t, v in cands:
            if not v:
                continue
            n_valid += 1
            if own.get((t, v)) != pid:
                missing.append(f"{t}:{v}")
    coverage = (n_valid - len(missing)) / n_valid if n_valid else 1.0

    # ── C1. 版本关系缺口 ──────────────────────────────────────────
    title_conf = [r for r in conf_rows if r[0] == "TITLE_COLLISION_CANDIDATE"]
    con2 = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    groups = con2.execute(
        "SELECT normalized_value, incoming_paper_uid, existing_paper_uid, provenance_json "
        "FROM identity_conflicts WHERE conflict_type='TITLE_COLLISION_CANDIDATE'").fetchall()
    version_candidates = []
    for tnorm, inc, ex, prov in groups:
        members = [inc, ex]
        try:
            members = json.loads(prov)["members"]
        except Exception:
            pass
        kinds = []
        for m in members:
            low = str(m).lower()
            kinds.append("PREPRINT_LIKE" if any(k in low for k in PREPRINT_MARKERS)
                         else ("SI_LIKE" if low.endswith(".s001") else "PUBLISHED_LIKE"))
        version_candidates.append({
            "title_norm": tnorm[:90], "members": members, "kinds": kinds,
            "needs": ("PART_OF/IDENTICAL 判定" if "SI_LIKE" in kinds
                      else "VERSION_OF 判定（preprint vs published）"
                      if "PREPRINT_LIKE" in kinds else "人工确认（同题非同文？）"),
        })
    con2.close()

    # ── C2. 历史 JSON 缺口 ────────────────────────────────────────
    files = sorted(glob.glob(os.path.join(BASE, json_glob), recursive=True))
    files = [f for f in files if "/releases/" not in f.replace("\\", "/")][:max_files]
    per_file, all_ids, by_stage = {}, set(), {}
    for f in files:
        ids, n_obj = _scan_json(f)
        if not ids:
            continue
        rel = os.path.relpath(f, BASE).replace("\\", "/")
        per_file[rel] = {"unique_ids": len(ids), "objects_with_id": n_obj}
        all_ids |= ids
        st = _stage_of(os.path.basename(rel))
        g = by_stage.setdefault(st, {"files": 0, "unique_ids": set()})
        g["files"] += 1
        g["unique_ids"] |= ids
    covered = {k for k in all_ids if k in own}
    uncovered = all_ids - covered
    by_stage_out = {}
    for st, g in sorted(by_stage.items(),
                        key=lambda kv: -len(kv[1]["unique_ids"])):
        uids = g["unique_ids"]
        cov = sum(1 for k in uids if k in own)
        by_stage_out[st] = {
            "files": g["files"], "unique_ids": len(uids),
            "already_represented": cov, "unrepresented": len(uids) - cov,
            "coverage": round(cov / len(uids), 4) if uids else 1.0,
        }

    # ── C3. 错位列 ────────────────────────────────────────────────
    misplaced = [r for r in conf_rows if r[0] == "MISPLACED_IDENTIFIER"]
    mis_by_uid = {}
    for (ct, iu) in con.execute(
            "SELECT conflict_type, incoming_paper_uid FROM identity_conflicts "
            "WHERE conflict_type='MISPLACED_IDENTIFIER'"):
        mis_by_uid[iu] = mis_by_uid.get(iu, 0) + 1
    con.close()

    terms = ident.compute_terms(papers)
    report = {
        "audited_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "db": os.path.relpath(db, BASE) if str(db).startswith(BASE) else db,
        "terms": terms,
        "A_writers": {
            "note": "两张表必须有真实写入者（对比 search_queries/paper_retrievals 两张 0 行空表）",
            "paper_identifiers": {"total": len(id_rows), "by_type": by_type,
                                  "by_source": by_source,
                                  "primary_marked": sum(1 for r in id_rows if r[3])},
            "identity_conflicts": {"total": len(conf_rows), "by_type": by_conf,
                                   "by_status": by_status},
        },
        "B_reverse_resolution": {
            "note": "从 papers 全量身份反查身份表，须 100% 命中",
            "valid_identifiers_checked": n_valid,
            "missing": len(missing),
            "coverage": round(coverage, 6),
            "ok": not missing,
            "sample_missing": missing[:5],
        },
        "C_gaps": {
            "C1_version_relations": {
                "note": "P0-A 不产出 IDENTICAL/VERSION_OF/ERRATUM_OF/PART_OF；"
                        "同题组即待建 paper_relations 的输入",
                "title_collision_groups": len(title_conf),
                "candidates": version_candidates,
            },
            "C2_history_json": {
                "note": "历史 JSON 未导入身份层（P0-B/P1）；导入须记 source_file/"
                        "file_hash/record_locator/imported_at/importer_version/run_stage",
                "json_files_scanned": len(files),
                "json_files_with_ids": len(per_file),
                "unique_ids_in_json": len(all_ids),
                "already_represented": len(covered),
                "unrepresented": len(uncovered),
                "unrepresented_ratio": round(len(uncovered) / len(all_ids), 4) if all_ids else 0.0,
                "by_stage": by_stage_out,
                "by_stage_note": (
                    "「未导入」不等于「丢失」：S1–S5 的 raw/query records 是检索原始命中"
                    "（audit frame 与探索过程的 universe），本就不全是 KB 成员。真正的问题是"
                    "这些身份没有统一记录 —— 无法回答「这篇我们是否见过/判过」，这是 P0-B"
                    "（retrieval_events + screening_decisions）的职责。AUDIT/CANDIDATE 组的"
                    "覆盖率更能反映「已判定但未入身份层」的缺口。"
                ),
                "top_files": dict(sorted(per_file.items(),
                                         key=lambda kv: -kv[1]["unique_ids"])[:12]),
            },
            "C3_misplaced_columns": {
                "note": "列装错标识：值可识别但位于错误列，uid 前缀亦错标；"
                        "uid 被 topic_papers 等引用，不可就地改写",
                "conflicts": len(misplaced),
                "affected_uids": len(mis_by_uid),
                "by_conflict_kind": by_conf,
            },
        },
        "explicitly_not_done": [
            "preprint ↔ published 合并（用户决策 1：不合并，保双 uid，用 VERSION_OF 关联）",
            "supporting information ↔ 正文关系",
            "title-based 自动合并（用户纠正 2：同题只作候选证据）",
            "历史 JSON 一次性逆向导入",
            "query / retrieval 事件回填（search_queries / paper_retrievals 仍为 0 行）",
            "Candidate / KB 状态统一",
        ],
    }
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)

    print("\n=== A. 写入者证据 ===")
    print(f"  paper_identifiers={len(id_rows)} {by_type}")
    print(f"    by_source={by_source}")
    print(f"    primary_marked={sum(1 for r in id_rows if r[3])}")
    print(f"  identity_conflicts={len(conf_rows)} {by_conf} {by_status}")
    print("=== B. 反向解析覆盖 ===")
    print(f"  检查 {n_valid} 个有效身份 -> 缺失 {len(missing)} | 覆盖 {coverage*100:.3f}%")
    print("=== C. 未解决缺口 ===")
    print(f"  C1 版本关系：同题组 {len(title_conf)} 组；"
          f"需判定={[c['needs'] for c in version_candidates]}")
    print(f"  C2 历史 JSON：扫 {len(files)} 文件，唯一身份 {len(all_ids)}，"
          f"已覆盖 {len(covered)}，**未导入 {len(uncovered)}**"
          f"（{report['C_gaps']['C2_history_json']['unrepresented_ratio']*100:.1f}%）")
    for st, g in by_stage_out.items():
        print(f"      {st:10s} files={g['files']:3d} ids={g['unique_ids']:6d} "
              f"覆盖={g['coverage']*100:5.1f}% 未导入={g['unrepresented']}")
    print(f"  C3 错位列：{len(misplaced)} 条冲突，涉及 {len(mis_by_uid)} 个 uid")
    print(f"\n[audit] 报告 -> {report_path}")
    return 0 if not missing else 3


def main():
    ap = argparse.ArgumentParser(description="P0-A 缺口量化审计")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--report", default=DEFAULT_REPORT)
    ap.add_argument("--json-glob", default="data/exports/**/*.json")
    ap.add_argument("--max-files", type=int, default=400)
    args = ap.parse_args()
    return audit(args.db, args.report, args.json_glob, args.max_files)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
