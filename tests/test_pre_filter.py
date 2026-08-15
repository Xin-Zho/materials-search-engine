"""pre_filter 批次索引回归测试。

防止"全局 idx 当局部索引"的 bug 再次出现。
覆盖 batch_size=15 的边界：N = 14, 15, 16, 29, 30, 31, 47。
"""

import json
import re
import pytest
from search_engine.relevance import RelevanceFilter
from search_engine.models import Paper


class FakeBackend:
    """从 prompt 中解析全局 index，按 decision_map 返回决策。"""

    def __init__(self, decision_map=None):
        # decision_map: {global_index: "RELEVANT"|"UNCERTAIN"|"IRRELEVANT"}
        self.decision_map = decision_map or {}

    async def chat(self, system_prompt, user_message, **kwargs):
        # 提取 prompt 里所有 [N] 的全局 index
        indices = [int(m) for m in re.findall(r"\[(\d+)\]", user_message)]
        result = [
            {"index": i, "decision": self.decision_map.get(i, "UNCERTAIN")}
            for i in indices
        ]
        return json.dumps(result)


def _make_papers(n: int) -> list[Paper]:
    return [Paper(paper_id=str(i), title=f"paper {i}") for i in range(n)]


@pytest.mark.parametrize("n", [14, 15, 16, 29, 30, 31, 47])
def test_pre_filter_no_batch_loss(n):
    """pre_filter 不因批次切分而丢论文（全部 UNCERTAIN 时保留全部）。"""
    papers = _make_papers(n)
    rf = RelevanceFilter(FakeBackend())  # 全部 UNCERTAIN

    import asyncio
    kept = asyncio.run(rf.pre_filter(papers, "test question"))

    kept_ids = {p.paper_id for p in kept}
    expected_ids = {p.paper_id for p in papers}
    assert kept_ids == expected_ids, \
        f"pre_filter 丢了论文: N={n}, 保留 {len(kept)}/{n}, 丢失 {expected_ids - kept_ids}"


def test_pre_filter_drops_only_irrelevant():
    """只删 IRRELEVANT，RELEVANT 和 UNCERTAIN 都保留（含跨批次）。"""
    n = 47
    papers = _make_papers(n)
    # 第 0、16、31、46 篇（跨 4 批）判 IRRELEVANT
    decision_map = {0: "IRRELEVANT", 16: "IRRELEVANT", 31: "IRRELEVANT", 46: "IRRELEVANT"}
    rf = RelevanceFilter(FakeBackend(decision_map))

    import asyncio
    kept = asyncio.run(rf.pre_filter(papers, "test question"))

    kept_ids = {p.paper_id for p in kept}
    for dropped in ["0", "16", "31", "46"]:
        assert dropped not in kept_ids, f"第 {dropped} 篇 IRRELEVANT 未被删除"
    assert len(kept) == n - 4, f"应保留 {n-4} 篇，实际 {len(kept)}"


def test_parse_decisions_returns_global_indices():
    """_parse_decisions 返回的是 LLM 的全局 index，调用方需自己减 batch_start。"""
    resp = json.dumps([
        {"index": 15, "decision": "UNCERTAIN"},
        {"index": 16, "decision": "IRRELEVANT"},
    ])
    decisions = RelevanceFilter._parse_decisions(resp, offset=15)
    assert decisions == [(15, "UNCERTAIN"), (16, "IRRELEVANT")]
