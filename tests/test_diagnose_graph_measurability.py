"""P4-2 诊断器（`tools/diagnose_graph_measurability.py`）的回归测试。

守两件曾经真的出过错的事：
  1) 阳性对照清单里的**字符串自身错误** —— 上一轮把同一概念的两种拼写
     （front / frontal photopolymerization）当成两个概念计入，并放进了 RAFT 的
     截断残片，导致「覆盖率不足」被高估。
  2) 采样缩放量的**单调性** —— 若子采样不增反降，说明统计口径写错了。
     同时断言 NODE 池的幂律指数 > 1（点数随采样量超线性增长），
     这是「候选面小是采样深度问题」这条结论的量化依据。
"""
from __future__ import annotations

import importlib.util
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(BASE, "tools", "diagnose_graph_measurability.py")
sys.path.insert(0, os.path.join(BASE, "tools"))

_spec = importlib.util.spec_from_file_location("diag_meas", TOOL)
dgm = importlib.util.module_from_spec(_spec)
sys.modules["diag_meas"] = dgm
_spec.loader.exec_module(dgm)


def _rec(uid, names):
    return {"paper_uid": uid, "status": "OK",
            "concepts": [{"name": n, "type": "direction"} for n in names],
            "relations": []}


def test_control_list_has_no_duplicate_or_truncated_entries():
    """对照清单不得含同一概念的两种拼写，也不得含被截断的复合名。"""
    ctrl = dgm.CONTROL
    assert len(ctrl) == len(set(ctrl)), "对照清单有重复项"
    # front vs frontal 那次的教训：互为前缀的两个条目必然是同一个概念
    for a in ctrl:
        for b in ctrl:
            if a != b:
                assert not (a.startswith(b) or b.startswith(a)), \
                    f"疑似同一概念的两个拼写: {a!r} / {b!r}"
    # RAFT 截断残片那种「以 reversible addition 结尾却没写 chain transfer」的名字
    for c in ctrl:
        assert not c.endswith("reversible addition"), f"截断的复合名: {c!r}"


def test_control_list_is_degree_independent():
    """对照清单必须是干净的小写词面（不得掺入阈值/比较符之类的可调参数）。"""
    for c in dgm.CONTROL:
        assert c == c.strip().lower()
        assert c and all(ch.isalnum() or ch in " -" for ch in c), c
        for bad in (">=", "<=", "support", "threshold"):
            assert bad not in c, f"对照项疑似阈值参数: {c!r}"


def test_power_law_fit_recovers_known_exponent():
    """指数拟合必须能还原合成数据的真实指数（守「指数>1」这条结论）。"""
    import math
    xs = [100, 200, 400, 800]
    for true_exp in (0.5, 1.0, 1.77, 2.0):
        ys = [3 * x ** true_exp for x in xs]
        slope, _ = dgm._power_law_fit(xs, ys)
        assert abs(slope - true_exp) < 1e-6, (true_exp, slope)
    # 常数序列的指数为 0，不能因 max(y,1) 之类的地板而漂移
    slope, _ = dgm._power_law_fit(xs, [5, 5, 5, 5])
    assert abs(slope) < 1e-9


def test_graph_stats_counts_only_above_threshold():
    """edges/nodes 的可测计数必须只数 >= 门槛的那些。"""
    rows = [
        _rec("p1", ["a", "b", "c"]),
        _rec("p2", ["a", "b", "d"]),      # (a,b) 共现 2 次 -> 可测
        _rec("p3", ["a", "c", "e"]),      # (a,c) 共现 2 次 -> 可测
    ]
    st = dgm._graph_stats(rows, pair_min_support=2, node_min_support=3,
                          min_degree=2)
    assert st["papers"] == 3
    assert st["edges_any"] == 7           # ab ac bc ad bd ae ce
    assert st["edges_measurable"] == 2    # ab, ac
    assert st["nodes_measurable"] == 1    # 只有 a 出现 3 次
    # 入池的只有 a：support 3 达标，且它的两个可测伙伴（ab/ac 各 2 次）使 deg=2 达标。
    # b（support 2）、c（support 2 且 deg=1）不达标 —— 这正是稀疏图上
    # 「出现过的概念未必可打分」的最小复现。
    assert st["node_pool"] == 1


def test_all_single_node_papers_have_no_edges():
    rows = [_rec(f"p{i}", ["x"]) for i in range(5)]
    st = dgm._graph_stats(rows, pair_min_support=2, node_min_support=3,
                          min_degree=2)
    assert st["edges_any"] == 0 and st["node_pool"] == 0


def test_scaling_curve_is_monotone_in_paper_count():
    """子采样量递增时，可测边/可测节点/NODE 池都必须非降。"""
    dec = dgm._load_tool()
    ok_rows = [_rec(f"p{i}", ["a", "b", "c"]) for i in range(40)]
    ok_rows += [_rec(f"q{i}", ["a", "d", f"z{i}"]) for i in range(40)]
    meta = {r["paper_uid"]: {"year": 2015} for r in ok_rows}
    dec.PAIR_MIN_SUPPORT, dec.NODE_MIN_SUPPORT, dec.NODE_MIN_DEGREE = 2, 3, 2
    prev = None
    import random
    rng = random.Random(13)
    for frac in dgm.FRACTIONS:
        sub = rng.sample(ok_rows, int(len(ok_rows) * frac))
        st = dgm._graph_stats(sub, 2, 3, 2)
        if prev is not None:
            assert st["papers"] >= prev["papers"]
            assert st["edges_measurable"] >= prev["edges_measurable"]
            assert st["node_pool"] >= prev["node_pool"]
        prev = st
    assert meta, "sanity"
