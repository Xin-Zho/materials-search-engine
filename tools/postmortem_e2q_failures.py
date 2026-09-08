"""tools/postmortem_e2q_failures.py — v2.2-A Postmortem Attribution（QGS-guided diagnostic）。

固定输入：Round3 未检索回的 4 篇 graph-visible QGS（1999 flowable / 1987 methacrylate
esters / 1998 shrink toward light / 1997 laser speckle）。

纪律（用户 2026-08-29 定稿）：
  analysis_mode = QGS_GUIDED_DIAGNOSTIC
  leakage = true
  algorithm_changes_allowed = false
  query_generation_allowed = false
  -> 本工具只解剖失败，不产生新 query、不改任何 v2.1 参数。

归因链（E2Q1-6，可程序判断，多标签）：
  Paper -> Term -> ExpansionCandidate -> Ranking -> Queryability
         -> PilotSelection -> Retrieval
  E2Q1 TERM_EXTRACTION_GAP       目标论文关键语言不在支持论文语言池（社区 terms）
  E2Q2 SUPPORT_GATE_GAP          语言在社区但未进入 v2.1.2 候选（quality gate /
                                 canonical family / df_dedup<2 过滤）
  E2Q3 RANKING_GAP               候选存在但 ExpansionScore 排名 > Top50 检查范围
  E2Q4 ANCHOR_COMPATIBILITY_GAP  term 是真实语言但 "anchor" AND term 无有效结果集
                                 （STANDALONE_ZERO / ANCHOR_INCOMPATIBLE）
  E2Q5 QUERY_UTILITY_SELECTION_GAP  anchor-queryable 但 pilot MCG<2 或
                                 per-community cap / greedy merge 淘汰
  E2Q6 QUERY_SET_GAP             query 正式生成执行但目标论文不在 Scopus result set

方法（不猜关键词）：用与 v2.1.2 完全相同的 extract_verbatim + quality_gate +
canonicalize_phrase（复用 tools/build_round3_expansion_terms.py）从目标论文
title/abstract 重新提取 phrase，逐个追踪其在系统中的经历，输出 phrase trace 表。

输入（默认路径，可 --xxx 覆盖）：
  --targets   data/exports/round3_qgs_evaluation.json   （NOT_RETRIEVED 4 篇）
  --round3    %TEMP%/round3_expansion_B_v5.json          （all_verbatim_terms + reps queryability）
  --pilot     %TEMP%/pilot_v215_offline.json             （EID 修复版 per-query MCG）
  --formal    data/exports/round3_queries_depth500.json  （16 条冻结 query）
  --comm      %TEMP%/term_communities_hop2.json          （社区语言池）
  abstract 补全：scopus_cache（doi）+ openalex_hop2_enriched.json（title/doi）

输出：data/exports/postmortem_e2q_failures.json + 控制台 trace 表 + E2Q1-6 汇总
（多标签，不强求总和=4）
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter, defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tools"))

from build_round3_expansion_terms import (canonicalize_phrase,  # noqa: E402
                                          extract_verbatim,
                                          quality_gate)

SCOPUS_CACHE = os.path.join(BASE, "data", "cache", "scopus_cache.db")
OUT_PATH = os.path.join(BASE, "data", "exports", "postmortem_e2q_failures.json")
ANCHOR = "polymerization shrinkage"
TOP_N = 50          # v2.1.4 anchored 检查范围（与 build 脚本一致）
MIN_MCG = 2         # v2.1.4 selection 门槛


# ── 数据加载 ──────────────────────────────────────────────

def _path(p: str) -> str:
    if os.path.exists(p):
        return p
    alt = os.path.join(os.environ.get("TEMP", ""), os.path.basename(p))
    if os.path.exists(alt):
        return alt
    raise FileNotFoundError(f"找不到数据文件: {p}（试 {alt}）")


def load_abstracts() -> dict[str, str]:
    """doi -> abstract（scopus_cache 优先，openalex enriched 兜底）。"""
    out: dict[str, str] = {}
    tmp = os.path.join(tempfile.gettempdir(), "scopus_cache_ro_pm.db")
    if not os.path.exists(tmp) or os.path.getmtime(tmp) < os.path.getmtime(SCOPUS_CACHE):
        shutil.copy2(SCOPUS_CACHE, tmp)
    con = sqlite3.connect(f"file:{tmp}?mode=ro&immutable=1", uri=True)
    for pid, j in con.execute("SELECT paper_id, normalized_json FROM papers"):
        nj = json.loads(j)
        doi = (nj.get("doi") or "").strip().lower()
        ab = (nj.get("abstract") or "").strip()
        if doi and ab:
            out.setdefault(doi, ab)
    con.close()
    # openalex enriched 兜底（title 用不到，这里按 doi；脚本里 title 匹配在 targets 侧）
    try:
        d = json.load(open(_path("openalex_hop2_enriched.json"), encoding="utf-8"))
        for wid, m in d.items():
            doi = (m.get("doi") or "").strip().lower()
            ab = (m.get("abstract") or "").strip()
            if doi and ab:
                out.setdefault(doi, ab)
    except FileNotFoundError:
        pass
    return out


def load_v5(path: str) -> dict:
    d = json.load(open(path, encoding="utf-8"))
    terms = {t["term"]: t for t in d["all_verbatim_terms"]}
    reps = d["representative_terms"]
    reps_sorted = sorted(reps, key=lambda r: -r["score"])
    return {"terms": terms, "reps": reps_sorted}


def load_pilot(path: str) -> dict[str, dict]:
    d = json.load(open(path, encoding="utf-8"))
    return {u["term"]: u["query_utility"] for u in d["all_utilities"]}


def load_formal(path: str) -> set[str]:
    d = json.load(open(path, encoding="utf-8"))
    return {q["expansion_term"] for q in d["queries"]}


def load_community_language(path: str) -> set[str]:
    """支持论文语言池：term_communities_hop2 全部社区的 terms + top_terms_used。
    canonical 化后返回集合（判定 E2Q1 vs E2Q2 用）。
    """
    d = json.load(open(path, encoding="utf-8"))
    lang: set[str] = set()
    for c in d["communities"]:
        for t in c.get("terms", []) or []:
            canon = canonicalize_phrase(t)
            if canon:
                lang.add(canon)
        for t in c.get("top_terms_used", []) or []:
            canon = canonicalize_phrase(t)
            if canon:
                lang.add(canon)
    return lang


def load_targets(path: str) -> list[dict]:
    d = json.load(open(path, encoding="utf-8"))
    return [p for p in d["g2r"]["per_paper"] if not p["round3_retrieved"]]


# ── 归因 ──────────────────────────────────────────────────

def trace_phrase(canon: str, surface: str, v5: dict, pilot: dict,
                 formal: set[str], comm_lang: set[str]) -> dict:
    """单个 phrase 的归因追踪。返回 trace row + failure stage。"""
    row = {"phrase": surface, "canonical": canon,
           "extracted": True,
           "quality": "PASS" if quality_gate(surface) else "FAIL",
           "df_dedup": None, "source_community": None, "score": None,
           "rank": None, "anchored": None, "pilot_mcg": None,
           "formal_query": None, "failure": None, "failure_detail": None}
    t = v5["terms"].get(canon)
    if t is None:
        # 语言在不在支持论文语言池？
        if canon in comm_lang:
            row["failure"] = "E2Q2_SUPPORT_GATE_GAP"
            row["failure_detail"] = "语言存在于社区术语池但未进入 v2.1.2 候选（quality gate/canonical family/df_dedup<2 过滤）"
        else:
            row["failure"] = "E2Q1_TERM_EXTRACTION_GAP"
            row["failure_detail"] = "支持论文语言池无此短语（evidence communities 文本中不存在）"
        return row
    row["df_dedup"] = t.get("df_dedup")
    row["source_community"] = t.get("source_community")
    row["score"] = t.get("expansion_score")
    # 找 representative（term 本身或族内代表）
    rep = next((r for r in v5["reps"] if r["representative"] == canon), None)
    if rep is None:
        rep = next((r for r in v5["reps"] if canon in (r.get("family") or [])), None)
    if rep is None:
        row["failure"] = "E2Q3_RANKING_GAP"
        row["failure_detail"] = f"候选存在但无 representative（score={row['score']}）"
        return row
    rank = next(i + 1 for i, r in enumerate(v5["reps"]) if r is rep)
    row["rank"] = rank
    qa = rep.get("queryability") or {}
    row["anchored"] = qa.get("status")
    if rank > TOP_N:
        row["failure"] = "E2Q3_RANKING_GAP"
        row["failure_detail"] = f"ExpansionScore 排名 {rank} > Top{TOP_N} 检查范围"
        return row
    st = qa.get("status")
    if st == "QUERYABILITY_UNKNOWN":
        row["failure"] = "QUERYABILITY_UNKNOWN"
        row["failure_detail"] = "queryability 执行失败（timeout/导出中断），非语义断点"
        return row
    if st in ("ANCHOR_INCOMPATIBLE", "STANDALONE_ZERO"):
        row["failure"] = "E2Q4_ANCHOR_COMPATIBILITY_GAP"
        row["failure_detail"] = f'"{ANCHOR}" AND "{surface}" 无法形成有效结果集（{st}）'
        return row
    if st != "ANCHOR_QUERYABLE":
        row["failure"] = "E2Q5_QUERY_UTILITY_SELECTION_GAP"
        row["failure_detail"] = f"queryability 状态 {st}（未进入 pilot）"
        return row
    # pilot
    pu = pilot.get(rep["preferred_surface"]) or {}
    mcg = pu.get("QueryMCG")
    row["pilot_mcg"] = mcg
    if mcg is None or mcg < MIN_MCG:
        row["failure"] = "E2Q5_QUERY_UTILITY_SELECTION_GAP"
        row["failure_detail"] = f"pilot MCG={mcg} < {MIN_MCG}（或未跑 pilot）"
        return row
    # formal
    if rep["preferred_surface"] not in formal:
        row["failure"] = "E2Q5_QUERY_UTILITY_SELECTION_GAP"
        row["failure_detail"] = "pilot MCG 达标但被 per-community cap / greedy coverage merge 淘汰"
        return row
    row["formal_query"] = next(q for q in formal if q == rep["preferred_surface"])
    row["failure"] = "E2Q6_QUERY_SET_GAP"
    row["failure_detail"] = "正式 query 已执行但目标论文不在其 Scopus result set（rank 超出导出深度）"
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", default=os.path.join(BASE, "data", "exports",
                                                      "round3_qgs_evaluation.json"))
    ap.add_argument("--round3", default="round3_expansion_B_v5.json")
    ap.add_argument("--pilot", default="pilot_v215_offline.json")
    ap.add_argument("--formal", default=os.path.join(BASE, "data", "exports",
                                                     "round3_queries_depth500.json"))
    ap.add_argument("--comm", default="term_communities_hop2.json")
    ap.add_argument("--out", default=OUT_PATH)
    args = ap.parse_args()

    v5 = load_v5(_path(args.round3))
    pilot = load_pilot(_path(args.pilot))
    formal = load_formal(_path(args.formal))
    comm_lang = load_community_language(_path(args.comm))
    abstracts = load_abstracts()
    targets = load_targets(args.targets)

    # 4 篇的 doi 从 QGS 审计补（evaluation 只有 eid/title/year）
    audit = json.load(open(os.path.join(BASE, "data", "exports",
                                        "research_usability_audit.json"),
                           encoding="utf-8"))
    qd = {d["eid"]: d for d in audit["qgs_detail"]}
    # openalex enriched 兜底 title 匹配
    try:
        oa = json.load(open(_path("openalex_hop2_enriched.json"), encoding="utf-8"))
    except FileNotFoundError:
        oa = {}

    print("=" * 96)
    print(f"v2.2-A Postmortem Attribution（QGS-guided diagnostic | "
          f"leakage=true | 不改算法 | 不产生 query）")
    print(f"ANCHOR='{ANCHOR}' | Top{TOP_N} | min_mcg={MIN_MCG}")
    print("=" * 96)

    agg = Counter()
    per_paper_out = []
    for w in targets:
        doi = (qd.get(w["eid"], {}).get("doi") or "").lower()
        title = w["title"]
        abstract = abstracts.get(doi, "")
        if not abstract:
            for wid, m in oa.items():
                if (m.get("title") or "").strip().lower() == title.strip().lower():
                    abstract = m.get("abstract") or ""
                    break
        # 用与 v2.1.2 完全相同的 extractor 重新提取
        rows = extract_verbatim([{"wid": w["eid"], "title": title,
                                  "abstract": abstract}])
        phrases: dict[str, str] = {}
        for r in rows:
            canon = canonicalize_phrase(r["phrase"])
            if canon and canon not in phrases:
                phrases[canon] = r["phrase"]
        # 标题短语单独提取（标题核心语言，primary 判定优先）
        title_rows = extract_verbatim([{"wid": w["eid"], "title": title,
                                        "abstract": ""}])
        title_canons = {canonicalize_phrase(r["phrase"])
                        for r in title_rows}

        traces = [trace_phrase(canon, surface, v5, pilot, formal, comm_lang)
                  for canon, surface in sorted(phrases.items())]
        # 排除 anchor 短语本身（"polymerization shrinkage" 是问题锚词，不是 expansion 语言，
        # 每篇论文都会提取到它，对归因无信息量）
        anchor_canon = canonicalize_phrase(ANCHOR)
        for t in traces:
            t["is_title_phrase"] = t["canonical"] in title_canons
        traces = [t for t in traces if t["canonical"] != anchor_canon]
        failures = [t["failure"] for t in traces if t["failure"]]

        # primary：标题核心短语优先，df 最小（最稀有）者优先，tie 取 E2Q 编号最大。
        # （df 最小 = 最能标识"这篇论文的独特语言"；E2Q1 里含大量单篇 n-gram 噪声，
        #   所以系统级断点 E2Q2-6 比 E2Q1 更有归因价值——tie-break 编号大优先。）
        def _key(t):
            df = t["df_dedup"]
            rank_df = 0 if df is None else df          # None（不在系统）视为最稀有
            e2q = int(t["failure"][3]) if t["failure"].startswith("E2Q") else 99
            return (rank_df, -e2q)                      # 升序 df，降序 e2q
        candidates = [t for t in traces if t["failure"] and t["is_title_phrase"]]
        if not candidates:
            candidates = [t for t in traces if t["failure"]]
        primary = None
        if candidates:
            best = min(candidates, key=_key)
            primary = best["failure"]
        secondary = sorted({f for f in failures if f != primary})
        # paper 级多标签计数（每篇论文每个 failure 类只计 1 次）
        for f in set(failures):
            agg[f] += 1

        print(f"\nPaper: {w['year']} {title[:58]}  [abstract_len={len(abstract)}]")
        print(f"  eid={w['eid']} doi={doi} | phrases={len(traces)} | "
              f"primary={primary} secondary={secondary}")
        hdr = (f"  {'phrase':<30} {'df':>3} {'src':>7} {'score':>7} {'rank':>4} "
               f"{'anchored':<24} {'MCG':>4} {'formal':<7} failure")
        print(hdr)
        shown = 0
        for t in traces:
            if not t["failure"]:
                continue                    # 控制台只显示有断点的短语
            df = t["df_dedup"] if t["df_dedup"] is not None else "-"
            sc = t["score"] if t["score"] is not None else "-"
            rk = t["rank"] if t["rank"] is not None else "-"
            an = t["anchored"] or "-"
            mcg = t["pilot_mcg"] if t["pilot_mcg"] is not None else "-"
            fq = t["formal_query"] or "-"
            fl = t["failure"] or "OK"
            mark = "*" if t.get("is_title_phrase") else " "
            print(f" {mark} {t['canonical'][:29]:<29} {df!s:>3} {str(t['source_community'] or '-'):>7} "
                  f"{str(sc):>7} {str(rk):>4} {an:<24} {str(mcg):>4} {fq:<7} {fl}")
            shown += 1
        if shown == 0:
            print("  (无断点短语——检查归因输入)")
        per_paper_out.append({
            "eid": w["eid"], "year": w["year"], "title": title, "doi": doi,
            "abstract_len": len(abstract),
            "primary_failure": primary,
            "secondary_failures": secondary,
            "all_failures": failures,
            "phrases": traces,
        })

    print("\n" + "=" * 96)
    print("E2Q1-6 failure distribution（多标签，sum 可 > 4）:")
    for k in sorted(agg):
        print(f"  {k:<38} {agg[k]} papers")
    if not agg:
        print("  (无失败记录——检查输入)")

    out = {
        "version": "v22a_postmortem_attribution",
        "analysis_mode": "QGS_GUIDED_DIAGNOSTIC",
        "leakage": True,
        "algorithm_changes_allowed": False,
        "query_generation_allowed": False,
        "anchor": ANCHOR,
        "top_n": TOP_N,
        "min_mcg": MIN_MCG,
        "method": "用与 v2.1.2 完全相同的 extract_verbatim + quality_gate + "
                  "canonicalize_phrase 从目标论文 title/abstract 重新提取 phrase，"
                  "逐个追踪其经历（不猜关键词）",
        "e2q_distribution": dict(agg),
        "papers": per_paper_out,
        "note": "多标签归因（一篇可多个 failure）；primary=链上走得最远且可解释的断点；"
                "QUERYABILITY_UNKNOWN 是执行失败非语义断点；本工具不产生新 query",
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n[OK] 已写: {args.out}")


if __name__ == "__main__":
    main()
