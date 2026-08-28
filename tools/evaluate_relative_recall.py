"""tools/evaluate_relative_recall.py — P3-C Relative Recall 正式评估（QGS v1 冻结后）。

主指标（用户 2026-08-27 定稿）：
  RR = |R_Agent ∩ B_scopus| / |B_scopus|
其中：
  B_scopus  —— Final QGS 中 Scopus eligibility = IN_SCOPUS 的论文（现 120）
  R_Agent   —— Agent 最终确认 relevant 的论文（KB route/mechanism edges ∪
               candidate VALIDATED/PROMOTED，DOI / openalex_id 双通道匹配）

输出：
  1) 主指标 + QGS missed by Agent
  2) 分层：by era / by source review / by reason_code（P3-E failure analysis 铺垫）
  3) missed 论文导出 data/exports/qgs_missed_by_agent.json（含每篇元数据 +
     missed 分层统计——回答"Agent 漏掉的是哪些年代/来源/机制方向的文献"）

⚠️ 防污染：本脚本只做匹配统计，不做 relevance 判断；benchmark 冻结后不可改。
RR 的分母是 B_scopus（120），NOT_IN_SCOPUS(4)/NOT_CHECKABLE(1) 不参与。

用法：
  python tools/evaluate_relative_recall.py
"""
import argparse
import json
import os
import re
import sqlite3
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KB_PATH = os.path.join(BASE, "data", "cache", "knowledge_base.db")
POOL_PATH = os.path.join(BASE, "data", "exports", "phase2_candidates.json")
QGS_POOL_PATH = os.path.join(BASE, "data", "exports", "qgs_candidates_v1.json")
BENCH_PATH = os.path.join(BASE, "data", "exports", "pc_001_external_qgs_v1.json")
MISSED_PATH = os.path.join(BASE, "data", "exports", "qgs_missed_by_agent.json")


def norm_doi(d: str) -> str:
    return (d or "").strip().lower().replace("https://doi.org/", "").replace("http://doi.org/", "").strip()


def norm_wid(pid: str) -> str:
    m = re.search(r"(W\d+)", pid or "")
    return m.group(1) if m else ""


def load_agent_relevant() -> dict:
    """Agent 最终确认 relevant：KB（有 route/mechanism edges）+ candidate
    VALIDATED/PROMOTED。返回 {dois, wids}。"""
    dois, wids = set(), set()
    con = sqlite3.connect(f"file:{KB_PATH}?mode=ro", uri=True)
    try:
        edge_pids = set(r[0] for r in con.execute(
            "SELECT DISTINCT paper_id FROM route_mechanism_edges"))
        rows = con.execute("SELECT paper_id, record_json FROM knowledge_records").fetchall()
        for pid, rj in rows:
            if pid not in edge_pids:
                continue
            try:
                rec = json.loads(rj)
            except Exception:
                rec = {}
            d = norm_doi(rec.get("doi", "") or rec.get("DOI", ""))
            if d:
                dois.add(d)
            w = norm_wid(rec.get("openalex_id", "") or pid)
            if w:
                wids.add(w)
    finally:
        con.close()
    if os.path.exists(POOL_PATH):
        pool = json.load(open(POOL_PATH, encoding="utf-8")).get("candidates", [])
        for c in pool:
            if c.get("status") in ("VALIDATED", "PROMOTED"):
                for sp in (c.get("source_papers") or []):
                    w = norm_wid(sp)
                    if w:
                        wids.add(w)
    return {"dois": dois, "wids": wids}


def era_of(y):
    try:
        y = int(y)
    except (TypeError, ValueError):
        return "UNKNOWN"
    return "PRE_2006" if y <= 2005 else ("2006_2020" if y <= 2020 else "POST_2020")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--benchmark", default=BENCH_PATH)
    args = ap.parse_args()

    bench = json.load(open(args.benchmark, encoding="utf-8"))
    st = bench["stats"].get("scopus_eligibility", {})
    papers = bench["papers"]

    # benchmark papers 无 openalex_id 字段（screen_qgs 未存）——从 pool 按 DOI
    # 反查补上（Agent 匹配需要 W id 通道，已踩坑：只靠 DOI 匹配 3/105）
    if os.path.exists(QGS_POOL_PATH):
        pool = json.load(open(QGS_POOL_PATH, encoding="utf-8"))["candidates"]
        pool_by_doi = {norm_doi(c.get("doi", "")): c for c in pool}
        n_back = 0
        for p in papers:
            if not p.get("openalex_id"):
                c = pool_by_doi.get(norm_doi(p.get("doi", "")))
                if c and c.get("openalex_id"):
                    p["openalex_id"] = c["openalex_id"]
                    n_back += 1
        print(f"benchmark openalex_id 反查补全: {n_back}/{len(papers)}")

    b_scopus = [p for p in papers if p.get("scopus_eligibility") == "IN_SCOPUS"]
    agent = load_agent_relevant()

    def matched(p: dict) -> bool:
        d = norm_doi(p.get("doi", ""))
        if d and d in agent["dois"]:
            return True
        w = norm_wid(p.get("openalex_id", ""))
        return bool(w and w in agent["wids"])

    found = [p for p in b_scopus if matched(p)]
    missed = [p for p in b_scopus if not matched(p)]
    rr = len(found) / len(b_scopus) if b_scopus else 0.0

    print("=" * 66)
    print("P3-C Relative Recall（External QGS v1 冻结后）")
    print("=" * 66)
    print(f"Final B_total          = {st.get('B_relevant_resolved')}")
    print(f"B_scopus              = {len(b_scopus)}   (IN_SCOPUS，均有 EID 正向证据)")
    print(f"  NOT_IN_SCOPUS       = {st.get('B_not_scopus')}")
    print(f"  NOT_CHECKABLE       = {st.get('B_not_checkable')}")
    print(f"Agent relevant set    = {len(agent['wids'])}   "
          f"({len(agent['dois'])} DOI / {len(agent['wids'])} OpenAlex ID)")
    print(f"Agent ∩ B_scopus      = {len(found)}")
    print(f"Relative Recall       = {len(found)}/{len(b_scopus)} = {rr * 100:.1f}%")
    print(f"QGS missed by Agent   = {len(missed)}")
    print("=" * 66)

    # ── 分层 1：era ──
    print("\nby era:")
    eras = {}
    for p in b_scopus:
        e = era_of(p.get("year"))
        eras.setdefault(e, {"n": 0, "hit": 0})
        eras[e]["n"] += 1
        if matched(p):
            eras[e]["hit"] += 1
    for e in sorted(eras):
        v = eras[e]
        print(f"  {e:<12} {v['hit']}/{v['n']} = {v['hit'] / v['n'] * 100:.1f}%")

    # ── 分层 2：source review ──
    print("\nby source review:")
    srcs = {}
    for p in b_scopus:
        for s in p.get("sources_from", []):
            srcs.setdefault(s, {"n": 0, "hit": 0, "miss": 0})
            srcs[s]["n"] += 1
            if matched(p):
                srcs[s]["hit"] += 1
            else:
                srcs[s]["miss"] += 1
    for s in sorted(srcs):
        v = srcs[s]
        print(f"  {s:<6} {v['hit']}/{v['n']} = {v['hit'] / v['n'] * 100:.1f}%  "
              f"(missed {v['miss']})")

    # ── 分层 3：reason_code（机制方向）──
    print("\nby reason_code（机制方向）:")
    rc = {}
    for p in b_scopus:
        k = p.get("reason_code") or "NO_CODE"
        rc.setdefault(k, {"n": 0, "hit": 0})
        rc[k]["n"] += 1
        if matched(p):
            rc[k]["hit"] += 1
    for k in sorted(rc):
        v = rc[k]
        print(f"  {k:<32} {v['hit']}/{v['n']} = {v['hit'] / v['n'] * 100:.1f}%")

    # ── missed 导出（P3-E failure analysis 输入）──
    missed_out = {
        "benchmark_id": bench["benchmark_id"],
        "version": bench.get("version"),
        "created_at": "2026-08-27",
        "B_scopus": len(b_scopus),
        "agent_matched": len(found),
        "missed_count": len(missed),
        "rr": round(rr, 4),
        "stats": {
            "by_era": {e: eras[e] for e in sorted(eras)},
            "by_source": srcs,
            "by_reason_code": rc,
        },
        "missed_papers": [{
            "idx": p.get("idx"),
            "title": p.get("title"),
            "year": p.get("year"),
            "venue": p.get("venue"),
            "doi": p.get("doi"),
            "openalex_id": p.get("openalex_id"),
            "sources_from": p.get("sources_from", []),
            "reason_code": p.get("reason_code"),
            "era": era_of(p.get("year")),
        } for p in sorted(missed, key=lambda x: str(x.get("year", "")))],
    }
    with open(MISSED_PATH, "w", encoding="utf-8") as f:
        json.dump(missed_out, f, ensure_ascii=False, indent=1)
    print(f"\n✓ missed 已导出: {MISSED_PATH} ({len(missed)} 篇)")

    # 状态分支（用户 2026-08-28 定：按 benchmark status 输出下一步）
    fs = bench.get("freeze_status", "UNKNOWN")
    if fs == "FINAL_FREEZE":
        print("✓ P3-C complete（QGS v1 final RR）")
        print("  P3-E failure analysis already available（data/exports/qgs_failure_analysis.json）")
    elif fs == "PENDING_ELIGIBILITY_AND_RR":
        print("⚠️ 本输出为 provisional（PENDING 论文未跑 eligibility）")
        print("  下一步：python tools/check_scopus_eligibility.py → 重跑本脚本出 final RR")
    elif fs == "PRE_SIGNOFF":
        print("下一步：完成 human sign-off / consistency-audit merge")
    else:
        print("下一步：确认 benchmark freeze_status 后再定")
    n_pending = sum(1 for p in papers if p.get("scopus_eligibility") == "PENDING")
    if n_pending:
        print(f"  当前 PENDING 待查: {n_pending} 篇（不在 B_scopus 分母）")
    print("方法学注：RR 分母为 B_scopus（IN_SCOPUS）；NOT_CHECKABLE 未计入，"
          "不在 Scopus/不可查的论文属 database coverage limitation，不影响 RR 分母语义")


if __name__ == "__main__":
    main()
