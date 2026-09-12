# -*- coding: utf-8 -*-
"""把 LLM 的桥质量判定接成**产品过滤层**：产出候选短名单。

为什么单独一个工具，而不是改 `evaluate_edge_event.py`：
预测器是**冻结**资产（产物被 P4-3 认证过），产品过滤层是它的**消费者**。
把过滤逻辑塞回预测器会让"候选是如何产生的"与"候选是如何被挑选的"混成一件事，
而这正是本项目反复踩过的坑（评估与排序混在一起，见 `evaluate_in_tree` 的教训）。

三条口径必须写死在产物里：

1. **过滤只在已分析的窗口内有效**。桥质量判定按 AA 排名前 N 条做的，
   窗口外的候选**没有判定** —— 它们不是"被拒绝"，是"未评估"。产物显式给出
   `not_analyzed` 计数，不许把它读成"质量不合格"。
2. **产品列不含任何评估期字段**。`eval_formed` / `joint_fut` / `rate_ratio` 一律
   不进 product 列；若要看效果，只在独立标注為 eval-only 的 `evaluation` 块里看。
3. **同源性硬校验**：解释产物必须认证同一份候选文件（条数 + sha256），否则拒绝。

用法：
    python tools/build_edge_shortlist.py                  # dry-run：打印短名单与统计
    python tools/build_edge_shortlist.py --apply          # 落 json + csv
    python tools/build_edge_shortlist.py --keep strong,unclear   # 自定义保留档
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

DATASET = os.path.join(BASE, "datasets", "photopolymerization_v1")
CANDIDATES = os.path.join(DATASET, "edge_new_link_candidates_v2.json")
ANALYSIS = os.path.join(DATASET, "edge_candidate_analysis_v1.json")
OUT_JSON = os.path.join(DATASET, "edge_shortlist_v1.json")
OUT_CSV = os.path.join(DATASET, "edge_shortlist_v1.csv")

# 产品列：**只有这些**字段允许出现在 shortlist 的候选条目里
PRODUCT_FIELDS = ("rank", "a", "b", "type_a", "type_b", "home_a", "home_b",
                  "adamic_adar", "common_neighbors", "bridge_quality",
                  "uncertainty", "mechanism", "falsifiable_check")

# 评估期字段：出现即说明产品列被污染
EVAL_ONLY_RE = ("eval", "joint_fut", "joint_hist", "formed", "rate_ratio")


def _rel(p):
    try:
        return os.path.relpath(p)
    except ValueError:
        return p


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def load_inputs(cand_path=CANDIDATES, ana_path=ANALYSIS):
    """载入并对齐两个产物。对齐按 **位置 + cand_id 双校验**（错位是静默的）。"""
    cand = json.loads(open(cand_path, encoding="utf-8").read())
    ana = json.loads(open(ana_path, encoding="utf-8").read())
    cands = list(cand.get("candidates") or [])
    rows = list(ana.get("analyses") or [])
    if not rows:
        raise SystemExit("[refused] 解释产物为空：%s" % _rel(ana_path))
    if len(rows) > len(cands):
        raise SystemExit("[refused] 解释产物 %d 条 > 候选 %d 条 —— 不同源，"
                         "按位置对齐会得到系统性错误" % (len(rows), len(cands)))
    rec = (ana.get("inputs") or {}).get("candidates_sha256")
    cur = _sha256(cand_path)
    if rec and rec != cur:
        raise SystemExit("[refused] 解释产物认证的候选文件已变（%s… != 磁盘 %s…）"
                         % (rec[:16], cur[:16]))
    for i, r in enumerate(rows):
        if r.get("cand_id") != "E%04d" % (i + 1):
            raise SystemExit("[refused] cand_id 与位置不一致：第 %d 条是 %s"
                             % (i + 1, r.get("cand_id")))
    return cand, cands, rows, cur


def build(cand, cands, rows, keep=("strong",), top_n=None):
    """产出短名单。`keep` 是允许进入产品列表的桥质量档位。"""
    n_analyzed = len(rows)
    shortlist, excluded = [], []
    for i, r in enumerate(rows):
        row = cands[i]
        a = r.get("analysis") or {}
        q = a.get("bridge_quality")
        item = {
            "a": row.get("a"), "b": row.get("b"),
            "type_a": row.get("type_a"), "type_b": row.get("type_b"),
            "home_a": row.get("home_a"), "home_b": row.get("home_b"),
            "adamic_adar": row.get("adamic_adar"),
            "common_neighbors": row.get("common_neighbors"),
            "bridge_quality": q,
            "uncertainty": a.get("uncertainty"),
            "mechanism": (a.get("shared_structure") or "")[:300],
            "falsifiable_check": (a.get("falsifiable_checks") or [None])[0],
        }
        item["_aa_rank"] = i + 1
        item["_fut"] = int(row.get("joint_fut") or 0)     # 仅内部用于 eval 块，不进产品列
        (shortlist if q in keep else excluded).append(item)

    # 先排序再截断，评估块必须按**截断后**的名单算（否则分子分母不同源）
    shortlist.sort(key=lambda x: -(x["adamic_adar"] or 0))
    if top_n:
        shortlist = shortlist[:top_n]
    for i, item in enumerate(shortlist, 1):
        item["rank"] = i

    eval_block = {}
    for key, pred in (("eval_formed", lambda fut: fut >= 5),
                      ("eval_any", lambda fut: fut >= 1)):
        n = len(shortlist)
        hit = sum(1 for x in shortlist if pred(x["_fut"]))
        base = sum(1 for i in range(n_analyzed) if (cands[i].get("joint_fut") or 0) >=
                   (5 if key == "eval_formed" else 1))
        eval_block[key] = {
            "shortlist_n": n,
            "shortlist_hit_rate": round(hit / n, 4) if n else None,
            "window_base_rate": round(base / n_analyzed, 4),
            "note": "eval-only：用 2021-2025 的未来标签算的，**不得**作为产品输入",
        }

    prod = [{k: it.get(k) for k in PRODUCT_FIELDS} for it in shortlist]
    bad = sorted({k for it in prod for k in it
                  if any(t in str(k).lower() for t in EVAL_ONLY_RE)})
    if bad:
        raise SystemExit("[refused] 产品列出现评估期字段：%s" % bad)

    return {
        "predictor": "edge_shortlist_v1",
        "brief": ("按 AA 排序、并只保留 LLM 判为 %s 的新边候选；"
                  "桥质量判定来自 <=2015 证据，不含未来信息" % "/".join(keep)),
        "keep": list(keep),
        "windowing": {
            "analyzed_window": n_analyzed,
            "n_candidates_total": len(cands),
            "not_analyzed": len(cands) - n_analyzed,
            "note": ("过滤只在已分析窗口内有效；窗口外的候选是**未评估**，"
                     "不是质量不合格"),
        },
        "counts": {
            "shortlist": len(prod),
            "excluded_by_bridge": len(excluded),
            "excluded_by_quality": dict(collections.Counter(
                x["bridge_quality"] for x in excluded)),
        },
        "evaluation": eval_block,
        "shortlist": prod,
        "excluded": [{"aa_rank": x["_aa_rank"], "a": x["a"], "b": x["b"],
                      "bridge_quality": x["bridge_quality"],
                      "adamic_adar": x["adamic_adar"]} for x in excluded],
    }


def write_csv(path, prod):
    cols = list(PRODUCT_FIELDS)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(",".join(cols) + "\n")
        for it in prod:
            cells = []
            for c in cols:
                v = it.get(c)
                s = "" if v is None else str(v)
                cells.append('"%s"' % s.replace('"', '""'))
            f.write(",".join(cells) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="落 json + csv（默认 dry-run）")
    ap.add_argument("--keep", default="strong",
                    help="允许进入短名单的桥质量档（逗号分隔），默认 strong")
    ap.add_argument("--top-n", type=int, default=None)
    ap.add_argument("--out", default=OUT_JSON)
    ap.add_argument("--csv", default=OUT_CSV)
    args = ap.parse_args(argv)

    keep = tuple(x.strip() for x in args.keep.split(",") if x.strip())
    cand, cands, rows, sha = load_inputs()
    rep = build(cand, cands, rows, keep=keep, top_n=args.top_n)
    rep["inputs"] = {
        "candidates_file": _rel(CANDIDATES), "candidates_sha256": sha,
        "analysis_file": _rel(ANALYSIS), "analysis_sha256": _sha256(ANALYSIS),
        "tool_file": _rel(os.path.abspath(__file__)),
        "tool_sha256": _sha256(os.path.abspath(__file__)),
    }
    w = rep["windowing"]
    c = rep["counts"]
    print("=" * 78)
    print("  边层候选短名单（产品过滤层）｜ 保留 %s" % "/".join(keep))
    print("  已分析窗口 %d 条 ｜ 候选总数 %d ｜ **未评估 %d**（不是不合格）"
          % (w["analyzed_window"], w["n_candidates_total"], w["not_analyzed"]))
    print("  短名单 %d 条 ｜ 被桥质量剔除 %d 条 %s"
          % (c["shortlist"], c["excluded_by_bridge"], c["excluded_by_quality"]))
    for k, v in rep["evaluation"].items():
        print("  [eval-only] %-12s 短名单命中 %.1f%% vs 窗口基线 %.1f%%"
              % (k, 100 * v["shortlist_hit_rate"], 100 * v["window_base_rate"]))
    print("  短名单 Top-10：")
    for it in rep["shortlist"][:10]:
        print("   #%-3d AA=%-7.3f %-30s x %-30s 桥=%s u=%s"
              % (it["rank"], it["adamic_adar"] or 0, (it["a"] or "")[:30],
                 (it["b"] or "")[:30], it["bridge_quality"], it["uncertainty"]))
    if not args.apply:
        print("\n[DRY-RUN] 未写文件。加 --apply 落盘")
        return 0
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    write_csv(args.csv, rep["shortlist"])
    print("\n[ok] 短名单 -> %s（%d 条）\n[ok] 表 -> %s"
          % (_rel(args.out), len(rep["shortlist"]), _rel(args.csv)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
