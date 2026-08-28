"""tools/enrich_unresolved_scopus.py — 用 Scopus 补 111 篇 UNRESOLVED 的 abstract 证据（用户定 2026-08-27）。

对缺 abstract 的 UNRESOLVED 论文按 DOI 分批查 Scopus，导出
EID/Title/Authors/Year/Abstract/DOI，把 abstract 补进 adjudication 清单。

⚠️ 方法学（用户拍板）：Scopus 这一步**只补证据，不自动判断 RELEVANT/
IRRELEVANT**——最终标签由 human reviewer 按冻结 B3 criteria 确认。

用法（需已登录的 Scopus 会话）：
  python tools/enrich_unresolved_scopus.py
      [--manifest C:/Users/Administrator/Downloads/qgs_unresolved_adjudication.json]
      [--save data/exports/qgs_unresolved_adjudication.json]
"""
import argparse
import asyncio
import csv
import io
import json
import os
import re
import sys

sys.path.insert(0, ".")
from search_engine.engine import ScopusSearchEngine

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 数据流闭环（2026-08-27 统一）：adjudicate_unresolved.py 生成 → 本脚本补
# abstract → adjudicate_unresolved.py --interactive 判定，全部读写项目内同一文件
DEFAULT_MANIFEST = os.path.join(BASE, "data", "exports", "qgs_unresolved_adjudication.json")
DEFAULT_SAVE = os.path.join(BASE, "data", "exports", "qgs_unresolved_adjudication.json")
EXPORT_FIELDS = ["eid", "doi", "titles", "year", "authors", "abstract"]


def norm_doi(d: str) -> str:
    return (d or "").strip().lower().replace("https://doi.org/", "").replace("http://doi.org/", "").strip()


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
            "abstract": (row.get("Abstract") or row.get("摘要") or "").strip(),
        })
    return out


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=DEFAULT_MANIFEST)
    ap.add_argument("--save", default=DEFAULT_SAVE)
    ap.add_argument("--batch", type=int, default=50)
    args = ap.parse_args()

    adjud = json.load(open(args.manifest, encoding="utf-8"))
    papers = adjud["papers"]
    have_ab = [p for p in papers if p.get("abstract")]
    need_ab = [p for p in papers if not p.get("abstract")]
    print(f"UNRESOLVED = {len(papers)}")
    print(f"已有 abstract: {len(have_ab)}")
    print(f"缺 abstract（待 Scopus 补）: {len(need_ab)}")

    engine = ScopusSearchEngine()
    await engine.start()
    try:
        n_scopus = 0
        n_have_doi = sum(1 for p in need_ab if norm_doi(p.get("doi", "")))
        for i in range(0, len(need_ab), args.batch):
            chunk = need_ab[i:i + args.batch]
            dois = [norm_doi(p.get("doi", "")) for p in chunk if norm_doi(p.get("doi", ""))]
            if not dois:
                continue
            q = "DOI(" + " OR ".join(dois) + ")"
            print(f"\n批 {i // args.batch + 1}: {len(dois)} DOI → Scopus 检索...")
            await engine.search(q, limit=len(dois), skip_cache=True)  # 建立结果页上下文
            csv_text = await engine._export_via_api(q, len(dois), fields=EXPORT_FIELDS)
            rows = parse_rows(csv_text)
            doi_to_row = {r["doi"]: r for r in rows if r["doi"] and r["abstract"]}
            for p in chunk:
                d = norm_doi(p.get("doi", ""))
                row = doi_to_row.get(d)
                if row:
                    p["abstract"] = row["abstract"][:1500]
                    p["scopus_eid_evidence"] = row["eid"] or None
                    n_scopus += 1
            print(f"  CSV {len(rows)} 行，本批补 {n_scopus}（累计）")

        with open(args.save, "w", encoding="utf-8") as f:
            json.dump(adjud, f, ensure_ascii=False, indent=1)

        still = [p for p in papers if not p.get("abstract")]
        print("\n" + "=" * 60)
        print(f"UNRESOLVED = {len(papers)}")
        print(f"已有 abstract      = {len(have_ab)}")
        print(f"Scopus 新补 abstract = {n_scopus}")
        print(f"仍无 abstract/fulltext = {len(still)}")
        print("=" * 60)
        if still:
            print("\n仍缺证据（需全文/publisher，供人工处理）:")
            for p in still:
                print(f"  [{p.get('year')}] {p['title'][:60]} | {p.get('doi') or 'NO_DOI'}")
        print(f"\n✓ 已写回: {args.save}")
        print("下一步：python tools/adjudicate_unresolved.py --interactive 逐篇判定")
    finally:
        await engine.close()


if __name__ == "__main__":
    asyncio.run(main())
