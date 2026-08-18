"""OpenAlex 检索层 sanity check。

对比 search= vs filter=title.search 两种调用方式，打印完整响应 + rate-limit。

用法:
    python diagnose_openalex.py
"""

import asyncio
import httpx

QUERIES = [
    "polymerization shrinkage",
    "filler loading polymerization shrinkage stress",
    "ring-opening polymerization dental composite shrinkage",
]


async def check_via_search(client, query):
    """用 search= 参数（官方推荐，支持布尔 AND）。"""
    r = await client.get(
        "https://api.openalex.org/works",
        params={"search": query, "per_page": 5},
    )
    return r


async def check_via_filter(client, query):
    """用 filter=title.search: 参数（我们代码里当前用的）。"""
    r = await client.get(
        "https://api.openalex.org/works",
        params={"filter": f"title.search:{query}", "per_page": 5},
    )
    return r


async def main():
    async with httpx.AsyncClient(timeout=30) as client:
        for query in QUERIES:
            print(f"=== query: {query!r} ===\n")

            # 方式 1: search=
            r1 = await check_via_search(client, query)
            print(f"[search=] HTTP {r1.status_code}")
            try:
                d1 = r1.json()
                print(f"  meta.count = {d1.get('meta', {}).get('count')}")
                print(f"  len(results) = {len(d1.get('results', []))}")
            except Exception:
                print(f"  body = {r1.text[:200]}")
            print(f"  rate-limit: remaining={r1.headers.get('X-RateLimit-Remaining')}, used={r1.headers.get('X-RateLimit-Credits-Used')}")
            print()

            # 方式 2: filter=title.search:
            r2 = await check_via_filter(client, query)
            print(f"[filter=title.search:] HTTP {r2.status_code}")
            try:
                d2 = r2.json()
                print(f"  meta.count = {d2.get('meta', {}).get('count')}")
                print(f"  len(results) = {len(d2.get('results', []))}")
                if d2.get('error'):
                    print(f"  error = {d2.get('error')}")
            except Exception:
                print(f"  body = {r2.text[:200]}")
            print()


if __name__ == "__main__":
    asyncio.run(main())
