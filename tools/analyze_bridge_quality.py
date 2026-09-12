# -*- coding: utf-8 -*-
"""`bridge_quality` 到底有没有用？（LLM 判定 → 回接候选面）

用户 2026-09-13 的建议：模型识别弱桥的能力目前「只被读了，还没被用」。
本工具回答两个可以分开的问题：

  A **构念效度**：LLM 说的 `weak` 桥，是不是就是我们已知的那个病理
    ——共享邻居里混进了表征手段（FTIR/DMA/DSC）？若是，词面正则可以廉价自动化；
    若不是，说明 LLM 提供了正则之外的信息。
    度量：共享邻居的「表征手段占比」vs `bridge_quality` 三档。

  B **效用**：把 `weak/unclear` 从 AA 排序里剔掉，命中率会不会上升？
    度量：同一条 Top-K 内的命中率（滤前 / 滤后）+ 同规模随机带。

⚠️ 口径声明（必须随结果一起报）：
  这里用的是 EVAL 侧标签（`eval_formed`），而 AA 排序**已经**在 EVAL 上评过一次
  （Top-20 2.22×）。同一次评估重看第二遍就是多重比较 —— 所以本工具的结果是
  **探索性**的，报 Bonferroni 校正后的随机带，且**不得**当作新的确证结论。

用法：
    python tools/analyze_bridge_quality.py            # 只读
    python tools/analyze_bridge_quality.py --json P   # 另存报告
"""
from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import math
import os
import random
import statistics
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tools"))

DATASET = os.path.join(BASE, "datasets", "photopolymerization_v1")
CANDIDATES = os.path.join(DATASET, "edge_new_link_candidates_v2.json")
ANALYSIS = os.path.join(DATASET, "edge_candidate_analysis_v1.json")
OUT_PATH = os.path.join(DATASET, "bridge_quality_effect_v1.json")

LABEL_STRICT = 5          # eval_lex>=5（与 edge_event_spec 的 primary_tightness 一致）
SEED = 13

# 三档的"离可用有多远"顺序（**必须显式，且与打印顺序一致**）。
#   strong  = 模型明确指出机制                        -> 0
#   unclear = 模型判断不了（离 strong 只差一步）        -> 1
#   weak    = 模型明确指出邻居是通用词（离可用最远）    -> 2
# 注意这是**约定**不是物理量：三档是名义类别，把它当序数本来就有代价，
# 所以顺序必须写在产物里，不能靠读者猜。
QUALITY_ORDER = {"strong": 0, "unclear": 1, "weak": 2}


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(BASE, "tools", filename))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_rows():
    """把「算法候选」与「LLM 解释」按 cand_id 对齐。"""
    dec = _load("dec_for_bq", "discover_emergence_candidates.py")
    cand = json.load(open(CANDIDATES, encoding="utf-8"))
    ana = json.load(open(ANALYSIS, encoding="utf-8"))
    cands = list(cand.get("candidates") or [])
    n_ana = len(ana.get("analyses") or [])
    if n_ana > len(cands):
        # 静默错位是最危险的一类失败：解释产物的 cand_id 是 AA 排名，候选文件重排过就全错
        raise SystemExit(
            "[refused] 解释产物有 %d 条，候选文件只有 %d 条 —— 两者不同源，"
            "按位置对齐会得到系统性错误的结论" % (n_ana, len(cands)))
    if ana.get("inputs", {}).get("candidates_sha256"):
        import hashlib
        cur = hashlib.sha256(open(CANDIDATES, "rb").read()).hexdigest()
        if cur != ana["inputs"]["candidates_sha256"]:
            raise SystemExit(
                "[refused] 解释产物认证的候选文件已变（%s… != 磁盘 %s…）—— "
                "二者不再对应" % (ana["inputs"]["candidates_sha256"][:16], cur[:16]))
    rows = cands[:n_ana]
    out = []
    for row, an in zip(rows, ana["analyses"]):
        a = an.get("analysis") or {}
        neighbours = an.get("payload_ref", {}).get("shared_neighbors") or []
        ml = [n for n in neighbours if dec.is_method_like(n)]
        out.append({
            "cand_id": an["cand_id"],
            "a": row["a"], "b": row["b"],
            "adamic_adar": row.get("adamic_adar"),
            "common_neighbors": float(row.get("common_neighbors") or 0),
            "shared_neighbors": neighbours,
            "method_like_share": round(len(ml) / len(neighbours), 3) if neighbours else None,
            "bridge_quality": a.get("bridge_quality"),
            "uncertainty": a.get("uncertainty"),
            "groundedness": a.get("groundedness"),
            "n_evidence": len(an.get("payload_ref", {}).get("evidence_uids") or []),
            "formed": bool(row.get("joint_fut", 0) >= LABEL_STRICT),
        })
    return out, dec


def auc(pos, neg):
    n1, n0 = len(pos), len(neg)
    if n1 < 5 or n0 < 5:
        return None
    w = sum(1.0 if a > b else (0.5 if a == b else 0.0) for a in pos for b in neg)
    return w / (n1 * n0)


# ── A 构念效度 ──────────────────────────────────────────────────────────
def construct_validity(rows):
    by = collections.defaultdict(list)
    for r in rows:
        if r["bridge_quality"] and r["method_like_share"] is not None:
            by[r["bridge_quality"]].append(r["method_like_share"])
    table = {k: {"n": len(v), "method_like_share_median": round(statistics.median(v), 3),
                 "mean": round(statistics.mean(v), 3)} for k, v in sorted(by.items())}
    pairs = [(QUALITY_ORDER[r["bridge_quality"]], r["method_like_share"]) for r in rows
             if r["bridge_quality"] in QUALITY_ORDER
             and r["method_like_share"] is not None]
    # Spearman（排名相关）：bridge_quality 越差 -> 表征手段占比应当越高
    rho = None
    if len(pairs) >= 8:
        x = _rank([a for a, _ in pairs])
        y = _rank([b for _, b in pairs])
        rho = round(_pearson(x, y), 3)
    return {"by_bridge_quality": table, "spearman_rho_quality_vs_method_share": rho,
            "quality_order_convention": QUALITY_ORDER, "n": len(pairs)}


def _rank(v):
    order = sorted(range(len(v)), key=lambda i: v[i])
    out = [0.0] * len(v)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            out[order[k]] = avg
        i = j + 1
    return out


def _pearson(x, y):
    n = len(x)
    mx, my = sum(x) / n, sum(y) / n
    num = sum((a - mx) * (b - my) for a, b in zip(x, y))
    den = math.sqrt(sum((a - mx) ** 2 for a in x) * sum((b - my) ** 2 for b in y))
    return num / den if den else 0.0


# ── B 效用（探索性）─────────────────────────────────────────────────────
def topk_hit(rows, k):
    sub = sorted(rows, key=lambda r: -(r["adamic_adar"] or 0))[:k]
    if not sub:
        return None
    return sum(1 for r in sub if r["formed"]) / len(sub)


def confound_check(rows):
    """`unclear` 是不是只是结构量的代理？（避免把"重读 AA"说成"LLM 加了信息"）

    对每一档报 common_neighbors / adamic_adar / 证据条数的中位数，
    并单独给 common_neighbors 与 AA 在池内的 AUC 作对照。
    """
    out = {}
    for q in sorted(QUALITY_ORDER, key=lambda x: QUALITY_ORDER[x]):
        sub = [r for r in rows if r["bridge_quality"] == q]
        if not sub:
            continue
        out[q] = {
            "n": len(sub),
            "cn_median": round(statistics.median([r["common_neighbors"] for r in sub]), 2),
            "aa_median": round(statistics.median([r["adamic_adar"] for r in sub]), 3),
            "ev_median": round(statistics.median([r["n_evidence"] for r in sub]), 1),
        }
    out["_pool"] = {
        "cn_median": round(statistics.median([r["common_neighbors"] for r in rows]), 2),
        "aa_median": round(statistics.median([r["adamic_adar"] for r in rows]), 3),
    }
    pos = [r for r in rows if r["formed"]]
    neg = [r for r in rows if not r["formed"]]
    out["_auc_in_pool"] = {
        "common_neighbors": round(auc([r["common_neighbors"] for r in pos],
                                      [r["common_neighbors"] for r in neg]) or 0, 3),
        "adamic_adar": round(auc([r["adamic_adar"] for r in pos],
                                 [r["adamic_adar"] for r in neg]) or 0, 3),
    }
    return out


def _wilson(k, n, z=1.96):
    """Wilson 95% 区间。小样本点估计必须配区间 —— 否则 11/20 与 69% 的差会被当真。"""
    if n == 0:
        return None
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def _two_prop_z(k1, n1, k2, n2):
    """两个比例的 z 检验（pooled）。返回 (z, p_two_sided)。"""
    if not (n1 and n2):
        return None, None
    p1, p2 = k1 / n1, k2 / n2
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return None, None
    z = (p1 - p2) / se
    pv = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))
    return round(z, 3), round(pv, 4)


def aa_matched(rows, target="weak", window=0.15):
    """AA 匹配对照：`weak` 的 AA 中位本来就比 `strong` 低，命中率差可能只是结构差。

    做法：对每个目标档候选，取 AA 相对差在 ±window 内的所有非目标档候选作对照，
    报两组命中率与双比例检验。这是"度数带匹配"在 AA 上的对应做法。
    """
    sub = [r for r in rows if r["bridge_quality"] == target]
    if not sub:
        return {"target": target, "n": 0}
    ctrl = [r for r in rows if r["bridge_quality"] != target
            and any(abs((r["adamic_adar"] or 0) - (s["adamic_adar"] or 0))
                    <= window * max(s["adamic_adar"] or 1, 1) for s in sub)]
    k1 = sum(1 for r in sub if r["formed"])
    k2 = sum(1 for r in ctrl if r["formed"])
    z, p = _two_prop_z(k1, len(sub), k2, len(ctrl))
    return {"target": target, "aa_window": window,
            "target_n": len(sub), "target_hit": round(k1 / len(sub), 4),
            "matched_control_n": len(ctrl),
            "matched_control_hit": round(k2 / len(ctrl), 4) if ctrl else None,
            "delta_pp": round(100 * (k1 / len(sub) - k2 / len(ctrl)), 1) if ctrl else None,
            "z": z, "p": p}


def utility(rows, k_list=(20, 50), draws=20000):
    rng = random.Random(SEED)
    pool = rows
    base = sum(1 for r in pool if r["formed"]) / len(pool)
    by_q = {}
    for q in sorted(QUALITY_ORDER, key=lambda x: QUALITY_ORDER[x]):
        sub = [r for r in pool if r["bridge_quality"] == q]
        k = sum(1 for r in sub if r["formed"])
        by_q[q] = {"n": len(sub), "hits": k,
                   "hit_rate": round(k / len(sub), 4) if sub else None,
                   "wilson95": _wilson(k, len(sub)) if sub else None}
    strong = by_q.get("strong") or {}
    for q in ("unclear", "weak"):
        v = by_q.get(q)
        if not v or not v["n"] or not strong.get("n"):
            continue
        z, pv = _two_prop_z(v["hits"], v["n"], strong["hits"], strong["n"])
        v["vs_strong_z"], v["vs_strong_p"] = z, pv
    unc_pos = [-r["uncertainty"] for r in pool
               if r["formed"] and r["uncertainty"] is not None]
    unc_neg = [-r["uncertainty"] for r in pool
               if not r["formed"] and r["uncertainty"] is not None]
    out = {"pool_n": len(pool), "pool_base_rate": round(base, 4),
           "n_bad_bridge": sum(1 for r in pool
                               if r["bridge_quality"] in ("weak", "unclear")),
           "hit_rate_by_bridge_quality": by_q,
           "auc_minus_uncertainty": (round(auc(unc_pos, unc_neg), 3)
                                     if auc(unc_pos, unc_neg) is not None else None),
           "n_uncertainty": len(unc_pos) + len(unc_neg),
           "topk": {}}
    for k in k_list:
        if k > len(pool):
            continue
        raw = topk_hit(pool, k)
        good = [r for r in pool if r["bridge_quality"] == "strong"]
        filt = topk_hit(good, k)
        hs = sorted(sum(1 for r in rng.sample(pool, k) if r["formed"]) / k
                    for _ in range(draws))
        out["topk"]["top%d" % k] = {
            "raw_hit": round(raw, 4), "raw_lift": round(raw / base, 3) if base else None,
            "strong_only_hit": (round(filt, 4) if filt is not None else None),
            "strong_only_lift": (round(filt / base, 3) if filt is not None and base else None),
            "strong_only_n": len(good),
            "random_band": {"lo95": round(hs[int(0.025 * draws)], 4),
                            "median": round(hs[draws // 2], 4),
                            "hi95": round(hs[int(0.975 * draws)], 4)},
        }
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", dest="json_out", default=None)
    args = ap.parse_args(argv)
    rows, dec = load_rows()
    print("=" * 78)
    print("  bridge_quality 回接：构念效度 + 效用（探索性，非确证）")
    print("  已解释候选 %d 条（AA 排名前 %d）｜ 标签 eval_lex>=%d"
          % (len(rows), len(rows), LABEL_STRICT))
    cv = construct_validity(rows)
    print("\n[A 构念效度] LLM 说的弱桥，是不是就是「共享邻居里混了表征手段」？")
    for k, v in cv["by_bridge_quality"].items():
        print("   %-8s n=%-4d 表征手段占比 中位=%.3f 均值=%.3f"
              % (k, v["n"], v["method_like_share_median"], v["mean"]))
    print("   Spearman rho（质量档位 vs 表征手段占比）= %s（正=越弱越像表征手段）"
          % cv["spearman_rho_quality_vs_method_share"])
    print("   顺序约定 %s（名义类别，当序数用只是约定 —— 写进产物以免读者猜）"
          % json.dumps(cv["quality_order_convention"], ensure_ascii=False))
    print("   [注] 我们已知 Top-12 大多含 ftir/dma/dsc 类邻居，所以这里预期为正；"
          "正只说明 LLM 与正则看到同一现象")
    cf = confound_check(rows)
    print("\n[A2 混淆项] unclear 是不是只是「结构量」的代理？")
    for q in sorted(QUALITY_ORDER, key=lambda x: QUALITY_ORDER[x]):
        v = cf.get(q)
        if not v:
            continue
        print("      %-8s n=%-4d common_neighbors 中位 %-6s AA 中位 %-6s 证据条数中位 %s"
              % (q, v["n"], v["cn_median"], v["aa_median"], v["ev_median"]))
    print("      池: common_neighbors 中位 %s｜AA 中位 %s"
          % (cf["_pool"]["cn_median"], cf["_pool"]["aa_median"]))
    print("      池内 AUC: common_neighbors %.3f｜adamic_adar %.3f"
          % (cf["_auc_in_pool"]["common_neighbors"], cf["_auc_in_pool"]["adamic_adar"]))
    ut = utility(rows)
    print("\n[B 效用] 池内基线 %.1f%%｜LLM 判为 weak/unclear 的 %d 条"
          % (100 * ut["pool_base_rate"], ut["n_bad_bridge"]))
    print("   按桥质量分档的命中率（这才是「这个字段有没有信息」的直接答案）：")
    for q in sorted(QUALITY_ORDER, key=lambda x: QUALITY_ORDER[x]):
        v = ut["hit_rate_by_bridge_quality"].get(q) or {}
        if not v.get("n"):
            continue
        delta = v["hit_rate"] - ut["pool_base_rate"]
        line = ("      %-8s n=%-4d 命中 %5.1f%%（%d/%d，Wilson95 %.1f%%–%.1f%%）相对池 %+.1f pp"
                % (q, v["n"], 100 * v["hit_rate"], v["hits"], v["n"],
                   100 * v["wilson95"][0], 100 * v["wilson95"][1], 100 * delta))
        if v.get("vs_strong_p") is not None:
            line += "｜ vs strong: z=%.2f p=%.3f" % (v["vs_strong_z"], v["vs_strong_p"])
        print(line)
    print("   uncertainty 判别力：AUC(-uncertainty) = %s（>0.5 = 低不确定更可能成真）｜n=%d"
          % (ut["auc_minus_uncertainty"], ut["n_uncertainty"]))
    for tgt in ("weak", "unclear"):
        m = aa_matched(rows, tgt)
        if not m.get("target_n") or m.get("matched_control_hit") is None:
            continue
        print("   [AA 匹配] %-8s 命中 %.1f%%（n=%d）vs 近 AA 对照 %.1f%%（n=%d）"
              "→ %+.1f pp｜z=%.2f p=%.3f"
              % (tgt, 100 * m["target_hit"], m["target_n"],
                 100 * m["matched_control_hit"], m["matched_control_n"],
                 m["delta_pp"], m["z"], m["p"]))
        ut.setdefault("aa_matched", {})[tgt] = m
    for k, v in ut["topk"].items():
        print("   %s: AA 原样 %.1f%%（%.2fx）→ 只留 strong %.1f%%（%s）｜strong 池 %d"
              % (k, 100 * v["raw_hit"], v["raw_lift"],
                 100 * v["strong_only_hit"] if v["strong_only_hit"] is not None else -1,
                 ("%.2fx" % v["strong_only_lift"]) if v["strong_only_lift"] else "n/a",
                 v["strong_only_n"]))
        print("        随机带 95%% = %.1f%%–%.1f%%"
              % (100 * v["random_band"]["lo95"], 100 * v["random_band"]["hi95"]))
    import hashlib

    def _sha(path):
        return hashlib.sha256(open(path, "rb").read()).hexdigest()

    rep = {"note": ("探索性分析：AA 排序已在 EVAL 上评过一次，本文件是同一测试集的第二遍看，"
                    "故一律报随机带且不得当作新的确证结论。"),
           "inputs": {
               "candidates_file": os.path.relpath(CANDIDATES, BASE),
               "candidates_sha256": _sha(CANDIDATES),
               "analysis_file": os.path.relpath(ANALYSIS, BASE),
               "analysis_sha256": _sha(ANALYSIS),
               "tool_file": os.path.relpath(__file__, BASE),
               "tool_sha256": _sha(__file__),
           },
           "n_analyzed": len(rows), "label": "eval_lex>=%d" % LABEL_STRICT,
           "construct_validity": cv, "confound_check": cf, "utility": ut}
    if args.json_out:
        json.dump(rep, open(args.json_out, "w", encoding="utf-8", newline="\n"),
                  ensure_ascii=False, indent=1)
        print("\n[ok] -> %s" % os.path.relpath(args.json_out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
