"""Phase 2.1 Round 指标（用户定 2026-08-26：看 discovery yield，不主要看 coverage）。

Yield 指标：
    Y_c     = NewCandidates / Queries           候选产出率
    Y_v     = Validated / Verified              验证通过率
    Y_p     = Promoted / Verified               升格率
    Y_n     = NewOntologyNodes / Queries        新节点产出率
    Y_paper = NewRelevantPapers / Queries       新论文产出率

分类型计数（用户定：不会"发现 20 个 effect"就误以为 ontology 扩展很强）：
    new_routes / new_mechanisms / new_process_strategies /
    new_formulation_strategies / new_effects

round 报告：按用户给的格式输出（== 分隔 + 明细 + yield）。
"""

from __future__ import annotations

from .round_state import DiscoveryRound

TYPE_KEY_MAP = {
    "ROUTE": "new_routes",
    "MECHANISM": "new_mechanisms",
    "PROCESS_STRATEGY": "new_process_strategies",
    "FORMULATION_STRATEGY": "new_formulation_strategies",
    "EFFECT": "new_effects",
}


def safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0


def compute_round_metrics(round_: DiscoveryRound) -> dict:
    """一轮的 yield 指标（纯函数，从 DiscoveryRound 计算）。"""
    verified = len(round_.verification_results)
    validated = sum(1 for v in round_.verification_results.values()
                    if v == "VALIDATED")
    queries = max(round_.queries_used, 1)
    return {
        "candidate_yield": safe_div(round_.new_candidates, queries),
        "validation_yield": safe_div(validated, verified),
        "promotion_yield": safe_div(len(round_.promotions), verified),
        "novel_node_yield": safe_div(len(round_.new_nodes), queries),
        "paper_yield": safe_div(round_.new_unique_papers, queries),
    }


def count_new_nodes_by_type(new_nodes: list[str]) -> dict:
    """分类型计数（用户定：防 effect 刷屏误判 ontology 扩展）。

    new_nodes 元素格式：'<node_name>|<TYPE>'（controller 写入时带类型）。
    无法解析类型的按 'other' 计。
    """
    counts = {v: 0 for v in TYPE_KEY_MAP.values()}
    counts["other"] = 0
    for node in new_nodes or []:
        if "|" in node:
            _name, t = node.rsplit("|", 1)
            key = TYPE_KEY_MAP.get(t, "other")
        else:
            key = "other"
        counts[key] += 1
    return counts


def format_round_report(round_: DiscoveryRound, metrics: dict) -> str:
    """按用户给的报告格式输出。"""
    lines = [
        "=" * 48,
        f"Phase 2.1 Round {round_.round_id}",
        "=" * 48,
        "",
        f"Candidates scanned:       {round_.candidates_scanned}",
        f"New candidates:           {round_.new_candidates}",
        "",
        f"Selected:                 {len(round_.selected_candidates)}",
        "",
        "Verification:",
    ]
    counts = {"VALIDATED": 0, "NEED_MORE_EVIDENCE": 0, "ADJACENT": 0,
              "SEARCH_INCONCLUSIVE": 0, "REJECTED": 0, "SEARCH_INCONCLUSIVE_FROZEN": 0}
    for v in round_.verification_results.values():
        counts[v] = counts.get(v, 0) + 1
    for k in ("VALIDATED", "NEED_MORE_EVIDENCE", "ADJACENT",
              "SEARCH_INCONCLUSIVE", "REJECTED"):
        lines.append(f"  {k:<20}{counts.get(k, 0)}")
    ntypes = count_new_nodes_by_type(round_.new_nodes)
    lines += [
        "",
        "Promoted:",
        f"  new route                 {ntypes['new_routes']}",
        f"  new mechanism             {ntypes['new_mechanisms']}",
        f"  new process strategy      {ntypes['new_process_strategies']}",
        f"  new formulation strategy  {ntypes['new_formulation_strategies']}",
        f"  new effect                {ntypes['new_effects']}",
        "",
        f"New relevant papers:       {round_.new_unique_papers}",
        f"New ontology relations:    {len(round_.new_relations)}",
        "",
        f"Queries:                   {round_.queries_used}",
        f"API cost:                  {round_.api_cost:.2f}",
        "",
        f"Novel node/query:          {metrics.get('novel_node_yield', 0):.3f}",
    ]
    if round_.stop_reason:
        lines.append(f"Stop reason:              {round_.stop_reason}")
    return "\n".join(lines)
