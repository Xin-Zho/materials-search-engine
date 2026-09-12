#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""边层事件型评估：预测「哪一对概念会在未来形成新连接」。

预测对象 = **事件**（不是份额）。这是换预测对象后的主线评估器。

── 干净口径（三条硬规则，缺一不可）──────────────────────────────────
  1. 分母/规模：所有"率"都用**该期的总体量**做分母，不用对象自身总量
  2. 各期各归一：每期用自己的规模
  3. **特征与标签不得共享窗口** —— 特征只取 <= CUT 的子图，
     标签只看 CUT 之后的窗口（history 期 / future 期）
  （前两条是 2026-09-12 用 AUC=0.014 的恒等式演示过的教训；第三条同理）

── 任务定义 ────────────────────────────────────────────────────────
  Task N（新边）  候选 = 两端在早期图上从未共现、但共享 >= 1 个邻居
                  标签 = 该对在 future 期形成连接
  Task S（增强）  候选 = 早期图上已共现的对
                  标签 = future 期的关联强于 history 期

── 过滤链（为什么必须可配置而不是写死在代码里）──────────────────────
  实测两轮病理：① 表征手段（FTIR/DMA/DSC）与谁都共现，AA 天然巨大
                ② 近义/嵌套概念（volume vs volumetric shrinkage）"将来一起出现"是语言必然
  两级过滤后提升**没有下降**（2.15x → 2.22x），说明病态确实被剔掉了、信号是真的。
  过滤规则写进 `edge_event_spec.yaml` 并记录 sha256，便于复现与评审。

用法：
    .venv/Scripts/python.exe tools/evaluate_edge_event.py                 # dry-run 报告
    .venv/Scripts/python.exe tools/evaluate_edge_event.py --apply         # 落候选清单
    .venv/Scripts/python.exe tools/evaluate_edge_event.py --profile raw   # 不过滤（诊断用）
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
TOOLS = os.path.join(ROOT, "tools")
DATASET = os.path.join(ROOT, "datasets", "photopolymerization_v1")
SPEC_PATH = os.path.join(DATASET, "edge_event_spec.yaml")
OUT_PATH = os.path.join(DATASET, "edge_new_link_candidates_v2.json")

# 抽取产物：扩树版存在则优先（否则会静默用回 993 篇的 v1，结论完全不可比）
CONCEPTS_FULL = os.path.join(DATASET, "concepts_full_v2.jsonl")
CONCEPTS_V1 = os.path.join(DATASET, "concepts_v1.jsonl")

CUT = 2015                       # 特征窗口右端（含）
HISTORY = (2016, 2020)           # 标签基线窗口
FUTURE = (2021, 2025)            # 标签未来窗口
K_ENDPOINTS = 8                  # 端点在其早期图上的最小支持度
LABEL_TIGHTNESS = (1, 3, 5)      # eval_lex 门槛：松 / 中 / 严

# 过滤档位：raw（不过滤，仅诊断）/ basic（剔表征手段）/ strict（+排近义嵌套+要求跨类型）
PROFILES = ("raw", "basic", "strict")


def _rel(p):
    try:
        return os.path.relpath(p)
    except ValueError:                       # 跨盘符
        return p


def _sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(TOOLS, filename))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── 早期图 ──────────────────────────────────────────────────────────────
def build_early_graph(ok_rows, meta, cut=CUT, k_endpoints=K_ENDPOINTS):
    """只用 <= cut 的论文建图。返回端点集与邻接表。"""
    early = [r for r in ok_rows
             if (meta.get(r["paper_uid"], {}).get("year") or 9999) <= cut]
    sup = collections.Counter()
    topic = collections.defaultdict(collections.Counter)
    pair = collections.Counter()
    for r in early:
        t = meta.get(r["paper_uid"], {}).get("primary_topic")
        ns = sorted({c["name"] for c in (r.get("concepts") or [])
                     if c.get("name") and c.get("type")})
        for n in ns:
            sup[n] += 1
            if t:
                topic[n][t] += 1
        for a, b in itertools.combinations(ns, 2):
            pair[(a, b)] += 1
    nodes = {n for n, c in sup.items() if c >= k_endpoints}
    home = {n: (topic[n].most_common(1)[0][0] if topic[n] else None) for n in nodes}
    adj = collections.defaultdict(set)
    for (a, b) in pair:
        if a in nodes and b in nodes:
            adj[a].add(b)
            adj[b].add(a)
    ctype = {}
    for r in ok_rows:
        for c in (r.get("concepts") or []):
            if c.get("name") and c.get("type"):
                ctype.setdefault(c["name"], c["type"])
    return {"early_papers": len(early), "pairs_early": pair, "sup": sup,
            "nodes": nodes, "home": home, "adj": adj, "type": ctype}


def pair_features(g, a, b):
    """全部只用早期图。"""
    na, nb = g["adj"][a], g["adj"][b]
    inter = na & nb
    union = na | nb
    aa = sum(1.0 / math.log(len(g["adj"][c])) for c in inter if len(g["adj"][c]) > 1)
    return {"common_neighbors": float(len(inter)), "adamic_adar": aa,
            "jaccard": len(inter) / len(union) if union else 0.0,
            "pref_attach": float(len(na) * len(nb)),
            "deg_min": float(min(len(na), len(nb))),
            "deg_max": float(max(len(na), len(nb)))}


# ── 候选与过滤 ──────────────────────────────────────────────────────────
def enumerate_new_edge_candidates(g, cross_domain=True):
    """从未共现、但共享 >= 1 个邻居的概念对。"""
    out = []
    nodes = sorted(g["nodes"])
    for a in nodes:
        for b in nodes:
            if b <= a:
                continue
            if g["pairs_early"].get((a, b)):
                continue
            if not (g["adj"][a] & g["adj"][b]):
                continue
            if cross_domain and g["home"][a] == g["home"][b]:
                continue
            out.append((a, b))
    return out


def apply_filters(g, pairs, profile, d):
    """过滤链。返回 (保留的对, 每级淘汰计数)。"""
    drop = collections.Counter()
    is_ml = d.is_method_like

    def toks(s):
        return {t for t in d._TOK.findall(s.lower()) if t not in d._VARIANT_STOP}

    kept = []
    for a, b in pairs:
        if profile in ("basic", "strict") and (is_ml(a) or is_ml(b)):
            drop["method_like"] += 1
            continue
        if profile == "strict":
            if a in b or b in a:
                drop["substring_nested"] += 1
                continue
            if toks(a) & toks(b):
                drop["shared_token"] += 1
                continue
            if g["type"].get(a) == g["type"].get(b):
                drop["same_type"] += 1
                continue
        kept.append((a, b))
    return kept, drop


# ── 标签（词面，只看 CUT 之后）──────────────────────────────────────────
def build_labels(nodes, rows, history=HISTORY, future=FUTURE):
    """返回 (docset, 期集合大小)。只用 2016+ 的窗口 —— 与特征零共享。"""
    v = _load("val", "validate_emergence_p4_3.py")
    lex = v.LexIndex(rows, history, future)
    ds = {}
    for n in nodes:
        s = lex.docset(n)
        if s is not None:
            ds[n] = s
    return {"ds": ds, "hist": lex.train, "fut": lex.eval,
            "n_hist": len(lex.train), "n_fut": len(lex.eval)}


def pair_counts(lab, a, b):
    da, db = lab["ds"].get(a), lab["ds"].get(b)
    if da is None or db is None:
        return None
    c = da & db
    return len(c & lab["hist"]), len(c & lab["fut"])


def label_of(counts, tightness):
    """标签 = future 期确实形成连接（joint 出现 >= tightness 次）。"""
    if counts is None:
        return None
    _h, f = counts
    return f >= tightness


def rate_ratio(counts, lab):
    if counts is None:
        return None
    h, f = counts
    rh = (h + 0.5) / (lab["n_hist"] + 1)     # 各期各用自己的规模
    rf = (f + 0.5) / (lab["n_fut"] + 1)
    return rf / rh if rh else None


# ── 评估 ────────────────────────────────────────────────────────────────
def auc_p(pos, neg):
    n1, n0 = len(pos), len(neg)
    if n1 < 10 or n0 < 10:
        return None, None, n1, n0
    w = sum(1.0 if a > b else (0.5 if a == b else 0.0) for a in pos for b in neg)
    A = w / (n1 * n0)
    sd = math.sqrt(n1 * n0 * (n1 + n0 + 1) / 12.0)
    z = (w - n1 * n0 / 2.0) / sd
    p = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))
    return A, p, n1, n0


FEATURES = ("adamic_adar", "common_neighbors", "jaccard", "pref_attach",
            "deg_min", "deg_max")


def evaluate(recs, feat, k_list=(20, 50, 100, 200)):
    """recs: [{'feat...', 'label'}...]。返回 AUC/分层 AUC/Top-K。"""
    pos = [r[feat] for r in recs if r["label"]]
    neg = [r[feat] for r in recs if not r["label"]]
    A, p, n1, n0 = auc_p(pos, neg)
    base = n1 / (n1 + n0) if (n1 + n0) else None
    out = {"auc": None if A is None else round(A, 4),
           "p": None if p is None else round(p, 4),
           "n_pos": n1, "n_neg": n0,
           "base_rate": None if base is None else round(base, 4),
           "strata_auc": {}, "topk": {}}
    if base:
        for k in k_list:
            if len(recs) < k:
                continue
            s = sorted(recs, key=lambda r: -r[feat])
            h = sum(1 for r in s[:k] if r["label"]) / k
            out["topk"]["top%d" % k] = {"hits": int(h * k), "rate": round(h, 4),
                                        "lift": round(h / base, 3)}
    # 规模匹配：按 deg_min 三分位分层（排除"只是度数大"）
    if len(recs) >= 30:
        dg = sorted(r["deg_min"] for r in recs)
        q1, q2 = dg[len(dg) // 3], dg[2 * len(dg) // 3]
        for name, lo, hi in (("low", -1, q1), ("mid", q1, q2), ("high", q2, 1e9)):
            sub = [r for r in recs if lo < r["deg_min"] <= hi]
            a2, _p2, _n1, _n0 = auc_p([r[feat] for r in sub if r["label"]],
                                      [r[feat] for r in sub if not r["label"]])
            out["strata_auc"][name] = None if a2 is None else round(a2, 4)
    return out


def random_band(recs, feat, k, draws=2000, seed=13):
    """同规模重抽的随机带 —— 没有它，Top-K 提升无法判断是否只是波动。"""
    rng = random.Random(seed)
    base = sum(1 for r in recs if r["label"]) / len(recs)
    hs = sorted(sum(1 for r in rng.sample(recs, k) if r["label"]) / k
                for _ in range(draws))
    return {"median": round(hs[draws // 2], 4),
            "lo95": round(hs[int(0.025 * draws)], 4),
            "hi95": round(hs[int(0.975 * draws)], 4)}


# ── spec ────────────────────────────────────────────────────────────────
def load_spec(path=SPEC_PATH):
    import yaml
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def check_spec_scope(spec):
    """规范必须写明「唯一目标」，并把 Task S 显式排除在目标之外。

    探针期的临时命名（Task L / Task S）曾混进目标描述里。这条守卫保证
    "只有新边形成事件参与排序"是被声明过的，而不是靠记忆维持。
    """
    if not spec:
        return {"ok": False, "reasons": ["缺少 spec：无法确认目标范围"]}
    tgt = spec.get("target") or {}
    diag = spec.get("diagnostic_only") or {}
    bad = []
    if not tgt.get("sole_target"):
        bad.append("target.sole_target 未声明为 true")
    if "task_strengthen" not in diag:
        bad.append("diagnostic_only.task_strengthen 缺失（Task S 未被显式排除）")
    return {"ok": not bad, "reasons": bad}


def run(args):
    d = _load("dec", "discover_emergence_candidates.py")
    v = _load("val", "validate_emergence_p4_3.py")
    spec = load_spec(args.spec) if args.spec else None
    scope = check_spec_scope(spec)
    if not scope["ok"]:
        raise SystemExit("[refused] 目标范围未声明清楚："
                         + "; ".join(scope["reasons"])
                         + "  —— 先修 spec（target.sole_target / diagnostic_only）")
    concepts = args.concepts
    if not concepts:
        concepts = CONCEPTS_FULL if os.path.exists(CONCEPTS_FULL) else CONCEPTS_V1
    ok_rows, meta = d.load_inputs(d.DB_PATH, concepts)
    concepts_sha = _sha256(concepts)
    print("[edge] 抽取产物 %s (sha256=%s…, %d 篇)"
          % (_rel(concepts), concepts_sha[:16], len(ok_rows)))
    g = build_early_graph(ok_rows, meta, args.cut, args.k_endpoints)
    print("[edge] 论文 %d ｜ 早期(<=%d) %d ｜ 端点(支持>=%d) %d"
          % (len(ok_rows), args.cut, g["early_papers"], args.k_endpoints,
             len(g["nodes"])))

    raw = enumerate_new_edge_candidates(g, cross_domain=args.cross_domain)
    pairs, drop = apply_filters(g, raw, args.profile, d)
    print("[edge] 候选 raw %d → profile=%s 后 %d ｜ 淘汰 %s"
          % (len(raw), args.profile, len(pairs), dict(drop) or "{}"))

    lab = build_labels(g["nodes"], v.load_text_rows(d.DB_PATH))
    recs = []
    for a, b in pairs:
        c = pair_counts(lab, a, b)
        if c is None:
            continue
        row = pair_features(g, a, b)
        row.update({"a": a, "b": b, "home_a": g["home"][a], "home_b": g["home"][b],
                    "type_a": g["type"].get(a), "type_b": g["type"].get(b),
                    "joint_hist": c[0], "joint_fut": c[1],
                    "rate_ratio": rate_ratio(c, lab)})
        recs.append(row)
    print("[edge] 可评估 %d" % len(recs))
    if len(g["nodes"]) < 100:
        print("[WARN] 端点仅 %d 个 —— 抽取产物可能不是扩树版；"
              "本结果与扩树前不可比" % len(g["nodes"]))

    out = {"predictor": "edge_new_link_v2", "profile": args.profile,
           "target": {"type": (spec or {}).get("target", {}).get("type", "EVENT"),
                      "sole_target": True,
                      "statement": (spec or {}).get("target", {}).get("statement")},
           "inputs": {"concepts_file": _rel(concepts),
                      "concepts_sha256": concepts_sha,
                      "db_file": _rel(d.DB_PATH), "n_papers": len(ok_rows),
                      "tool_file": _rel(os.path.abspath(__file__)),
                      "tool_sha256": _sha256(os.path.abspath(__file__))},
           "cut": args.cut, "k_endpoints": args.k_endpoints,
           "history": list(HISTORY), "future": list(FUTURE),
           "cross_domain_only": bool(args.cross_domain),
           "n_raw": len(raw), "n_candidates": len(recs), "dropped": dict(drop),
           "evaluation": {}, "random_band": {}}

    for t in LABEL_TIGHTNESS:
        sub = []
        for r in recs:
            lb = label_of((r["joint_hist"], r["joint_fut"]), t)
            if lb is None:
                continue
            sub.append(dict(r, label=lb))
        key = "eval_lex>=%d" % t
        out["evaluation"][key] = {"n": len(sub),
                                  "features": {f: evaluate(sub, f) for f in FEATURES}}
        best = max(FEATURES, key=lambda f: out["evaluation"][key]["features"][f]["auc"] or 0)
        out["evaluation"][key]["best_feature"] = best
        if len(sub) >= 1000:
            out["random_band"][key] = {f: {("top%d" % k): random_band(sub, f, k)
                                           for k in (20, 50)}
                                       for f in ("adamic_adar",)}

    # 任务 S（已共现 → 增强）仅作对照
    pairsS = sorted({(a, b) for (a, b) in g["pairs_early"]
                     if a in g["nodes"] and b in g["nodes"] and a < b})
    recS = []
    for a, b in pairsS:
        c = pair_counts(lab, a, b)
        if c is None:
            continue
        ok = rate_ratio(c, lab)
        if ok is None:
            continue
        row = pair_features(g, a, b)
        row.update({"label": ok > 1.0, "rate_ratio": ok})
        recS.append(row)
    if recS:
        blk = {"role": "diagnostic_control", "diagnostic_only": True,
               "question": "已共现的概念对，未来关联强度会不会上升",
               "n": len(recS), "features": {f: evaluate(recS, f) for f in FEATURES}}
        aucs = [e["auc"] for e in blk["features"].values() if e["auc"] is not None]
        blk["verdict"] = "REVERSED" if (aucs and max(aucs) < 0.5) else "NOT_REVERSED"
        blk["note"] = "不参与候选排序。判定若从 REVERSED 变化，必须在报告里显式说明。"
        out["task_S_strengthen"] = blk

    out["candidates"] = sorted(
        [dict(r, eval_formed=bool(r["joint_fut"] >= 5)) for r in recs],
        key=lambda r: (-r["adamic_adar"], -r["common_neighbors"]))
    for i, r in enumerate(out["candidates"], 1):
        r["pred_rank"] = i
    if args.spec:
        out["spec_file"] = _rel(args.spec)
        out["spec_sha256"] = _sha256(args.spec)
    return out, d


def print_report(rep):
    print("\n%s\n边层事件型评估 ｜ profile=%s ｜ 特征窗口 <=%d ｜ 标签 %s|%s\n%s"
          % ("=" * 74, rep["profile"], rep["cut"], rep["history"], rep["future"],
             "=" * 74))
    for key, blk in rep["evaluation"].items():
        base = None
        for f in FEATURES:
            e = blk["features"][f]
            if e["base_rate"] is not None:
                base = e["base_rate"]
        print("\n[%s] n=%d ｜ 基线 %.2f%% ｜ 最优特征 %s"
              % (key, blk["n"], 100 * (base or 0), blk["best_feature"]))
        print("   %-18s %7s %9s %9s %-26s" % ("特征", "AUC", "p", "分层内", "Top-K 提升"))
        for f in FEATURES:
            e = blk["features"][f]
            if e["auc"] is None:
                continue
            st = "/".join(str(e["strata_auc"].get(s)) for s in ("low", "mid", "high"))
            tk = "  ".join("%s %.2fx" % (k.replace("top", "T"), v["lift"])
                           for k, v in e["topk"].items() if k in ("top20", "top50"))
            mark = "★" if (e["auc"] > 0.55 and e["p"] < 0.05) else (
                "▼" if e["auc"] < 0.45 else " ")
            print("   %-18s %7.3f %9.4f %-26s %s %s"
                  % (f, e["auc"], e["p"], st, tk, mark))
    for key, b in rep.get("random_band", {}).items():
        for f, kk in b.items():
            for k, v in kk.items():
                print("   [随机带] %s %s %s: 中位 %.1f%% 95%%区间 %.1f%%-%.1f%%"
                      % (key, f, k, 100 * v["median"], 100 * v["lo95"], 100 * v["hi95"]))
    if rep.get("task_S_strengthen"):
        print("\n[诊断对照 Task S 关联增强 | diagnostic_only=True | 不参与排序] n=%d verdict=%s" % (rep["task_S_strengthen"]["n"], rep["task_S_strengthen"].get("verdict")))
        for f in FEATURES:
            e = rep["task_S_strengthen"]["features"][f]
            if e["auc"] is None:
                continue
            print("   %-18s AUC=%.3f p=%.4f" % (f, e["auc"], e["p"]))
    print("\n候选 Top-10")
    for r in rep["candidates"][:10]:
        print("   #%-3d AA=%-7.3f cn=%-3d %-28s[%s] x %-28s[%s] %s"
              % (r["pred_rank"], r["adamic_adar"], int(r["common_neighbors"]),
                 r["a"][:28], (r["type_a"] or "")[:10], r["b"][:28],
                 (r["type_b"] or "")[:10], "已连上" if r["eval_formed"] else ""))


def to_abs(rel):
    if not rel:
        return None
    return rel if os.path.isabs(rel) else os.path.join(ROOT, rel)


def verify(out_path):
    """拿产物反过来核对磁盘：产物说「我基于 X」，就必须能验证 X 还在、还没变。

    2026-09-12 实测过一次脱钩（报告认证了一份已不存在的候选文件，时间差 32 秒）。
    这条不是洁癖：**验证器必须为自己认证的文件把关**，否则证据链会静默断裂。
    """
    if not os.path.exists(out_path):
        return {"ok": False, "reasons": ["产物不存在: %s" % _rel(out_path)], "checks": []}
    with open(out_path, encoding="utf-8") as f:
        rep = json.load(f)
    inp = rep.get("inputs") or {}
    checks = []

    def _chk(field, rel, recorded):
        path = to_abs(rel)
        if path is None:
            checks.append({"field": field, "ok": False,
                           "reason": "产物未记录该输入的路径"})
            return
        if not os.path.exists(path):
            checks.append({"field": field, "ok": False, "file": _rel(path),
                           "recorded": (recorded or "")[:16], "current": "MISSING",
                           "reason": "产物认证的文件已不存在"})
            return
        cur = _sha256(path)
        checks.append({"field": field, "ok": cur == recorded, "file": _rel(path),
                       "recorded": (recorded or "未记录")[:16], "current": cur[:16]})

    _chk("inputs.concepts_sha256", inp.get("concepts_file"), inp.get("concepts_sha256"))
    _chk("spec_sha256", rep.get("spec_file"), rep.get("spec_sha256"))
    _chk("inputs.tool_sha256", inp.get("tool_file"), inp.get("tool_sha256"))
    bad = [c for c in checks if not c["ok"]]
    return {"ok": not bad, "checks": checks,
            "reasons": [(c.get("field", "") + ": " + c.get("reason",
                        "sha 不符（recorded %s / 磁盘 %s）"
                        % (c.get("recorded"), c.get("current")))) for c in bad]}


def print_verify(res, out_path):
    print("源码核对：%s" % _rel(out_path))
    for c in res["checks"]:
        print("   [%s] %-24s 记录 %-16s 磁盘 %-16s %s"
              % ("ok" if c["ok"] else "BAD", c["field"], c.get("recorded", "-"),
                 c.get("current", "-"), c.get("file", "")))
    if res["ok"]:
        print("   -> 一致：产物能为自己认证的输入把关")
    else:
        print("   -> 脱钩：%s" % "; ".join(res["reasons"]))
        print("      修法：用同一 spec/输入重跑 --apply（覆盖前先留档旧产物）")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="写候选清单（默认 dry-run）")
    ap.add_argument("--out", default=OUT_PATH)
    ap.add_argument("--concepts", default=None)
    ap.add_argument("--spec", default=SPEC_PATH if os.path.exists(SPEC_PATH) else None)
    ap.add_argument("--profile", default="strict", choices=PROFILES)
    ap.add_argument("--cut", type=int, default=CUT, help="特征窗口右端（含）")
    ap.add_argument("--k-endpoints", type=int, default=K_ENDPOINTS)
    ap.add_argument("--cross-domain", action="store_true", default=True)
    ap.add_argument("--no-cross-domain", dest="cross_domain", action="store_false")
    ap.add_argument("--verify", nargs="?", const=OUT_PATH, default=None,
                    help="只做溯源核对：拿产物核对磁盘上的输入/spec/工具哈希")
    args = ap.parse_args(argv)

    if args.verify is not None:
        res = verify(args.verify)
        print_verify(res, args.verify)
        return 0 if res["ok"] else 1

    rep, _d = run(args)
    print_report(rep)
    if not args.apply:
        print("\n[DRY-RUN] 未写文件。加 --apply 落候选清单")
        return 0
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    print("\n[ok] 候选清单 -> %s（%d 对）" % (_rel(args.out), len(rep["candidates"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
