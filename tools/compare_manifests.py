"""Manifest comparison + lightweight query stability test.

判断两次运行的"搜索覆盖"是否稳定（route 覆盖一致性），
而不是 query 字符串是否字面一致。

用法:
    python compare_manifests.py data/manifests/run_1.json data/manifests/run_2.json ...
"""

import json
import sys
from search_engine.iterative_searcher import IterativeSearcher

ROUTE_KEYWORDS = IterativeSearcher.ROUTE_KEYWORDS


def route_coverage(manifest: dict) -> set[str]:
    """从 manifest 的 query 集，判断覆盖了哪些机制路线。"""
    queries = [q["query"].lower() for q in manifest.get("queries", [])]
    covered = set()
    for route, keywords in ROUTE_KEYWORDS.items():
        for q in queries:
            if any(kw.lower() in q for kw in keywords):
                covered.add(route)
                break
    return covered


def main():
    paths = sys.argv[1:]
    if len(paths) < 2:
        print("需要至少 2 个 manifest 文件")
        return

    manifests = [json.load(open(p, encoding="utf-8")) for p in paths]
    coverages = [route_coverage(m) for m in manifests]

    print("=== Manifest Query 覆盖对比 ===\n")
    for i, (m, cov) in enumerate(zip(manifests, coverages), 1):
        print(f"[{i}] {m.get('timestamp','?')} — {len(m.get('queries',[]))} 条 query")
        print(f"    覆盖 {len(cov)} 条 route: {sorted(cov) if cov else '无'}")

    # 一致性指标
    all_union = set().union(*coverages) if coverages else set()
    all_intersection = set.intersection(*coverages) if coverages else set()

    print("\n=== 稳定性结论 ===")
    print(f"所有运行都覆盖的 route ({len(all_intersection)}): {sorted(all_intersection) if all_intersection else '无'}")
    print(f"至少一次覆盖的 route 并集 ({len(all_union)}): {sorted(all_union)}")

    # 每条 route 的覆盖次数
    print("\n各 route 覆盖次数:")
    for route in sorted(all_union):
        count = sum(1 for cov in coverages if route in cov)
        bar = "█" * count + "░" * (len(coverages) - count)
        print(f"  {route}: {count}/{len(coverages)} {bar}")

    # 判定
    stable = len(all_intersection) >= 4  # 至少 4 条核心路线稳定覆盖
    print(f"\n覆盖稳定性: {len(all_intersection)}/{len(all_union)} 条 route 跨运行稳定")
    print(f"判定: {'稳定（可冻结 Phase 0）' if stable else '不稳定（需修 query generation）'}")


if __name__ == "__main__":
    main()
