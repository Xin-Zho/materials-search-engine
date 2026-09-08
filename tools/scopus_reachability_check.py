#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/scopus_reachability_check.py — R05 43 miss 的 Scopus Reachability 分类
（2026-09-01 用户拍板：先做 failure taxonomy，再谈 S6/RL）。

背景：Recall_E2E=46.25% 是相对 OpenAlex universe 的 end-to-end recall，混合了
  Database Coverage Failure（Scopus 不收录）与 Search/Query Failure。必须先拆：

    Recall_E2E = Coverage_Scopus × Recall_Search | Scopus-reachable

判定链（每篇 miss，按序）：
  1. DOI exact lookup：Scopus 检索 DOI("...") 有命中 → REACHABLE（match=doi）
  2. Title exact lookup：TITLE("...") 精确标题命中 → REACHABLE（match=title）
  3. Source/year 证据（用户公开核验 4 篇 + DOI 源特征）→ REACHABLE_PROBABLE /
     PROBABLE_UNREACHABLE（source 收录但 article-level 未证实 / 期刊未被 Scopus 收录）
  4. 兜底 → UNKNOWN

分类合并（与 r05_miss_query_reachability.json 的 verdict 组合成 taxonomy）：
  BACKEND_UNREACHABLE          —— Scopus 不收录 / indexing lag（RL 修不了，禁入训练）
  REACHABLE + MATCHES_NO_QUERY  → QUERY_EXPRESSIVITY miss（Query Generator 的锅）
  REACHABLE + MATCHES_QUERY     → RETRIEVAL / TOKEN-SEMANTICS / BUG miss

重算指标（输出）：
  - Scopus coverage rate among R05 relevant（reachable relevant / 99）
  - Recall_query = P(Seen | Relevant, ScopusReachable)
  - Query-expressivity miss rate among reachable misses
  - Retrieval miss rate among reachable + query-matched

用法（需登录 Scopus 会话，用户终端跑）：
  python tools/scopus_reachability_check.py            # 默认 43 miss
  python tools/scopus_reachability_check.py --plan-only
"""
import argparse
import asyncio
import datetime
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tools"))

from build_r02_seen import _norm_doi, _norm_title  # noqa: E402

T = os.path.join(BASE, "data", "exports", "terminology")
DEFAULT_MISSES = os.path.join(T, "r05_misses.json")
DEFAULT_MQR = os.path.join(T, "r05_miss_query_reachability.json")
DEFAULT_OUT = os.path.join(T, "r05_miss_scopus_reachability.json")

# 用户 2026-09-01 公开核验结论（source-level 证据，替代不了 article-level Scopus 实测）
SOURCE_EVIDENCE = {
    "10.1179/146580103225009068": {
        "source": "Plastics, Rubber and Composites",
        "user_verdict": "SOURCE_SCOPUS_REACHABLE（期刊 Scopus 收录 2001-2026，2003 在覆盖期内）",
        "preclass": "REACHABLE_PROBABLE",
        "note": "article-level 需 DOI lookup 证实",
    },
    "10.1615/compmechcomputapplintj.v9.i2.30": {
        "source": "Composites: Mechanics, Computations, Applications",
        "user_verdict": "SOURCE_SCOPUS_REACHABLE（Scopus Source ID 19800188005，2010-2026 覆盖）",
        "preclass": "REACHABLE_PROBABLE",
        "note": "article-level 需 DOI lookup 证实",
    },
    "10.30880/jamea.2020.01.01.001": {
        "source": "Journal of Advanced Mechanical Engineering Applications (UTHM)",
        "user_verdict": "PROBABLE_SCOPUS_COVERAGE_GAP（未见 Scopus 收录证据，e-ISSN XXXX-XXXX）",
        "preclass": "PROBABLE_UNREACHABLE",
        "note": "source 未被确认收录",
    },
    "10.1117/12.3104495": {
        "source": "SPIE Proceedings 14102 (Electronic Imaging 2026)",
        "user_verdict": "POSSIBLE_INDEXING_LAG（2026-05-27 新卷，volumes submitted for evaluation）",
        "preclass": "POSSIBLE_INDEXING_LAG",
        "note": "SPIE 整体提交 Scopus，新卷有索引延迟",
    },
}
# DOI 源特征预判（非期刊/机构库/附件/preprint → Scopus 收录概率低）
SCOPUS_RISK_MARKERS = ("10.15781", "10.26153", "10.5281", "zenodo", "10.6084",
                       "figshare", "10.21203", "10.2172", "10.82308", ".s001", ".s0")


def _norm(s: str) -> str:
    return _norm_title(s or "")


def _quote_title(t: str) -> str:
    """Scopus TITLE() 引号短语：去内部引号/换行，截断超长。"""
    t = (t or "").replace('"', " ").replace("\n", " ").strip()
    if len(t) > 180:
        t = t[:180].rsplit(" ", 1)[0]
    return t


async def classify_one(engine, m: dict) -> dict:
    doi = _norm_doi(m.get("doi") or "")
    title = m.get("title") or ""
    rec = {"paper_id": m["paper_id"], "doi": doi, "title": title,
           "reachability": "UNKNOWN", "match_channel": None,
           "scopus_hits": None, "matched_scopus_title": None, "evidence": []}

    # 1. DOI exact lookup（Scopus 权威）
    if doi:
        try:
            res = await engine.search(f'DOI("{doi}")', limit=3,
                                      skip_cache=True, write_cache=False)
            hits = res.papers
            rec["scopus_hits"] = len(hits)
            if hits:
                first = hits[0]
                rec["reachability"] = "REACHABLE"
                rec["match_channel"] = "doi"
                rec["matched_scopus_title"] = (getattr(first, "title", None) or "")[:120]
                rec["evidence"].append(f"DOI lookup 命中 {len(hits)} 条")
                return rec
        except Exception as e:
            rec["evidence"].append(f"DOI lookup 异常: {str(e)[:80]}")

    # 2. Title exact lookup
    qt = _quote_title(title)
    if qt:
        try:
            res = await engine.search(f'TITLE("{qt}")', limit=3,
                                      skip_cache=True, write_cache=False)
            hits = res.papers
            rec["scopus_hits"] = rec["scopus_hits"] or 0
            for p in hits:
                pt = _norm(getattr(p, "title", None) or "")
                if pt and pt == _norm(title):
                    rec["reachability"] = "REACHABLE"
                    rec["match_channel"] = "title"
                    rec["matched_scopus_title"] = (getattr(p, "title", None) or "")[:120]
                    rec["evidence"].append(f"TITLE lookup 精确命中")
                    return rec
            if hits:
                rec["evidence"].append(f"TITLE lookup {len(hits)} 条但无精确标题匹配")
        except Exception as e:
            rec["evidence"].append(f"TITLE lookup 异常: {str(e)[:80]}")

    # 3. Source/year 证据（用户核验 + DOI 特征）
    if doi in SOURCE_EVIDENCE:
        se = SOURCE_EVIDENCE[doi]
        rec["evidence"].append(se["user_verdict"])
        pre = se["preclass"]
        if pre in ("REACHABLE_PROBABLE",):
            rec["reachability"] = "REACHABLE_PROBABLE"
            rec["match_channel"] = "source_evidence"
        elif pre == "POSSIBLE_INDEXING_LAG":
            rec["reachability"] = "POSSIBLE_INDEXING_LAG"
            rec["match_channel"] = "source_evidence"
        else:
            rec["reachability"] = "PROBABLE_UNREACHABLE"
            rec["match_channel"] = "source_evidence"
        return rec

    if any(x in doi for x in SCOPUS_RISK_MARKERS):
        rec["reachability"] = "PROBABLE_UNREACHABLE"
        rec["match_channel"] = "doi_pattern"
        rec["evidence"].append("DOI 源特征（机构库/preprint/SI 附件）——Scopus 收录概率低")
        return rec

    return rec  # UNKNOWN 兜底


async def main_async(args):
    from search_engine.engine import ScopusSearchEngine

    misses = json.load(open(args.misses, encoding="utf-8"))["misses"]
    mqr = json.load(open(args.mqr, encoding="utf-8"))
    qv = {r["paper_id"]: r for r in mqr["results"]}
    if args.sample:
        import random
        rng = random.Random(20260901)
        misses = rng.sample(misses, min(args.sample, len(misses)))

    print(f"[reachability] {len(misses)} miss × Scopus DOI→TITLE lookup（+source 证据）")

    if args.plan_only:
        print("\n[plan-only] 将执行（需登录 Scopus）：")
        for m in misses:
            print(f"    {m['paper_id']}  {m.get('doi','')[:40]:<42} {(m.get('title') or '')[:52]}")
        print("\n[plan-only] 结束：未发送任何请求。")
        return

    engine = ScopusSearchEngine(data_dir=args.engine_data_dir)
    await engine.start()
    results = []
    try:
        for i, m in enumerate(misses, 1):
            r = await classify_one(engine, m)
            r["query_verdict"] = qv.get(m["paper_id"], {}).get("verdict")
            r["matched_actions"] = qv.get(m["paper_id"], {}).get("matched_actions", [])
            results.append(r)
            print(f"  [{i}/{len(misses)}] {r['reachability']:<22} {m.get('doi','')[:38]:<40} "
                  f"{(m.get('title') or '')[:46]}")
            await asyncio.sleep(0.5)
    finally:
        await engine.close()

    # ── taxonomy 合并 ──
    from collections import Counter
    tax = Counter()
    for r in results:
        if r["reachability"] in ("REACHABLE", "REACHABLE_PROBABLE"):
            if r["query_verdict"] == "MATCHES_QUERY_BUT_NOT_RETRIEVED":
                tax["RETRIEVAL_OR_TOKEN_SEMANTICS"] += 1
            else:
                tax["QUERY_EXPRESSIVITY"] += 1
        elif r["reachability"] in ("PROBABLE_UNREACHABLE", "POSSIBLE_INDEXING_LAG"):
            tax["BACKEND_UNREACHABLE"] += 1
        else:
            tax["UNKNOWN"] += 1

    n_reachable = tax["QUERY_EXPRESSIVITY"] + tax["RETRIEVAL_OR_TOKEN_SEMANTICS"]
    n_unreachable = tax["BACKEND_UNREACHABLE"]

    out = {
        "version": "r05_miss_scopus_reachability_v1",
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "method": "DOI exact lookup → TITLE exact lookup → source/year 证据（用户核验 4 篇 + DOI 特征）→ UNKNOWN",
        "inputs": {"misses": os.path.basename(args.misses),
                   "n_misses": len(results),
                   "query_reachability": os.path.basename(args.mqr)},
        "taxonomy": dict(tax),
        "recall_decomposition": {
            "relevant_total_R05": 99, "seen_S5": 37, "miss": 43,
            "reachable_misses": n_reachable,
            "unreachable_misses": n_unreachable,
            "recall_e2e": round(37 / 99, 4),
            "recall_query_conditional": round(37 / (37 + n_reachable), 4)
            if (37 + n_reachable) else None,
            "coverage_scopus_among_relevant": round((37 + n_reachable) / 99, 4),
            "note": "Recall_E2E = Coverage_Scopus × Recall_Search|reachable；"
                    "unreachable miss 禁入 Query Generator/RL 训练（P(retrieve)=0）",
        },
        "results": results,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print("\n=== Taxonomy（43 miss）===")
    for k, v in tax.items():
        print(f"  {k:<28} {v}")
    print(f"\n=== Recall 拆分 ===")
    for k, v in out["recall_decomposition"].items():
        if k != "note":
            print(f"  {k:<28} {v}")
    print(f"\n[OK] {args.out}")


def main():
    ap = argparse.ArgumentParser(description="R05 43 miss Scopus Reachability 分类")
    ap.add_argument("--misses", default=DEFAULT_MISSES)
    ap.add_argument("--mqr", default=DEFAULT_MQR)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--engine-data-dir", default="data")
    ap.add_argument("--plan-only", action="store_true")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
