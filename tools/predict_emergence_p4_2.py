#!/usr/bin/env python
"""P4-2：Emergence Score 排序 —— 从 TRAIN 概念图预测「哪些方向会增长」。

用户 2026-09-12 的 P4-2 规格（v1 **不做机器学习，先做 ranking**）：

    Growth        N2020 / N2015
    Acceleration  slope(2018-2020)
    Novelty       first_seen
    Connectivity  degree centrality（知识图谱）
    Cross-domain  跨主题共现
         ↓
    Emergence Score  ->  排序  ->  Top-K 预测

反泄漏（本工具的第一属性）
--------------------------
1. 输入只有 **TRAIN 侧**（所有论文 year <= 2020）—— 工具启动即断言。
2. **禁用 `citation_count` 快照**（含预测窗口的引用）。本工具只读
   `year / primary_topic / citations_asof_cutoff`，且 v1 的五个指标**根本不用引用**
   —— 引用速率留到 v2，届时必须用 `citations_asof_cutoff`。
3. **EVAL 侧概念不存在于输入里**（抽取只在 TRAIN 侧做过），所以对答案天然隔离。
4. 输出**冻结**：记录输入 sha256 + 权重 + 时间戳，P4-3 验证时不得再调参。

为什么要按「期」归一化而不是直接比计数
-------------------------------------
本数据集是**分层采样**（A 高影响 / B 快速增长 / C 长尾），**不是按年份等比例抽样**：
实测 2016-2020 有 420 篇、2011-2015 只有 200 篇。直接比 ``df_late / df_early``
会把这个采样偏斜算成"增长"。故一律用**论文份额**（df / 该期论文数）+ Laplace 平滑。

用法::

    python tools/predict_emergence_p4_2.py                 # 默认 dry-run，只打印
    python tools/predict_emergence_p4_2.py --apply         # 冻结预测产物
    python tools/predict_emergence_p4_2.py --min-support 5 --top-k 20
"""
from __future__ import annotations

import argparse
import collections
import datetime
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

PREDICTOR_VERSION = "p4_2_emergence_v1"
DATASET_DIR = BASE / "datasets" / "photopolymerization_v1"
DEFAULT_DB = DATASET_DIR / "paper_meta.db"
DEFAULT_CONCEPTS = DATASET_DIR / "concepts_v1.jsonl"
DEFAULT_OUT = DATASET_DIR / "emergence_scores_p4_2.json"
DEFAULT_CSV = DATASET_DIR / "emergence_top50.csv"

CUTOFF_YEAR = 2020
EARLY = (2011, 2015)
LATE = (2016, 2020)
ACCEL_YEARS = (2018, 2019, 2020)

# Emergence Score 权重（**冻结**；rank 百分位加权，非原始值加权）
WEIGHTS = {
    "growth": 0.30,
    "acceleration": 0.25,
    "connectivity": 0.20,
    "cross_domain": 0.15,
    "novelty": 0.10,
}
LAPLACE = 0.5          # 期份额的加性平滑；避免 0/0 与单次出现造成的爆炸式比值

ST_OK = "OK"

# ⚠️ 一次**失败的**尝试，保留为诊断量，**不要**用它做门槛。
#
# 想解决的问题：`scanning electron microscopy` / `tensile testing` /
# `light scattering` / `finite element method` 这类**表征/分析手段**会随论文数
# 自然增长（越来越多人例行报告），于是挤进 emergence 榜，但它们不是研究方向。
#
# 尝试的判别量：`dir_ratio` = 该概念参与的「解决问题型」关系占比
#   （addresses/enables/improves/causes/alternative_to）
# 假设：真方向会 `addresses` 某挑战 / `enables` 某应用；表征手段只会出现在
#       `requires` / `part_of` 位置。
#
# **实测证伪**：dir_ratio < 0.30 挡下的是 `photoinitiator`(0.12)、
# `vinylcyclopropane`(0.00)、`epoxy resin`(0.15)、`composite resin`(0.00)
# 这些**真概念**，而 `scanning electron microscopy` 并没有被挡下。
# 根因：材料类概念在图里天然处于 `requires` / `part_of` 的位置
# （"photopolymerization --requires--> photoinitiator"），
# 所以这个量与「是否表征手段」**正交**，不是它的测量。
#
# 改用**构造性依据**：concept 的 `type` 是抽取时就定好的语义类别 ——
#   `direction`   = 研究方向的完整表述（用户定义）
#   `challenge`   = 未解决的问题（用户定义，且"未来热点来自未解决问题"）
# 这两类**按构造**就是方向性的；`fabrication_method` 混了"加工方法"（真方向）
# 与"表征手段"（噪声），需要 v2 用一次 LLM 分类来拆开 —— 见文档「未决」。
PROBLEM_SOLVING_RELATIONS = frozenset(
    {"addresses", "enables", "improves", "causes", "alternative_to"})
STRUCTURAL_RELATIONS = frozenset({"requires", "part_of", "combines_with", "other"})

DIR_RATIO_MIN = 0.30          # 仅诊断用；**不**参与筛选
# 默认预测口径：按构造即为方向性的两类
DEFAULT_PREDICTION_TYPES = ("direction", "challenge")


def _rel(path) -> str:
    """展示用相对路径（跨盘符安全）。"""
    try:
        return os.path.relpath(str(path), str(BASE)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_inputs(db_path, concepts_path):
    """只读装载；**不读 citation_count**（见模块 docstring 的反泄漏第 2 条）。"""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        meta = {r["paper_uid"]: {"year": r["year"],
                                 "primary_topic": r["primary_topic"],
                                 "split": r["split"]}
                for r in con.execute(
                    "SELECT paper_uid, year, primary_topic, split FROM paper_meta")}
    finally:
        con.close()
    concepts = [json.loads(l) for l in
                Path(concepts_path).read_text(encoding="utf-8").splitlines()
                if l.strip()]
    return meta, concepts


def assert_no_leakage(meta, concepts):
    """反泄漏硬断言。**先于任何计算**。"""
    uids = [r["paper_uid"] for r in concepts]
    years = [meta[u]["year"] for u in uids if u in meta]
    splits = collections.Counter(meta[u]["split"] for u in uids if u in meta)
    bad = [y for y in years if y is None or y > CUTOFF_YEAR]
    if bad:
        raise SystemExit(f"[refused] 输入含 >{CUTOFF_YEAR} 的论文：{sorted(set(bad))}")
    if splits.get("EVAL"):
        raise SystemExit(f"[refused] 输入含 EVAL 侧论文 {splits['EVAL']} 篇 —— "
                         "EVAL 只用于对答案，不得参与指标计算")
    return {"n_concepts_rows": len(concepts), "n_with_meta": len(years),
            "max_year": max(years) if years else None,
            "split_distribution": dict(splits)}


def build_graph(ok_rows):
    """关系图（无向，按概念名）。返回 ``(neighbors, edge_type_stats)``。"""
    nb = collections.defaultdict(set)
    etypes = collections.defaultdict(set)
    for r in ok_rows:
        for x in r["relations"]:
            s, t, rel = x["source"], x["relation"], x["target"]
            nb[s].add(t)
            nb[t].add(s)
            etypes[s].add(rel)
            etypes[t].add(rel)
    return nb, etypes


def compute_metrics(ok_rows, meta):
    """逐概念计算五项指标（纯计算）。"""
    df_year = collections.defaultdict(collections.Counter)   # concept -> {year: n}
    topics = collections.defaultdict(set)                    # concept -> topics
    ctype = {}
    for r in ok_rows:
        u = r["paper_uid"]
        m = meta.get(u)
        if not m or m["year"] is None:
            continue
        y = m["year"]
        for c in r["concepts"]:
            n = c["name"]
            ctype.setdefault(n, c["type"])
            df_year[n][y] += 1
            topics[n].add(m["primary_topic"])

    papers_period = collections.Counter()
    papers_year = collections.Counter()
    for m in meta.values():
        y = m["year"]
        if y is None:
            continue
        papers_year[y] += 1
        if EARLY[0] <= y <= EARLY[1]:
            papers_period["early"] += 1
        elif LATE[0] <= y <= LATE[1]:
            papers_period["late"] += 1

    nb, etypes = build_graph(ok_rows)
    max_deg = max((len(v) for v in nb.values()), default=1)
    # 逐概念统计「解决问题型」关系占比
    rel_counter = collections.defaultdict(collections.Counter)
    for r in ok_rows:
        for x in r["relations"]:
            rel_counter[x["source"]][x["relation"]] += 1
            rel_counter[x["target"]][x["relation"]] += 1

    def rate(n, period_papers):
        return (n + LAPLACE) / (period_papers + 1)

    out = {}
    for name, yr in df_year.items():
        support = sum(yr.values())
        early_n = sum(n for y, n in yr.items() if EARLY[0] <= y <= EARLY[1])
        late_n = sum(n for y, n in yr.items() if LATE[0] <= y <= LATE[1])
        r_e = rate(early_n, papers_period["early"])
        r_l = rate(late_n, papers_period["late"])
        # 加速度：2018-2020 三年年度份额的最小二乘斜率
        pts = [(y, rate(yr.get(y, 0), papers_year.get(y, 1))) for y in ACCEL_YEARS]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        den = sum((x - mx) ** 2 for x in xs)
        slope = (sum((x - mx) * (y - my) for x, y in pts) / den) if den else 0.0
        first_seen = min(yr) if yr else None
        rc = rel_counter.get(name) or collections.Counter()
        tot_rel = sum(rc.values())
        ps = sum(v for k, v in rc.items() if k in PROBLEM_SOLVING_RELATIONS)
        dir_ratio = (ps / tot_rel) if tot_rel else 0.0
        out[name] = {
            "concept": name,
            "type": ctype.get(name),
            "support": support,
            "df_early": early_n,
            "df_late": late_n,
            "rate_early": round(r_e, 6),
            "rate_late": round(r_l, 6),
            "growth": round(r_l / r_e, 4) if r_e else None,
            "acceleration": round(slope, 8),
            "novelty_raw": first_seen,
            "connectivity_raw": len(nb.get(name, ())),
            "relation_types": len(etypes.get(name, ())),
            "cross_domain_raw": len({t for t in topics[name] if t}),
            "first_seen": first_seen,
            "rel_total": tot_rel,
            "rel_problem_solving": ps,
            "dir_ratio": round(dir_ratio, 4),
            "directional": dir_ratio >= DIR_RATIO_MIN,
        }
    return out, {"papers_period": dict(papers_period),
                 "papers_year": dict(sorted(papers_year.items())),
                 "period_note": {
                     "early": list(EARLY), "late": list(LATE),
                     "why_share_not_count": ("分层采样不是按年份等比例，"
                                             "实测 late/early = "
                                             f"{papers_period['late']}/{papers_period['early']}"
                                             "；直接比计数会把采样偏斜算成增长"),
                     "laplace": LAPLACE},
                 "max_degree": max_deg,
                 "n_concepts_with_edges": len(nb)}


def _pct_rank(values, higher_is_better=True):
    """百分位排名（0-1）。并列取平均秩，避免顺序依赖。"""
    n = len(values)
    if n <= 1:
        return {k: 0.5 for k in values}
    order = sorted(values.items(), key=lambda kv: (kv[1], kv[0]))
    out, i = {}, 0
    while i < n:
        j = i
        while j + 1 < n and order[j + 1][1] == order[i][1]:
            j += 1
        avg = (i + j) / 2 / (n - 1)
        for k in range(i, j + 1):
            out[order[k][0]] = avg if higher_is_better else 1 - avg
        i = j + 1
    return out


def score(metrics, min_support, prediction_types=DEFAULT_PREDICTION_TYPES):
    """rank 组合成 Emergence Score。

    用**百分位排名**而不是原始值加权：growth 的分布极度长尾（平滑会把
    df=0→1 放大成很大的比值），原始值加权会让一个只出现一次的噪声概念
    压过真正持续增长的方向。rank 化把这个尺度问题消掉。

    用**分类型独立排名**而不是全局排名：`fabrication_method` 里的
    `digital light processing`（真方向）与 `tensile testing`（例行表征）
    无可比性；全局排名会让后者靠 connectivity/cross_domain 挤进 Top-20。
    故每个 type 内部各自排百分位，再拼成一张表（`rank_in_type`）。
    """
    cand = {k: v for k, v in metrics.items()
            if v["support"] >= min_support
            and (not prediction_types or v["type"] in prediction_types)}
    if not cand:
        return []
    keys = list(cand)
    # 分类型独立百分位排名（type 内的可比性才有意义）
    by_type = collections.defaultdict(list)
    for k in keys:
        by_type[cand[k]["type"] or "(none)"].append(k)
    r_growth, r_accel, r_conn, r_cross, r_novel = {}, {}, {}, {}, {}
    type_rank = {}
    for t, ks in by_type.items():
        r_growth.update(_pct_rank({k: cand[k]["growth"] for k in ks}))
        r_accel.update(_pct_rank({k: cand[k]["acceleration"] for k in ks}))
        r_conn.update(_pct_rank({k: cand[k]["connectivity_raw"] for k in ks}))
        r_cross.update(_pct_rank({k: cand[k]["cross_domain_raw"] for k in ks}))
        r_novel.update(_pct_rank({k: cand[k]["novelty_raw"] or 0 for k in ks}))
        ordered = sorted(ks, key=lambda k: (
            -(WEIGHTS["growth"] * r_growth[k] + WEIGHTS["acceleration"] * r_accel[k]
              + WEIGHTS["connectivity"] * r_conn[k]
              + WEIGHTS["cross_domain"] * r_cross[k]
              + WEIGHTS["novelty"] * r_novel[k]), k))
        for i, k in enumerate(ordered, 1):
            type_rank[k] = i

    rows = []
    for k in keys:
        c = dict(cand[k])
        c["rank_growth"] = round(r_growth[k], 4)
        c["rank_acceleration"] = round(r_accel[k], 4)
        c["rank_connectivity"] = round(r_conn[k], 4)
        c["rank_cross_domain"] = round(r_cross[k], 4)
        c["rank_novelty"] = round(r_novel[k], 4)
        c["rank_in_type"] = type_rank[k]
        c["emergence_score"] = round(
            WEIGHTS["growth"] * r_growth[k]
            + WEIGHTS["acceleration"] * r_accel[k]
            + WEIGHTS["connectivity"] * r_conn[k]
            + WEIGHTS["cross_domain"] * r_cross[k]
            + WEIGHTS["novelty"] * r_novel[k], 6)
        rows.append(c)
    rows.sort(key=lambda r: (-r["emergence_score"], r["concept"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def power_report(metrics):
    """统计功效：在多少 support 阈值下还剩多少候选（决定预测能不能做）。"""
    out = {}
    for s in (1, 2, 3, 5, 8, 10, 20):
        sub = [v for v in metrics.values() if v["support"] >= s]
        by_type = collections.Counter(v["type"] for v in sub)
        out[str(s)] = {"candidates": len(sub),
                       "direction": by_type.get("direction", 0),
                       "challenge": by_type.get("challenge", 0),
                       "by_type": dict(by_type)}
    return out


def print_summary(rows, meta_block, power, top_k, min_support):
    print("═" * 76)
    print(f"  P4-2 Emergence Score  [{PREDICTOR_VERSION}]  "
          f"min_support={min_support}  top_k={top_k}")
    print("═" * 76)
    pp = meta_block["papers_period"]
    print(f"  期样本：early{list(EARLY)} {pp.get('early')} 篇 | "
          f"late{list(LATE)} {pp.get('late')} 篇")
    print(f"  {meta_block['period_note']['why_share_not_count']}")
    print(f"  图：{meta_block['n_concepts_with_edges']} 个概念有边 | "
          f"最大度 {meta_block['max_degree']}")
    print(f"  权重 {WEIGHTS}")
    print("─" * 76)
    print("  统计功效（候选概念数 / 其中 direction）：")
    for s in ("2", "3", "5", "8", "10"):
        p = power[s]
        print(f"    support>={s:<3} {p['candidates']:>5} 个  "
              f"(direction {p['direction']:>3}, challenge {p['challenge']:>3})")
    print("─" * 76)
    print(f"  Top-{top_k} emerging concepts：")
    print(f"    {'#':>3} {'score':>6} {'sup':>4} {'grw':>6} {'acc':>9} "
          f"{'deg':>4} {'xdom':>4} {'1st':>5}  type / concept")
    for r in rows[:top_k]:
        print(f"    {r['rank']:>3} {r['emergence_score']:.3f} {r['support']:>4} "
              f"{r['growth']:>6.2f} {r['acceleration']:>9.2e} "
              f"{r['connectivity_raw']:>4} {r['cross_domain_raw']:>4} "
              f"{str(r['first_seen']):>5}  [{r['type'][:17]:<17}] {r['concept']}")
    dirs = [r for r in rows if r["type"] == "direction"]
    print(f"\n  仅 direction 类型（用户定义的「研究方向」粒度，共 {len(dirs)} 个候选）：")
    for r in dirs[:min(15, len(dirs))]:
        print(f"    {r['rank']:>3} {r['emergence_score']:.3f} sup={r['support']:<3} "
              f"grw={r['growth']:>5.2f}  {r['concept']}")
    print("═" * 76)


def print_secondary(metrics, min_support, prediction_types):
    """非预测类型的概念 —— 作为**支撑信号**单列，并给出功效警示。

    为什么不混进主榜：`fabrication_method` 里既有真方向（`digital light
    processing`、`vat photopolymerization`）也有例行表征手段
    （`scanning electron microscopy`、`tensile testing`），跨类型比较不成立。
    单列它们既保留信息（DLP 的 growth 8.60 是很强的信号），又不污染主榜。
    """
    others = sorted((v for v in metrics.values()
                     if v["support"] >= min_support
                     and v["type"] not in prediction_types),
                    key=lambda v: -v["support"])
    print("-" * 76)
    print(f"  支撑信号（非预测类型，{len(others)} 个 support>={min_support}）："
          f"其中 fabrication_method 混了加工方法与表征手段，未做拆分 —— 见文档「未决」")
    bt = collections.defaultdict(list)
    for v in others:
        bt[v["type"]].append(v)
    for t in ("fabrication_method", "material", "mechanism", "application"):
        vs = bt.get(t) or []
        if not vs:
            continue
        print(f"    ── {t}（{len(vs)} 个）按 support 最高：")
        for v in vs[:6]:
            print(f"       sup={v['support']:<4} grw={str(v['growth']):>6} "
                  f"xdom={v['cross_domain_raw']:<3} {v['concept']}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="P4-2 Emergence Score 排序")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--concepts", default=str(DEFAULT_CONCEPTS))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--csv", default=str(DEFAULT_CSV))
    ap.add_argument("--min-support", type=int, default=3)
    ap.add_argument("--prediction-types", default=",".join(DEFAULT_PREDICTION_TYPES),
                    help="参与预测的 concept 类型（逗号分隔；空串=全部）。"
                         "默认 direction,challenge —— 按构造即为方向性的两类")
    ap.add_argument("--top-k", type=int, default=20)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)

    print(f"[p4.2] {PREDICTOR_VERSION}  concepts={_rel(args.concepts)}  "
          f"mode={'APPLY' if args.apply else 'DRY-RUN'}")
    meta, concepts = load_inputs(args.db, args.concepts)
    guard = assert_no_leakage(meta, concepts)
    print(f"[p4.2] 反泄漏断言通过：{guard['n_concepts_rows']} 行，"
          f"max year {guard['max_year']}，split {guard['split_distribution']}")

    ok = [r for r in concepts if r["status"] == ST_OK]
    metrics, meta_block = compute_metrics(ok, meta)
    power = power_report(metrics)
    ptypes = tuple(x.strip() for x in args.prediction_types.split(",")
                   if x.strip())
    rows = score(metrics, args.min_support, prediction_types=ptypes)
    if not rows:
        raise SystemExit(f"[refused] min_support={args.min_support} 下无候选")
    print_summary(rows, meta_block, power, args.top_k, args.min_support)
    print_secondary(metrics, args.min_support, ptypes)

    if not args.apply:
        print("\n[DRY-RUN] 零写入。加 --apply 冻结预测。")
        return 0

    payload = {
        "predictor_version": PREDICTOR_VERSION,
        "frozen_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "role": ("P4-2 冻结预测：**先冻结、后验证**。P4-3 用它对 EVAL 侧答案，"
                 "不得再调权重或阈值"),
        "inputs": {
            "concepts_file": _rel(args.concepts),
            "concepts_sha256": _sha256(args.concepts),
            "db_file": _rel(args.db),
            "db_sha256": _sha256(args.db),
        },
        "leakage_guard": guard,
        "config": {"min_support": args.min_support, "top_k": args.top_k,
                   "weights": WEIGHTS, "laplace": LAPLACE,
                   "early_period": list(EARLY), "late_period": list(LATE),
                   "acceleration_years": list(ACCEL_YEARS),
                   "cutoff_year": CUTOFF_YEAR,
                   "no_citation_features": True,
                   "scoring": "指标先转百分位排名再加权（growth 长尾，raw 加权会被噪声压过）",
                   "rank_within_type": True,
                   "prediction_types": list(ptypes),
                   "why_type_filter": (
                       "只取 direction + challenge —— 按抽取时的语义类别即为方向性的两类。"
                       "fabrication_method 混了'加工方法'（真方向，如 digital light "
                       "processing）与'表征手段'（噪声，如 scanning electron microscopy / "
                       "tensile testing），跨类型比较不成立，故单列为支撑信号"),
                   "rejected_heuristic": {
                       "name": "dir_ratio",
                       "definition": ("该概念参与的解决问题型关系占比 "
                                      f"({sorted(PROBLEM_SOLVING_RELATIONS)})"),
                       "threshold_tried": DIR_RATIO_MIN,
                       "verdict": "REJECTED",
                       "evidence": ("实测挡下 photoinitiator(0.12)/vinylcyclopropane(0.00)/"
                                    "epoxy resin(0.15) 等**真概念**，却未挡下 "
                                    "scanning electron microscopy —— 该量与'是否表征手段'正交"),
                       "kept_as": "仅记录在 metrics 里的诊断量",
                   }},
        "corpus_block": meta_block,
        "power": power,
        "n_metrics": len(metrics),
        "n_candidates": len(rows),
        "top_k": rows[:args.top_k],
        "predictions": rows,
    }
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    with open(args.csv, "w", encoding="utf-8", newline="\n") as f:
        f.write("rank,emergence_score,support,df_early,df_late,growth,"
                "acceleration,connectivity,cross_domain,relation_types,"
                "first_seen,type,concept\n")
        for r in rows[:50]:
            f.write(f"{r['rank']},{r['emergence_score']},{r['support']},"
                    f"{r['df_early']},{r['df_late']},{r['growth']},"
                    f"{r['acceleration']},{r['connectivity_raw']},"
                    f"{r['cross_domain_raw']},{r['relation_types']},"
                    f"{r['first_seen']},{r['type']},{r['concept']}\n")
    print(f"\n[applied] {_rel(args.out)}  ({len(rows)} 条候选，Top-{args.top_k} 已冻结)")
    print(f"[applied] {_rel(args.csv)}")
    print(f"[applied] 冻结指纹 concepts_sha256={payload['inputs']['concepts_sha256'][:16]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
