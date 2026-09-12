#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""树内评估（in-tree evaluation）：**测试集必须取自知识树之内**。

用户 2026-09-12 的评估修正：

> 树太浅不是问题，评估要改。评估的有潜力方向要在树之内 —— 先了解树涵盖了哪些
> 方向，找到树中有潜力的方向作为测试集。

为什么这条修正是对的：上一轮的阳性对照清单里 7/12 个概念根本不在图内
（`machine learning`、`two-photon polymerization`、`vat photopolymerization` …）。
拿"树外"的对象去判"树内"的排序器，等于让一次评估同时承担两个职责
（覆盖够不够 + 算法对不对），而**两者混在一起会把"不可测"误判成"特征错"**。

── 但有一条边界不能越 ─────────────────────────────────────────────
候选宇宙限制在树内 = 对；**"有没有潜力"这个标签不能也从树里推出来**，否则循环。
标签必须来自**树外**：未来窗口（EVAL 2021-2025）的真实文献事实。
即：

    候选宇宙 / 特征  <-  树（<=2020）
    标签             <-  未来文献（2021-2025）      # 唯一不与特征同源的东西

本工具不产生候选、不覆盖任何冻结产物。它做四件事：

  1. 树覆盖盘点：树里到底有多少个"够格叫方向"的概念（按支撑度分档）
  2. 树内测试集：宇宙 = 树内 support>=k 的节点；标签 = EVAL 期份额上升与否
  3. 排序质量：打分在**树内全域**的 AUC（Mann-Whitney）
     —— 这个口径不受"候选池只有 243 个"影响，是算法的真实分辨力
  4. 逐特征 AUC：哪个特征真在测"潜力"、哪个在反向

── 为什么用 AUC 而不是"候选命中率" ────────────────────────────────
候选命中率（lift）把两个问题绑在一起：候选池多大、池内排序对不对。
AUC 只看排序，因此可以先回答"算法有没有分辨力"，再单独回答"候选池该多大"。

用法：
    .venv/Scripts/python.exe tools/evaluate_in_tree.py
    .venv/Scripts/python.exe tools/evaluate_in_tree.py --k 2 --json out.json
"""

from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import math
import os
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")

# 标签窗口变体。默认用**零重叠**的那个：特征的 late 窗口(2016-2020) 与
# 标签的分母窗口必须错开，否则"增速预测增速"会共享同一批计数。
LABEL_WINDOWS = (
    ("宽松（train 2011-2020 vs eval 2021-2025）", (2011, 2020), (2021, 2025)),
    ("严格零重叠（late 2016-2020 vs eval 2021-2025）", (2016, 2020), (2021, 2025)),
    ("严格更远（late 2016-2020 vs eval 2023-2025）", (2016, 2020), (2023, 2025)),
)
SUPPORT_LADDER = (1, 2, 3, 5, 10, 20)

# ── 内部门槛：纯 TRAIN 三分窗口 ─────────────────────────────────────────
# 为什么必须有它：用 EVAL 上的 AUC 去**挑特征/定权重**，本身就是在测试集上调参。
# 内部门槛全程只用 <=2020 的数据，因此可以反复跑；只有在它上面先站住的特征，
# 才允许拿去 EVAL 做一次性确认（确认之后 EVAL 对该特征就不再洁净）。
#
# 窗口切法：特征取 A->B，标签取 B->C。三段互不重叠，且都在 cutoff 之前。
INTERNAL_WINDOWS = (("A", (2011, 2014)), ("B", (2015, 2017)), ("C", (2018, 2020)))
GATE_AUC = 0.55          # 内部门槛：AUC 必须 > 此值且 p < 0.05 才算"有分辨力"
GATE_P = 0.05

# ── ⚠️ 无共享项窗口（修掉一类致命的机械伪影）────────────────────────────
# 旧设计：特征 = rate_B / rate_A，标签 = (rate_C > rate_B) —— **两者都含 rate_B**。
# 共享项使它们机械反相关：概念若集中在 B，则特征大、而标签要求 rate_C > rate_B 更难。
# 极端演示：用 `r(2016-17)/r(2015)` 对 `r(2018) > r(2016-17)`，AUC = **0.014**（p=0）
# —— 这不是"均值回归"，是恒等式。旧设计下所有"反向"结论都不可信。
#
# 修法：特征只用 A/B，标签只用 C/D，**窗口两两不相交**：
#     feature = f(A, B)      label = rate_D > rate_C
# 代价是 4 个窗口各摊薄 1/4，功效更低 —— 所以功效诊断更要紧。
DISJOINT_WINDOWS = (("A", (2011, 2013)), ("B", (2014, 2016)),
                    ("C", (2017, 2018)), ("D", (2019, 2020)))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(TOOLS, filename))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def auc_against_label(score_of, names, label_of):
    """AUC = P(score(正例) > score(负例))，附 Mann-Whitney 正态近似 p。

    样本量小的时候正态近似偏乐观 —— 本工具报 n_pos/n_neg，
    调用方必须自己看这两个数，不要只看 p。
    """
    pos = [score_of(n) for n in names if label_of(n)]
    neg = [score_of(n) for n in names if not label_of(n)]
    if not pos or not neg:
        return {"auc": None, "p": None, "n_pos": len(pos), "n_neg": len(neg),
                "median_pct": None}
    win = sum(1.0 if a > b else (0.5 if a == b else 0.0) for a in pos for b in neg)
    n1, n0 = len(pos), len(neg)
    auc = win / (n1 * n0)
    sd = math.sqrt(n1 * n0 * (n1 + n0 + 1) / 12.0)
    z = (win - n1 * n0 / 2.0) / sd if sd else 0.0
    p = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))
    allv = pos + neg
    med = statistics.median(
        [sum(1 for x in allv if x <= pv) / len(allv) for pv in pos])
    return {"auc": round(auc, 3), "p": round(p, 4), "z": round(z, 2),
            "n_pos": n1, "n_neg": n0, "median_pct": round(med, 3)}


def internal_lexical_sweep(d, v, nodes):
    """纯 TRAIN 三分窗口的特征穷举（不触碰任何 2021+ 数据）。

    特征窗口 = A(2011-2014) -> B(2015-2017)
    标签      = C(2018-2020) 份额 > B 份额
    全部用同一把词面尺子（`validate_emergence_p4_3.LexIndex`），因此在 TRAIN 内
    是自洽的：特征与标签既同测量体系、又同期窗口错开。

    这一步的用途是**门槛**，不是结论：只有在它上面 AUC > GATE_AUC 的特征，
    才允许拿去 EVAL 做一次性确认。
    """
    rows = v.load_text_rows(d.DB_PATH)
    idx = v.LexIndex(rows, INTERNAL_WINDOWS[0][1], INTERNAL_WINDOWS[1][1])
    wset = {k: {i for i, (_u, y, _t) in enumerate(idx.docs)
                if y is not None and lo <= y <= hi}
            for k, (lo, hi) in INTERNAL_WINDOWS}
    ndoc = {k: len(s) for k, s in wset.items()}

    # 论文 -> 该窗口内命中的概念（用于共现型特征）
    belong = collections.defaultdict(list)
    doc = {}
    for nm, nd_ in nodes.items():
        if nd_["support"] < 3:
            continue
        s = idx.docset(nm)
        if s:
            doc[nm] = s
            for i in s:
                for k, ws in wset.items():
                    if i in ws:
                        belong[(k, i)].append(nm)
    partner = {}
    for k, _ in INTERNAL_WINDOWS:
        pa = collections.defaultdict(set)
        for (kk, _i), cs in belong.items():
            if kk != k:
                continue
            for a in cs:
                pa[a].update(b for b in cs if b != a)
        partner[k] = pa

    def rate(c, k):
        return (c + 0.5) / (ndoc[k] + 1)

    recs = []
    for nm in doc:
        c = {k: len(doc[nm] & wset[k]) for k in ndoc}
        if not sum(c.values()):
            continue
        pa, pb = partner["A"].get(nm, set()), partner["B"].get(nm, set())
        years = sorted({idx.docs[i][1] for i in doc[nm]
                        if idx.docs[i][1] and 2011 <= idx.docs[i][1] <= 2020})
        recs.append({
            "concept": nm,
            "scale_B": rate(c["B"], "B"),
            "growth_AB": rate(c["B"], "B") / rate(c["A"], "A"),
            "accel_AB": rate(c["B"], "B") - rate(c["A"], "A"),
            "new_entrant": 1.0 if c["A"] == 0 else 0.0,
            "recency": -float(years[0]) if years else 0.0,
            "partner_growth": len(pb) / max(1, len(pa)),
            "partner_novelty": len(pb - pa) / max(1, len(pb)),
            "partner_novelty_abs": float(len(pb - pa)),
            "breadth_B": float(len(pb)),
            "label": rate(c["C"], "C") > rate(c["B"], "B"),
        })

    feats = ["scale_B", "growth_AB", "accel_AB", "new_entrant", "recency",
             "partner_growth", "partner_novelty", "partner_novelty_abs",
             "breadth_B"]
    table = {}
    for f in feats:
        table[f] = auc_against_label(
            lambda r, f=f: r[f], recs, lambda r: r["label"])
    passed = [f for f, m in table.items()
              if m["auc"] is not None and m["auc"] > GATE_AUC and m["p"] < GATE_P]
    return {"windows": [[k, list(w)] for k, w in INTERNAL_WINDOWS],
            "docs_per_window": ndoc,
            "n_evaluated": len(recs),
            "n_pos": sum(1 for r in recs if r["label"]),
            "gate_auc": GATE_AUC, "gate_p": GATE_P,
            "per_feature": table,
            "features_passing_gate": passed}


def internal_extraction_sweep(nodes, min_support=3, meta=None):
    """**抽取族**的内部门槛：用抽取计数（不是词面计数）做纯 TRAIN 时间切分。

    为什么必须与词面族分开做：抽取族特征（`growth` / `accel` / 伙伴获取速率）
    只能在**抽取出来的图**上算，而抽取样本的深度决定了计数规模。
    993 篇时每窗口计数中位只有 1–3 次 → 时间切分**根本算不出增长率**，
    结论只能是"测不出"，不能写成"不成立"。扩样本后要重跑本函数。

    本函数同时输出**功效诊断**（`median_count_per_window`）——
    它比 AUC 更重要：若每窗口计数仍是 1–2 次，AUC 报什么都不该被采信。

    ⚠️ **份额的分母必须是"该窗口的论文数"**，不能用"该概念自己的总数"。
    首版用了后者，立刻产出一个假结果：某概念若全部出现于 B 期，
    则 `share_B` 最大而 `share_C ≈ 0` → 标签必然为负 ⇒ `scale_B` 与标签
    **构造性反相关**，AUC 掉到 0.13。那是度量缺陷，不是"规模强反向"。
    改用窗口论文数做分母后，与词面族用的是同一把尺子，两族可比。
    """
    # 用**无共享项**的四窗口：特征取自 A/B，标签取自 C/D（见 DISJOINT_WINDOWS 注释）
    win = [(k, w) for k, w in DISJOINT_WINDOWS]
    ndoc = {k: 0 for k, _w in win}
    if meta:
        for rec in meta.values():
            y = rec.get("year")
            for k, (lo, hi) in win:
                if y is not None and lo <= y <= hi:
                    ndoc[k] += 1

    def rate(c, k):
        # 与词面族同形：拉普拉斯平滑 / 窗口论文数
        return (c + 0.5) / (ndoc[k] + 1) if ndoc[k] else None

    rows = []
    for nm, nd in nodes.items():
        if nd["support"] < min_support:
            continue
        c = {}
        for k, (lo, hi) in win:
            c[k] = sum(v for y, v in nd["years"].items() if lo <= y <= hi)
        if sum(c.values()) == 0:
            continue
        r = {k: rate(c[k], k) for k in c}
        if any(v is None for v in r.values()):
            continue
        rows.append({
            "concept": nm,
            "scale_B": r["B"],
            "growth_AB": r["B"] / r["A"],
            "accel_AB": r["B"] - r["A"],
            "new_entrant": 1.0 if c["A"] == 0 else 0.0,
            "recency": -float(min(y for y, v in nd["years"].items() if v > 0)),
            "support": float(nd["support"]),
            "counts": (c["A"], c["B"], c["C"], c["D"]),
            # 标签只用 C/D 窗口 —— 与任何特征窗口都不相交
            "label": r["D"] > r["C"],
        })
    feats = ["scale_B", "growth_AB", "accel_AB", "new_entrant", "recency", "support"]
    table = {f: auc_against_label(lambda r, f=f: r[f], rows, lambda r: r["label"])
             for f in feats}
    med = {k: statistics.median([r["counts"][i] for r in rows])
           for i, (k, _w) in enumerate(win)} if rows else {}
    thin = (not rows) or min(med.values()) < 3
    passed = [f for f, m in table.items()
              if m["auc"] is not None and m["auc"] > GATE_AUC and m["p"] < GATE_P]
    return {"window": "抽取计数 " + " / ".join("%s %d-%d" % (k, w[0], w[1])
                                               for k, w in win),
            "min_support": min_support,
            "n_evaluated": len(rows),
            "n_pos": sum(1 for r in rows if r["label"]),
            "median_count_per_window": med,
            "power_warning": ("每窗口计数中位 < 3 次 —— 时间切分算不出增长率，"
                              "只能报『测不出』，不得读成『不成立』" if thin else None),
            "per_feature": table,
            # ⚠️ 功效不足时**必须作废门槛判定**：每窗口计数 1 次时，
            # 份额经拉普拉斯平滑后由常数项主导，(c+0.5)/(tot+1.5) 基本是噪声，
            # 于是「new_entrant」「support」这类特征会因构造性原因拿到高 AUC。
            # 那种"通过"是把平滑常数当成信号 —— 比不给结论更有害。
            "features_passing_gate": [] if thin else passed,
            "features_passing_gate_raw": passed,
            "verdict": ("UNDERPOWERED（不判定）" if thin else
                        ("PASS" if passed else "NO_FEATURE_PASSES"))}


def feature_names(rows):
    """打分特征名 = scores 字典的键 + 若干直接量（规模/变化型各留几个作对照）。"""
    base = list(rows[0]["scores"].keys()) if rows else []
    return base + ["growth", "new_edge_share", "support", "degree"]


def value_of(row, feat):
    if feat in (row.get("scores") or {}):
        return row["scores"][feat]
    return row.get(feat)


def provenance_block(d, concepts_path):
    """产物必须写明自己认证了哪些输入 —— 否则"可复现"只是感觉。

    2026-09-12 实测过一次脱钩（`validation_report_v1.json` 记录了一份已被后续写入
    取代的候选文件，时间差 32 秒），故从这一份产物起，评估输出自带输入与工具 sha256，
    由 `tools/verify_artifacts.py` 统一核对。
    """
    import hashlib

    def _sha(p):
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
        return h.hexdigest()

    def _rel(p):
        try:
            return os.path.relpath(p)
        except ValueError:                      # 跨盘符
            return p

    me = os.path.abspath(__file__)
    return {"concepts_file": _rel(concepts_path),
            "concepts_sha256": _sha(concepts_path),
            "db_file": _rel(d.DB_PATH), "db_sha256": _sha(d.DB_PATH),
            "tool_file": _rel(me), "tool_sha256": _sha(me)}


def run(args):
    d = _load("dec", "discover_emergence_candidates.py")
    v = _load("val", "validate_emergence_p4_3.py")

    concepts_path = args.concepts or d.CONCEPTS_PATH
    ok_rows, meta = d.load_inputs(d.DB_PATH, concepts_path)
    nodes, typed, pairs, node_papers, pair_adj, diag = d.build_tree(ok_rows, meta)
    rows, _ = d.build_node_candidates(nodes, typed, pairs, pair_adj, diag,
                                      node_papers, meta)
    by = {r["concept"]: r for r in rows}
    support = {n: nd["support"] for n, nd in nodes.items()}

    # (1) 树覆盖盘点
    coverage = {f"support>={th}": sum(1 for s in support.values() if s >= th)
                for th in SUPPORT_LADDER}

    text = v.load_text_rows(d.DB_PATH)
    universe = [n for n in support if support[n] >= args.k]
    universe = [n for n in universe if n in by]        # 只保留可打分的（特征可算）
    out = {"inputs": provenance_block(d, concepts_path),
           "tree_coverage": coverage, "k": args.k,
           "n_universe_scorable": len(universe),
           "n_universe_unsorable": sum(1 for n in support
                                       if support[n] >= args.k and n not in by),
           "label_windows": []}

    for tag, tp, ep in LABEL_WINDOWS:
        idx = v.LexIndex(text, tp, ep)
        label = {}
        for n in universe:
            h = v.is_hit("NODE", v.measure(idx, [n], tp, ep))
            if h is not None:
                label[n] = h
        names = [n for n in universe if n in label]
        n_pos = sum(1 for n in names if label[n])
        block = {
            "window": tag, "train_period": list(tp), "eval_period": list(ep),
            "n_scored": len(names), "n_pos": n_pos,
            "pos_rate": round(n_pos / len(names), 4) if names else None,
            "composite": auc_against_label(
                lambda n: by[n]["emergence_score"], names, lambda n: label[n]),
            "by_stratum": {
                s: auc_against_label(lambda n: by[n]["emergence_score"],
                                     [n for n in names if by[n]["stratum"] == s],
                                     lambda n: label[n])
                for s in ("small", "mid", "large")},
            "per_feature": {},
        }
        for f in feature_names(rows):
            block["per_feature"][f] = auc_against_label(
                lambda n, f=f: value_of(by[n], f), names, lambda n: label[n])
        out["label_windows"].append(block)

    # 树内"有潜力的方向"清单（默认窗口 = 零重叠那个）
    idx = v.LexIndex(text, LABEL_WINDOWS[1][1], LABEL_WINDOWS[1][2])
    pos_rows = []
    for n in universe:
        m = v.measure(idx, [n], LABEL_WINDOWS[1][1], LABEL_WINDOWS[1][2])
        if v.is_hit("NODE", m):
            pos_rows.append({"concept": n, "support": support[n],
                             "degree": by[n]["degree"],
                             "stratum": by[n]["stratum"],
                             "rate_ratio": m["rate_ratio"]})
    pos_rows.sort(key=lambda r: -(r["rate_ratio"] or 0))
    out["in_tree_positives"] = pos_rows

    if args.internal:
        out["internal_gate"] = internal_lexical_sweep(d, v, nodes)
    if args.internal_family in ("extraction", "both"):
        out["internal_gate_extraction"] = internal_extraction_sweep(
            nodes, args.k, meta)
    return out


def print_extraction_gate(rep):
    eg = rep.get("internal_gate_extraction")
    if not eg:
        return
    under = bool(eg["power_warning"])
    print("══ 内部门槛（抽取族：用**抽取计数**做纯 TRAIN 时间切分）")
    print("   窗口 %s | support>=%d | 可评估 %d 个概念，正例 %d"
          % (eg["window"], eg["min_support"], eg["n_evaluated"], eg["n_pos"]))
    print("   每窗口计数中位 %s" % eg["median_count_per_window"])
    print("   判定：%s" % eg["verdict"])
    for f, m in sorted(eg["per_feature"].items(), key=lambda x: -(x[1]["auc"] or 0)):
        if m["auc"] is None:
            continue
        if under:
            tag = "不判定"
        else:
            tag = ("反向" if m["auc"] < 0.45 else
                   ("通过门槛" if f in eg["features_passing_gate"] else "零信息"))
        print("   %-14s AUC=%.3f p=%.4f %-10s (pos=%d neg=%d)"
              % (f, m["auc"], m["p"], tag, m["n_pos"], m["n_neg"]))
    if under:
        print("   [WARN] %s" % eg["power_warning"])
        if eg["features_passing_gate_raw"]:
            print("   [WARN] 这些特征表面上过线但**已作废**：%s"
                  % ", ".join(eg["features_passing_gate_raw"]))
            print("   [WARN] 原因：每窗口计数 ~1 次时率值被平滑常数 (c+0.5)/(n+1) 主导，")
            print("          任何与「总出现次数」相关的特征都会拿到构造性虚高，不能当证据。")
    elif eg["features_passing_gate"]:
        print("   → 通过内部门槛: %s" % ", ".join(eg["features_passing_gate"]))
    else:
        print("   → 无特征通过内部门槛（功效已足，可下『不成立』的结论）")
    print()


def print_report(rep):
    ig = rep.get("internal_gate")
    if ig:
        print("══ 内部门槛（纯 TRAIN 三分窗口，不含任何 2021+ 数据）")
        print("   窗口 %s  |  论文 A=%d B=%d C=%d"
              % (" / ".join("%s %d-%d" % (k, w[0], w[1]) for k, w in ig["windows"]),
                 ig["docs_per_window"]["A"], ig["docs_per_window"]["B"],
                 ig["docs_per_window"]["C"]))
        print("   特征 A->B ｜ 标签 B->C ｜ 可评估 %d 个概念，正例 %d"
              % (ig["n_evaluated"], ig["n_pos"]))
        print("   %-22s %7s %8s %7s %5s" % ("特征", "AUC", "p", "正例%", "判定"))
        for f, m in sorted(ig["per_feature"].items(),
                           key=lambda x: -(x[1]["auc"] or 0)):
            if m["auc"] is None:
                continue
            tag = ("反向" if m["auc"] < 0.45 else
                   ("通过门槛" if (m["auc"] > ig["gate_auc"] and m["p"] < ig["gate_p"])
                    else "零信息"))
            print("   %-22s %7.3f %8.4f %7.3f %5s"
                  % (f, m["auc"], m["p"], m["median_pct"], tag))
        if ig["features_passing_gate"]:
            print("   → 通过内部门槛: %s" % ", ".join(ig["features_passing_gate"]))
        else:
            print("   → 无任何特征通过内部门槛（AUC>%.2f 且 p<%.2f）"
                  % (ig["gate_auc"], ig["gate_p"]))
            print("     结论：这批特征在**同尺子、有功效**的 TRAIN 内部检验下没有分辨力，")
            print("          不能因为它在 EVAL 上好看就采用（跨测量体系的 AUC 不可复现）。")
        print()

    if not rep.get("tree_coverage"):
        return
    print("树覆盖盘点（概念节点总数 %d）" % rep["tree_coverage"]["support>=1"])
    for k, v_ in rep["tree_coverage"].items():
        print("    %-14s %5d 个" % (k, v_))
    print("\n树内测试集：宇宙 support>=%d 且可打分 = %d 个（另有 %d 个够支撑但度数不足，不可打分）"
          % (rep["k"], rep["n_universe_scorable"], rep["n_universe_unsorable"]))
    for blk in rep["label_windows"]:
        print("\n[%s]" % blk["window"])
        print("    可标记 %d | 正例 %d (%.1f%%)"
              % (blk["n_scored"], blk["n_pos"], 100 * blk["pos_rate"]))
        c = blk["composite"]
        print("    复合分 AUC=%.3f p=%.3f | 正例百分位中位=%.3f"
              % (c["auc"], c["p"], c["median_pct"]))
        for f, m in blk["per_feature"].items():
            if m["auc"] is None:
                continue
            mark = "  <<< 反向" if m["auc"] < 0.45 else (
                "  <<< 正向" if (m["auc"] > 0.55 and m["p"] < 0.05) else "")
            print("      %-20s AUC=%.3f p=%.3f%s" % (f, m["auc"], m["p"], mark))
    print("\n树内'有潜力的方向'（零重叠标签）Top-20 / 共 %d 个"
          % len(rep["in_tree_positives"]))
    for r in rep["in_tree_positives"][:20]:
        print("      %-50s sup=%-3d deg=%-3d ratio=%.2f"
              % (r["concept"][:50], r["support"], r["degree"], r["rate_ratio"]))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--k", type=int, default=3,
                    help="树内宇宙的支撑度下限（默认 3 = 全树 7359 节点里的 429 个）")
    ap.add_argument("--internal", action="store_true",
                    help="同时跑内部门槛（纯 TRAIN 三分窗口，不含 2021+ 数据）")
    ap.add_argument("--internal-family", default="lexical",
                    choices=("lexical", "extraction", "both"),
                    help="内部门槛用哪一族特征：词面计数 / 抽取计数 / 两者")
    ap.add_argument("--internal-only", action="store_true",
                    help="只跑内部门槛（不碰 EVAL 侧的任何标签）")
    ap.add_argument("--json", help="可选：另存 JSON（不写任何冻结产物）")
    ap.add_argument("--concepts", default=None,
                    help="抽取产物路径（默认 = discoverer 的 CONCEPTS_PATH；扩样本后指向新文件）")
    args = ap.parse_args(argv)
    if args.internal_only:
        d = _load("dec", "discover_emergence_candidates.py")
        v = _load("val", "validate_emergence_p4_3.py")
        _ok, _meta = d.load_inputs(d.DB_PATH, args.concepts or d.CONCEPTS_PATH)
        nodes, _t, _p, _np, _a, _dg = d.build_tree(_ok, _meta)
        rep = {}
        if args.internal_family in ("lexical", "both"):
            rep["internal_gate"] = internal_lexical_sweep(d, v, nodes)
        if args.internal_family in ("extraction", "both"):
            rep["internal_gate_extraction"] = internal_extraction_sweep(
                nodes, args.k, _meta)
    else:
        rep = run(args)
    print_report(rep)
    print_extraction_gate(rep)
    if args.json:
        with open(args.json, "w", encoding="utf-8", newline="\n") as f:
            json.dump(rep, f, ensure_ascii=False, indent=1)
        print("\n[ok] -> %s" % args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
