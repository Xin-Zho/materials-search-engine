"""P4-2 v3（NODE = 知识树位置）的回归测试。

用户 2026-09-12 纠正：NODE 不该预测「哪些节点会变大」，而该预测「哪些节点处于
知识高潜力位置」。本文件守住这次改动的三条结构性质。
"""
from __future__ import annotations

import os
import sqlite3
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "tools"))

import discover_emergence_candidates as dsc  # noqa: E402


def _paper(uid, year, topic="Photopolymerization techniques and applications"):
    return (uid, year, topic)


def _rec(uid, concepts, relations=()):
    return {"paper_uid": uid, "status": "OK",
            "concepts": [{"name": n, "type": t} for n, t in concepts],
            "relations": [{"source": s, "relation": r, "target": t}
                          for s, r, t in relations]}


def _env(tmp_path, papers, records):
    db = os.path.join(str(tmp_path), "paper_meta.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE paper_meta (paper_uid TEXT, year INT, title TEXT, "
                "abstract TEXT, primary_topic TEXT, in_scope INT, "
                "exclusion_reason TEXT, split TEXT)")
    for uid, year, topic in papers:
        con.execute("INSERT INTO paper_meta VALUES (?,?,?,?,?,1,NULL,?)",
                    (uid, year, f"Title {uid}", f"Abstract {uid}", topic,
                     "TRAIN" if year <= 2020 else "EVAL"))
    con.commit()
    con.close()
    cp = os.path.join(str(tmp_path), "concepts.jsonl")
    import json
    with open(cp, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    ok, meta = dsc.load_inputs(db, cp)
    dsc.assert_no_leakage(meta, ok)
    nodes, typed, pairs, npapers, adj, diag = dsc.build_tree(ok, meta)
    dsc.EXCLUDED.clear()
    rows, by = dsc.build_node_candidates(nodes, typed, pairs, adj, diag,
                                        npapers, meta)
    return rows, by, nodes, adj


def _chain_env(tmp_path):
    """一条 4 节点的链 + 一个只与自己家主题相连的枢纽。"""
    recs = []
    papers = []
    for i in range(3):
        papers.append(_paper(f"p{i}", 2015))
        recs.append(_rec(f"p{i}", [("a", "challenge"), ("b", "material")]))
    for i in range(3):
        papers.append(_paper(f"q{i}", 2016, "Bone Tissue Engineering Materials"))
        recs.append(_rec(f"q{i}", [("b", "material"), ("c", "application")]))
    for i in range(3):
        papers.append(_paper(f"r{i}", 2016, "Bone Tissue Engineering Materials"))
        recs.append(_rec(f"r{i}", [("c", "application"), ("d", "direction")]))
    return _env(tmp_path, papers, recs)


# ══ 1. 层内排名：规模不参与打分，只决定在哪个组里竞争 ═══════════════════
def test_ranking_is_within_stratum_not_across(tmp_path):
    """**关键性质**：每个度数层**各有**自己的第 1 名。

    若排名是跨层的，度数大的节点会靠"邻居多、模块多、中介多"垄断前排 ——
    实测未分层时 `photopolymerization`(deg 93) 的参与系数 0.91、中介度 0.97
    都是全榜第一。分层之后，小节点只与同层节点比。
    """
    rows, by, _nodes, _adj = _chain_env(tmp_path)
    assert by, "没有产生任何候选"
    for stratum, group in by.items():
        ranks = sorted(r["rank_in_type"] for r in group)
        assert ranks == list(range(1, len(group) + 1)), \
            f"{stratum} 层内排名不连续：{ranks}"
    # 每个层都有自己的 #1
    assert all(any(r["rank_in_type"] == 1 for r in g) for g in by.values())


def test_growth_is_only_a_weak_term():
    """用户明确「w5 → 0」：增长不得作为主特征。"""
    assert dsc.NODE_WEIGHTS["growth_weak"] <= 0.05
    assert abs(sum(dsc.NODE_WEIGHTS.values()) - 1.0) < 1e-9
    assert (dsc.NODE_WEIGHTS["structural_novelty"] + dsc.NODE_WEIGHTS["bridge"]
            + dsc.NODE_WEIGHTS["problem"]
            + dsc.NODE_WEIGHTS["combination"]) >= 0.9


def test_node_records_carry_structural_features(tmp_path):
    """候选必须带四个结构特征与结构证据（供 LLM 分析层接地）。"""
    rows, _by, _n, _a = _chain_env(tmp_path)
    assert rows
    for r in rows:
        for f in ("new_edge_share", "bridge_raw", "problem_raw",
                  "combination_raw", "degree", "stratum"):
            assert f in r, f"缺结构特征 {f}"
        assert r["scores_basis"].startswith("knowledge_tree_position")
        assert "neighbors_by_module" in r
        assert all(k in r["scores"] for k in dsc.NODE_WEIGHTS)


# ══ 2. 表征手段不得进主榜（它靠天生高中介度混入）════════════════════════
def test_method_like_is_excluded_from_main_board(tmp_path):
    """表征/测量手段的中介度**天生很高**（每篇论文都要报告测量），
    按度数分层挡不住（实测 DMA deg=7 落在 mid 层、中介度仍 0.57）。
    故按可评审的词面规则标记，并排除出主榜。
    """
    assert dsc.is_method_like("dynamic mechanical analysis")
    assert dsc.is_method_like("scanning electron microscopy")
    assert dsc.is_method_like("fourier transform infrared spectroscopy")
    assert not dsc.is_method_like("ceramic stereolithography")
    assert not dsc.is_method_like("cationic ring-opening polymerization")

    rows, _by, _n, _a = _chain_env(tmp_path)
    names = {r["concept"] for r in dsc._node_prediction_rows(rows)}
    assert not any(dsc.is_method_like(n) for n in names)


def test_large_stratum_is_not_in_main_board(tmp_path):
    """主榜只取 small+mid —— 用户目标正是「目前还小、但可能改变结构的方向」。"""
    rows, _by, _n, _a = _chain_env(tmp_path)
    for r in dsc._node_prediction_rows(rows):
        assert r["stratum"] in dsc.NODE_PREDICTION_STRATA
        assert r["stratum"] != dsc.NODE_STRATUM_LARGE


# ══ 3. 可测量下限被记录（不是规模奖励）════════════════════════════════
def test_measurability_floor_is_recorded_not_silent(tmp_path):
    """degree<2 与 support<2 的节点被挡掉，但**计数必须写进产物** ——
    否则「候选变少」会被误读成「数据变干净」。"""
    _rows, _by, _n, _a = _chain_env(tmp_path)
    assert dsc.EXCLUDED, "下限挡掉的计数没有记录"
    assert "node_below_min_degree" in dsc.EXCLUDED


# ══ 4. 结构打分必须有阳性对照（方向校验）═══════════════════════════════
def test_structural_positive_control_exists_and_is_flagged(tmp_path):
    """P4-3 实测新口径候选在结构对齐判据上 NULL（lift 1.045, p=0.65）——
    这时必须能区分「特征方向错」与「数据太薄」。做法与词面尺子一致：
    拿已知新近打开连接的概念看它们排在哪。**必须标明是事后构造**。
    """
    assert len(dsc.STRUCTURAL_POSITIVE_CONTROL) >= 8
    rows, by, _n, _a = _chain_env(tmp_path)
    pc = dsc.structural_positive_control(rows, by)
    assert "事后构造" in pc["note"]
    assert pc["concepts_total"] == len(dsc.STRUCTURAL_POSITIVE_CONTROL)
    assert "median_score_percentile_in_stratum" in pc
