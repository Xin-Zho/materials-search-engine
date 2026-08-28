"""tools/run_community_round1.py — v2.1 第一轮 community 检索执行（用户 2026-08-28 定稿）。

ROUND 1：TC_006 + TC_015（TC_017 = HOLDOUT）。
输入：data/exports/community_round1_queries.json（⑤ 生成）
执行：每 community 独立，逐 query Scopus search+export（直接路径，参考 run_query_families）
输出：data/exports/community_round1_retrieval.json（每 query records + 每 community 汇总）

用法：
  python tools/run_community_round1.py                 # 全部（depth 默认 500）
  python tools/run_community_round1.py --community TC_006 --depth 1000
"""
import argparse
import asyncio
import csv
import io
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

QUERIES_PATH = os.path.join(BASE, "data", "exports", "community_round1_queries.json")
OUT_PATH = os.path.join(BASE, "data", "exports", "community_round1_retrieval.json")


def norm_eid(e: str) -> str:
    return (e or "").strip()


def norm_doi(d: str) -> str:
    return (d or "").strip().lower().replace("https://doi.org/", "").replace("http://doi.org/", "")


async def run_community(engine, community_id: str, queries: list[dict],
                        depth: int) -> dict:
    results = []
    for qi, q in enumerate(queries, 1):
        query = q["query"]
        # search 建立会话（limit 用 depth 会触发内部大导出——用 limit=10 只建上下文，
        # 导出走 direct export；已踩坑：search(limit=depth) 内部会先发起大 job → RETRY）
        await engine.search(query, limit=10, skip_cache=True)
        csv_text = ""
        for attempt in range(3):
            csv_text = await engine._export_via_api(
                query, depth, fields=["eid", "doi", "titles", "year", "venue"],
                poll_retries=90)
            if csv_text.strip():
                break
            print(f"    ⚠️ {community_id} Q{qi} 导出超时/空（{attempt + 1}/3），5s 后重试...")
            await asyncio.sleep(5)
        records = []
        if csv_text.strip():
            for i, row in enumerate(csv.DictReader(io.StringIO(csv_text)), 1):
                eid = norm_eid(row.get("EID") or "")
                doi = norm_doi(row.get("DOI") or "")
                if eid or doi:
                    records.append({"rank": i, "eid": eid, "doi": doi,
                                    "title": (row.get("Title") or row.get("文献标题") or "").strip(),
                                    "year": (row.get("Year") or row.get("年份") or "").strip(),
                                    "venue": (row.get("Source title")
                                              or row.get("来源出版物名称") or "").strip()})
        results.append({"query_id": q["query_id"], "family_id": q["family_id"],
                        "query": query, "exported_count": len(records),
                        "records": records})
        print(f"  ✓ [{qi}/{len(queries)}] {query[:62]} → {len(records)} 条")
        await asyncio.sleep(1)
    # 每 community 汇总（独立，不合并）
    uniq: dict[str, dict] = {}
    for r in results:
        for rec in r["records"]:
            if rec["eid"]:
                uniq.setdefault(rec["eid"], rec)
    return {"community_id": community_id, "n_queries": len(queries),
            "retrieved_unique": len(uniq), "queries": results}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--community", default=None, help="只跑指定 community（默认全部）")
    ap.add_argument("--depth", type=int, default=500, help="导出深度（默认 500）")
    args = ap.parse_args()

    data = json.load(open(QUERIES_PATH, encoding="utf-8"))
    from search_engine.engine import ScopusSearchEngine
    engine = ScopusSearchEngine()
    await engine.start()
    out = {"version": "round1", "created_at": "2026-08-28", "depth": args.depth,
           "communities": {}}
    try:
        for cid, spec in data["communities"].items():
            if args.community and cid != args.community:
                continue
            print(f"\n=== {cid} {spec['community_name']}（depth={args.depth}）===")
            result = await run_community(engine, cid, spec["queries"], args.depth)
            out["communities"][cid] = result
            with open(OUT_PATH, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=1)
    finally:
        await engine.close()
    print(f"\n✓ 已写: {OUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
