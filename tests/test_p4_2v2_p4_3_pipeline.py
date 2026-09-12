"""P4-2v2（知识树演化候选）与 P4-3（未来验证）的回归测试。

全部用**合成语料**，不依赖真库快照 —— 真库一变测试就失效的话，
这些守卫就没有长期价值。

两条主线：
  * 反泄漏与口径：越界必须硬失败；期规模差异必须归一化
  * 本轮实测踩过的坑（每一条都对应一个真实 bug）
"""
from __future__ import annotations

import ast
import json
import os
import random
import sqlite3
import sys

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "tools"))

import discover_emergence_candidates as dsc   # noqa: E402
import validate_emergence_p4_3 as val         # noqa: E402

DSC_PATH = os.path.join(BASE, "tools", "discover_emergence_candidates.py")
VAL_PATH = os.path.join(BASE, "tools", "validate_emergence_p4_3.py")


# ══ 合成环境 ═════════════════════════════════════════════════════════════
def _paper(uid, year, topic="Photopolymerization techniques and applications"):
    return (uid, year, topic)


def _rec(uid, concepts, relations=()):
    return {"paper_uid": uid, "status": "OK",
            "concepts": [{"name": n, "type": t} for n, t in concepts],
            "relations": [{"source": s, "relation": r, "target": tg}
                          for s, r, tg in relations]}


def mk_env(tmp_path, papers, records):
    db = os.path.join(str(tmp_path), "paper_meta.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE paper_meta (paper_uid TEXT, year INT, title TEXT, "
                "abstract TEXT, primary_topic TEXT, in_scope INT, "
                "exclusion_reason TEXT, split TEXT)")
    for uid, year, topic in papers:
        con.execute("INSERT INTO paper_meta VALUES (?,?,?,?,?,1,NULL,?)",
                    (uid, year, f"Title {uid}", f"Abstract of {uid} about " + uid,
                     topic, "TRAIN" if year <= 2020 else "EVAL"))
    con.commit()
    con.close()
    cpath = os.path.join(str(tmp_path), "concepts.jsonl")
    with open(cpath, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return db, cpath


def build(tmp_path, papers, records):
    db, cp = mk_env(tmp_path, papers, records)
    ok, meta = dsc.load_inputs(db, cp)
    guard = dsc.assert_no_leakage(meta, ok)
    nodes, typed, pairs, node_papers, adj, diag = dsc.build_tree(ok, meta)
    return dict(ok=ok, meta=meta, guard=guard, nodes=nodes, typed=typed,
                pairs=pairs, node_papers=node_papers, adj=adj, diag=diag)


# ══ 1. 反泄漏 ════════════════════════════════════════════════════════════
def test_future_paper_in_extraction_is_hard_failure(tmp_path):
    """year > 2020 的抽取结果必须 **SystemExit**，不是警告。

    候选发现一旦吃到预测窗口的数据，整个 P4-3 就失去意义 ——
    这种错误必须是"跑不起来"，不能是"打印一行字继续跑"。
    """
    papers = [_paper("p1", 2019), _paper("p2", 2023)]
    records = [_rec("p1", [("a", "direction")]), _rec("p2", [("a", "direction")])]
    db, cp = mk_env(tmp_path, papers, records)
    ok, meta = dsc.load_inputs(db, cp)
    with pytest.raises(SystemExit):
        dsc.assert_no_leakage(meta, ok)


def test_guard_reports_max_year(tmp_path):
    env = build(tmp_path, [_paper("p1", 2018), _paper("p2", 2020)],
                [_rec("p1", [("a", "direction")]), _rec("p2", [("a", "direction")])])
    g = env["guard"]
    assert g["max_year_seen"] == 2020
    assert g["assert_no_future_in_train"] is True


# ══ 2. 期规模归一化（采样偏斜不能被算成增长）══════════════════════════════
def test_growth_normalizes_uneven_period_sizes(tmp_path):
    """前后期**出现次数相同**、但后期论文更多 -> 份额其实在下降。

    朴素比值 2/2 = 1.0 会把它读成"持平"；按期份额应为显著下降。
    数据集是分层抽样、各期规模不等，不做归一化就会把采样偏斜当增长。
    """
    papers = [_paper("E1", 2013), _paper("E2", 2014), _paper("E3", 2015),
              _paper("E4", 2015)]
    papers += [_paper(f"L{i}", 2017 if i <= 2 else 2019) for i in range(1, 17)]
    records = [_rec("E1", [("a", "challenge"), ("b", "material")]),
               _rec("E2", [("a", "challenge"), ("b", "material")]),
               _rec("E3", [("a", "challenge")]),
               _rec("E4", [("a", "challenge")]),
               _rec("L1", [("a", "challenge"), ("b", "material")]),
               _rec("L2", [("a", "challenge"), ("b", "material")])]
    records += [_rec(f"L{i}", [("a", "challenge")]) for i in range(3, 17)]
    env = build(tmp_path, papers, records)
    assert env["diag"]["period_papers"] == {"early": 4, "late": 16}
    rows = dsc.build_pair_candidates(env["nodes"], env["typed"], env["pairs"],
                                     env["meta"], env["diag"],
                                     __import__("collections").Counter(), env["adj"])
    ab = [r for r in rows if {r["a"]["name"], r["b"]["name"]} == {"a", "b"}]
    assert ab, "配对未进入候选（检查语义门与支撑度门槛）"
    g = ab[0]["growth"]
    assert 0.2 < g < 0.5, f"期份额增长应为 ~0.29，实际 {g}（未归一化会得到 1.0）"


# ══ 3. 新边判定边界 ══════════════════════════════════════════════════════
def test_new_edge_boundary(tmp_path):
    papers = [_paper("p1", 2016), _paper("p1b", 2016), _paper("p1c", 2016),
              _paper("p2", 2017), _paper("p2b", 2017), _paper("p2c", 2017)]
    records = [_rec("p1", [("a", "challenge"), ("b", "material")]),
               _rec("p1b", [("a", "challenge"), ("b", "material")]),
               _rec("p1c", [("a", "challenge"), ("b", "material")]),
               _rec("p2", [("c", "challenge"), ("d", "material")]),
               _rec("p2b", [("c", "challenge"), ("d", "material")]),
               _rec("p2c", [("c", "challenge"), ("d", "material")])]
    env = build(tmp_path, papers, records)
    rows = dsc.build_pair_candidates(env["nodes"], env["typed"], env["pairs"],
                                     env["meta"], env["diag"],
                                     __import__("collections").Counter(), env["adj"])
    by = {frozenset((r["a"]["name"], r["b"]["name"])): r for r in rows}
    assert by[frozenset(("a", "b"))]["new_edge"] is False      # 2016 < 2017
    assert by[frozenset(("c", "d"))]["new_edge"] is True       # 2017 >= 2017


# ══ 4. 缺口候选必须是**未共现**的 ═══════════════════════════════════════
def test_gap_candidates_are_never_cooccurring(tmp_path):
    """缺口 = 「应该连但还没连」。若它其实已共现，那它根本不是缺口。"""
    papers = [_paper("p1", 2015), _paper("p2", 2015), _paper("p3", 2015)]
    records = [_rec("p1", [("a", "challenge"), ("m", "material"),
                           ("z", "application")]),
               _rec("p2", [("b", "challenge"), ("m", "material"),
                           ("z", "application")]),
               _rec("p3", [("m", "material"), ("z", "application")])]
    env = build(tmp_path, papers, records)
    rows = dsc.build_gap_candidates(env["nodes"], env["pairs"], env["meta"],
                                    env["node_papers"], env["adj"])
    for r in rows:
        key = (r["a"]["name"], r["b"]["name"])
        assert key not in env["pairs"] and (key[1], key[0]) not in env["pairs"]
        assert r["support"] == 0 and r["first_seen"] is None
        assert r["n_common_neighbors"] >= dsc.GAP_MIN_COMMON_NEIGHBORS


# ══ 5. 跨域判据必须可达（首版曾不可达）═══════════════════════════════════
def test_cross_domain_is_reachable(tmp_path):
    """**真实 bug 回归**：首版用「主题集合是否不相交（Jaccard==0）」判跨域 ——
    但共现对必然共享那篇论文的主题，Jaccard 恒 > 0，该判据**永不成立**
    （实测跨域候选为 0）。改为「两端端点的**主场主题**是否不同」。
    """
    papers = [_paper(f"X{i}", 2013, "Photopolymerization techniques and applications")
              for i in range(5)]
    papers += [_paper(f"Y{i}", 2014, "Bone Tissue Engineering Materials")
               for i in range(5)]
    papers += [_paper("XY1", 2016, "Dental materials and restorations"),
               _paper("XY2", 2017, "Dental materials and restorations")]
    records = [_rec(f"X{i}", [("x", "challenge")]) for i in range(5)]
    records += [_rec(f"Y{i}", [("y", "application")]) for i in range(5)]
    records += [_rec("XY1", [("x", "challenge"), ("y", "application")]),
                _rec("XY2", [("x", "challenge"), ("y", "application")])]
    env = build(tmp_path, papers, records)
    rows = dsc.build_pair_candidates(env["nodes"], env["typed"], env["pairs"],
                                     env["meta"], env["diag"],
                                     __import__("collections").Counter(), env["adj"])
    xy = [r for r in rows if {r["a"]["name"], r["b"]["name"]} == {"x", "y"}]
    assert xy and xy[0]["cross_domain"] is True
    assert xy[0]["home_topic_a"] != xy[0]["home_topic_b"]
    assert 0.0 < xy[0]["topic_contrast"] <= 1.0


# ══ 6. ΔPMI 区分「特异连接」与「枢纽共现」═══════════════════════════════
def test_assoc_growth_rewards_specific_over_ubiquitous(tmp_path):
    """枢纽概念（与谁都共现）的 PMI 低；特异性连接强化时 ΔPMI 大。

    这是替换「连通度」特征的依据：连通度**奖励枢纽**，实测让
    `photoinitiator + 组织工程` 这类配对占据榜首。
    """
    papers = [_paper(f"G{i}", 2013) for i in range(20)]
    papers += [_paper(f"G{i}b", 2018) for i in range(20)]
    papers += [_paper(f"S{i}", 2019) for i in range(4)]
    records = [_rec(f"G{i}", [("g1", "material"), ("g2", "material")],
                    [("g1", "combines_with", "g2")]) for i in range(20)]
    records += [_rec(f"G{i}b", [("g1", "material"), ("g2", "material")],
                     [("g1", "combines_with", "g2")]) for i in range(20)]
    records += [_rec(f"S{i}", [("s1", "challenge"), ("s2", "material")])
                for i in range(4)]
    env = build(tmp_path, papers, records)
    rows = dsc.build_pair_candidates(env["nodes"], env["typed"], env["pairs"],
                                     env["meta"], env["diag"],
                                     __import__("collections").Counter(), env["adj"])
    by = {frozenset((r["a"]["name"], r["b"]["name"])): r for r in rows}
    spec = by[frozenset(("s1", "s2"))]["assoc_growth"]
    ubiq = by[frozenset(("g1", "g2"))]["assoc_growth"]
    # 新兴配对：早期 conf≈0 -> Δconf 明显为正；长期平凡配对：Δconf≈0。
    # 用 ΔPMI 时这里会得到 **-2.92 vs +0.25**（特征方向反了），见 _assoc_growth 注释。
    assert spec > 0.2, f"新兴配对 Δconf={spec} 应为明显正值"
    assert abs(ubiq) < 0.1, f"长期平凡配对 Δconf={ubiq} 应接近 0"
    assert spec > ubiq


def test_weights_sum_to_one():
    assert abs(sum(dsc.WEIGHTS.values()) - 1.0) < 1e-9


# ══ 7. 源码级守卫 ═══════════════════════════════════════════════════════
def test_no_citation_metric_in_discoverer_source():
    """反泄漏第 2 条：候选发现不得读任何引用量。

    用 AST 检查真实代码，**不**用文本搜索 —— 那个字符串必然出现在
    「解释它为什么被禁用」的注释里，文本搜索会把说明当成违规
    （P0-B1b 的静态守卫上踩过同一个坑）。
    """
    tree = ast.parse(open(DSC_PATH, encoding="utf-8").read())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    strs = {s.value for s in ast.walk(tree) if isinstance(s, ast.Constant)
            and isinstance(s.value, str)}
    for banned in ("citation_count", "citations_asof_cutoff", "fwci",
                   "cited_by_count"):
        assert banned not in names, f"代码里引用了 {banned}"
        assert banned not in strs, f"字符串常量里出现 {banned}"


# ══ 8. NODE 判据不能用 PMI（单概念 PMI 恒为 0）═══════════════════════════
def test_node_hit_uses_rate_ratio_not_pmi():
    """**度量假象回归**：单概念的 PMI = log2(p/p) = 0 恒成立，
    首版误用 PMI 判据时节点命中率恒为 0 —— 那是尺子坏了，不是实验结果。
    """
    m = {"measurable": True, "eval_lex": 10, "rate_ratio": 1.5,
         "lex_dpmi": 0.0}
    assert val.is_hit("NODE", m) is True
    m2 = dict(m, rate_ratio=0.9)
    assert val.is_hit("NODE", m2) is False
    # 对 PAIR，ΔPMI 才是判据
    assert val.is_hit("PAIR_PRESENT", dict(m, lex_dpmi=0.5)) is True
    assert val.is_hit("PAIR_PRESENT", dict(m, lex_dpmi=-0.5)) is False


def test_two_proportion_z_matches_hand_calculation():
    """n=424/372、44.6% vs 31.2% -> z≈3.9、p<1e-4（本轮 PAIR 的实际数值）。"""
    r = val.two_proportion_z(189, 424, 116, 372)
    assert 3.8 < r["z"] < 4.1
    assert r["p_two_sided"] < 1e-3
    assert val.two_proportion_z(5, 20, 5, 20)["z"] == 0.0


def test_verdict_translation():
    assert "SIGNAL" in val._verdict(1.4, {"p_two_sided": 0.001})
    assert "ANTI" in val._verdict(0.7, {"p_two_sided": 0.001})
    assert "NULL" in val._verdict(1.4, {"p_two_sided": 0.4})


# ══ 9. 词面匹配能力 ═════════════════════════════════════════════════════
def test_lex_matching_conjunction_and_morphology(tmp_path):
    rows = [("u1", 2015, "Study of polymerization shrinkage in "
                         "photopolymerization systems"),
            ("u2", 2015, "Ceramic sintering shrinkage and densification"),
            ("u3", 2015, "Photopolymerization of acrylate monomers")]
    ix = val.LexIndex(rows, (2011, 2020), (2021, 2025))
    # 端点**内部**也是合取：只有 u1 同时出现两个词
    assert ix.count(["polymerization shrinkage"], (2011, 2020)) == 1
    # 复合词包含：polymerization ⊂ photopolymerization
    assert ix.count(["polymerization"], (2011, 2020)) == 2
    # 同形异义会同时命中（陶瓷烧结），这正是需要随机基线的原因
    assert ix.count(["shrinkage"], (2011, 2020)) == 2


def test_lex_index_period_partition(tmp_path):
    rows = [("a", 2015, "x"), ("b", 2022, "y")]
    ix = val.LexIndex(rows, (2011, 2020), (2021, 2025))
    assert ix.n_docs((2011, 2020)) == 1 and ix.n_docs((2021, 2025)) == 1


# ══ 10. 基线可复现与阳性对照 ════════════════════════════════════════════
def test_baseline_present_is_seed_reproducible():
    cands = [{"cand_id": f"c{i}", "a": {"name": f"a{i}"}, "b": {"name": f"b{i}"},
              "support": 2 + i % 3} for i in range(10)]
    pool = {}
    for i in range(300):
        pool[(f"x{i}", f"y{i}")] = 2 + i % 3
    r1 = val.baseline_present(cands, pool, random.Random(13))
    r2 = val.baseline_present(cands, pool, random.Random(13))
    r3 = val.baseline_present(cands, pool, random.Random(7))
    assert r1 == r2, "同 seed 必须完全可复现"
    assert r1 != r3, "换 seed 应换样本"
    for r, c in zip(r1, cands):
        assert r["support"] == c["support"], "基线必须与候选同 support 分层"


def test_positive_control_is_flagged_post_hoc():
    """阳性对照是**尺子灵敏度检验**，必须在报告里标明它不构成方法有效性的证据。"""
    assert len(val.POSITIVE_CONTROL) >= 8
    assert "additive manufacturing" in val.POSITIVE_CONTROL


def test_limitations_document_the_gap_blind_spot():
    """Tier 1 无法检验 GAP 的「缺口被填补」—— 这条必须写在报告里，
    否则读报告的人会把 GAP 的零结果误读成「缺口候选无效」。"""
    src = open(VAL_PATH, encoding="utf-8").read()
    assert "无法检验 PAIR_GAP" in src
    assert "993" in src
