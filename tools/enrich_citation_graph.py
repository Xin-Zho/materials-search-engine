"""tools/enrich_citation_graph.py — v3.0 DATA ENRICHMENT BACKLOG 通用工具（接口先行）。

背景（用户 2026-08-29 定稿）：gold 联调显示 backward/forward citation 覆盖不足
（backward 0/16），但**不要**为了 QGS gold 16 篇决定参数和修复策略——等真实
Audit Round misses 验证 citation 是不是实际瓶颈后再投入批量。

本工具 = 通用接口（参数化 paper_ids + backward/forward + 缓存），默认 DRY_RUN。
任何 Audit Round 的 misses 都可以复用缓存。

数据源：OpenAlex referenced_works（backward）+ cited_by（forward，cites:{id} cursor）。
复用 search_engine/completeness/universe_builder 的分页 fetch 函数。

用法：
  python tools/enrich_citation_graph.py --papers W1000524895,W100950536 \
      --backward --forward                 # 真实拉取（缓存到 data/cache/citation_graph.json）
  python tools/enrich_citation_graph.py --audit-round pc_001::20260829043656 \
      --backward --dry-run                 # 只显示计划（对 audit 样本）
  python tools/enrich_citation_graph.py --check-coverage                    # 缓存覆盖统计

缓存结构 data/cache/citation_graph.json：
  {"works": {wid: {"referenced_works": [wid...], "cited_by": [wid...],
                   "updated_at": ts, "source": "openalex"}}}
"""
import argparse
import asyncio
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

CACHE_PATH = os.path.join(BASE, "data", "cache", "citation_graph.json")


def load_cache() -> dict:
    if os.path.exists(CACHE_PATH):
        return json.load(open(CACHE_PATH, encoding="utf-8"))
    return {"works": {}, "created_at": None}


def save_cache(cache: dict):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)


def check_coverage(paper_ids: list[str] | None = None) -> dict:
    cache = load_cache()
    works = cache.get("works", {})
    if paper_ids:
        ids = set(paper_ids)
        with_b = sum(1 for i in ids
                     if works.get(i, {}).get("referenced_works"))
        with_f = sum(1 for i in ids
                     if works.get(i, {}).get("cited_by"))
        return {"n_targets": len(ids),
                "backward_covered": f"{with_b}/{len(ids)}",
                "forward_covered": f"{with_f}/{len(ids)}",
                "cache_works": len(works)}
    return {"cache_works": len(works)}


async def enrich(paper_ids: list[str], backward: bool, forward: bool,
                 dry_run: bool, mailto: str = "") -> dict:
    """对 paper_ids 拉取 citation 边并入缓存（OpenAlex，幂等）。"""
    from search_engine.discovery.openalex_backend import OpenAlexBackend  # noqa: E402
    from search_engine.completeness.universe_builder import (
        fetch_references_by_id, fetch_cited_by)                          # noqa: E402

    cache = load_cache()
    works = cache.setdefault("works", {})
    todo = [w for w in paper_ids if w not in works]
    print(f"targets={len(paper_ids)} | 缓存已有={len(paper_ids) - len(todo)} | "
          f"待拉取={len(todo)}{'（dry-run 不执行）' if dry_run else ''}")
    if dry_run or not todo:
        return check_coverage(paper_ids)

    backend = OpenAlexBackend(mailto=mailto or None)
    ok_b = ok_f = fail = 0
    for i, wid in enumerate(todo, 1):
        entry = works.setdefault(wid, {"referenced_works": [], "cited_by": [],
                                       "updated_at": None, "source": "openalex"})
        try:
            if backward:
                refs = await fetch_references_by_id(backend, wid)
                entry["referenced_works"] = list(dict.fromkeys(refs))
                ok_b += 1
            if forward:
                cites = await fetch_cited_by(backend, wid)
                entry["cited_by"] = list(dict.fromkeys(cites))
                ok_f += 1
            entry["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        except Exception as e:
            fail += 1
            print(f"  [WARN] {wid} 失败: {e}")
        if i % 20 == 0:
            save_cache(cache)
            print(f"  ... {i}/{len(todo)}")
    save_cache(cache)
    print(f"done: backward_ok={ok_b} forward_ok={ok_f} fail={fail}")
    return check_coverage(paper_ids)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--papers", default="", help="逗号分隔 W ids")
    ap.add_argument("--audit-round", default="",
                    help="对指定 audit 的 sampled_paper_ids 操作（读 completeness_audits）")
    ap.add_argument("--backward", action="store_true", help="拉 referenced_works")
    ap.add_argument("--forward", action="store_true", help="拉 cited_by")
    ap.add_argument("--dry-run", action="store_true", help="只显示计划，不拉取")
    ap.add_argument("--check-coverage", action="store_true")
    ap.add_argument("--mailto", default="", help="OpenAlex polite pool 邮箱")
    args = ap.parse_args()

    if args.check_coverage:
        print(json.dumps(check_coverage(), ensure_ascii=False))
        return

    paper_ids: list[str] = []
    if args.papers:
        paper_ids = [p.strip() for p in args.papers.split(",") if p.strip()]
    elif args.audit_round:
        sys.path.insert(0, os.path.join(BASE, "search_engine", "completeness"))
        from completeness.audit import find_audit
        a = find_audit(args.audit_round)
        if a is None:
            sys.exit(f"audit 不存在: {args.audit_round}")
        paper_ids = list(a.sampled_paper_ids)
        print(f"audit {args.audit_round}: 样本 {len(paper_ids)} 篇")
    else:
        ap.error("需要 --papers 或 --audit-round 或 --check-coverage")

    asyncio.run(enrich(paper_ids, args.backward, args.forward,
                       args.dry_run, args.mailto))


if __name__ == "__main__":
    main()
