"""tools/fetch_review_candidates.py — B1 多入口综述候选拉取（用户定 2026-08-27 v2）。

External QGS 构造第 B1 步：用 6 条**独立 broad problem query**（不含 route 名，
避免 lexical bias 和依赖 Agent 已发现路线）拉综述候选，union 去重后输出
20–30 篇给领域专家筛选，冻结 5–10 个 source reviews。

⚠️ 防污染：本脚本只输出综述列表，不做 relevance 判断；筛选由领域专家完成；
已参与系统开发的综述（W3026448945、W4280619650）不得入选。

运行（需已登录的 Scopus 会话，data/scopus_profile/）：
  python tools/fetch_review_candidates.py [--per-query 20] [--save data/exports/review_candidates.json]
"""
import argparse
import asyncio
import json
import sys

sys.path.insert(0, ".")
from search_engine.engine import ScopusSearchEngine

# 6 条独立 broad problem query（协议 v2 第 4 节）——不含 route 名
REVIEW_QUERIES = [
    'DOCTYPE(re) AND TITLE-ABS-KEY(photopolymer* AND shrinkage)',
    'DOCTYPE(re) AND TITLE-ABS-KEY(photocur* AND shrinkage)',
    'DOCTYPE(re) AND TITLE-ABS-KEY("polymerization stress" AND photocur*)',
    'DOCTYPE(re) AND TITLE-ABS-KEY("volume contraction" AND photopolymer*)',
    'DOCTYPE(re) AND TITLE-ABS-KEY("photocurable resin" AND shrinkage)',
    'DOCTYPE(re) AND TITLE-ABS-KEY("vat photopolymerization" AND shrinkage)',
]


def norm_doi(doi: str) -> str:
    return (doi or "").strip().lower().replace("https://doi.org/", "").strip()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-query", type=int, default=20)
    ap.add_argument("--save", default="data/exports/review_candidates.json")
    args = ap.parse_args()

    engine = ScopusSearchEngine()
    await engine.start()
    try:
        seen: dict[str, dict] = {}
        print("=" * 80)
        for qi, q in enumerate(REVIEW_QUERIES, 1):
            result = await engine.search(q, limit=args.per_query)
            print(f"[query {qi}] total={result.total_count}  {q}")
            for p in result.papers:
                doi = norm_doi(p.doi or "")
                key = doi or (p.title or "").lower()[:80]
                if key in seen:
                    seen[key]["queries"].append(qi)
                    continue
                seen[key] = {
                    "title": p.title,
                    "year": p.year,
                    "doi": p.doi,
                    "citation_count": p.citation_count,
                    "venue": p.venue,
                    "scopus_url": p.scopus_url,
                    "document_type": p.document_type,
                    "queries": [qi],
                }
            await asyncio.sleep(1)  # 温和间隔，避免触发风控

        # 按被引排序输出
        candidates = sorted(seen.values(), key=lambda x: -(x["citation_count"] or -1))
        print("=" * 80)
        print(f"union 去重后候选综述 {len(candidates)} 篇（来自 {len(REVIEW_QUERIES)} 条 query）:")
        print(f"{'#':>3} {'年':>5} {'被引':>6} {'q':>3}  {'标题'}")
        for i, c in enumerate(candidates):
            qs = ",".join(map(str, c["queries"]))
            cited = c["citation_count"] if c["citation_count"] is not None else -1
            print(f"{i+1:>3} {c['year'] or '?':>5} {cited:>6} {qs:>3}  {(c['title'] or '')[:88]}")
            c["rank"] = i + 1

        with open(args.save, "w", encoding="utf-8") as f:
            json.dump({"queries": REVIEW_QUERIES, "candidates": candidates},
                      f, ensure_ascii=False, indent=2)
        print()
        print(f"✓ 候选已保存: {args.save}")
        print("下一步：人工按 5 条标准冻结 5–10 个 source reviews（覆盖本课题/系统全面/"
              "未参与开发/年代多样/来源独立），把 # 编号或 DOI 报给我，我来拉 references。")
    finally:
        await engine.close()


if __name__ == "__main__":
    asyncio.run(main())
