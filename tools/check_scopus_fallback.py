"""tools/check_scopus_fallback.py — B5 fallback v2：4 篇 DOI no-hit 的 title/year 复核（用户定 2026-08-27）。

v2 修复（用户抓的 false-positive bug）：TITLE-ABS-KEY 候选**必须通过 identity
validation** 才能进 IN_SCOPUS，否则维持 NOT_IN_SCOPUS：
  DOI 精确命中            → IN_SCOPUS（DOI_EXACT_FALLBACK，最强证据）
  title 相似度 ≥ 0.90
  AND |year_target - year_hit| ≤ 1
  AND （可选）first author 一致
                          → IN_SCOPUS（TITLE_YEAR_FALLBACK）
  否则                     → reject，NOT_IN_SCOPUS（证据升级）

已知反例（不会再有）：paraquat toxicity vs radical ring-opening；
benzoxazine vs low-shrinkage monomers；mechanochemical grafting vs
chain cross-linking photopolymerization。

运行（需已登录的 Scopus 会话）：
  python tools/check_scopus_fallback.py
"""
import argparse
import asyncio
import csv
import difflib
import io
import json
import os
import re
import sys

sys.path.insert(0, ".")
import httpx
from search_engine.engine import ScopusSearchEngine

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH_PATH = os.path.join(BASE, "data", "exports", "pc_001_external_qgs_v1.json")

# 每篇的手工 title 检索词（取显著词，避免整句过严）
TITLE_QUERIES = {
    "10.7243/2053-5775-1-1": "flowable composite resins electronic mercury dilatometer",
    "10.1080/23337931.2018.1444488": "low-shrinkage monomers physicochemical experimental composite resin",
    "10.1002/polc.5070640104": "radical ring-opening polymerization expansion in volume",
    "10.1021/bk-1988-0367.ch028": "chain cross-linking photopolymerization tetraethyleneglycol diacrylate",
}

TITLE_SIM_THRESHOLD = 0.95   # 用户定：宁可 NOT_IN_SCOPUS 不产生假阳性
YEAR_TOLERANCE = 1


def norm_doi(d: str) -> str:
    return (d or "").strip().lower().replace("https://doi.org/", "").replace("http://doi.org/", "").strip()


def title_sim(a: str, b: str) -> float:
    na = re.sub(r"[^a-z0-9 ]", " ", (a or "").lower())
    nb = re.sub(r"[^a-z0-9 ]", " ", (b or "").lower())
    na, nb = " ".join(na.split()), " ".join(nb.split())
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def first_author(auth_str: str) -> str:
    """Scopus Authors 列 'Surname A.B., Surname C.D.' → 首作者 surname。"""
    if not auth_str:
        return ""
    first = (auth_str or "").split(",")[0].strip()
    # 去掉名字缩写（"Feilzer A.J." → "Feilzer"；"Bowen" → "Bowen"）
    m = re.match(r"([A-Za-zÀ-ž'\-]+)", first)
    return m.group(1).lower() if m else first.lower()


def author_match(hit_auth: str, target_first: str) -> bool | None:
    """首作者匹配：命中作者未知返回 None（不因缺 author 拒绝）；有则严格比对。"""
    if not target_first:
        return None
    ha = first_author(hit_auth)
    if not ha:
        return None
    return ha == target_first.lower()


def year_diff(hit_year, target_year) -> int:
    try:
        return abs(int(hit_year or 0) - int(target_year or 0))
    except (TypeError, ValueError):
        return 999


def validate_candidate(row: dict, target: dict) -> tuple[bool, str]:
    """identity validation（用户 2026-08-27 定稿）：
    DOI 精确命中 → IN_SCOPUS；
    title_sim ≥0.95 AND |year差|≤1 AND first-author 匹配（缺 author 时不因缺而拒）
    → IN_SCOPUS；否则 REJECTED_IDENTITY_MISMATCH。"""
    t_doi = norm_doi(target.get("doi", ""))
    r_doi = norm_doi(row.get("doi", ""))
    if t_doi and r_doi and t_doi == r_doi:
        return True, "DOI_EXACT_FALLBACK"
    sim = title_sim(row.get("title", ""), target.get("title", ""))
    yd = year_diff(row.get("year"), target.get("year"))
    am = author_match(row.get("authors", ""), target.get("first_author", ""))
    if sim >= TITLE_SIM_THRESHOLD and yd <= YEAR_TOLERANCE and am is not False:
        return True, (f"TITLE_YEAR_AUTHOR_FALLBACK(sim={sim:.2f}, ydiff={yd}, "
                      f"author={'match' if am else 'unknown'})")
    return False, (f"REJECTED_IDENTITY_MISMATCH(sim={sim:.2f}<{TITLE_SIM_THRESHOLD} "
                   f"or ydiff={yd}>{YEAR_TOLERANCE} or author={'no' if am is False else 'unknown'})")


def parse_rows(csv_text: str) -> list[dict]:
    if not csv_text.strip():
        return []
    out = []
    for row in csv.DictReader(io.StringIO(csv_text)):
        out.append({
            "doi": norm_doi(row.get("DOI", "")),
            "eid": (row.get("EID") or "").strip(),
            "title": (row.get("Title") or row.get("文献标题") or "").strip(),
            "year": row.get("Year") or row.get("年份") or "",
            "authors": (row.get("Authors") or row.get("作者") or "").strip(),
        })
    return out


async def main():
    ap = argparse.ArgumentParser()
    args = ap.parse_args()

    bench = json.load(open(BENCH_PATH, encoding="utf-8"))
    targets = [p for p in bench["papers"]
               if p.get("scopus_eligibility") in ("NOT_IN_SCOPUS", "PENDING_RECHECK")]
    print(f"待 fallback: {len(targets)} 篇（NOT_IN_SCOPUS + PENDING_RECHECK，identity validation 开启）")

    # target first_author：Crossref works/{doi} 补（4 篇，成本低）
    async with httpx.AsyncClient(timeout=30, trust_env=False,
                                 headers={"User-Agent": "materials-search/0.1"}) as cr:
        for t in targets:
            doi = norm_doi(t.get("doi", ""))
            t["first_author"] = ""
            if not doi:
                continue
            try:
                r = await cr.get(f"https://api.crossref.org/works/{doi}")
                if r.status_code == 200:
                    auths = r.json().get("message", {}).get("author", [])
                    if auths:
                        fam = auths[0].get("family", "")
                        t["first_author"] = fam or ""
            except Exception:
                pass
            await asyncio.sleep(0.3)

    engine = ScopusSearchEngine()
    await engine.start()
    try:
        for p in targets:
            doi = norm_doi(p.get("doi", ""))
            title_q = TITLE_QUERIES.get(doi) or p["title"]
            year = p.get("year")
            q = f'TITLE("{title_q}")'
            if year:
                q += f" AND PUBYEAR = {year}"
            print(f"\n[{p['title'][:55]}] ({year})  first_author={p.get('first_author','')}")
            print(f"  query: {q}")
            try:
                result = await engine.search(q, limit=5, skip_cache=True)
                print(f"  total={result.total_count}")
                csv_text = await engine._export_via_api(
                    q, 5, fields=["eid", "doi", "titles", "year", "authors"])
                rows = parse_rows(csv_text)
                ok = False
                if not rows:
                    # 放宽：TITLE-ABS-KEY 只取首尾显著词
                    q2 = (f'TITLE-ABS-KEY({title_q.split()[0]}) AND '
                          f'TITLE-ABS-KEY({title_q.split()[-1]})')
                    if year:
                        q2 += f" AND PUBYEAR = {year}"
                    print(f"  fallback: {q2}")
                    await engine.search(q2, limit=5, skip_cache=True)
                    csv_text = await engine._export_via_api(
                        q2, 5, fields=["eid", "doi", "titles", "year", "authors"])
                    rows = parse_rows(csv_text)
                for r in rows:
                    good, why = validate_candidate(r, p)
                    sim = title_sim(r.get("title", ""), p.get("title", ""))
                    yd = year_diff(r.get("year"), p.get("year"))
                    am = author_match(r.get("authors", ""), p.get("first_author", ""))
                    print(f"  ⚠️ candidate EID={r['eid']} | {r['title'][:55]} | "
                          f"{r['year']} | {r['authors'][:25]}")
                    print(f"     title similarity = {sim:.2f} | year match = "
                          f"{'yes' if yd <= YEAR_TOLERANCE else 'no'} | "
                          f"author match = {('yes' if am else 'no' if am is False else 'unknown')}")
                    print(f"     → {'✅ ' + why if good else '❌ ' + why}")
                    if good:
                        p["scopus_eligibility"] = "IN_SCOPUS"
                        p["match_method"] = why
                        p["scopus_eid"] = r["eid"]
                        ok = True
                        break
                if not ok:
                    # 显式写回三态（不能只打印"维持"——已踩坑：PENDING_RECHECK
                    # 未覆盖导致统计少 1：109-105=4 但 B_not_scopus=3）
                    print(f"  ❌ 无通过 identity validation 的候选 → 落 NOT_IN_SCOPUS")
                    p["scopus_eligibility"] = "NOT_IN_SCOPUS"
                    p["scopus_eid"] = None
                    p["evidence"] = "TITLE_AUTHOR_YEAR_NO_IDENTITY_MATCH"
                    p["match_method"] = "REJECTED_IDENTITY_MISMATCH"
            except Exception as e:
                print(f"  ⚠️ 查询失败: {type(e).__name__} {e}")

        st = bench.setdefault("stats", {}).setdefault("scopus_eligibility", {})
        st["B_relevant_resolved"] = len(bench["papers"])
        st["B_scopus"] = sum(1 for x in bench["papers"] if x.get("scopus_eligibility") == "IN_SCOPUS")
        st["B_not_scopus"] = sum(1 for x in bench["papers"] if x.get("scopus_eligibility") == "NOT_IN_SCOPUS")
        st["B_pending"] = sum(1 for x in bench["papers"] if x.get("scopus_eligibility") == "PENDING_RECHECK")
        st["B_not_checkable"] = sum(1 for x in bench["papers"] if x.get("scopus_eligibility") == "NOT_CHECKABLE")
        # 硬 invariant（用户 2026-08-27）：四态之和必须等于 resolved relevant
        total = st["B_scopus"] + st["B_not_scopus"] + st["B_pending"] + st["B_not_checkable"]
        assert total == st["B_relevant_resolved"], (
            f"scopus 状态账目不平: {total} != {st['B_relevant_resolved']}")
        st["ScopusCoverage_QGS_provisional"] = round(
            st["B_scopus"] / st["B_relevant_resolved"], 4) if st["B_relevant_resolved"] else 0.0
        with open(BENCH_PATH, "w", encoding="utf-8") as f:
            json.dump(bench, f, ensure_ascii=False, indent=1)
        print("\n" + "=" * 60)
        print(f"B_relevant_resolved = {st['B_relevant_resolved']}")
        print(f"B_scopus            = {st['B_scopus']}")
        print(f"B_not_scopus        = {st['B_not_scopus']}")
        print(f"B_pending           = {st['B_pending']}")
        print(f"B_not_checkable     = {st['B_not_checkable']}")
        print(f"provisional ScopusCoverage_QGS = {st['ScopusCoverage_QGS_provisional']}")
        print(f"✓ benchmark 已更新（四态 invariant 通过）: {BENCH_PATH}")
    finally:
        await engine.close()


if __name__ == "__main__":
    asyncio.run(main())
