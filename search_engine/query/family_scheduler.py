"""family_scheduler.py — v2.0 预算分配 + coverage merge（用户 2026-08-28 定稿）。

原则：
  1. 每 family 相同 retrieval budget K（第一版 K_f = K，不做动态预算）。
  2. coverage merge：每个 family 独立取 top K，然后**并集合并**——
     不允许全局 top N 截断（否则热门 family 挤死 Problem-only，回到 v1 老路）。
  3. 输出按 family 分组的结果集，保留 family 归属（供 stats/new_venues 分析）。
"""
from dataclasses import dataclass, field


@dataclass
class FamilyRunResult:
    family_id: str
    family_type: str
    budget: int
    retrieved: list[str] = field(default_factory=list)   # 该 family 实际取回论文 id
    total_hits: int = 0                                   # 导出条数合计（Scopus 页面 total 不可靠，仅参考）
    executed: bool = False
    errors: list[str] = field(default_factory=list)       # 失败的 query + 错误信息


def merge_family_results(runs: list[FamilyRunResult]) -> tuple[dict[str, int], list[str]]:
    """coverage merge：跨 family 并集，保留每篇的 family 归属。

    返回 (paper -> family_ids, union_ids)。
    """
    owner: dict[str, set[str]] = {}
    for r in runs:
        if not r.executed:
            continue
        for pid in r.retrieved:
            owner.setdefault(pid, set()).add(r.family_id)
    union = sorted(owner.keys())
    return {p: sorted(s) for p, s in owner.items()}, union


def anchor_concentration(queries_by_family: dict[str, list[str]]) -> dict:
    """Anchor Concentration：max_t #{queries containing anchor t} / #{all queries}。

    v1 基线：22/22 query 含 'bulk-fill composite' → AC ≈ 1。
    v2 目标：AC < 0.4（任何单一锚点最多出现在四成 query 中）。
    返回 {"max_anchor", "max_count", "total_queries", "ac"}。
    """
    from collections import Counter
    all_queries = []
    for qs in queries_by_family.values():
        all_queries.extend(qs)
    if not all_queries:
        return {"max_anchor": None, "max_count": 0, "total_queries": 0, "ac": 0.0}
    # anchor = query 里的双引号短语（词面锚点）
    import re
    anchors = Counter()
    for q in all_queries:
        for m in re.findall(r'"([^"]+)"', q):
            anchors[m.lower()] += 1
    max_anchor, max_count = anchors.most_common(1)[0] if anchors else (None, 0)
    return {"max_anchor": max_anchor, "max_count": max_count,
            "total_queries": len(all_queries),
            "ac": round(max_count / len(all_queries), 4) if all_queries else 0.0}


def family_coverage(registry_families: list[dict], executed_family_ids: set[str]) -> float:
    """Family coverage：实际执行的 family 比例。"""
    if not registry_families:
        return 0.0
    return round(len(executed_family_ids) / len(registry_families), 4)
