"""tools/probe_scopus_sampling.py — Scopus 概率抽样可行性实验（用户定 2026-08-27）。

新版 Phase 3 把 Scopus 当作外部抽样框，但工程前提是：能拿到每层 N_h、
能真正随机抽样。本脚本用真实 Scopus 会话回答 4 个关键未知：

  E1  total_count 提取是否可靠（页面正则 vs 实际导出条数）
  E2  export offset 参数是否生效（offset=0 与 offset=500 导出的 EID 是否不重叠；
      重叠 = offset 被忽略 → 随机 offset 抽样不可行）
  E3  itemCount 上限（2000/5000 是否被接受，实际返回多少条）
  E4  排序稳定性（同 query 两次导出前 100，EID 顺序是否一致——概率抽样
      要求排序可复现，否则随机 offset 会引入偏差）
  E5  EID 是否可导出（Scopus 稳定主键，hash-based sampling 的基石）

需要已登录的 Scopus 会话（data/scopus_profile/）。运行：
  python tools/probe_scopus_sampling.py [--query "..."] [--limit 200]
"""
import argparse
import asyncio
import csv
import io
import sys

sys.path.insert(0, ".")
from search_engine.engine import ScopusSearchEngine

FIELDS = ["titles", "year", "doi", "abstract", "eid"]


def parse_eids(csv_text: str) -> list[str]:
    if not csv_text.strip():
        return []
    reader = csv.DictReader(io.StringIO(csv_text))
    eids = []
    for row in reader:
        eid = (row.get("EID") or "").strip()
        if eid:
            eids.append(eid)
    return eids


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default='"photopolymerization" AND "shrinkage"')
    ap.add_argument("--limit", type=int, default=200)
    args = ap.parse_args()
    Q = args.query

    engine = ScopusSearchEngine()
    await engine.start()
    try:
        # ── E1: total_count ──
        print("=" * 70)
        print(f"E1  total_count 可靠性  query={Q}")
        print("=" * 70)
        r = await engine.search(Q, limit=20)
        print(f"  search() total_count = {r.total_count}  (页面文本正则提取)")
        csv_small = await engine._export_via_api(Q, args.limit, fields=FIELDS)
        n_csv = len(parse_eids(csv_small))
        print(f"  导出 {args.limit} 条实际解析 EID = {n_csv}  "
              f"({'OK' if n_csv == args.limit else '注意：实际条数 ≠ 请求条数'})")

        # ── E2: offset 生效性 ──
        print()
        print("=" * 70)
        print("E2  offset 是否生效（offset=0 vs offset=500 导出的 EID 重叠度）")
        print("=" * 70)
        csv0 = await engine._export_via_api(Q, args.limit, fields=FIELDS, offset=0)
        csv5 = await engine._export_via_api(Q, args.limit, fields=FIELDS, offset=500)
        e0, e5 = parse_eids(csv0), parse_eids(csv5)
        overlap = set(e0) & set(e5)
        print(f"  offset=0   → {len(e0)} EID")
        print(f"  offset=500 → {len(e5)} EID")
        print(f"  重叠 = {len(overlap)}  "
              f"→ {'offset 生效 ✅（随机 offset 抽样可行）' if not overlap and e5 else 'offset 被忽略 ⚠️（深 offset 不可行）'}")

        # ── E3: itemCount 上限 ──
        print()
        print("=" * 70)
        print("E3  itemCount 上限（尝试 2000）")
        print("=" * 70)
        csv2k = await engine._export_via_api(Q, 2000, fields=FIELDS, offset=0)
        n2k = len(parse_eids(csv2k))
        print(f"  itemCount=2000 实际返回 {n2k} 条  "
              f"→ {'OK ✅（2000 可行）' if n2k > 0 else '失败/受限'}")

        # ── E4: 排序稳定性 ──
        print()
        print("=" * 70)
        print("E4  排序稳定性（同 query 两次 offset=0 导出前 100）")
        print("=" * 70)
        csv0b = await engine._export_via_api(Q, 100, fields=FIELDS, offset=0)
        e0b = parse_eids(csv0b)
        same_order = e0 == e0b[:len(e0)]
        n_same = sum(1 for a, b in zip(e0[:100], e0b) if a == b)
        print(f"  两次导出前 100 顺序一致数 = {n_same}/100  "
              f"→ {'排序稳定 ✅' if n_same == 100 else '排序不稳定 ⚠️（需 hash 抽样，不能依赖 offset 位置）'}")

        # ── E5: EID 列存在性 ──
        print()
        print("=" * 70)
        print("E5  EID 可导出性（hash-based sampling 基石）")
        print("=" * 70)
        header = csv_small.splitlines()[0] if csv_small else ""
        has_eid = "EID" in header
        print(f"  CSV 表头: {header[:200]}")
        print(f"  含 EID 列 = {has_eid}  "
              f"→ {'可做 EID hash 抽样 ✅' if has_eid else 'EID 字段组名不对，需换 identifiers 重试 ⚠️'}")

        print()
        print("=" * 70)
        print("结论速查：E2 ✅+E4 ✅ → 随机 offset 抽样可行 | E2 ❌/E4 ❌ → 必须 EID hash")
        print("E5 ✅ → hash-based sampling 可行（sha256(EID) 取最小 n 个，完全可复现）")
        print("=" * 70)
    finally:
        await engine.close()


if __name__ == "__main__":
    asyncio.run(main())
