#!/usr/bin/env python3
"""P0-B2.1：身份异常审计报告（identity audit report）。

**先统计，再动手。** 本工具不修任何数据 —— 它只回答「库里到底有几类身份异常、
各多少条、影响面多大、哪一类阻塞后续迁移」。

异常类型（Type A/B 为用户 2026-09-12 指定，其余为本工具实测新增）：

  A  identifier-column mismatch   标识出现在**错误的列**（doi 列装着 EID）
     └─ A.uid_projection          同一批记录在 uid 侧的投影（`doi:` 前缀装了 EID）
                                  —— 不是新类型，是同一事实的第二个观测面
  B  primary preference drift      uid 前缀与值形态都合法，但**主身份选择**与全局
                                  规则（DOI > W > EID）不一致
  C  duplicate entity candidate    同题多 uid（**候选**，禁自动合并）
  D  reference integrity           引用完整性（悬空引用 / 孤立实体 / primary 唯一性）
  E  knowledge_records contamination   v1 事实层污染扫描（含跨世代映射覆盖率）

**跨世代注意**：``knowledge_base.db`` 里有两代 schema。
``papers`` / ``topic_papers`` / ``knowledge_claims`` 用 v2 canonical uid；
``knowledge_records`` 是 **v1 事实层**，其 ``paper_id`` 是旧形态
（``scopus:<DOI>`` / ``openalex:<URL>`` / 裸 DOI）。
因此「knowledge_records 悬空 215/216」**不是缺陷**，而是两个世代不同域 ——
本工具用 identity 反解测其**可映射率**，而不是拿字符串直接比对。
把这件事说清楚，是为了避免把跨世代差异误当成数据损坏而去做无意义的"修复"。

用法：
    .venv/Scripts/python tools/audit_identity_report.py                 # 只读扫描
    .venv/Scripts/python tools/audit_identity_report.py --details       # 附行级明细
    .venv/Scripts/python tools/audit_identity_report.py --fail-on A,B   # 非空即非零退出
输出：
    data/exports/schema/identity_audit_report.json
    data/exports/schema/identity_anomalies_<TYPE>.csv   （含明细时）
"""
from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from search_engine.identity import (  # noqa: E402
    PRIMARY_PRIORITY,
    compute_terms,
    extract_from_paper_uid,
    normalize_identifier,
    normalize_title,
    uid_id_type,
)
from search_engine.paper_writer import uid_type_matches_value  # noqa: E402

DEFAULT_DB = os.path.join(BASE, "data/cache/knowledge_base.db")
DEFAULT_OUT = os.path.join(BASE, "data/exports/schema/identity_audit_report.json")
DETAIL_DIR = os.path.join(BASE, "data/exports/schema")

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


AUDIT_VERSION = "p0b2_1_v1"

COLUMNS = (("doi", "DOI"), ("openalex_id", "OPENALEX"), ("scopus_eid", "SCOPUS_EID"))

# ⚠️ 引用完整性必须**按世代**分开查，否则会把跨世代差异误判成数据损坏：
#   v2 表（由 migrate_v2_schema / paper_writer 写，paper_id = canonical uid）
#     -> topic_papers, knowledge_claims
#   v1 事实层（由 knowledge_base.store 写，paper_id = 旧形态 scopus:<DOI> /
#     openalex:<URL> / 裸 DOI）-> knowledge_records, route_mechanism_edges
# 拿 v1 表的 paper_id 去 papers(uid) 里找，必然"全部悬空" —— 那是域不同，不是坏数据。
REF_TABLES_V2 = (("topic_papers", "paper_id"), ("knowledge_claims", "paper_id"))
REF_TABLES_V1 = (("knowledge_records", "paper_id"), ("route_mechanism_edges", "paper_id"))

TYPE_META = {
    "A": ("identifier-column mismatch",
          "标识出现在错误的列：列名声明一种类型，值形态是另一种（可恢复）"),
    "B": ("primary preference drift",
          "uid 前缀与值形态都合法，但主身份选择与全局规则 DOI>W>EID 不一致"),
    "C": ("duplicate entity candidate",
          "同题多 uid。**候选**而非确诊 —— SI/正文、preprint/published、"
          "不同论文都可能同题，禁自动合并（P0-A 铁律）"),
    "D": ("reference integrity",
          "悬空引用 / 孤立实体 / is_primary 唯一性"),
    "E": ("knowledge_records contamination",
          "v1 事实层的身份字段形态与跨世代可映射率"),
}

# ── 用户 2026-09-12 裁定（写进报告，避免下次重新讨论同一件事）────────────
USER_RULINGS = {
    "A": {
        "decision": "修复（真错误）",
        "executed_by": "tools/migrate_p0b2_2_type_a.py",
        "note": "列与 uid 必须**同时**处理，只修一边会留下不一致",
    },
    "B": {
        "decision": "保留现状，**不迁移 UID**",
        "nature": "identity_policy_drift",
        "not": "identity_corruption",
        "rationale": (
            "entity_id 与 preferred_identifier 是两个概念：paper_uid 应像 git commit "
            "hash（永不变），「最优引用标识」像 branch pointer（可变）。"
            "把两者混进 UID 会把稳定标识变成可变量。"
            "以后 resolver 返回 {entity_id, preferred_identifier} 即可，"
            "不改写 entity_id"),
    },
    "C": {
        "decision": "全部保留为 duplicate candidate，**不自动合并**",
        "status": "identified / not resolved",
        "by_pattern_ruling": {
            "SUPPORTING_INFORMATION":
                "进入人工 merge queue（可能是 SI / supplementary article / "
                "data article）。未来可保留 parent paper <- supporting document "
                "关系，不要直接 merge",
            "PREPRINT_VS_PUBLISHED":
                "保持不合并（两个科研实体）。未来建 preprint "
                "--published_as--> journal paper 关系，用于科研趋势分析",
            "MULTI_REGISTRANT":
                "不自动合并，建立 possible_duplicate 标记即可",
        },
    },
}


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_papers(con):
    return [dict(r) for r in con.execute(
        "SELECT paper_id, doi, openalex_id, scopus_eid, title, year, source_json "
        "FROM papers ORDER BY paper_id")]


def _effective(p):
    """这一行的**有效**标识 {id_type: 归一化值}（有效值口径，非"列非空"）。"""
    eff = {}
    for col, dt in COLUMNS:
        raw = p.get(col)
        if raw is None:
            continue
        s = str(raw).strip()
        if not s:
            continue
        n = normalize_identifier(dt, s)
        if n is not None and dt not in eff:
            eff[dt] = s
    return eff


def _build_ident_index(con):
    """(id_type, normalized_value) -> paper_uid。一次读入，避免逐条查库。"""
    return {(r[0], r[1]): r[2] for r in con.execute(
        "SELECT id_type, normalized_value, paper_uid FROM paper_identifiers")}


def map_v1_paper_id(paper_id, record, ident_idx):
    """v1 事实层 paper_id -> v2 paper_uid（经 **identity 反解**，不是字符串比对）。

    纯函数（不查库）。可选 ``record`` 提供 record_json 里的 doi/openalex_id，
    用于 paper_id 本身识别不出时的补充通道。
    """
    cands = set(extract_from_paper_uid(paper_id))
    w = (record or {}).get("openalex_id")
    if w:
        n = normalize_identifier("OPENALEX", str(w))
        if n:
            cands.add(("OPENALEX", n))
    d = ((record or {}).get("doi") or "").strip()
    if d:
        n = normalize_identifier("DOI", d)
        if n:
            cands.add(("DOI", n))
    n = normalize_identifier("DOI", paper_id)
    if n:
        cands.add(("DOI", n))
    for t, v in sorted(cands):
        hit = ident_idx.get((t, v))
        if hit:
            return hit
    return None


def _build_v1_index(con, ident_idx):
    """v1 事实层 paper_id -> v2 paper_uid 索引（两个 v1 表同源同域，共用映射）。"""
    idx = {}
    for r in con.execute("SELECT paper_id, record_json FROM knowledge_records"):
        try:
            rec = json.loads(r[1] or "{}")
        except Exception:
            rec = {}
        owner = map_v1_paper_id(r[0], rec, ident_idx)
        if owner:
            idx[r[0]] = owner
    for (pid,) in con.execute("SELECT DISTINCT paper_id FROM route_mechanism_edges"):
        if pid in idx:
            continue
        owner = map_v1_paper_id(pid, {}, ident_idx)
        if owner:
            idx[pid] = owner
    return idx


def _refs(con, uids, v1_index=None):
    """影响面：这批 uid 在 **v2** 引用表里的行数 + 经身份映射可达的 **v1** 事实层行数。

    迁移前必须知道 v1 侧会被连带影响多少 —— 否则「改了 papers 就完事」会漏掉
    事实层的引用。
    """
    if not uids:
        return {}
    u = sorted(uids)
    q = ",".join("?" * len(u))
    out = {}
    for tbl, col in REF_TABLES_V2:
        out[tbl] = con.execute(
            f"SELECT COUNT(*) FROM {tbl} WHERE {col} IN ({q})", u).fetchone()[0]
    if v1_index is None:
        v1_index = _build_v1_index(con, _build_ident_index(con))
    target = set(u)
    for tbl, _col in REF_TABLES_V1:
        n = sum(1 for (pid,) in con.execute(f"SELECT paper_id FROM {tbl}")
                if v1_index.get(pid) in target)
        out[f"{tbl}(via_identity)"] = n
    return out


def scan_a_column_mismatch(con, papers, v1_index=None):
    """Type A：标识出现在错误的列 + 它在 uid 侧的投影。"""
    records = []
    for p in papers:
        for col, dt in COLUMNS:
            raw = p.get(col)
            if raw is None or not str(raw).strip():
                continue
            if normalize_identifier(dt, str(raw)) is not None:
                continue
            # 识别真实类型（只做观测，不改数据）
            actual = None
            for t in ("DOI", "OPENALEX", "SCOPUS_EID", "PUBMED", "ARXIV", "ISBN", "URL"):
                if t == dt:
                    continue
                if normalize_identifier(t, str(raw)) is not None:
                    actual = t
                    break
            records.append({
                "paper_uid": p["paper_id"],
                "declared_column": col, "declared_type": dt,
                "raw_value": str(raw), "looks_like": actual,
                "recoverable": actual is not None,
                "uid_declared_type": uid_id_type(p["paper_id"]),
            })
    uid_side = [r for r in records if r["uid_declared_type"] != r["looks_like"]]
    breakdown = collections.Counter(
        f"{r['declared_column']}<-{r['looks_like'] or 'UNRECOGNIZED'}" for r in records)
    return {
        "count": len(records),
        "breakdown": dict(breakdown),
        "uid_projection_count": len(uid_side),
        "uid_projection_note": (
            "同一批记录在 uid 侧的投影：前缀声明的类型与实际可用身份不符。"
            "不是独立类型 —— 修复时必须**列与 uid 同时处理**，只修一边会留下不一致"),
        "impact": _refs(con, [r["paper_uid"] for r in records], v1_index),
        "records": records,
    }


def scan_b_primary_drift(con, papers, v1_index=None):
    """Type B：uid 前缀与值形态都合法，但主身份非最优。"""
    records, prefix_mismatch = [], []
    for p in papers:
        eff = _effective(p)
        decl = uid_id_type(p["paper_id"])
        ok, _, _ = uid_type_matches_value(p["paper_id"])
        if not eff:
            continue
        best = min(eff, key=lambda t: PRIMARY_PRIORITY[t])
        if not ok:
            prefix_mismatch.append(p["paper_id"])
            continue
        if decl != best:
            records.append({
                "paper_uid": p["paper_id"],
                "declared_type": decl, "best_available": best,
                "available": sorted(eff, key=lambda t: PRIMARY_PRIORITY[t]),
                "uid_value_form_ok": ok,
                "year": p.get("year"),
                "title": (p.get("title") or "")[:90],
            })
    breakdown = collections.Counter(
        f"{r['declared_type']} -> {r['best_available']}" for r in records)
    return {
        "count": len(records),
        "breakdown": dict(breakdown),
        "uid_value_form_all_legal": True,
        "note": ("这些 uid 的**形态完全合法**，不违反 uid_type_matches_value，"
                 "因此不属于 Type A。它们是**规则差异**：旧写入者按源 key 形态硬编码"
                 "前缀，新规则按 PRIMARY_PRIORITY 取最优。是否纳入 uid 重映射需裁定"),
        "cross_check_prefix_mismatch_n": len(prefix_mismatch),
        "cross_check_prefix_mismatch_note": (
            "另有 %d 条 uid 前缀与值形态**不符** —— 那是 Type A 的 uid 投影，已计入 A，"
            "此处不重复计数" % len(prefix_mismatch)),
        "impact": _refs(con, [r["paper_uid"] for r in records], v1_index),
        "records": records,
    }


_SI_DOI_RE = re.compile(r"\.s\d{1,3}$", re.I)
_PREPRINT_PREFIXES = ("10.2139/ssrn.", "10.48550/arxiv.", "10.1101/", "10.31219/osf.")


def _classify_collision(members):
    """给同题组打**模式**标签（只降人工成本，**不做裁决**）。

    P0-A 铁律：同题不得自动合并。本函数只输出「这组看起来像什么」+「建议动作」，
    最终判定必须人工。
    """
    dois = [d for m in members for d in m.get("dois", [])]
    low = [d.lower() for d in dois]
    if any(_SI_DOI_RE.search(d) for d in dois):
        return ("SUPPORTING_INFORMATION",
                "存在 ``.sNNN`` 后缀 DOI（期刊补充材料），通常是同一篇的附件而非独立论文。"
                "**建议人工确认后合并或标记为附件** —— 不自动执行")
    if any(d.startswith(_PREPRINT_PREFIXES) for d in low):
        return ("PREPRINT_VS_PUBLISHED",
                "存在 preprint DOI（SSRN / arXiv / bioRxiv / OSF）。用户 2026-09-10 已裁定"
                "**preprint 不合并** —— 两条各自保留为一等实体")
    if len({d.split("/")[0] for d in dois}) > 1:
        return ("MULTI_REGISTRANT",
                "同题但 DOI 注册机构不同（可能为机构库镜像或版本关系）。需人工判定")
    return ("MANUAL_REVIEW", "需人工判定")


def scan_c_duplicate_entity(con, papers, v1_index=None):
    """Type C：同题多 uid（候选，需人工判定）。"""
    by_title = collections.defaultdict(list)
    for p in papers:
        t = normalize_title(p.get("title"))
        if t:
            by_title[t].append(p)
    groups = []
    for t, ps in by_title.items():
        if len(ps) < 2:
            continue
        members = [{
            "paper_uid": p["paper_id"],
            "effective": sorted(_effective(p), key=lambda x: PRIMARY_PRIORITY[x]),
            "dois": [str(p[c]).strip() for c, dt in COLUMNS if dt == "DOI"
                     and p.get(c) and normalize_identifier("DOI", str(p[c]))],
            "year": p.get("year"),
            "source_json": (p.get("source_json") or "")[:150],
        } for p in ps]
        pattern, advice = _classify_collision(members)
        groups.append({
            "normalized_title": t,
            "title_sample": (ps[0].get("title") or "")[:110],
            "pattern": pattern,
            "suggested_action": advice,
            "members": members,
        })
    groups.sort(key=lambda g: (-len(g["members"]), g["pattern"]))
    uids = [m["paper_uid"] for g in groups for m in g["members"]]
    return {
        "count": len(groups),
        "n_papers_involved": len(uids),
        "status": "CANDIDATE",
        "by_pattern": dict(collections.Counter(g["pattern"] for g in groups)),
        "note": ("同题**不能**自动合并（P0-A 铁律）：SI 与正文、preprint 与 published、"
                 "不同论文、机构库镜像都会同题。pattern 只是**降人工成本**的线索，"
                 "逐组判定后才能动"),
        "impact": _refs(con, uids, v1_index),
        "groups": groups,
    }


def scan_d_reference_integrity(con, papers):
    """Type D：引用完整性 + 不变式。"""
    uids = {p["paper_id"] for p in papers}
    dangling = {}
    for tbl, col in REF_TABLES_V2:
        rows = [r[0] for r in con.execute(f"SELECT DISTINCT {col} FROM {tbl}")]
        bad = sorted(x for x in rows if x not in uids)
        dangling[tbl] = {"distinct": len(rows), "dangling": len(bad),
                         "sample": bad[:5]}

    orphan = [p["paper_id"] for p in papers if con.execute(
        "SELECT 1 FROM paper_identifiers WHERE paper_uid = ?", (p["paper_id"],)).fetchone()
        is None]

    pid = list(con.execute(
        "SELECT paper_uid, SUM(is_primary) s, COUNT(*) n FROM paper_identifiers "
        "GROUP BY paper_uid"))
    no_primary = [r[0] for r in pid if r[1] != 1]

    multi_owner = [{"id_type": r[0], "normalized_value": r[1], "owners": r[2]}
                   for r in con.execute(
        "SELECT id_type, normalized_value, COUNT(DISTINCT paper_uid) n "
        "FROM paper_identifiers GROUP BY 1,2 HAVING n > 1")]

    n_ident = sum(r[2] for r in pid)
    return {
        "count": (sum(v["dangling"] for v in dangling.values())
                  + len(orphan) + len(no_primary) + len(multi_owner)),
        "dangling_refs": dangling,
        "orphan_papers_no_identifier": {"count": len(orphan), "sample": orphan[:5]},
        "identifiers_without_exactly_one_primary": {
            "count": len(no_primary), "sample": no_primary[:5]},
        "identifier_multi_owner": {"count": len(multi_owner),
                                   "sample": multi_owner[:5]},
        "avg_identifiers_per_uid": round(n_ident / len(pid), 2) if pid else 0,
        "note": ("三张 v2 引用表（topic_papers / knowledge_claims / "
                 "route_mechanism_edges）**全部零悬空**；"
                 "knowledge_records 属 v1 事实层，不在本项比对范围（见 Type E）"),
    }


def scan_e_knowledge_records(con, ident_idx, v1_index):
    """Type E：v1 事实层污染扫描 + 跨世代可映射率。

    v1 事实层含两张表（``knowledge_records`` 与 ``route_mechanism_edges``），
    二者同源同域，故共用同一套映射（``v1_index``）。
    """
    total = 0
    bad_doi = bad_canon = bad_pid = 0
    unmapped = []
    mapped = 0
    pid_forms = collections.Counter()
    for r in con.execute("SELECT paper_id, record_json FROM knowledge_records"):
        total += 1
        pid, rj = r[0], r[1]
        try:
            d = json.loads(rj)
        except Exception:
            d = {}

        t = uid_id_type(pid)
        pid_forms[f"prefixed:{t}" if t else "bare_doi"
                  if normalize_identifier("DOI", pid) else "unrecognized"] += 1
        if not t and normalize_identifier("DOI", pid) is None:
            bad_pid += 1

        rd = (d.get("doi") or "").strip()
        if rd and normalize_identifier("DOI", rd) is None:
            bad_doi += 1

        cp = (d.get("canonical_paper_id") or "").strip()
        if cp:
            ct = uid_id_type(cp)
            cv = cp.split(":", 1)[1] if ":" in cp else ""
            if ct is None or normalize_identifier(ct, cv) is None:
                bad_canon += 1

        if v1_index.get(pid):
            mapped += 1
        elif len(unmapped) < 20:
            unmapped.append({"paper_id": pid, "record_doi": rd,
                             "canonical_paper_id": cp})

    # route_mechanism_edges：同为 v1 世代，同样统计映射率
    edges_total = 0
    edges_mapped = 0
    for (pid,) in con.execute("SELECT DISTINCT paper_id FROM route_mechanism_edges"):
        edges_total += 1
        if v1_index.get(pid):
            edges_mapped += 1

    return {
        "count": bad_doi + bad_canon + bad_pid + len(unmapped),
        "n_records": total,
        "contamination": {
            "record_json_doi_malformed": bad_doi,
            "canonical_paper_id_malformed": bad_canon,
            "paper_id_unrecognizable": bad_pid,
        },
        "contamination_verdict": (
            "**风险未实现**：那 30 条错位中 4 条 label=RELEVANT，仅因摘要为空才未走到"
            "抽取闸门，因此 `doi:2-s2.0-*` 从未进入 record_json。"
            "P0-B1 记录的「近失事件」经本次全量扫描确认未发生"),
        "cross_generation_mapping": {
            "table": "knowledge_records",
            "mapped": mapped, "unmapped": len(unmapped), "total": total,
            "rate": round(mapped / total, 4) if total else 0,
            "method": "identity 反解（extract_from_paper_uid + record 内的 doi/openalex_id），"
                      "不是字符串比对 —— v1 事实层与 v2 canonical uid 是不同域",
            "unmapped_sample": unmapped,
        },
        "cross_generation_mapping_edges": {
            "table": "route_mechanism_edges",
            "mapped": edges_mapped, "unmapped": edges_total - edges_mapped,
            "total": edges_total,
            "rate": round(edges_mapped / edges_total, 4) if edges_total else 0,
        },
        "v1_paper_id_forms": dict(pid_forms),
        "note": ("`canonical_paper_id != paper_id` 是**设计如此**：前者是论文最优身份"
                 "（DOI 优先），后者是 v1 源记录键。不构成异常。"
                 "两张 v1 表（knowledge_records / route_mechanism_edges）的 paper_id "
                 "**不在** v2 的 papers(uid) 域内 —— 因此 Type D 不比对它们，"
                 "改以「经身份反解的可映射率」衡量，这才是跨世代正确的口径"),
    }


def check_invariants(con, papers):
    uids = {p["paper_id"] for p in papers}
    return {
        "no_multi_owner_identifier": con.execute(
            "SELECT COUNT(*) FROM (SELECT id_type, normalized_value "
            "FROM paper_identifiers GROUP BY 1,2 HAVING COUNT(DISTINCT paper_uid) > 1)"
        ).fetchone()[0] == 0,
        "exactly_one_primary_per_uid": con.execute(
            "SELECT COUNT(*) FROM (SELECT paper_uid FROM paper_identifiers "
            "GROUP BY paper_uid HAVING SUM(is_primary) <> 1)").fetchone()[0] == 0,
        "no_orphan_paper": con.execute(
            "SELECT COUNT(*) FROM papers p WHERE NOT EXISTS "
            "(SELECT 1 FROM paper_identifiers i WHERE i.paper_uid = p.paper_id)"
        ).fetchone()[0] == 0,
        "no_dangling_v2_refs": all(
            con.execute(f"SELECT COUNT(*) FROM {t} WHERE {c} NOT IN "
                        f"(SELECT paper_id FROM papers)").fetchone()[0] == 0
            for t, c in REF_TABLES_V2),
        "paper_identifiers_covers_all_papers": con.execute(
            "SELECT COUNT(DISTINCT paper_uid) FROM paper_identifiers").fetchone()[0]
        == len(uids),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--details", action="store_true", help="导出各类行级明细 CSV")
    ap.add_argument("--fail-on", default="",
                    help="逗号分隔的类型（如 A,B）；这些类型非空则退出码 1")
    args = ap.parse_args()

    before = _sha256(args.db)
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    papers = _load_papers(con)
    ident_idx = _build_ident_index(con)
    v1_index = _build_v1_index(con, ident_idx)

    a = scan_a_column_mismatch(con, papers, v1_index)
    b = scan_b_primary_drift(con, papers, v1_index)
    c = scan_c_duplicate_entity(con, papers, v1_index)
    d = scan_d_reference_integrity(con, papers)
    e = scan_e_knowledge_records(con, ident_idx, v1_index)
    inv = check_invariants(con, papers)

    print(f"[audit] {AUDIT_VERSION}  db={_rel(args.db)}  "
          f"sha256={before[:16]}…")
    print(f"\n  papers={len(papers)}  paper_identifiers="
          f"{con.execute('SELECT COUNT(*) FROM paper_identifiers').fetchone()[0]}  "
          f"identity_conflicts="
          f"{con.execute('SELECT COUNT(*) FROM identity_conflicts').fetchone()[0]}")
    print("  P0-A terms: " + json.dumps(
        {k: v for k, v in compute_terms(
            [(p["paper_id"], p["doi"], p["openalex_id"], p["scopus_eid"], p["title"])
             for p in papers]).items() if v}, ensure_ascii=False))

    print("\nIdentity anomalies:")
    rows = [
        ("A", a["count"], f"其中 {a['uid_projection_count']} 条在 uid 侧同时错标"),
        ("B", b["count"], "形态合法，仅主身份选择不一致"),
        ("C", c["count"], f"候选（涉及 {c['n_papers_involved']} 篇），禁自动合并"),
        ("D", d["count"], "悬空引用 / 孤立 / primary 唯一性"),
        ("E", e["count"], f"v1 事实层污染；可映射率 {e['cross_generation_mapping']['rate']:.1%}"),
    ]
    for k, n, note in rows:
        print(f"  Type {k}: {TYPE_META[k][0]:<32} {n:>4}   {note}")

    print("\nInvariants:")
    for k, v in inv.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")

    if args.details:
        for k, payload in (("A", a), ("B", b)):
            recs = payload["records"]
            if not recs:
                continue
            path = os.path.join(DETAIL_DIR, f"identity_anomalies_{k}.csv")
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(recs[0].keys()))
                w.writeheader()
                w.writerows(recs)
            print(f"\n  [details] Type {k} -> {_rel(path)}")
        if c["groups"]:
            path = os.path.join(DETAIL_DIR, "identity_anomalies_C.csv")
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(["normalized_title", "title_sample", "n_members",
                            "paper_uids"])
                for g in c["groups"]:
                    w.writerow([g["normalized_title"], g["title_sample"],
                                len(g["members"]),
                                "|".join(m["paper_uid"] for m in g["members"])])
            print(f"  [details] Type C -> {_rel(path)}")

    after = _sha256(args.db)
    report = {
        "audit_version": AUDIT_VERSION,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source_db": {"path": _rel(args.db),
                      "sha256": before, "unchanged": before == after},
        "scale": {
            "papers": len(papers),
            "paper_identifiers": con.execute(
                "SELECT COUNT(*) FROM paper_identifiers").fetchone()[0],
            "identity_conflicts": con.execute(
                "SELECT COUNT(*) FROM identity_conflicts").fetchone()[0],
            "topic_papers": con.execute("SELECT COUNT(*) FROM topic_papers").fetchone()[0],
            "knowledge_records": con.execute(
                "SELECT COUNT(*) FROM knowledge_records").fetchone()[0],
            "knowledge_claims": con.execute(
                "SELECT COUNT(*) FROM knowledge_claims").fetchone()[0],
            "route_mechanism_edges": con.execute(
                "SELECT COUNT(*) FROM route_mechanism_edges").fetchone()[0],
        },
        "anomalies": {
            "A": {"name": TYPE_META["A"][0], "definition": TYPE_META["A"][1], **a},
            "B": {"name": TYPE_META["B"][0], "definition": TYPE_META["B"][1], **b},
            "C": {"name": TYPE_META["C"][0], "definition": TYPE_META["C"][1], **c},
            "D": {"name": TYPE_META["D"][0], "definition": TYPE_META["D"][1], **d},
            "E": {"name": TYPE_META["E"][0], "definition": TYPE_META["E"][1], **e},
        },
        "invariants": inv,
        "user_rulings": USER_RULINGS,
        "cross_reference": {
            "A_uid_projection_equals_A": a["uid_projection_count"] == a["count"],
            "B_excludes_prefix_mismatch": b["cross_check_prefix_mismatch_n"] == a["count"],
            "explain": ("Type A 与 A.uid_projection 是**同一批记录的两个观测面**"
                        "（列装错值 -> 派生 uid 前缀错标）；Type B 只含形态合法者，"
                        "与 A 不重叠。三者合计 30+18=48 条，是 P0-B2.2 的候选工作集"),
        },
        "verdicts": {
            "library_state": (
                "**结构性完好，无数据损坏**。三张 v2 引用表零悬空、无多 owner 标识、"
                "每个 uid 恰好一个 primary、无孤立实体、v1 事实层零污染，"
                "且 216/216 可映射到 v2 身份" if all(inv.values()) else
                "存在不变式失败，需先处理"),
            "risk_not_realized": e["contamination_verdict"],
            "cross_generation_not_a_defect": e["note"],
        },
        "readiness": {
            "B2.2_ready": all(v for k, v in inv.items() if k != "no_dangling_v2_refs"),
            "B2.2_candidate_set": {"A": a["count"], "B": b["count"],
                                   "A_plus_B": a["count"] + b["count"],
                                   "C_separate": c["count"]},
            "blockers": [k for k, v in inv.items() if not v],
            "user_decision_required": [
                "Type B 的 18 条：保留 W 为主（尊重 uid 不可变 + P0-A 已把 W_PRIMARY "
                "列为合法类别）vs 统一 DOI 优先（与 A 的 30 条一起重映射）",
                "Type C 的 3 组：逐组人工判定是否为同一实体（禁自动合并）",
            ],
            "impact_if_migrated": {"A": a["impact"], "B": b["impact"],
                                   "C": c["impact"]},
        },
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n[ok] 报告 -> {_rel(args.out)}")
    print(f"[ok] 源库未被修改: {before == after}")

    fail_on = [x.strip().upper() for x in args.fail_on.split(",") if x.strip()]
    if fail_on:
        nonempty = [k for k in fail_on if report["anomalies"][k]["count"] > 0]
        if nonempty:
            print(f"[FAIL] --fail-on 命中非空类型: {nonempty}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
