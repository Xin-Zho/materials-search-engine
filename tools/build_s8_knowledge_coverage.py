"""S8 Knowledge coverage 矩阵（附加层，不改动 s7_coverage_matrix.json）。

用户 Step 3：检索面矩阵（S5/S6 searched + S7 explored cells）升级为
"Knowledge coverage matrix"——反映 KB 对每个 (domain × property/observable)
的知识覆盖状态，用 S8 已入库的论文级抽取产物（edges/mechanisms）标注。

方法（纯数据派生，确定性）：
- S7 R 论文经 ex_sources 归 EX 组 → EX records_by_group 给 (domain, concept_B)
- S6 R 论文经 families 归族（A=anchor-free 语义社区，无 EX domain；单列）
- 对每个 cell 聚合：n_kb_papers（入库 S8 论文数）、n_edges、n_mechanisms、
  top_mechanisms（频次 Top N）、knowledge_status：
    covered   — n_kb_papers >= 3
    partial   — 1 <= n_kb_papers < 3
    empty     — 0（无知识证据）
- 检索层状态并置（原矩阵 covered/failed/unprobed），知识层独立。

产物：data/exports/terminology/s8_knowledge_coverage.json
"""
import json
import os
import sqlite3
from collections import Counter, defaultdict

KB_DB = "data/cache/knowledge_base.db"
T = "data/exports/terminology"
OUT = os.path.join(T, "s8_knowledge_coverage.json")


def main():
    # 1. KB scopus 论文（S8 入库的 152 篇）→ edges/mechanisms
    conn = sqlite3.connect(KB_DB)
    rows = conn.execute(
        "SELECT paper_id, record_json FROM knowledge_records "
        "WHERE paper_id LIKE 'scopus:%'").fetchall()
    conn.close()
    kb = {}
    for pid, rj in rows:
        d = json.loads(rj)
        kb[d["paper_id"]] = d

    # 2. catalog：S8 论文 → source/evidence（ex_sources / families）
    cat = json.load(open(os.path.join(T, "s8_finalkb_catalog.json"),
                         encoding="utf-8"))
    by_key = {p["key"]: p for p in cat["papers"]}

    # 3. EX 组 → (domain, concept_B)
    rec = json.load(open(os.path.join(T, "s7_execute_query_records.json"),
                         encoding="utf-8"))
    ex_dom = {}
    for gid, w in rec["records_by_group"].items():
        ex_dom[gid] = (w.get("domain"), w.get("concept_B"))

    # 4. 聚合：cell_key = "domain × property" 或 "S6-families"
    cells = defaultdict(lambda: {"n_kb_papers": 0, "n_edges": 0,
                                 "n_mechanisms": 0, "labels": Counter(),
                                 "ex_groups": set(), "mechs": Counter(),
                                 "sample_titles": []})
    unassigned = []
    for pid, d in kb.items():
        key = pid.replace("scopus:", "")
        cp = by_key.get(key)
        if cp is None:
            unassigned.append(key)
            continue
        mechs = [m.get("canonical") or m.get("mechanism") or ""
                 for m in d.get("physical_mechanisms") or []]
        n_edge = len(d.get("route_mechanism_edges") or [])
        if cp["source"] in ("S7", "S7_EX12"):
            exs = cp["evidence"].get("ex_sources") or []
            # 取 EX 组 → domain×B；混合组拆多个 cell 会重复计数——用主组
            gid = exs[0] if exs else None
            if gid and gid in ex_dom:
                dom, prop = ex_dom[gid]
                ck = f"{dom} × {prop}"
            else:
                ck = "S7-unknown-ex"
        else:  # S6
            fams = cp["evidence"].get("families") or []
            ck = "S6-family-" + ("+".join(fams) if fams else "none")
        cell = cells[ck]
        cell["n_kb_papers"] += 1
        cell["n_edges"] += n_edge
        cell["n_mechanisms"] += len(mechs)
        cell["labels"][cp["label"]] += 1
        if gid:
            cell["ex_groups"].add(gid)
        for m in mechs:
            if m:
                cell["mechs"][m] += 1
        if len(cell["sample_titles"]) < 3:
            cell["sample_titles"].append(cp["title"][:90])

    # 5. 输出
    out_cells = []
    for ck, c in sorted(cells.items(),
                        key=lambda kv: -kv[1]["n_kb_papers"]):
        n = c["n_kb_papers"]
        status = "covered" if n >= 3 else ("partial" if n >= 1 else "empty")
        top = [m for m, _ in c["mechs"].most_common(8)]
        out_cells.append({
            "cell": ck,
            "n_kb_papers": n,
            "n_edges": c["n_edges"],
            "n_mechanisms": c["n_mechanisms"],
            "labels": dict(c["labels"]),
            "ex_groups": sorted(c["ex_groups"]),
            "top_mechanisms": top,
            "knowledge_status": status,
            "sample_titles": c["sample_titles"],
        })

    # domain 汇总（EX domain 层）
    dom_sum = defaultdict(lambda: {"n_papers": 0, "n_edges": 0})
    for ck, c in cells.items():
        dom = ck.split(" × ")[0] if " × " in ck else ck
        dom_sum[dom]["n_papers"] += c["n_kb_papers"]
        dom_sum[dom]["n_edges"] += c["n_edges"]

    out = {
        "role": "S8 Knowledge coverage（附加层；检索面矩阵见 s7_coverage_matrix.json）",
        "built_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "method": "S8 已入库论文(152 R) 抽取产物按 EX(domain×B)/S6-family 聚合",
        "status_rule": "covered n>=3 / partial 1-2 / empty 0",
        "domain_summary": {k: v for k, v in sorted(
            dom_sum.items(), key=lambda x: -x[1]["n_papers"])},
        "n_unassigned": len(unassigned),
        "cells": out_cells,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print(f"[ok] {OUT}")
    print("=== S8 Knowledge coverage cells ===")
    for c in out_cells:
        print(f"  {c['cell']:<38} {c['knowledge_status']:<9} "
              f"papers={c['n_kb_papers']:>3} edges={c['n_edges']:>3} "
              f"mech={c['n_mechanisms']:>3}")
        print(f"      top: {', '.join(c['top_mechanisms'][:5])}")
    print("\ndomain_summary:")
    for k, v in sorted(dom_sum.items(), key=lambda x: -x[1]["n_papers"]):
        print(f"  {k:<14} papers={v['n_papers']:>3} edges={v['n_edges']:>3}")
    if unassigned:
        print(f"\n⚠ unassigned keys: {len(unassigned)} {unassigned[:5]}")


if __name__ == "__main__":
    main()
