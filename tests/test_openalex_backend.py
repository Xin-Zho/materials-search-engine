"""OpenAlexBackend 集成单测：锁定 query 参数真实生效，防止 count 退化为 corpus。

回归锚点（2026-08-25）：search_strict 曾用 `q=` 参数被 OpenAlex 静默忽略——
meta.count=321,958,325 退化整个 corpus + 检索条件不应用，导致 loop 首轮
total=3.2 亿 / retr=0、fallback 全卡 L0、诊断误判"recall 不足"。

正确做法（实证）：`filter=title_and_abstract.search:"a" "b" "c"`，
多短语 AND 语义。

这些测试锁定 invariants：
    meta_count << corpus_count      —— query 真的被应用
    results == 0 or meta_count >= len(results)
    不同 query 的 count 不全等于 corpus

网络依赖：需要可访问 api.openalex.org。
"""

import asyncio

from search_engine.backends import OpenAlexBackend


def test_no_query_is_corpus_count():
    """无 query → count 为亿级 corpus（基线 sanity，确认测试环境有效）。"""
    async def _t():
        async with OpenAlexBackend() as oa:
            await oa.search("", limit=1)
            assert oa.last_total_hits > 100_000_000
    asyncio.run(_t())


def test_search_applies_query():
    """普通 query → count 远小于 corpus 且返回结果。"""
    async def _t():
        async with OpenAlexBackend() as oa:
            r = await oa.search('"machine learning"', limit=5)
            assert len(r) == 5
            assert oa.last_total_hits < 10_000_000
    asyncio.run(_t())


def test_search_strict_and_semantics():
    """strict（title_and_abstract.filter）→ count 远小于 corpus；多短语 AND 递减。"""
    async def _t():
        async with OpenAlexBackend() as oa:
            r1 = await oa.search_strict('"machine learning"', limit=5)
            assert len(r1) > 0
            c1 = oa.last_total_hits
            r2 = await oa.search_strict('"machine learning" "neural network"', limit=5)
            c2 = oa.last_total_hits
            assert 0 < c1 < 5_000_000
            assert 0 < c2 < c1  # AND：短语越多 count 越小
    asyncio.run(_t())


def test_different_queries_never_degrade_to_corpus():
    """不同 query 的 count 都远小于 corpus（不退化）。"""
    async def _t():
        async with OpenAlexBackend() as oa:
            for q in ['"machine learning"', '"polymerization shrinkage"', '"ring opening"']:
                await oa.search(q, limit=1)
                assert oa.last_total_hits < 50_000_000, \
                    f"{q} 退化 corpus: {oa.last_total_hits}"
    asyncio.run(_t())


def test_results_never_exceed_count():
    """invariant：results == 0 or meta_count >= len(results)。"""
    async def _t():
        async with OpenAlexBackend() as oa:
            for fn in (oa.search, oa.search_strict):
                r = await fn('"machine learning"', limit=5)
                assert len(r) == 0 or oa.last_total_hits >= len(r)
    asyncio.run(_t())
