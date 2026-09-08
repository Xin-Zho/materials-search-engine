#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/build_s7_coverage.py — S7.3 relation coverage matrix（S7.2 memory 之上）
（2026-09-07 用户方向：round3 证明 memory deny 有效，下一步从"避免重复"转向
 "主动寻找未覆盖区域"；RL 之前先做 coverage-guided generation）

矩阵 = domain × B 端概念族（variant family 归一 + 独立概念词），单元格状态：
  covered   — S6 真实检索过且 KEEP（有真实 R/U 产出）
  failed    — S6 真实检索过但 FAIL
  queued    — S7 QA=RUN（纸面排队，从未真实检索）
  rejected  — S7 QA=SKIP_LOW（变体/死区）
  invalid   — S7 QA=SKIP_INVALID
  unprobed  — 从未被任何 relation 触碰（真 gap）

设计纪律：
  ① 列只取 generator 的 B 池词（term layers 词源），不把 A 端 mechanism 词当 observable 列
  ② family 归一：F01-F06 命中词并入族（delamination/warpage→F01 是错配：delamination 独立）
  ③ domain 物理可达性：B 词词源的 domain 属性是软约束；QA 已裁决的 domain 归属为准
  ④ missing≠全填：unprobed 单元格只在该 (domain,family) 物理语义成立时是 gap
  ⑤ 产物双用途：coverage matrix（图景）+ uncovered_cells（gap 清单喂 generator v4）

产物：data/exports/terminology/s7_coverage_matrix.json
只读 sources：s7_relation_memory.json（S6+S7 统一记忆，单一事实源）
"""
import json
import os
import sys
from collections import Counter, defaultdict

T = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data",
                 "exports", "terminology")
T = os.path.abspath(T)
OUT = os.path.join(T, "s7_coverage_matrix.json")

# variant families（只读冻结源）
VF = os.path.join(T, "s7_variant_families.json")


def load(name):
    return json.load(open(os.path.join(T, name), encoding="utf-8"))


# ── family 归一 ──────────────────────────────────────────────────────────
def family_map():
    """B 端词 → (family_id, canonical)。F01 只收 deformation 家族，
    delamination 是独立概念（QA RC 多次给 RUN，非 structural deformation 变体）。"""
    v = load("s7_variant_families.json")
    m = {}
    for f in v["families"]:
        fid = f["family_id"]
        canon = f["canonical"].lower()
        # F01 例外：deformation/warpage 属族；delamination 独立（QA 判 RUN 为主）
        terms = [canon] + [x.lower() for x in f["variants"]]
        for x in terms:
            m[x] = fid
    # delamination 独立概念（不并 F01）
    return m


# 独立概念词（不在 F01-F06、有独立搜索价值、QA 多次给 RUN / S6 真实检索 KEEP）
STANDALONE = {
    "delamination": "delamination",
    "dimensional accuracy": "dimensional accuracy",
    "post-operative sensitivity": "post-operative sensitivity",
    "marginal discoloration": "marginal discoloration",
    "cusp deflection": "cusp deflection",
    "fabrication error": "fabrication error",     # S6-A-05 optics KEEP / D-16 FAIL
    "softening effect": "softening effect",       # S6-A-07 general KEEP
}
# 归 F05 的 void formation 从 standalone 移除
STANDALONE.pop("void formation", None)

# domain 物理语义白名单：(domain, family) 物理成立才可能是 gap。
# 依据 = 各 domain 的自然科学领域。dental 临床域收 clinical 后果词；
# 制造域（3dp/coatings/sla/packaging）不收 clinical 后果词（post-op/marginal）。
PHYSICAL = {
    "dental": {"F01_structural_deformation", "F02_marginal_gap_leakage",
               "F03_crack_enamel", "F04_shrink_outcome", "F05_internal_gap_void",
               "cusp deflection", "marginal discoloration",
               "post-operative sensitivity", "dimensional accuracy",
               "delamination", "softening effect"},
    "3dp": {"F01_structural_deformation", "F03_crack_enamel",
            "F04_shrink_outcome", "F05_internal_gap_void", "F06_printability",
            "delamination", "dimensional accuracy"},
    "sla": {"F01_structural_deformation", "F03_crack_enamel",
            "F04_shrink_outcome", "F05_internal_gap_void", "F06_printability",
            "delamination", "dimensional accuracy"},
    "coatings": {"F01_structural_deformation", "F03_crack_enamel",
                 "F04_shrink_outcome", "F05_internal_gap_void", "delamination",
                 "dimensional accuracy"},
    "composites": {"F01_structural_deformation", "F03_crack_enamel",
                   "F04_shrink_outcome", "F05_internal_gap_void", "delamination",
                   "dimensional accuracy"},
    "packaging": {"F01_structural_deformation", "F03_crack_enamel",
                  "F04_shrink_outcome", "F05_internal_gap_void", "delamination",
                  "dimensional accuracy"},
    "general": {"F01_structural_deformation", "F04_shrink_outcome",
                "F05_internal_gap_void", "F06_printability", "delamination",
                "dimensional accuracy", "softening effect"},
    "optics": {"F05_internal_gap_void", "F01_structural_deformation",
               "fabrication error", "dimensional accuracy"},
    "general_to_3dp": {"F01_structural_deformation", "F05_internal_gap_void",
                       "F06_printability", "delamination", "dimensional accuracy"},
    "optics_to_3dp": {"F01_structural_deformation", "F05_internal_gap_void",
                      "F06_printability", "fabrication error",
                      "dimensional accuracy"},
}


def normalize_concept(s):
    return (s or "").lower().rstrip("*").strip()


def map_to_family(concept):
    """B 端词 → family_id 或独立概念 id（小写输入）。"""
    c = normalize_concept(concept)
    fm = family_map()
    if c in fm:
        return fm[c]
    if c in STANDALONE:
        return STANDALONE[c]
    return None


# ── 矩阵构建 ─────────────────────────────────────────────────────────────
def build_matrix():
    mem = load("s7_relation_memory.json")
    rels = mem["relations"]

    # (domain, family) → {status: {kind: count}, community: [ids], proposals: [ids], R/U}
    cells = defaultdict(lambda: {"covered": 0, "failed": 0, "queued": 0,
                                 "rejected": 0, "invalid": 0, "dup": 0,
                                 "unprobed": 0, "comm": [], "prop": [],
                                 "R": 0, "U": 0, "new": 0, "last_round": 0})
    domains = set()

    for r in rels:
        dom = r["domain"]
        domains.add(dom)
        if r["kind"] == "community":
            fams = {map_to_family(o) for o in r["observables"]}
            fams = {f for f in fams if f}
            for f in fams:
                c = cells[(dom, f)]
                # EX community（source=s7_execute）decision 用 VALIDATED 等；
                # covered = 真实检索有产出（S6 KEEP / EX VALIDATED）
                st = ("covered" if r["decision"] in ("KEEP", "VALIDATED",
                                                     "PENDING_QA")
                      else "failed")
                c[st] += 1
                c["comm"].append(r["id"])
                c["R"] += r["outcome"].get("relevant") or 0
                c["U"] += r["outcome"].get("uncertain") or 0
                c["new"] += r["outcome"].get("new") or 0
                c["last_round"] = max(c["last_round"], 0)
        else:
            fam = map_to_family(r["concept_B"])
            if not fam:
                continue  # B 不在词源族内（不应发生；filter 保证）
            c = cells[(dom, fam)]
            dec = r["decision"]
            if dec == "RUN":
                c["queued"] += 1
            elif dec == "SKIP_LOW_NOVELTY":
                c["rejected"] += 1
            elif dec == "SKIP_INVALID":
                c["invalid"] += 1
            elif dec == "DUPLICATE":
                c["dup"] = c.get("dup", 0) + 1
            c["prop"].append(r["id"])
            c["last_round"] = max(c["last_round"], r.get("round", 0))

    # B 池全量概念族（generator 可生成的列全集）
    layers = load("s7_term_layers.json")
    b_concepts = set()
    for key in ("R_main_verified", "R_main_normalized", "R_discovery"):
        for e in layers["layers"][key]:
            if e["role"] == "observable":
                f = map_to_family(e["term"])
                if f:
                    b_concepts.add(f)
    # 补：词源 observable 全量族 + QA 实际裁决族
    for (dom, f) in cells:
        b_concepts.add(f)

            # 组装 matrix（域只保留物理集合里出现的）
    matrix = {}
    for dom in sorted(domains):
        row = {}
        for f in sorted(b_concepts):
            cell = cells.get((dom, f))
            # physical = 白名单 ∧/∨ 有真实记录（KEEP/FAIL/RUN 任一即证明该组合存在——
            # 白名单只裁决"从未触达"的格子，防手写漏项把有证据的格标成 off-domain）
            has_evidence = cell is not None and (
                cell["covered"] or cell["failed"] or cell["queued"])
            phys_ok = (f in PHYSICAL.get(dom, set())) or has_evidence
            if cell is None:
                if phys_ok:
                    row[f] = {"status": "unprobed", "note": "从未触达",
                              "physical": True}
                continue
            # 状态优先级：covered(真实产出) > failed(真实检索死) > queued(纸面RUN)
            #            > rejected > invalid。covered 的 family 若仍有大量 rejected
            #            提案 → saturation 信号（空间接近穷尽）。
            d = dict(cell)
            if d["covered"]:
                st = "covered"
            elif d["failed"]:
                st = "failed"
            elif d["queued"]:
                st = "queued"
            elif d["rejected"]:
                st = "rejected"
            elif d["invalid"]:
                st = "invalid"
            else:
                st = "unprobed" if phys_ok else "off-domain"
            d["status"] = st
            d["physical"] = phys_ok
            n_prop = d["queued"] + d["rejected"] + d["invalid"]
            d["saturation"] = round(d["rejected"] / n_prop, 2) if n_prop else 0.0
            row[f] = d
        matrix[dom] = row

    return matrix, domains, b_concepts


# ── 汇总 ────────────────────────────────────────────────────────────────
def summarize(matrix, b_concepts):
    """双层汇总：
    ① dom_stat = 实例级计数（proposal RUN/SKIP/INV 条数 + community KEEP/FAIL 条数）
    ② family_stat = family 级主状态分布（矩阵单元格数）
    ③ gaps = 物理成立 ∧ unprobed（从未触达）"""
    dom_inst = defaultdict(Counter)   # domain → {RUN/SKIP_LOW_NOVELTY/...: n}
    dom_comm = defaultdict(Counter)   # domain → {KEEP/FAIL: n}
    fam_stat = defaultdict(Counter)   # domain → {covered/queued/...: n cells}
    all_cells = []
    for dom, row in matrix.items():
        for f, cell in row.items():
            if not isinstance(cell, dict) or "status" not in cell:
                continue
            st = cell["status"]
            fam_stat[dom][st] += 1
            # 从 cell 反推实例计数（cell 里 covered 是 community 命中数？不——
            # covered/failed 是 community 条目数，queued/rejected/invalid 是 proposal 条数）
            for k in ("queued", "rejected", "invalid", "dup"):
                if cell.get(k):
                    dom_inst[dom][k] += cell[k]
            for k in ("covered", "failed"):
                if cell.get(k):
                    dom_comm[dom][k] += cell[k]
            all_cells.append((dom, f, cell))

    dom_stat = {}
    for dom in set(list(dom_inst) + list(dom_comm)):
        inst = dict(dom_inst.get(dom, {}))
        comm = dict(dom_comm.get(dom, {}))
        dom_stat[dom] = {
            "community": comm,           # KEEP/FAIL 条数（真实检索）
            "proposal": inst,            # RUN/SKIP/INV 条数（QA 裁决）
            "n_proposal_instances": sum(inst.values()),
            "n_community": sum(comm.values()),
            "cells": dict(fam_stat.get(dom, {})),
        }

    # gap = 物理成立 ∧ unprobed（从未触达）∧ family 属于 B 池
    gaps = []
    gen_domains = {"dental", "composites", "sla", "coatings", "optics",
                   "packaging", "general"}
    for dom, f, cell in all_cells:
        if cell["status"] == "unprobed" and cell.get("physical"):
            gaps.append({"domain": dom, "family": f,
                         "reachable": dom in gen_domains,
                         "note": cell.get("note", "")})
    return dom_stat, gaps


def main():
    matrix, domains, b_concepts = build_matrix()
    dom_stat, gaps = summarize(matrix, b_concepts)

    print("=== domain × 命中计数（community: family 命中数 | proposal: QA 条数）===")
    print(f"{'domain':<14s} {'KEEP命中':>7s} {'FAIL命中':>7s} {'RUN':>4s} "
          f"{'SKIP':>4s} {'INV':>3s} {'DUP':>3s}  {'family_cells':>12s}")
    for dom in sorted(dom_stat):
        s = dom_stat[dom]
        c = s["community"]; p = s["proposal"]
        print(f"{dom:<14s} {c.get('covered',0):>7d} {c.get('failed',0):>7d} "
              f"{p.get('queued',0):>4d} {p.get('rejected',0):>4d} "
              f"{p.get('invalid',0):>3d} {p.get('dup',0):>3d}  {s['cells']}")

    print(f"\n=== GAP: 物理成立 ∧ 从未触达（uncovered cells n={len(gaps)}）===")
    for g in gaps:
        print(f"  {g['domain']:14s} × {g['family']}")

    # 输出
    out = {
        "schema_version": "1.0",
        "role": "S7.3 coverage matrix（memory v2 之上，纯只读派生）",
        "status": "DEVELOPMENT_DIAGNOSTIC",
        "built_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "sources": ["s7_relation_memory.json", "s7_variant_families.json",
                    "s7_term_layers.json"],
        "design": {
            "rows": "domain（community+proposal 出现的域）",
            "cols": "B 池概念族（variant family F01-F06 + 独立概念）",
            "cell_status": ["covered(S6 KEEP)", "failed(S6 FAIL)",
                            "queued(QA RUN 纸面)", "rejected(QA SKIP_LOW)",
                            "invalid(QA SKIP_INVALID)", "unprobed(从未触达)"],
            "saturation": "单元格内 rejected/(queued+rejected+invalid) —— 空间穷尽信号",
            "gap_def": "物理成立 ∧ unprobed —— 喂 generator 的 uncovered cells",
        },
        "domain_summary": dom_stat,
        "matrix": matrix,
        "uncovered_cells": gaps,
        "counts": {
            "domains": len(domains),
            "concept_families": len(b_concepts),
            "physical_unprobed": len(gaps),
        },
    }
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n[ok] {OUT}")
    print(f"  domains={len(domains)} concept_families={len(b_concepts)} "
          f"physical_gaps={len(gaps)}")


if __name__ == "__main__":
    main()
