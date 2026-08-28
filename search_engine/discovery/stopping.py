"""Phase 2.1 停止条件（用户定 2026-08-26）。

**Phase 2.1 停止 ≠ 证明搜全。** discovery_saturation 只代表：

> 当前 discovery loop 继续跑的边际收益已经很低。

真正"搜全没"由 Phase 3 独立统计审计回答——报告措辞永远不写 literature complete。

工程停止（用户定）：
    连续 3 轮：
        new_validated = 0
        new_promoted  = 0
        new_route     = 0
        new_mechanism = 0
    → stop_reason = discovery_saturation
"""

from __future__ import annotations

from .round_state import DiscoveryRound
from .metrics import count_new_nodes_by_type

SATURATION_WINDOW = 3


def _round_is_barren(round_: DiscoveryRound) -> bool:
    """一轮是否"无产出"：无 VALIDATED、无 promotion 队列、无新 route/mechanism。"""
    validated = sum(1 for v in round_.verification_results.values()
                    if v == "VALIDATED")
    if validated > 0:
        return False
    if round_.promotions:
        return False
    ntypes = count_new_nodes_by_type(round_.new_nodes)
    return ntypes["new_routes"] == 0 and ntypes["new_mechanisms"] == 0


def should_stop(rounds: list[DiscoveryRound],
                window: int = SATURATION_WINDOW) -> tuple[bool, str | None]:
    """连续 window 轮无产出 → (True, 'discovery_saturation')。

    注意措辞：只代表边际收益低，**不代表搜全**（Phase 3 独立审计负责）。
    """
    if len(rounds) < window:
        return False, None
    tail = rounds[-window:]
    if all(_round_is_barren(r) for r in tail):
        return True, "discovery_saturation"
    return False, None


def stop_reason_label(reason: str | None) -> str:
    """停止原因的可读说明（用户定：绝不写 literature complete）。"""
    if reason == "discovery_saturation":
        return ("discovery_saturation——连续多轮无新 validated/promoted/新结构，"
                "边际收益低而停止；不代表文献已搜全（由 Phase 3 统计审计回答）")
    return reason or ""
