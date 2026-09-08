#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""S4 Step 5：unified six-channel evidence matrix（2026-08-30 用户口径冻结）。

把 Step 0.5~4 的诊断证据统一成 per-miss 矩阵，严格区分：
  A. Mechanism evidence（可修复机制 → S4 repair action 依据）：
     TERM / CIT1 / CIT2_STRONG / COMMUNITY / IDENTITY_REPAIRABLE / SOURCE
  B. Explanatory features（为什么机制可能失效，非修复通道）：
     RECENCY / CITATION_MATURITY / DATA_GAP / DOC_TYPE / INDEX_QUALITY

口径纪律（用户定）：
- CIT2_WEAK=1 记录但**不计入 actionable citation coverage**：
    citation_reachable_diagnostic = CIT1(15) + CIT2_STRONG(14) + CIT2_WEAK(1) = 30
    citation_actionable_strong    = CIT1(15) + CIT2_STRONG(14) = 29
- IDENTITY：IDENTITY_DQ=1 canonical group 记录；只有 alias 一个在 S3 seen、另一个因
  join failure 判 FALSE 才算 IDENTITY_REPAIRABLE（计入 coverage）。本 case 两个 alias
  均 Seen_S3=FALSE（residual 定义保证）→ REPAIRABLE=False，不计 coverage。
- TERM(m)=1 ⇔ ∃ eligible term（undercovered ∧ specificity gate ∧ support rule）覆盖 m
- COMMUNITY=TRUE 仅赋给 STRONG 社区（C_dental-measurement 14 篇）
- SOURCE_GAP 全 FALSE（37 篇全在 R03 universe——抽样保证）
- repair path count K(m) = T+C1+C2+CM+I+S；0-path = unexplained tail
"""

import json
import os
import sys
from datetime import date

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TERM = os.path.join(BASE, "data", "exports", "terminology")

RESIDUAL = os.path.join(TERM, "s4_residual_misses.json")
REACH = os.path.join(TERM, "s4_citation_reachability.json")
TERM_EV = os.path.join(TERM, "s4_term_evidence.json")
COMM = os.path.join(TERM, "s4_community_discovery.json")
SRC_TM = os.path.join(TERM, "s4_source_time_audit.json")

DUP_WID = "W7110794929"          # .s001 补充材料记录（Step 0.5 确认 duplicate）
KEEP_WID = "W4416134588"         # 保留的 canonical WID
STRONG_COMMUNITY = "C_dental-measurement"


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    out_path = os.path.join(sys.argv[sys.argv.index("--out") + 1]
                            if "--out" in sys.argv else
                            os.path.join(TERM, "s4_six_channel_matrix.json"))
    residual = load(RESIDUAL)
    reach = load(REACH)
    term_ev = load(TERM_EV)
    comm = load(COMM)
    st = load(SRC_TM)

    # ── canonical 37（排除 duplicate W7110794929）──
    misses = [m for m in residual["misses"] if m["wid"] != DUP_WID]
    wids = [m["wid"] for m in misses]
    print(f"canonical misses: {len(misses)}")

    # ── 通道索引 ──
    # TERM：eligible term -> miss_wids
    eligible_by_wid: dict[str, list[dict]] = {}
    for t in term_ev["terms"]:
        if not t.get("eligible"):
            continue
        for w in t.get("miss_wids", []):
            eligible_by_wid.setdefault(w, []).append(t)
    # CIT1 / CIT2_STRONG / CIT2_WEAK（per_miss 是 raw 38；以 wid 索引）
    cit = {p["miss_wid"]: p for p in reach["per_miss"]}
    # COMMUNITY members（STRONG 社区）
    strong_members = set()
    for c in comm["communities"]:
        if c["verdict"] == "STRONG":
            strong_members |= set(c["members"])
    # SOURCE：source_time rows 的 data_gap / verdict
    st_by_wid = {r["wid"]: r for r in st["rows"]}

    # ── IDENTITY ──
    # 两 alias 的 S3 seen 状态（residual 定义 R03 RELEVANT ∧ Seen_S3=FALSE 保证均为 FALSE）
    seen_map = {m["wid"]: m.get("seen_s3") for m in residual["misses"]}
    dup_seen = [seen_map.get(DUP_WID), seen_map.get(KEEP_WID)]
    identity_repairable = any(s is True for s in dup_seen)  # 本 case 均 False
    identity_dq = 1  # 1 canonical group（W4416134588 + W7110794929）

    # ── per-miss matrix ──
    per_miss = []
    mech_counters = {"TERM": 0, "CIT1": 0, "CIT2_STRONG": 0, "COMMUNITY": 0,
                     "IDENTITY_REPAIRABLE": 0, "SOURCE": 0}
    for m in misses:
        wid = m["wid"]
        r = cit.get(wid, {})
        st_row = st_by_wid.get(wid, {})

        # TERM
        elig = eligible_by_wid.get(wid, [])
        best = max(elig, key=lambda t: (t.get("miss_support", 0),
                                        {"MEASUREMENT": 3, "PROBLEM": 2,
                                         "METHOD": 1, "REACTION": 1,
                                         "MATERIAL": 1, "CONTEXT": 0}.get(
                                            t.get("term_type"), 0))) if elig else None
        term_flag = bool(elig)

        # CIT（distance/gate 语义：1=distance1；2_STRONG=distance2∧gate=2H_STRONG；
        # 2_WEAK=distance2∧gate=2H_WEAK）；distance 在 reachability 产物中是 int
        dist = r.get("distance", 99)
        gate = r.get("gate", "2H_NONE")
        try:
            dist_i = int(dist)
        except (TypeError, ValueError):
            dist_i = 99
        cit1 = dist_i == 1
        cit2_strong = dist_i == 2 and gate == "2H_STRONG"
        cit2_weak = dist_i == 2 and gate == "2H_WEAK"

        # COMMUNITY
        comm_flag = wid in strong_members

        # IDENTITY（本 case 无 repairable——两 alias 均未 seen；记录 DQ 单独段）
        ident_flag = False

        # SOURCE：SOURCE_GAP 判定（data_gap 是数据缺失特征，不是 source gap——
        # 37 篇全在 universe/OpenAlex，SOURCE_GAP=FALSE）
        source_flag = False

        # 机制路径
        paths = []
        if term_flag:
            paths.append("TERM")
        if cit1:
            paths.append("CIT1")
        if cit2_strong:
            paths.append("CIT2_STRONG")
        if comm_flag:
            paths.append("COMMUNITY")
        if ident_flag:
            paths.append("IDENTITY")
        if source_flag:
            paths.append("SOURCE")
        k = len(paths)
        for p in paths:
            mech_counters[p] += 1

        # explanatory features
        year = m.get("oa_year") or m.get("year")
        age = st_row.get("age_years")
        recent = False
        if age is not None:
            try:
                recent = float(age) < 3.0
            except (TypeError, ValueError):
                recent = False
        elif year:
            try:
                recent = (date.today().year - int(year)) < 3
            except (TypeError, ValueError):
                recent = False
        data_gap = bool(st_row.get("data_gap", False))
        features = {
            "recent": recent,
            "maturity": st_row.get("citation_maturity", "UNKNOWN"),
            "data_gap": data_gap,
            "language": "UNKNOWN",          # openalex 缓存无 language 字段（数据源限制）
            "doc_type": st_row.get("doc_type", "UNKNOWN"),
            "index_quality": "DATA_GAP" if data_gap else "OK",
        }

        per_miss.append({
            "wid": wid,
            "title": m.get("oa_title") or m.get("title"),
            "TERM": term_flag,
            "term_evidence_count": len(elig),
            "best_term": best["term_family"] if best else None,
            "best_term_type": best["term_type"] if best else None,
            "CIT1": cit1,
            "CIT2_STRONG": cit2_strong,
            "CIT2_WEAK": cit2_weak,
            "COMMUNITY": comm_flag,
            "IDENTITY_REPAIRABLE": ident_flag,
            "SOURCE": source_flag,
            "repair_paths": paths,
            "K": k,
            "features": features,
        })

    # ── 汇总 ──
    n = len(misses)
    cit_diag = sum(1 for p in per_miss if p["CIT1"] or p["CIT2_STRONG"] or p["CIT2_WEAK"])
    cit_action = sum(1 for p in per_miss if p["CIT1"] or p["CIT2_STRONG"])
    t_set = {p["wid"] for p in per_miss if p["TERM"]}
    c_set = {p["wid"] for p in per_miss if p["CIT1"] or p["CIT2_STRONG"]}
    cm_set = {p["wid"] for p in per_miss if p["COMMUNITY"]}
    union_all = {p["wid"] for p in per_miss if p["K"] >= 1}
    unexplained = [p for p in per_miss if p["K"] == 0]

    k0 = sum(1 for p in per_miss if p["K"] == 0)
    k1 = sum(1 for p in per_miss if p["K"] == 1)
    k2p = sum(1 for p in per_miss if p["K"] >= 2)

    out = {
        "version": "s4_six_channel_matrix_v1",
        "frozen_at": "2026-08-30",
        "development_source": "AUDIT_R03",
        "denominator": f"{n} canonical",
        "discipline": "通道=可修复机制；features=机制为何失效（非修复通道）",
        "identity": {
            "dq_groups": identity_dq,
            "duplicate_wids": [DUP_WID, KEEP_WID],
            "both_seen_s3": dup_seen,
            "identity_repairable": identity_repairable,
            "note": "两 alias 均 Seen_S3=FALSE（residual 定义保证）→ reconciliation 仅把 "
                    "38 raw 变 37 canonical，未让 Search 多找到一篇 → REPAIRABLE=False，"
                    "不计入 mechanism coverage",
        },
        "mechanism_coverage": {
            "TERM": mech_counters["TERM"],
            "CIT1": mech_counters["CIT1"],
            "CIT2_STRONG": mech_counters["CIT2_STRONG"],
            "CIT2_WEAK": sum(1 for p in per_miss if p["CIT2_WEAK"]),
            "COMMUNITY": mech_counters["COMMUNITY"],
            "IDENTITY_REPAIRABLE": 0,
            "SOURCE": 0,
        },
        "citation": {
            "reachable_diagnostic": cit_diag,
            "actionable_strong": cit_action,
            "note": "CIT2_WEAK 记录不计入 actionable",
        },
        "overlaps": {
            "TERM_CIT": len(t_set & c_set),
            "COMMUNITY_CIT": len(cm_set & c_set),
            "TERM_COMMUNITY": len(t_set & cm_set),
            "TERM_ONLY": len(t_set - c_set - cm_set),
            "CIT_ONLY": len(c_set - t_set - cm_set),
            "COMMUNITY_ONLY": len(cm_set - t_set - c_set),
        },
        "union_coverage": {
            "total": len(union_all),
            "of": n,
            "fraction": round(len(union_all) / n, 4) if n else None,
            "unexplained_tail": len(unexplained),
            "unexplained_wids": [p["wid"] for p in unexplained],
        },
        "repair_path_count": {
            "0_path": k0, "1_path": k1, "2plus_path": k2p,
            "note": "0-path=真正 unexplained tail；1-path=单机制脆弱；2+-path=多机制可追回",
        },
        "per_miss": per_miss,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    # ── 终端报告（ASCII 安全）──
    def safe(s: str, n: int = 70) -> str:
        return (s or "").encode("ascii", "replace").decode("ascii")[:n]

    print("=" * 78)
    print("S4 Step 5 unified six-channel matrix")
    print("=" * 78)
    print(f"denominator = {n} canonical")
    print(f"mechanism coverage: TERM {mech_counters['TERM']} | CIT1 {mech_counters['CIT1']} "
          f"| CIT2_STRONG {mech_counters['CIT2_STRONG']} | COMMUNITY {mech_counters['COMMUNITY']} "
          f"| IDENT_REPAIR 0 | SOURCE 0")
    print(f"citation: diagnostic {cit_diag}/{n} | actionable strong {cit_action}/{n}")
    print(f"overlaps: TERM∩CIT {len(t_set & c_set)} | COMM∩CIT {len(cm_set & c_set)} "
          f"| TERM∩COMM {len(t_set & cm_set)}")
    print(f"union coverage = {len(union_all)}/{n} | unexplained tail = {len(unexplained)}")
    print(f"K distribution: 0-path {k0} | 1-path {k1} | 2+-path {k2p}")
    if unexplained:
        print("\nunexplained tail:")
        for p in unexplained:
            print(f"  {p['wid']} | {safe(p['title'])}")
    print(f"\n[OK] written: {out_path}")


if __name__ == "__main__":
    main()
