"""tools/relative_recall.py — P3-C Relative Recall 计算（用户定 2026-08-27）。

主指标（B5 冻结后）：
  RR = |R_Agent ∩ B_scopus| / |B_scopus|
其中：
  B_scopus      —— Final QGS 中 Scopus eligibility = IN_SCOPUS 的论文
  R_Agent       —— Agent 最终确认 relevant 的论文（KB edges + candidate
                   VALIDATED/PROMOTED，DOI/openalex_id 归一化匹配）
分层输出：overall / by era / by source review / by reason_code。
另输出 missed 列表（B_scopus − R_Agent）供 P3-E Failure Analysis；
MustHitRecall 单独报告（不混入 RR——Expert Must-Hit Set 另行提供）。

⚠️ 防污染：RR 只做匹配统计，不做 relevance 判断；benchmark 冻结后不可改。

用法：
  python tools/relative_recall.py [--benchmark data/exports/pc_001_external_qgs_v1.json]
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
BENCH_PATH = os.path.join(BASE, "data", "exports", "pc_001_external_qgs_v1.json")


def norm_doi(d: str) -> str:
    return (d or "").strip().lower().replace("https://doi.org/", "").replace("http://doi.org/", "").strip()


def norm_wid(pid: str) -> str:
    m = re.search(r"(W\d+)", pid or "")
    return m.group(1) if m else ""


def load_agent_relevant() -> dict:
    """Agent 最终确认 relevant 论文：KB（有 route/mechanism edges）+ candidate
    VALIDATED/PROMOTED。返回 {doi_set, wid_set, by_key}。"""
    dois, wids = set(), set()
    # KB：knowledge_records 的 record_json 里有 doi/openalex_id；edges 表确认 relevant
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
    # candidate pool VALIDATED/PROMOTED source_papers
    if os.path.exists(POOL_PATH):
        pool = json.load(open(POOL_PATH, encoding="utf-8")).get("candidates", [])
        for c in pool:
            if c.get("status") in ("VALIDATED", "PROMOTED"):
                for sp in (c.get("source_papers") or []):
                    w = norm_wid(sp)
                    if w:
                        wids.add(w)
    return {"dois": dois, "wids": wids}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--benchmark", default=BENCH_PATH)
    args = ap.parse_args()

    bench = json.load(open(args.benchmark, encoding="utf-8"))
    st = bench["stats"].get("scopus_eligibility", {})
    papers = bench["papers"]
    # benchmark papers 无 openalex_id 字段（screen_qgs 未存）——从 pool 按 DOI
    # 反查补上（Agent 匹配需要 W id 通道，已踩坑：只靠 DOI 匹配 3/105）
    pool_path = os.path.join(BASE, "data", "exports", "qgs_candidates_v1.json")
    if os.path.exists(pool_path):
        pool = json.load(open(pool_path, encoding="utf-8"))["candidates"]
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
    print("=" * 66)
    print(f"Agent relevant（KB edges ∪ VALIDATED/PROMOTED）: "
          f"{len(agent['dois'])} DOI / {len(agent['wids'])} OpenAlex ID")
    print(f"B_relevant_resolved = {st.get('B_relevant_resolved')}  "
          f"B_scopus = {st.get('B_scopus')}")
    print("=" * 66)

    def matched(p: dict) -> bool:
        d = norm_doi(p.get("doi", ""))
        if d and d in agent["dois"]:
            return True
        w = norm_wid(p.get("openalex_id", ""))
        return bool(w and w in agent["wids"])

    found = [p for p in b_scopus if matched(p)]
    missed = [p for p in b_scopus if not matched(p)]
    rr = len(found) / len(b_scopus) if b_scopus else 0.0
    print(f"\nRR = |R_Agent ∩ B_scopus| / |B_scopus| = {len(found)} / {len(b_scopus)}")
    print(f"Relative Recall = {rr:.4f} ({rr * 100:.1f}%)")
    print()

    # 分层
    def era_of(y):
        try:
            y = int(y)
        except (TypeError, ValueError):
            return "UNKNOWN"
        return "PRE_2006" if y <= 2005 else ("2006_2020" if y <= 2020 else "POST_2020")

    print("by era:")
    eras = {}
    for p in b_scopus:
        eras.setdefault(era_of(p.get("year")), {"n": 0, "hit": 0})
        eras[era_of(p.get("year"))]["n"] += 1
        if matched(p):
            eras[era_of(p.get("year"))]["hit"] += 1
    for e in sorted(eras):
        v = eras[e]
        print(f"  {e:<12} {v['hit']}/{v['n']} = {v['hit'] / v['n'] * 100:.1f}%")

    print("by source review:")
    srcs = {}
    for p in b_scopus:
        for s in p.get("sources_from", []):
            srcs.setdefault(s, {"n": 0, "hit": 0})
            srcs[s]["n"] += 1
            if matched(p):
                srcs[s]["hit"] += 1
    for s in sorted(srcs):
        v = srcs[s]
        print(f"  {s:<6} {v['hit']}/{v['n']} = {v['hit'] / v['n'] * 100:.1f}%")

    print("\nMissed（B_scopus − R_Agent，供 P3-E Failure Analysis）:")
    for p in sorted(missed, key=lambda x: str(x.get("year", ""))):
        print(f"  [{p.get('year')}] {p['title'][:70]} | {p.get('doi')}")
    print(f"\nmissed = {len(missed)}")


if __name__ == "__main__":
    main()
