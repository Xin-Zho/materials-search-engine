#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P4-2 诊断：把「阳性对照不过」拆成 **树太稀** 与 **操作化错** 两个可分离的问题。

用户 2026-09-12 追问：「是知识树搭建不好，还是算法不好不能找到有潜力方向？」

只读脚本，不写任何冻结产物（`--json` 可选另存）。两个互相独立的测量：

  A) 采样缩放曲线
     在 993 篇已抽取论文上做分层子采样（25/50/75/100%），测每个采样深度下的
     「可测边数」`edges(support>=2)`、「可测节点数」`nodes(support>=3)`、
     「NODE 可打分池」。对采样量 N 幂律拟合，外推到全部 TRAIN（5,520 篇）。
     若指数 >> 1，说明当前 243 的候选面主要是**采样深度的函数**，不是算法缺陷。

  B) 单变量稠密化实验
     只把共现边的可测门槛 `PAIR_MIN_SUPPORT` 从 2 降到 1（其余全部冻结），
     重算结构打分，再看已知扩张概念（阳性对照）在同**度数带**内的百分位。
     度数带匹配是为了免受「分层阈值在稠密图上整体失效」的干扰
     （稀疏图上 deg 中位 4、稠密图上 40 —— 原分层 small/mid/large 是给稀疏图校准的）。
       对照从「明显低于中位」抬到「约等于中位」= 稀疏是**一部分**原因
       对照仍 ≈ 中位                 = 静态拓扑量本身没有预测力（主因）

结论口径：这两个测量**不能**互相替代。A 说明树的深度不够；B 说明即使把树变稠，
静态拓扑特征仍然拿不到区分度。所以修法不是「先免费改特征、通过再扩量」，
而是两者耦合 —— 加粗的树是结构量**可测**的前提。

用法：
    .venv/Scripts/python.exe tools/diagnose_graph_measurability.py
    .venv/Scripts/python.exe tools/diagnose_graph_measurability.py --json out.json
"""

from __future__ import annotations

import argparse
import collections
import importlib.util
import itertools
import json
import math
import os
import random
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "tools", "discover_emergence_candidates.py")

# 阳性对照：已知在 2020 前后扩张 / 打开新连接的概念。
# 注意：这是**度量方向校验**，不是候选集，不构成方法有效性的证据。
# 已剔除上一轮控制清单里的两个字符串自身错误：
#   `front photopolymerization`（真名 `frontal photopolymerization`，重复计入）
#   `photoinduced electron transfer reversible addition`（RAFT 名的截断残片）
CONTROL = [
    "pet-raft polymerization", "digital light processing", "4d printing",
    "frontal photopolymerization", "thiol-ene click chemistry",
    "vat photopolymerization", "two-photon polymerization", "bioprinting",
    "low shrinkage", "anisotropic shrinkage",
    "continuous liquid interface production", "machine learning",
]
FRACTIONS = (0.25, 0.50, 0.75, 1.00)


def _load_tool():
    spec = importlib.util.spec_from_file_location("dec", TOOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dec"] = mod
    spec.loader.exec_module(mod)
    return mod


def _graph_stats(rows, pair_min_support, node_min_support, min_degree):
    pairs = collections.Counter()
    nodes = collections.Counter()
    for rec in rows:
        names = sorted({c["name"] for c in rec["concepts"]
                        if c.get("name") and c.get("type")})
        for n in names:
            nodes[n] += 1
        for a, b in itertools.combinations(names, 2):
            pairs[(a, b)] += 1
    adj = collections.defaultdict(set)
    for (a, b), v in pairs.items():
        if v >= pair_min_support:
            adj[a].add(b)
            adj[b].add(a)
    pool = sum(1 for n, s in nodes.items()
               if s >= node_min_support and len(adj.get(n) or ()) >= min_degree)
    return {
        "papers": len(rows),
        "nodes_any": len(nodes),
        "edges_any": len(pairs),
        "edges_measurable": sum(1 for v in pairs.values() if v >= pair_min_support),
        "nodes_measurable": sum(1 for v in nodes.values() if v >= node_min_support),
        "node_pool": pool,
    }


def _power_law_fit(xs, ys):
    """对 log(y) = a·log(x) + b 做最小二乘。返回 (a, b)。

    抽成独立函数是为了让「指数 > 1」这条量化结论可被单测守住 ——
    它是「候选面小是采样深度问题」的**全部**依据，不能只有一次性计算。
    """
    lx = [math.log(x) for x in xs]
    ly = [math.log(max(y, 1)) for y in ys]
    n = len(lx)
    mx, my = sum(lx) / n, sum(ly) / n
    num = sum((a - mx) * (b - my) for a, b in zip(lx, ly))
    den = sum((a - mx) ** 2 for a in lx)
    if den == 0:
        return 0.0, my
    slope = num / den
    return slope, my - slope * mx


def measure_scaling(dec, ok_rows, target_papers):
    """A) 采样深度 -> 图可测性；幂律外推到全量 TRAIN。"""
    rng = random.Random(13)
    curve = []
    for frac in FRACTIONS:
        sub = rng.sample(ok_rows, int(len(ok_rows) * frac))
        st = _graph_stats(sub, dec.PAIR_MIN_SUPPORT, dec.NODE_MIN_SUPPORT,
                          dec.NODE_MIN_DEGREE)
        st["frac"] = frac
        curve.append(st)

    mult = target_papers / curve[-1]["papers"]
    ext = {}
    for key in ("edges_measurable", "node_pool"):
        slope, icept = _power_law_fit([s["papers"] for s in curve],
                                      [s[key] for s in curve])
        ext[key] = {
            "exponent": round(slope, 3),
            "at_target": int(round(math.exp(icept + slope * math.log(target_papers)))),
        }
    return {"curve": curve, "target_papers": target_papers,
            "multiplier": round(mult, 3), "extrapolation": ext}


def _build(dec, ok_rows, meta, thr):
    """B) 只改边门槛，其余冻结，重算结构打分。"""
    dec.PAIR_MIN_SUPPORT = thr
    dec.EXCLUDED = collections.Counter()
    nodes, typed, pairs, node_papers, pair_adj, diag = dec.build_tree(ok_rows, meta)
    rows, _ = dec.build_node_candidates(nodes, typed, pairs, pair_adj, diag,
                                        node_papers, meta)
    return {"n_pool": len(rows),
            "deg_p50": statistics.median([r["degree"] for r in rows]) if rows else 0,
            "deg_max": max((r["degree"] for r in rows), default=0),
            "score": {r["concept"]: r["emergence_score"] for r in rows},
            "deg": {r["concept"]: r["degree"] for r in rows},
            "rows": rows}


def measure_control(dec, ok_rows, meta, thr):
    g = _build(dec, ok_rows, meta, thr)
    if not g["rows"]:
        return {"thr": thr, "n_in_pool": 0}
    ordered = sorted(g["rows"], key=lambda r: r["emergence_score"])
    pcts, detail = [], []
    for c in CONTROL:
        if c not in g["score"]:
            detail.append({"concept": c, "in_pool": False})
            continue
        d0 = g["deg"][c]
        band = [r for r in ordered if abs(r["degree"] - d0) <= 2]
        p = sum(1 for r in band if r["emergence_score"] <= g["score"][c]) / len(band)
        pcts.append(p)
        detail.append({"concept": c, "in_pool": True, "degree": d0,
                       "score": round(g["score"][c], 4),
                       "band_n": len(band), "pct_in_band": round(p, 3)})
    solid = [d["pct_in_band"] for d in detail
             if d.get("in_pool") and d.get("band_n", 0) >= 5]
    return {
        "thr": thr,
        "n_pool": g["n_pool"],
        "deg_p50": g["deg_p50"],
        "deg_max": g["deg_max"],
        "n_in_pool": len(pcts),
        "median_pct_in_band": round(statistics.median(pcts), 3) if pcts else None,
        "median_pct_in_band_band_n_ge_5": round(statistics.median(solid), 3) if solid else None,
        "detail": detail,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", help="可选：把结果另存到该路径（不写冻结产物）")
    args = ap.parse_args(argv)

    dec = _load_tool()
    ok_rows, meta = dec.load_inputs()
    ok_rows = [r for r in ok_rows
               if meta.get(r["paper_uid"], {}).get("year") is not None]
    full_train = 5520          # v_photopolymerization.train（见 build_report.json）
    print(f"已抽取论文 {len(ok_rows)} / in-scope TRAIN {full_train} "
          f"= {len(ok_rows)/full_train:.1%} 采样")

    a = measure_scaling(dec, ok_rows, full_train)
    print("\n[A] 采样深度 -> 图可测性（固定 seed=13 分层子采样）")
    print("%-8s %8s %8s %14s %14s" % ("frac", "papers", "nodes", "edges(>=2)", "NODE池"))
    for s in a["curve"]:
        print("%-8.2f %8d %8d %14d %14d" % (s["frac"], s["papers"], s["nodes_any"],
                                            s["edges_measurable"], s["node_pool"]))
    print("    幂律指数：edges>=2 ∝ N^%.2f ｜ NODE池 ∝ N^%.2f"
          % (a["extrapolation"]["edges_measurable"]["exponent"],
             a["extrapolation"]["node_pool"]["exponent"]))
    print("    外推至 %d 篇（×%.2f）：edges>=2 ≈ %d ｜ NODE池 ≈ %d"
          % (full_train, a["multiplier"],
             a["extrapolation"]["edges_measurable"]["at_target"],
             a["extrapolation"]["node_pool"]["at_target"]))

    b = []
    for thr in (2, 1):
        b.append(measure_control(dec, ok_rows, meta, thr))
        dec.PAIR_MIN_SUPPORT, dec.NODE_MIN_SUPPORT = 2, 3   # 复位，避免污染下一轮
        dec.EXCLUDED = collections.Counter()
    print("\n[B] 单变量：只改共现边可测门槛（其余冻结），阳性对照在同度数带内百分位")
    print("%-22s %8s %10s %12s %12s %10s"
          % ("边门槛", "打分池", "对照进池", "deg中位", "同带%中位", "排除退化带"))
    for r in b:
        print("%-22s %8d %10s %12s %12s %10s"
              % (f"support>={r['thr']}", r["n_pool"],
                 "%d/%d" % (r["n_in_pool"], len(CONTROL)), r["deg_p50"],
                 r["median_pct_in_band"], r["median_pct_in_band_band_n_ge_5"]))
    print("    读法：稀疏图上对照明显低于中位；稠密化后升到约等于中位 ->")
    print("          稀疏是**一部分**原因，静态拓扑量本身也没有区分度（主因）。")

    if args.json:
        with open(args.json, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"sampling": a, "densification": b,
                       "control": CONTROL, "full_train": full_train},
                      f, ensure_ascii=False, indent=1)
        print(f"\n[ok] -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
