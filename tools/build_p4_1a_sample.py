#!/usr/bin/env python
"""P4-1A 分层采样：从干净视图的 TRAIN 侧抽 1000 篇做 concept extraction。

用户 2026-09-12 裁定：
  · 用干净视图（CORE + ADJACENT = 11,027 行），**不启用 CHANNEL_ONLY**
  · 首轮抽 ~1000 篇
  · **分层采样，不随机**

三层（用户指定权重，本工具按 TRAIN 可抽池规模落地）：

  Layer A 高影响 300  ── 按 ``citations_asof_cutoff`` 降序（**不是快照**）
                         目的：捕获成熟的 foundational 方向
  Layer B 快速增长 400 ── 按 ``primary_topic`` 在 TRAIN 内的增速
                         g = n(2016-2020) / n(2011-2015)，要求基准期 >= 3 篇
                         目的：发现 emerging 方向
  Layer C 长尾   300  ── 低引用（<= TRAIN 池 p25）+ 固定随机种子
                         目的：避免知识树只看热点

约束
----
1. **三层互斥**：先 A，再 B 去掉 A，再 C 去掉 A∪B。一篇论文只出现一次。
2. **单主题上限**：任一层内同一 ``primary_topic`` 不超过该层目标数的
   ``--topic-cap-ratio``（默认 15%）—— 用户明确要求「否则热门方向占满」。
   （池内 Dental 占 22.6%、Photopolymerization 18.1%，不加限会明显偏斜）
3. **只用 TRAIN 侧**：EVAL 侧永不参与 concept 抽取（它只用于对答案）。
4. **只抽有实质摘要的论文**：摘要长度 >= ``--min-abstract-chars``（默认 200）。
   无摘要论文在 P4-1 只能靠标题，会拉低 coverage 且污染概念质量 ——
   本约束把「覆盖率」定义在**可抽取池**上，而不是全视图上。
5. **可复现**：固定 ``--seed``（默认 13，与 R06 一致）；所有排序都带
   tie-breaker（``paper_uid``），不依赖 dict 顺序。
6. ``citations_asof_cutoff`` 为 NULL 的论文（窗口外旧论文，实测 789 篇 /
   14.6%）**不进 Layer A** —— 无法排序就不参与「高影响」这一层的竞争。

用法::

    python tools/build_p4_1a_sample.py                 # dry-run（零写入）
    python tools/build_p4_1a_sample.py --apply
"""
from __future__ import annotations

import argparse
import collections
import datetime
import hashlib
import json
import os
import random
import sqlite3
import statistics
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

DATASET_DIR = BASE / "datasets" / "photopolymerization_v1"
DEFAULT_DB = DATASET_DIR / "paper_meta.db"
VIEW = "v_photopolymerization"

SAMPLE_VERSION = "p4_1a_sample_v1"
DEFAULT_SEED = 13
DEFAULT_ABSTRACT_MIN = 200
DEFAULT_TOPIC_CAP_RATIO = 0.15

# 增速统计的两个基准期（都在 TRAIN 内）
GROWTH_EARLY = (2011, 2015)
GROWTH_LATE = (2016, 2020)
GROWTH_MIN_BASE = 3          # 基准期样本太少的主题不参与增速排名（防小基数爆炸）


def _rel(path) -> str:
    """展示用相对路径（跨盘符安全 —— 本项目已踩过 3 次）。"""
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


def load_view(db_path):
    """读干净视图（只读）。视图本身就是 P4-1 的既定口径，不在此处重写 WHERE。"""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute(f"SELECT * FROM {VIEW}")]
    finally:
        con.close()
    return rows


def topic_growth(train_rows):
    """``primary_topic`` -> 增速（基于 TRAIN 内部的两段年份计数）。

    口径明确写死：g = n(2016-2020) / n(2011-2015)；基准期为 0 或 < GROWTH_MIN_BASE
    的主题不参与排名（记 ``eligible=False``），避免 1/0 类假增长。
    """
    early, late = collections.Counter(), collections.Counter()
    for r in train_rows:
        y, t = r.get("year"), r.get("primary_topic")
        if not t or not isinstance(y, int):
            continue
        if GROWTH_EARLY[0] <= y <= GROWTH_EARLY[1]:
            early[t] += 1
        elif GROWTH_LATE[0] <= y <= GROWTH_LATE[1]:
            late[t] += 1
    out = {}
    for t in set(early) | set(late):
        base, cur = early.get(t, 0), late.get(t, 0)
        ok = base >= GROWTH_MIN_BASE
        out[t] = {
            "n_early": base, "n_late": cur,
            "growth": round(cur / base, 4) if (ok and base) else None,
            "eligible": bool(ok),
        }
    return out


def _take(ranked, n, cap, taken_uids, layer_name):
    """按给定顺序取 n 篇，遵守单主题上限与全局去重。返回 (rows, 被上限挡下的数)。"""
    out, per_topic, blocked = [], collections.Counter(), 0
    for r in ranked:
        if len(out) >= n:
            break
        if r["paper_uid"] in taken_uids:
            continue
        t = r.get("primary_topic") or "(none)"
        if per_topic[t] >= cap:
            blocked += 1
            continue
        out.append(r)
        per_topic[t] += 1
        taken_uids.add(r["paper_uid"])
    return out, blocked


def build_sample(rows, *, seed=DEFAULT_SEED, abstract_min=DEFAULT_ABSTRACT_MIN,
                 n_a=300, n_b=400, n_c=300, cap_ratio=DEFAULT_TOPIC_CAP_RATIO):
    """纯计算：返回 (sampled_records, report)。零写入。"""
    train_all = [r for r in rows if r["split"] == "TRAIN"]
    pool = [r for r in train_all
            if len((r.get("abstract") or "").strip()) >= abstract_min]

    growth = topic_growth(train_all)
    asof_vals = sorted((r.get("citations_asof_cutoff") or 0) for r in pool)
    p25 = asof_vals[int(len(asof_vals) * 0.25)] if asof_vals else 0

    taken = set()
    report = {"pool": {"view_train": len(train_all), "extractable": len(pool),
                       "abstract_min_chars": abstract_min,
                       "p25_citations_asof": p25},
              "growth_basis": {"early": list(GROWTH_EARLY),
                               "late": list(GROWTH_LATE),
                               "min_base": GROWTH_MIN_BASE}}

    # ── Layer A：高影响（按 asof 引用降序；NULL 不参与）──────────────────
    cap_a = max(1, int(round(n_a * cap_ratio)))
    ranked_a = sorted(
        [r for r in pool if r.get("citations_asof_cutoff") is not None],
        key=lambda r: (-r["citations_asof_cutoff"], r["paper_uid"]))
    layer_a, blocked_a = _take(ranked_a, n_a, cap_a, taken, "A")

    # ── Layer B：快速增长主题（限主题内 2016-2020 优先）────────────────
    cap_b = max(1, int(round(n_b * cap_ratio)))
    grow_topics = sorted(
        [t for t, v in growth.items() if v["eligible"] and v["growth"] is not None],
        key=lambda t: (-growth[t]["growth"], t))
    grow_rank = {t: i + 1 for i, t in enumerate(grow_topics)}
    gset = set(grow_topics)

    def b_key(r):
        t = r.get("primary_topic")
        in_window = 0 if (isinstance(r.get("year"), int)
                          and GROWTH_LATE[0] <= r["year"] <= GROWTH_LATE[1]) else 1
        return (grow_rank.get(t, 10 ** 6), in_window,
                -(r.get("citations_asof_cutoff") or 0), r["paper_uid"])

    ranked_b = sorted([r for r in pool if r.get("primary_topic") in gset], key=b_key)
    layer_b, blocked_b = _take(ranked_b, n_b, cap_b, taken, "B")

    # ── Layer C：长尾（低引用 + 固定种子）─────────────────────────────
    cap_c = max(1, int(round(n_c * cap_ratio)))
    lowest = [r for r in pool
              if r["paper_uid"] not in taken
              and (r.get("citations_asof_cutoff") or 0) <= p25]
    # 排序后再打乱：保证「候选集」确定，随机只作用在已确定的集合上
    lowest.sort(key=lambda r: ((r.get("citations_asof_cutoff") or 0),
                               r.get("year") or 0, r["paper_uid"]))
    rng = random.Random(seed)
    shuffled = lowest[:]
    rng.shuffle(shuffled)
    layer_c, blocked_c = _take(shuffled, n_c, cap_c, taken, "C")

    layers = [("A", layer_a, layer_a and layer_a[0]["citations_asof_cutoff"]),
              ("B", layer_b, None), ("C", layer_c, None)]
    records = []
    for name, lrows, _ in layers:
        for rank, r in enumerate(lrows, 1):
            g = growth.get(r.get("primary_topic")) or {}
            records.append({
                "paper_uid": r["paper_uid"],
                "title": r["title"],
                "abstract": r["abstract"],
                "year": r["year"],
                "doi": r["doi"],
                "openalex_id": r["openalex_id"],
                "venue": r["venue"],
                "primary_topic": r["primary_topic"],
                "citations_asof_cutoff": r.get("citations_asof_cutoff"),
                "scope_tier": r.get("scope_tier"),
                "sample_layer": name,
                "layer_rank": rank,
                "topic_growth": g.get("growth"),
                "topic_growth_rank": grow_rank.get(r.get("primary_topic")),
            })

    if not layer_a:
        raise RuntimeError("Layer A 为空 —— 检查 citations_asof_cutoff 是否全为 NULL")

    report["layers"] = {
        "A": {"target": n_a, "taken": len(layer_a), "topic_cap": cap_a,
              "blocked_by_cap": blocked_a, "candidates": len(ranked_a),
              "rank_by": "citations_asof_cutoff desc（禁快照）"},
        "B": {"target": n_b, "taken": len(layer_b), "topic_cap": cap_b,
              "blocked_by_cap": blocked_b, "candidates": len(ranked_b),
              "rank_by": "primary_topic 增速 desc -> 主题内 2016-2020 优先 -> asof 引用",
              "eligible_growth_topics": len(grow_topics)},
        "C": {"target": n_c, "taken": len(layer_c), "topic_cap": cap_c,
              "blocked_by_cap": blocked_c, "candidates": len(lowest),
              "rank_by": f"citations_asof <= p25 内固定种子 seed={seed} 打乱",
              "seed": seed},
    }
    report["total_sampled"] = len(records)
    report["disjoint"] = len({r["paper_uid"] for r in records}) == len(records)

    topic_dist = collections.Counter(r["primary_topic"] for r in records)
    report["topic_concentration"] = {
        "distinct_topics": len(topic_dist),
        "top10": [{"topic": t, "n": n,
                   "share": round(n / max(len(records), 1), 4)}
                  for t, n in topic_dist.most_common(10)],
        "max_share": round(max(topic_dist.values()) / max(len(records), 1), 4)
        if topic_dist else 0,
    }
    yc = collections.Counter()
    for r in records:
        y = r["year"]
        yc["<=2010" if y <= 2010 else "2011-2015" if y <= 2015
           else "2016-2020"] += 1
    report["year_distribution"] = dict(yc)
    report["layer_year_distribution"] = {
        name: dict(collections.Counter(
            ("<=2010" if r["year"] <= 2010 else "2011-2015" if r["year"] <= 2015
             else "2016-2020") for r in lrows))
        for name, lrows, _ in layers}
    report["top_growth_topics"] = [
        {"topic": t, "growth": growth[t]["growth"], "n_early": growth[t]["n_early"],
         "n_late": growth[t]["n_late"], "papers_in_sample": topic_dist.get(t, 0)}
        for t in grow_topics[:10]]
    return records, report


def write_outputs(records, report, out_jsonl, out_manifest, db_path):
    with open(out_jsonl, "w", encoding="utf-8", newline="\n") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    manifest = {
        "sample_version": SAMPLE_VERSION,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "source_db": _rel(db_path),
        "source_view": VIEW,
        "source_db_sha256": _sha256(db_path),
        "sample_jsonl": os.path.basename(out_jsonl),
        "sample_sha256": _sha256(out_jsonl),
        "n": len(records),
        "builder": "tools/build_p4_1a_sample.py",
        "report": report,
    }
    with open(out_manifest, "w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return manifest


def print_summary(report):
    print("═" * 70)
    print(f"  P4-1A 分层采样  [{SAMPLE_VERSION}]")
    print("═" * 70)
    p = report["pool"]
    print(f"  视图 TRAIN {p['view_train']} -> 可抽池 {p['extractable']}"
          f"（摘要 >= {p['abstract_min_chars']} 字符；p25 asof 引用 = {p['p25_citations_asof']}）")
    print("─" * 70)
    for name in ("A", "B", "C"):
        L = report["layers"][name]
        print(f"  Layer {name}: {L['taken']:>4}/{L['target']:<4} "
              f"候选 {L['candidates']:<5} 主题上限 {L['topic_cap']:<3} "
              f"被上限挡 {L['blocked_by_cap']}")
        print(f"          {L['rank_by']}")
    print("─" * 70)
    tc = report["topic_concentration"]
    print(f"  合计 {report['total_sampled']} | 互斥={report['disjoint']} | "
          f"主题数 {tc['distinct_topics']} | 最大单主题占比 {tc['max_share']:.1%}")
    print(f"  年份分布 {report['year_distribution']}")
    print("  各层年份分布:")
    for name, d in report["layer_year_distribution"].items():
        print(f"    {name}: {d}")
    print("  抽样覆盖的增速 Top 主题:")
    for t in report["top_growth_topics"][:6]:
        print(f"    x{t['growth']:<7} {str(t['topic'])[:44]:<46} "
              f"(early {t['n_early']} -> late {t['n_late']}, 抽 {t['papers_in_sample']})")
    print("  主题集中度 Top5:")
    for e in tc["top10"][:5]:
        print(f"    {e['n']:>3} ({e['share']:.1%}) {str(e['topic'])[:50]}")
    print("═" * 70)


def main(argv=None):
    ap = argparse.ArgumentParser(description="P4-1A 分层采样（1000 篇）")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--out", default=str(DATASET_DIR / "sample_v1.jsonl"))
    ap.add_argument("--manifest", default=str(DATASET_DIR / "sample_v1_manifest.json"))
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--min-abstract-chars", type=int, default=DEFAULT_ABSTRACT_MIN)
    ap.add_argument("--target-a", type=int, default=300)
    ap.add_argument("--target-b", type=int, default=400)
    ap.add_argument("--target-c", type=int, default=300)
    ap.add_argument("--topic-cap-ratio", type=float, default=DEFAULT_TOPIC_CAP_RATIO)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)

    print(f"[p4.1a] {SAMPLE_VERSION}  db={_rel(args.db)}  "
          f"seed={args.seed}  mode={'APPLY' if args.apply else 'DRY-RUN'}")
    rows = load_view(args.db)
    records, report = build_sample(
        rows, seed=args.seed, abstract_min=args.min_abstract_chars,
        n_a=args.target_a, n_b=args.target_b, n_c=args.target_c,
        cap_ratio=args.topic_cap_ratio)
    print_summary(report)

    if not args.apply:
        print("\n[DRY-RUN] 零写入。加 --apply 落盘。")
        return 0
    m = write_outputs(records, report, args.out, args.manifest, args.db)
    print(f"\n[applied] {_rel(args.out)}  n={m['n']}  sha256={m['sample_sha256'][:16]}…")
    print(f"[applied] {_rel(args.manifest)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
