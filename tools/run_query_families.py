"""tools/run_query_families.py — v2.0.1 Depth-only Retrieval Diagnosis（用户 2026-08-28 定稿）。

v2.0 结论：Query-Family Diversification VALIDATED（RR 2.99%→16.42%），但 coverage
不足。v2.0.1 只改变导出深度 d（query/family/K_f/backend 全冻结），把 EXPORT_DEPTH
与 FAMILY_BUDGET 两个瓶颈拆开。

关键设计：**一次 d=1000 run，保存截断前完整记录（含 rank）**——RR(d) 整条曲线
可离线按 rank 阈值重放（d=100/200/500/1000 无需重跑）。

每条记录：
  {query_id, family_id, rank(1-based), eid, doi, title, year, venue, leakage}
  - rank = CSV 行序（Scopus export 返回顺序 = relevance 排序近似）
  - leakage = query 是否含 QGS-learned 词（contraction/setting stress/hardening
    stress/dental/holography/coating 等）——CLEAN-only vs FULL 曲线需要

用法：
  python tools/run_query_families.py --plan-only
  python tools/run_query_families.py --depth 1000     # 单次大导出（默认）
  python tools/run_query_families.py --depth 2000     # 若 export-service 支持
"""
import argparse
import asyncio
import csv
import io
import json
import os
import re
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from search_engine.query.family_registry import load_registry

RUNS_PATH = os.path.join(BASE, "data", "exports", "query_family_runs_depth.json")

# QGS-learned 词（判定 query leakage）——与 concept_slots 的 QGS_V1_LEARNED 一致
LEAKAGE_TERMS = ("contraction", "setting stress", "hardening stress",
                 "cure-induced stress", "cure induced stress", "dental",
                 "holograph", "coating", "volume change", "dimensional change")


def query_is_leakage(query: str) -> bool:
    q = query.lower()
    return any(t in q for t in LEAKAGE_TERMS)


async def export_records(engine, query: str, depth: int, query_id: str,
                         family_id: str) -> list[dict]:
    """导出单条 query 的 depth 条记录（含 rank），失败抛异常（由调用方记录）。

    ⚠️ 大导出（d≥1000）bulk-job 生成时间常超 30s——engine._export_via_api
    传 poll_retries=90（默认 30 会超时返回空，已踩坑 2026-08-28）；
    空结果重试 2 次（超时后 job 可能已完成，重试可拿到）。
    """
    # 1. search 建立结果页上下文（skip_cache=True 强制真实导航；total_count
    #    解析不可靠，忽略——已踩坑 2026-08-27 B5 / 2026-08-28 v2.0 run）
    await engine.search(query, limit=depth, skip_cache=True)
    # 2. export 拿 EID/DOI/Title/Year/Venue（正向数据源；大导出延长轮询）
    #    ⚠️ fields 里的 "venue" 不是合法 fieldGroupIdentifier，被 Scopus 静默
    #    忽略 → 22133/22133 条 venue 全空（2026-08-28 实锤）。期刊名字段组
    #    用 "source"；解析侧另有宽 fallback 兜底列名差异。
    csv_text = ""
    for attempt in range(3):
        csv_text = await engine._export_via_api(
            query, depth, fields=["eid", "doi", "titles", "year", "source"],
            poll_retries=90)
        if csv_text.strip():
            break
        print(f"  ⚠️ 导出超时/空，重试 {attempt + 1}/3...")
        await asyncio.sleep(5)
    records = []
    if csv_text.strip():
        rows = list(csv.DictReader(io.StringIO(csv_text)))
        for i, row in enumerate(rows, 1):
            eid = (row.get("EID") or "").strip()
            doi = (row.get("DOI") or "").strip()
            title = (row.get("Title") or row.get("文献标题") or "").strip()
            year = (row.get("Year") or row.get("年份") or "").strip()
            # locale=zh-CN 时列名是中文；兼容多种可能的期刊名列名
            venue = (row.get("Source title") or row.get("来源出版物名称")
                     or row.get("Venue") or row.get("Source")
                     or row.get("Publication name") or row.get("期刊名")
                     or row.get("来源") or "").strip()
            if not (eid or doi):
                continue
            records.append({
                "query_id": query_id, "family_id": family_id,
                "rank": i,
                "eid": eid, "doi": doi,
                "title": title, "year": year, "venue": venue,
                "leakage": query_is_leakage(query),
            })
    return records


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan-only", action="store_true")
    ap.add_argument("--depth", type=int, default=1000,
                    help="每条 query 导出深度（v2.0.1 唯一变量；默认 1000）")
    ap.add_argument("--registry", default="")
    args = ap.parse_args()

    reg_path = args.registry or os.path.join(BASE, "data", "query_registry_v2.json")
    registry = load_registry(reg_path)
    families = registry["families"]
    n_q = sum(len(f["generated_queries"]) for f in families)
    print(f"registry: {len(families)} families / {n_q} queries")
    print(f"v2.0.1 depth sweep: d={args.depth}（query/family/K_f=200/backend 冻结，"
          f"仅 depth 变化；不 offset 分页）")

    # plan 摘要
    if args.plan_only:
        n_clean = sum(1 for f in families for q in f["generated_queries"]
                      if not query_is_leakage(q))
        print(f"[plan-only] 预计请求 {n_q}（CLEAN-only {n_clean} / "
              f"含 leakage {n_q - n_clean}）→ 存 {args.depth} 条/query 全量记录")
        return

    from search_engine.engine import ScopusSearchEngine
    engine = ScopusSearchEngine()
    # ⚠️ 必须先 start() 启动 CloakBrowser + 建立页面（已踩坑 2026-08-28：
    # 漏掉 start 导致 _page=None → 'NoneType' has no attribute 'url'，且
    # _check_access 会检测登录会话是否有效）
    await engine.start()
    results: dict[str, list[dict]] = {}      # query_id -> records
    errors: dict[str, str] = {}
    qid = 0
    try:
        for f in families:
            fid = f["family_id"]
            for q in f["generated_queries"]:
                qid += 1
                qkey = f"{fid}::{qid}::{q[:40]}"
                try:
                    recs = await export_records(engine, q, args.depth, qkey, fid)
                    results[qkey] = recs
                    print(f"  ✓ {fid} query#{qid} 导出 {len(recs)} 条")
                except Exception as e:
                    errors[qkey] = f"{type(e).__name__}: {str(e)[:100]}"
                    print(f"  ⚠️ {fid} query#{qid} 失败: {type(e).__name__} {str(e)[:80]}")
                await asyncio.sleep(1)   # Scopus 礼貌间隔
    finally:
        await engine.close()

    out = {
        "created_at": "2026-08-28",
        "architecture": "query_family",
        "v2_0_1": {"depth": args.depth,
                   "note": "只改导出深度；rank=CSV 行序；RR(d) 曲线离线按 rank 阈值重放"},
        "budget_policy": "K_f=200（retained 曲线离线重放，本文件存 raw 全量）",
        "queries_executed": qid,
        "queries_ok": len(results),
        "queries_failed": len(errors),
        "errors": errors,
        "records": results,
    }
    os.makedirs(os.path.dirname(RUNS_PATH), exist_ok=True)
    with open(RUNS_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n✓ raw 全量已写: {RUNS_PATH}（{len(results)} queries, "
          f"{sum(len(v) for v in results.values())} records）")
    print("下一步：python tools/evaluate_query_diversity.py --depth（出 RR(d) 双曲线）")


if __name__ == "__main__":
    asyncio.run(main())
