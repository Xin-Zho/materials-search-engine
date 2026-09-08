#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""S4 candidate relevance QA（2026-08-30 用户定，克制开发次数前最后一道检查）。

目标 query：CM_V2_imaging（new=835）与 QF17（new=548）——new/raw>0.8 且有明确机制依据
（STRONG dental-measurement community / debonding 语义），不能仅凭 high-new 判定宽/窄。

方法（用户规则）：
- 从 scopus api_cache 读这两条 query 的完整结果（v2 pilot 刚跑过，缓存存在）
- new_vs_S3 判定（build_s3_found_sets 三通道，与 S4 同口径）
- 从 new 集合随机抽 n=40（固定 seed，可复现）
- theme-based 三态判定（**不涉及 R03 miss**——防 miss-specific overfit）：
    RELEVANT   = title/abstract 命中 PROBLEM 词（shrinkage/shrink/contraction/volumetric change/
                shrinkage stress/polymerization stress 等）
    UNCERTAIN  = title 命中 DOMAIN 词 >=2（photopolymer/polymerization/dental/composite/resin/
                curing/light cure 等）
    IRRELEVANT = 无
- 报告 relevant 比例；relevant 比例可观 → 证明 query 打开了 S3 未覆盖的真区域（KEEP）；
  大量无关 → REWRITE

输出：s4_candidate_qa.json（per-query 抽样明细 + 三态分布 + 判定建议）
"""

import json
import os
import random
import re
import sys

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TERM = os.path.join(BASE, "data", "exports", "terminology")
OUT = os.path.join(TERM, "s4_candidate_qa.json")

sys.path.insert(0, os.path.join(BASE, "tools"))
from build_r02_seen import _norm_title  # noqa: E402

PROBLEM = ["shrinkage", "shrink", "contraction", "volumetric change", "volumetric expansion",
           "shrinkage stress", "contraction stress", "polymerization stress", "curing stress",
           "dimensional change", "curing shrinkage", "polymerization shrinkage"]
DOMAIN = ["polymerization", "photopolymer", "photopolymerization", "photocuring", "photocure",
          "light curing", "light cure", "light-cured", "uv curing", "uv-cured", "photocurable",
          "composite resin", "resin composite", "dental", "restorative", "composite", "resin",
          "monomer", "methacrylate", "acrylate", "dental composite", "adhesive", "filling",
          "restoration", "enamel", "dentin", "tooth", "teeth"]


def has_any(text: str, terms) -> bool:
    t = (text or "").lower()
    return any(re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", t) for w in terms)


def label(title: str, abstract: str) -> str:
    if has_any(title, PROBLEM) or has_any(abstract, PROBLEM):
        return "RELEVANT"
    dom = sum(1 for w in DOMAIN
              if re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])",
                           (title or "").lower()))
    return "UNCERTAIN" if dom >= 2 else "IRRELEVANT"


def load_cached_query(qstr: str):
    """从 scopus api_cache 读 query 结果（只读 URI，防 wal 写锁）。"""
    import sqlite3
    db = os.path.join(BASE, "data", "cache", "scopus_cache.db")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    row = con.execute("SELECT result_json FROM api_cache WHERE query_string=?",
                      (qstr,)).fetchone()
    con.close()
    if row is None:
        return None
    from search_engine.cache import SearchCache
    return SearchCache._deserialize_result(row[0])


def new_papers_of(qstr: str, found: dict | None = None):
    """new 判定论文列表（(title, abstract, doi)）。found 三通道可参数化（默认 S3/S4 口径）。"""
    if found is None:
        from build_r03_seen import build_s3_found_sets
        found = build_s3_found_sets()
    res = load_cached_query(qstr)
    if res is None:
        return None
    out = []
    for p in res.papers:
        doi = (getattr(p, "doi", None) or "").lower().replace("https://doi.org/", "")
        title = (getattr(p, "title", None) or "").strip()
        if doi and doi in found["dois"]:
            continue
        if title and _norm_title(title) in found["titles"]:
            continue
        eid = getattr(p, "scopus_url", None)
        if eid:
            import re as _re
            m = _re.search(r"2-s2\.0-(\d+)", str(eid))
            if m and f"2-s2.0-{m.group(1)}" in found["eids"]:
                continue
        out.append({"title": title, "abstract": getattr(p, "abstract", None) or "",
                    "doi": doi})
    return out


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="candidate relevance QA（theme-based 三态）")
    ap.add_argument("--actions", default="",
                    help="actions JSON（{actions:[{action_id, query_string}]}）——S5 PA QA 用；"
                         "缺省 = S4 内置目标（CM_V2_imaging/QF_17）")
    ap.add_argument("--new-basis", default="s3", choices=["s3", "s4"],
                    help="new 判定基准：s3（S4 QA）/ s4（S5 PA QA，build_s4_found_sets）")
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--sample", type=int, default=40, help="每 query 随机抽样数")
    ap.add_argument("--union-n", type=int, default=0,
                    help=">0 时进入 union-level 模式：所有 actions 的 new 候选 union 后"
                         "随机抽 N 篇（不分 query）——估计 P(Relevant | new union)")
    args = ap.parse_args()

    out_path = args.out
    if args.actions:
        # S5 模式：目标 query 从 actions 文件读（PA 子集）
        acts = json.load(open(args.actions, encoding="utf-8"))["actions"]
        targets = {a["action_id"]: args.sample for a in acts}
        qstr_of = {a["action_id"]: a["query_string"] for a in acts}
        if args.new_basis == "s4":
            from build_r04_seen import build_s4_found_sets
            found = build_s4_found_sets()
        else:
            from build_r03_seen import build_s3_found_sets
            found = build_s3_found_sets()
        seed = 20260831
        src_note = f"S5 candidate QA（actions={os.path.basename(args.actions)}，"
        src_note += f"new-basis={args.new_basis}，seed={seed}）"
    else:
        # S4 兼容模式：内置目标
        pilot = json.load(open(os.path.join(TERM, "s4_query_pilot_results.json"),
                               encoding="utf-8"))
        targets = {"CM_V2_imaging": 40, "QF_17": 40}
        qstr_of = {a["action_id"]: a["query_string"] for a in pilot["actions"]
                   if a["action_id"] in targets}
        found = None
        seed = 20260830
        src_note = "S4 candidate QA（CM_V2/QF17）"

    per_query = []
    union_pool = {}
    for aid, n in targets.items():
        qstr = qstr_of.get(aid)
        if not qstr:
            print(f"[WARN] {aid} 不在 actions")
            continue
        news = new_papers_of(qstr, found)
        if news is None:
            print(f"[WARN] {aid} 无缓存结果（pilot 后应已写 api_cache；若缺失需重跑该条）")
            continue
        # union-level 模式也收集 pool
        for p in news:
            key = p["doi"] or _norm_title(p["title"])
            union_pool.setdefault(key, p)
        if args.union_n:
            continue        # union 模式：不逐 query 抽样，最后统一抽
        rng = random.Random(seed)
        sample = rng.sample(news, min(n, len(news)))
        rows = []
        for p in sample:
            lb = label(p["title"], p["abstract"])
            rows.append({"title": (p["title"] or "")[:110], "doi": p.get("doi"),
                         "label": lb})
        from collections import Counter
        dist = Counter(r["label"] for r in rows)
        rel_frac = dist.get("RELEVANT", 0) / len(rows) if rows else 0
        # 边界（用户 2026-08-31）：new=0 → REDUNDANT（无 QA 样本，不是 REWRITE）；
        # 表述：KEEP（高相关新增候选）——candidate relevance ≠ community discovery
        if len(news) == 0:
            verdict = "REDUNDANT（无新候选，无 QA 样本）"
            rel_frac = None
        elif rel_frac >= 0.5:
            verdict = "KEEP（高相关新增候选）"
        elif rel_frac <= 0.2:
            verdict = "REWRITE（大量无关）"
        else:
            verdict = "REVIEW（混合）"
        per_query.append({
            "action_id": aid, "new_total": len(news), "sampled": len(rows),
            "distribution": dict(dist), "relevant_fraction": round(rel_frac, 3)
            if rel_frac is not None else None,
            "verdict_suggestion": verdict, "rows": rows,
            "qa_note": "theme-based 三态（PROBLEM 词/DOMAIN 词），不使用 R04 miss——防 miss-specific overfit；"
                       "KEEP 标签只表示高相关新增候选，不等于发现新区域",
        })
        rel_str = f"{rel_frac:.0%}" if rel_frac is not None else "n/a"
        print(f"  {aid}: new={len(news)} 抽样 {len(rows)} -> {dict(dist)} "
              f"rel={rel_str} [{verdict}]")

    # ── union-level sanity QA（用户 2026-08-31）：不分 query 随机抽 N，估计 P(Relevant|union) ──
    union_result = None
    if args.union_n and union_pool:
        rng = random.Random(seed)
        usample = rng.sample(list(union_pool.values()), min(args.union_n, len(union_pool)))
        urows = []
        for p in usample:
            lb = label(p["title"], p["abstract"])
            urows.append({"title": (p["title"] or "")[:110], "doi": p.get("doi"),
                          "label": lb})
        from collections import Counter
        udist = Counter(r["label"] for r in urows)
        urel = udist.get("RELEVANT", 0) / len(urows) if urows else 0
        union_result = {
            "union_new_total": len(union_pool),
            "sampled": len(urows), "distribution": dict(udist),
            "relevant_fraction": round(urel, 3),
            "note": "union-level：所有 query new 候选 union 后不分层随机抽样——"
                    "估计 P(Relevant | PA/SF new union)；防 action-level sampling artifact",
            "rows": urows,
        }
        print(f"\n[union] new_total={len(union_pool)} 抽样 {len(urows)} -> {dict(udist)} "
              f"rel={urel:.0%}")

    out = {"version": "candidate_qa_v2", "frozen_at": "2026-08-31",
           "development_source": "AUDIT_R04" if args.new_basis == "s4" else "AUDIT_R03",
           "qa_rule": "theme-based 三态（PROBLEM 词命中=RELEVANT；title DOMAIN>=2=UNCERTAIN；否则=IRRELEVANT）；不使用 miss",
           "source": src_note,
           "per_query": per_query,
           "union_qa": union_result}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n[OK] written: {out_path}")


if __name__ == "__main__":
    main()
