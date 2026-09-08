#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/build_s7_relation_memory.py — S7 Agentic-search 关系记忆构建器（v2 多轮合并）

把冻结/裁决产物统一成一份可被生成器 / query composer / RL 消费的「关系记忆」。
v2 相对 v1（仅 round1）的升级：支持多轮 QA 合并 + pair 级去重 + deny 清单导出。

  community 层（S6 真实检索过的已探索空间 + 产出）：
    s6_trajectory.json  17 action → KEEP（R+U>0）/ FAIL{ZERO_HIT,NO_NEW,
                        RELATION_EXPRESSION_ABSENT}，含真实 R/U/new 产出
  relation 层（S7 多轮候选 × QA 盲评裁决）：
    round1 = s7_relation_candidates_v2.json × s7_relation_qa_v2.json (40)
    round2 = s7_relation_candidates_round2.json × s7_relation_qa_round2.json (49)
    → 裁决 RUN（VALID∧novelty∈{H,M}，排队待真实搜索）/ SKIP_INVALID /
      SKIP_LOW_NOVELTY（RL 负样本，不丢）
    pair 级去重：跨轮同款 (A,B) 只保留首见裁决，后轮实例标 duplicate（round2 与
    round1 同款 6 条 → 全部标注，证明纯文本 memory 拦不住 LLM，需 hard deny）
  novelty_risk：
    复用 generate_relation_candidates_v2.compute_novelty_risk（importlib 加载，
    单一事实源，避免逻辑漂移），对全部候选确定性重算

记忆的消费方：
  - generator 下一轮 hard_filter 用 generator_context.deny_pairs（全部已裁决 pair，
    程序拦截——round2 教训：prompt 文本 EXCLUDE 提示挡不住 LLM 重复已裁决组合）
  - generator prompt 注入 generator_context.prompt_ready（deny 摘要/strategy policy/
    thin domain/失败模式）
  - query composer 取 relations[] 首见 decision=RUN 的候选去真实检索
  - RL Phase 2 的 trajectory store：relations[] 即 (relation, outcome/decision) 序列

可复现纪律：每轮重建（只读冻结源，确定性输出），不做增量编辑。

用法：
  .venv/Scripts/python.exe tools/build_s7_relation_memory.py
  .venv/Scripts/python.exe tools/build_s7_relation_memory.py --extra-cands X.json --extra-qa Y.json
输出：
  data/exports/terminology/s7_relation_memory.json（schema_version 2.0）
"""
import argparse
import importlib.util
import json
import os
from collections import Counter, defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T = os.path.join(BASE, "data", "exports", "terminology")
GEN_TOOL = os.path.join(BASE, "tools", "generate_relation_candidates_v2.py")

# 内置轮次：round1(主文件 v2 命名) + 后续 glob s7_relation_*_roundN.json
DEFAULT_ROUNDS = [
    {"round": 1, "cands": "s7_relation_candidates_v2.json",
     "qa": "s7_relation_qa_v2.json"},
]

# S7.3 execute 源（2026-09-07 起）：46 条 QA=RUN → 11 组 Scopus query 真实检索
EXECUTE_RECORDS = os.path.join(T, "s7_execute_query_records.json")

# 用户裁决 09-07：EX-01/02（3dp crack/deformation 宽 query）不补跑，
# stdout 证据 EX-01 new=0（1383056 hits 前 1000 全 S6 seen）→ 弃跑观察
EX_ABANDONED = {"EX-01", "EX-02"}
EX_ABANDONED_REASON = ("用户裁决 09-07 弃跑：3dp 宽 observable query（crack/deformation）"
                       "对 S6 增量≈0（EX-01 stdout 1383056 hits/new=0），"
                       "不投入 live 成本；将来同类须带 mechanism/domain 约束")


def _ex_decision(gid, status):
    """EX community decision（执行处置层，非 QA 裁决）。"""
    if gid in EX_ABANDONED:
        return "ABANDONED"
    if status == "EXECUTED_PENDING_QA":
        return "PENDING_QA"
    if status in ("ZERO_HIT", "NO_NEW"):
        return status
    return "NOT_EXECUTED"


def _ex_decision_reason(gid, status):
    if gid in EX_ABANDONED:
        return EX_ABANDONED_REASON
    if status == "EXECUTED_PENDING_QA":
        return None
    if status == "ZERO_HIT":
        return ("Scopus 真实检索 0 命中（两次独立 live 复现）——"
                "packaging×delamination/void formation 组合无记录，"
                "term 真≠relation 真")
    if status == "NO_NEW":
        return ("检索有命中但 new_vs_S6=0（composites×delamination 仅 1 hit 且全 "
                "S6 seen）——该 (domain,B) 组合无增量，勿再生同型")
    return "live 未跑/缓存缺失——待补跑后才有检索产出"


def discover_rounds():
    """自动发现 roundN 文件（round>=2 命名 *_roundN.json），与 round1 合并。"""
    rounds = list(DEFAULT_ROUNDS)
    found = {}
    for fn in sorted(os.listdir(T)):
        for prefix, kind in (("s7_relation_candidates_round", "cands"),
                             ("s7_relation_qa_round", "qa")):
            if fn.startswith(prefix) and fn.endswith(".json"):
                n = fn[len(prefix):-5]
                if n.isdigit():
                    found.setdefault(int(n), {})[kind] = fn
    for n in sorted(found):
        if n >= 2 and "cands" in found[n] and "qa" in found[n]:
            rounds.append({"round": n, "cands": found[n]["cands"],
                           "qa": found[n]["qa"]})
    return rounds


# ── S6 action 产出分类 ──────────────────────────────────────────────────────
def classify_action(a):
    """S6 action → (status, failure_mode)。KEEP 需 hits>0 ∧ new>0 ∧ R+U>0。"""
    f, l = a.get("features", {}), a.get("labels", {})
    hits, new = f.get("scopus_total_hits", 0), f.get("new_vs_S5", 0)
    r, u = l.get("RELEVANT", 0), l.get("UNCERTAIN", 0)
    rpu = l.get("R_plus_U", r + u)
    if hits == 0:
        return "FAIL", "ZERO_HIT"
    if new == 0:
        return "FAIL", "NO_NEW"
    if rpu == 0:
        return "FAIL", "RELATION_EXPRESSION_ABSENT"
    return "KEEP", None


def load_gen_module():
    """加载 generator v2 模块（只取其函数，main 有 __main__ 守卫）。"""
    spec = importlib.util.spec_from_file_location("genv2", GEN_TOOL)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def proposal_decision(lab):
    """QA label → 裁决。与 run_s7_relation_qa.py 派生同规则，负样本细分。"""
    p, nv = lab.get("plausibility"), lab.get("novelty")
    if p == "VALID" and nv in ("HIGH", "MEDIUM"):
        return "RUN"
    if p == "INVALID":
        return "SKIP_INVALID"
    return "SKIP_LOW_NOVELTY" if nv == "LOW" else "SKIP"


def pair_key(a, b):
    return (a.strip().lower().rstrip("*"), b.strip().lower().rstrip("*"))


# ── S7.3 execute 源 → community 层（EX 组检索结果，2026-09-07）──────────────
def classify_execute(w):
    """EX group 检索结果 → outcome.status。
    cache_miss=True → NOT_EXECUTED；hits==0 → ZERO_HIT；new==0（hits>0）→
    NO_NEW（S6 类 FAIL：检索面全被 S6 seen）；否则 EXECUTED_PENDING_QA。"""
    if w.get("cache_miss"):
        return "NOT_EXECUTED", None
    if (w.get("total_hits") or 0) == 0:
        return "ZERO_HIT", "scopus_total_hits=0"
    if (w.get("new_vs_S6") or 0) == 0:
        return "NO_NEW", "new_vs_S6=0（检索面全 S6 seen）"
    return "EXECUTED_PENDING_QA", None


def execute_communities():
    """query_records_by_group → 每 EX 组一个 community 实例（source='s7_execute'）。
    relation 层 RUN proposal 经 run_ids 关联；observables=concept_B（组检索的 B 端）。"""
    if not os.path.exists(EXECUTE_RECORDS):
        return []
    rec = json.load(open(EXECUTE_RECORDS, encoding="utf-8"))
    groups = []
    for gid, w in sorted(rec.get("records_by_group", {}).items()):
        status, fail_mode = classify_execute(w)
        has_rows = bool(w.get("rows"))
        groups.append({
            "id": gid, "kind": "community", "source": "s7_execute",
            "family": None, "domain": w.get("domain"),
            "strategy": "grouped_run_execute",
            "exploratory": False,
            "contains_shrinkage_anchor": None,
            "observables": [w.get("concept_B")],
            "term_source": "S7_RUN_QUEUE(relation QA 产物)",
            "searched": not w.get("cache_miss"),
            "outcome": {
                "status": status, "failure_mode": fail_mode,
                "hits": w.get("total_hits"),
                "new": (w.get("new_vs_S6") if w.get("new_vs_S6") is not None
                        else None),
                "relevant": None, "uncertain": None, "R_plus_U": None,
            },
            "novelty_gate": None, "novelty_risk": None, "qa": None,
            "decision": _ex_decision(gid, status),
            "decision_reason": _ex_decision_reason(gid, status),
            "a_anchors": sorted(w.get("a_anchors", [])),
            "recall_layer": w.get("recall_layer"),
            "run_ids": w.get("run_ids", []),
            "n_rows": len(has_rows and w["rows"] or []),
        })
    return groups


def merge_community_qa(ex_comms):
    """EX community 加 community_qa 字段（读 s7_community_verdict.json）。
    簇级 QA（480 盲评 + 用户裁决）冻结后：EX 组检索结果在 KEEP 池中的贡献 =
    relation retrieval gain（reward coverage 分量雏形）。论文级 relevant 留待
    KEEP 池后续处置，不在此填。"""
    vpath = os.path.join(T, "s7_community_verdict.json")
    if not os.path.exists(vpath):
        return ex_comms
    verdict = json.load(open(vpath, encoding="utf-8"))
    contrib = verdict.get("ex_group_keep_contribution", {})
    for c in ex_comms:
        v = contrib.get(c["id"])
        if not v:
            continue
        c["community_qa"] = {
            "status": "QA_DONE",
            "verdict_file": os.path.basename(vpath),
            "decided_at": verdict.get("decided_at"),
            "n_rows_unique": v["n_rows_unique"],
            "n_keep_pool": v["n_keep_pool"],
            "keep_ratio": v["keep_ratio"],
            "recall_layer": v["recall_layer"],
        }
    return ex_comms


def merge_ex_small_qa(ex_comms):
    """round4 后小批量 EX 组论文级 QA（无聚类，直接全量盲评）回填。
    目前仅 EX-12（coatings×warpage 36 new）：s7_ex12_qa_labels.json 计数
    R/U → outcome（决策 VALIDATED/NO_RELEVANT）。"""
    import glob
    for f in sorted(glob.glob(os.path.join(T, "s7_ex*_qa_labels.json"))):
        gid = "EX-" + os.path.basename(f).split("_")[0][-2:].lstrip("0")
        # 文件名模式 s7_ex12_qa_labels.json → EX-12
        name = os.path.basename(f)  # s7_ex12_qa_labels.json
        parts = name.replace(".json", "").split("_")  # ['s7','ex12','qa','labels']
        ex_part = next((p for p in parts if p.startswith("ex")), None)
        if not ex_part:
            continue
        num = ex_part[2:]
        gid = f"EX-{int(num):02d}"
        tgt = next((c for c in ex_comms if c["id"] == gid), None)
        if not tgt or tgt["outcome"]["status"] != "EXECUTED_PENDING_QA":
            continue
        d = json.load(open(f, encoding="utf-8"))
        if "labels" in d and isinstance(d["labels"], dict):
            d = d["labels"]
        r = sum(1 for v in d.values() if v == "RELEVANT")
        u = sum(1 for v in d.values() if v == "UNCERTAIN")
        tgt["outcome"]["relevant"] = r
        tgt["outcome"]["uncertain"] = u
        tgt["outcome"]["R_plus_U"] = r + u
        tgt["decision"] = "VALIDATED" if r > 0 else "NO_RELEVANT"
        tgt["decision_reason"] = (
            f"小批量论文级 QA（无聚类）：{os.path.basename(f)} R{r}/U{u}")
    return ex_comms


def merge_candidate_rewards(ex_comms):
    """论文级 QA 终裁（s7_candidate_set.json）→ EX outcome.relevant/uncertain 回填。
    口径：EX 组 relevant = KEEP 池 750 篇论文级 QA 中 ex_sources 含该组的 R 数
    （FAIL 簇簇级抽样 R+U=0 → 组内非池论文无 R 假设成立，近似备注）。"""
    cpath = os.path.join(T, "s7_candidate_set.json")
    if not os.path.exists(cpath):
        return ex_comms
    cand = json.load(open(cpath, encoding="utf-8"))
    from collections import defaultdict
    per_ex = defaultdict(lambda: {"R": 0, "U": 0})
    for p in cand.get("papers", []):
        lab = p.get("label")
        if lab not in ("RELEVANT", "UNCERTAIN"):
            continue
        for g in p.get("ex_sources", []):
            per_ex[g][lab[:1]] += 1   # 'R' / 'U'
    for c in ex_comms:
        if c["outcome"]["status"] != "EXECUTED_PENDING_QA":
            continue
        r, u = per_ex.get(c["id"], {}).get("R", 0), \
            per_ex.get(c["id"], {}).get("U", 0)
        c["outcome"]["relevant"] = r
        c["outcome"]["uncertain"] = u
        c["outcome"]["R_plus_U"] = r + u
        c["decision"] = "VALIDATED" if r > 0 else "NO_RELEVANT"
        c["decision_reason"] = (
            f"KEEP 池论文级 QA：EX 组内 R {r}/U {u}（750 篇盲评终裁）"
            if r > 0 else "KEEP 池论文级 QA 组内 0 relevant")
    return ex_comms


def mark_proposals_searched(proposals, ex_communities):
    """RUN proposal 若 run_id ∈ EX 组 run_ids → searched=True + execute_outcome。
    修改 proposal（relation 层）与 EX community（community 层）的连接。"""
    by_run_id = {}
    for c in ex_communities:
        for rid in c.get("run_ids", []):
            by_run_id[rid] = c
    for p in proposals:
        if p["decision"] == "RUN" and p["id"] in by_run_id:
            ex = by_run_id[p["id"]]
            if ex["outcome"]["status"] in ("EXECUTED_PENDING_QA", "ZERO_HIT",
                                           "NO_NEW"):
                p["searched"] = True
                p["execute_outcome"] = {
                    "ex_group": ex["id"], "domain": ex["domain"],
                    "hits": ex["outcome"]["hits"], "new": ex["outcome"]["new"],
                    "status": ex["outcome"]["status"],
                }
            elif ex.get("decision") == "ABANDONED":
                # 用户裁决弃跑：不标 searched（未真实检索），记录处置去向
                p["execute_outcome"] = {
                    "ex_group": ex["id"], "domain": ex["domain"],
                    "status": "ABANDONED",
                    "reason": EX_ABANDONED_REASON,
                }
    return proposals


# ── generator_context 渲染 ─────────────────────────────────────────────────
def render_context(communities, proposals, dom_cov, typ_cov, swept_a,
                   dup_records, qa_by_round):
    """渲染 prompt 文本 + 结构化 deny 清单。deny 全量结构化导出（hard filter 用）；
    prompt 文本保持精简（deny 靠程序拦，不再依赖 LLM 读长文本）。"""
    kept = sorted([c for c in communities if c["outcome"]["status"] == "KEEP"],
                  key=lambda c: -c["outcome"]["R_plus_U"])
    fails = [c for c in communities if c["outcome"]["status"] == "FAIL"]
    firsts = [p for p in proposals if not p.get("duplicate")]
    # run_queue = 实际可执行 RUN：排除已检索(searched)/弃跑(ABANDONED)/
    # 组级 dedup 覆盖（(domain,B) ∈ 已执行 EX 组——与 run_s7_execute.build_groups 同规则）
    ex_mem = [c for c in communities if c.get("source") == "s7_execute"]
    swept_combos = {
        (c.get("domain"),
         (c.get("observables") or [""])[0].lower().rstrip("*"))
        for c in ex_mem}
    run_q = [p for p in firsts if p["decision"] == "RUN"
             and not p.get("searched")
             and (p.get("execute_outcome") or {}).get("status") != "ABANDONED"
             and (p.get("domain"),
                  p["concept_B"].lower().rstrip("*")) not in swept_combos
             ]   # 已真实检索/弃跑/同组已扫 的 RUN 不算 queued
    sk_inv = [p for p in firsts if p["decision"] == "SKIP_INVALID"]
    sk_low = [p for p in firsts if p["decision"] == "SKIP_LOW_NOVELTY"]

    def cline(c):
        o = c["outcome"]
        return (f"[{c['id']}] {c['domain']} ({c['strategy']}, fam {c['family']}): "
                f"{'|'.join(c['observables'])}  R{o['relevant']}/U{o['uncertain']} "
                f"new{o['new']}")

    def pline(p):
        tag = "dup" if p.get("duplicate") else p["decision"][:4]
        return (f"{p['id']} {p['concept_A']}->{p['concept_B']}@{p['domain']}"
                f"({p['qa']['novelty']})")

    lines = ["RELATION MEMORY — explored/failed/queued relation space "
             "(S6 real search + S7 QA rounds "
             + "+".join(str(r) for r in qa_by_round)
             + "). Generate relations that EXPAND UNCOVERED regions; never "
               "re-propose anything below (hard-filtered)."]

    # ① FAILED（搜索过零产出——最强 deny）
    lines.append("")
    lines.append(f"[SEARCHED & FAILED — do NOT re-propose under same strategy] "
                 f"n={len(fails)}")
    lines += ["  " + cline(c) + f"  -> {c['outcome']['failure_mode']}"
              for c in fails]

    # ② QA 裁决摘要（首见）——只给统计与禁提短清单
    lines.append("")
    lines.append(f"[QA VERDICT rounds on {len(proposals)} instances / "
                 f"{len(firsts)} unique pairs]")
    dec = Counter(p["decision"] for p in firsts)
    lines.append(f"  unique verdicts: {dict(dec)}  (duplicates filtered: "
                 f"{len(dup_records)})")
    lines.append(f"  RUN (queued for real search; hard-denied in generator): "
                 f"n={len(run_q)}  "
                 + "; ".join(pline(p) for p in run_q[:14]))
    if len(run_q) > 14:
        lines.append(f"    ...(+{len(run_q) - 14} more RUN, see memory JSON)")
    executed = [p for p in firsts if p.get("searched")]
    if executed:
        lines.append(f"  EXECUTED (real search done, QA pending): "
                     f"n={len(executed)}  "
                     + "; ".join(f"{p['id']}->{p['execute_outcome']['ex_group']}"
                                 f"(new{p['execute_outcome']['new']})"
                                 for p in executed[:10]))
        if len(executed) > 10:
            lines.append(f"    ...(+{len(executed) - 10} more EXECUTED, "
                         f"see memory JSON)")
    lines.append(f"  SKIP_INVALID (scientifically invalid; hard-denied): "
                 f"n={len(sk_inv)}  "
                 + "; ".join(pline(p) for p in sk_inv))
    lines.append(f"  SKIP_LOW (variant of swept cluster; hard-denied): "
                 f"n={len(sk_low)}")
    for p in sk_low[:12]:
        lines.append(f"    {pline(p)}  risk={p['novelty_risk']['flags'] or '-'}")
    if len(sk_low) > 12:
        lines.append(f"    ...(+{len(sk_low) - 12} more SKIP_LOW, see memory JSON)")

    # ③ KEPT（真实检索成功——不重扫）
    lines.append("")
    lines.append(f"[SEARCHED & YIELDED — already searched, relevant found; "
                 f"do not re-sweep] n={len(kept)}")
    for c in kept[:12]:
        lines.append("  " + cline(c))
    if len(kept) > 12:
        lines.append(f"  ...(+{len(kept) - 12} more KEPT, see memory JSON)")

    # ④ strategy policy（S6 检索成功率 × QA RUN 率 → bandit/imitation 先验雏形）
    lines.append("")
    lines.append("[STRATEGY POLICY — retrieval yield (S6) vs QA RUN rate (S7):]")
    for st, v in sorted(typ_cov.items()):
        rate = v["R_plus_U"] / v["n_searched"] if v["n_searched"] else 0
        lines.append(f"  {st:38s} S6 searched {v['n_searched']} fail {v['n_fail']} "
                     f"R+U {v['R_plus_U']} (avg {rate:.1f}/q)  |  QA RUN "
                     f"{v['qa_n']}/{v['qa_run']}")

    # ⑤ domain 覆盖 / thin
    lines.append("")
    lines.append("[DOMAIN COVERAGE — prefer thin domains as B-end target]: "
                 + "; ".join(f"{d}(searched {v['n_searched']}, R{v['R']})"
                             for d, v in sorted(dom_cov.items())))
    thin = sorted(d for d, v in dom_cov.items()
                  if v["n_searched"] <= 1 and v["R"] <= 5)
    if thin:
        lines.append("  THIN (low density AND low yield — prefer targets here): "
                     + ", ".join(thin))
    lines.append("[SWEPT OBSERVABLES (A-family right anchors)]: "
                 + ", ".join(sorted(swept_a)))

    # ⑥ S7 execute 实证（relation reward 证据——真实 Scopus + 论文级 QA）
    exs = [c for c in communities if c.get("source") == "s7_execute"]
    val = [c for c in exs if c["decision"] == "VALIDATED"]
    zero = [c for c in exs if c["outcome"]["status"] == "ZERO_HIT"]
    aband = [c for c in exs if c["decision"] == "ABANDONED"]
    if val or zero or aband:
        lines.append("")
        lines.append("[S7 EXECUTE YIELD — real Scopus + paper-level QA reward "
                     "evidence; prefer high R-density B-observables in these "
                     "domains, never regenerate low-yield/ZERO_HIT forms]")
        for c in sorted(val, key=lambda x: -x["outcome"]["relevant"]):
            o = c["outcome"]
            qa = c.get("community_qa") or {}
            kp = (f"{qa['n_keep_pool']}/{qa['n_rows_unique']}"
                  if qa.get("status") == "QA_DONE" else "-")
            lines.append(f"  {c['id']} {c['domain']}×{c['observables'][0]}: "
                         f"new{o['new']} R{o['relevant']}/U{o['uncertain']} "
                         f"(KEEP池 {kp})")
        if zero:
            lines.append("  ZERO_HIT (do not re-propose under same A-phrase×domain): "
                         + "; ".join(f"{c['id']} {c['domain']}×{c['observables'][0]}"
                                     for c in zero)
                         + " — packaging×{delamination,void formation} Scopus 0 hits")
        if aband:
            lines.append("  ABANDONED (wide observable query, ~0 S6 increment): "
                         + "; ".join(f"{c['id']} {c['domain']}×{c['observables'][0]}"
                                     for c in aband)
                         + " — future wide-observable relations must add "
                           "mechanism/domain constraint")
        lines.append("  POLICY: specific observable B (dimensional accuracy, "
                     "warpage) in 3dp yields high R; broad coating/packaging "
                     "observable sweeps yield R<5 even at new>800")
        lines.append("  NOTE: every EX-xx above has SWEPT its (domain × B) "
                     "combination — do NOT propose new A-anchors for the SAME "
                     "(domain × B) again (re-sweep = zero gain). New relations "
                     "must target UNEXPLORED (domain × B) cells, e.g. "
                     "composites/coatings × {warpage, delamination, void, "
                     "dim-accuracy} with a process or mechanism A-anchor.")

    deny_pairs = [[p["concept_A"], p["concept_B"], p["round"],
                   p["id"], p["decision"], p["qa"]["novelty"]]
                  for p in firsts]
    return {
        "prompt_ready": "\n".join(lines),
        "deny_pairs": deny_pairs,   # hard filter 程序 deny（全量已裁决 pair）
        "n_unique_pairs": len(firsts),
        "n_instances": len(proposals),
        "n_duplicates": len(dup_records),
        "run_queue": [p["id"] for p in run_q],
        "skip_invalid": [p["id"] for p in sk_inv],
        "skip_low": [p["id"] for p in sk_low],
        "thin_domains": thin,
        "swept_observables": sorted(swept_a),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="s7_relation_memory.json")
    args = ap.parse_args()

    gm = load_gen_module()
    _, _, a_used_obs = gm.load_s6_feedback()

    # ── 1. S6 trajectory → community 层 ──────────────────────────────────
    traj = json.load(open(os.path.join(T, "s6_trajectory.json"), encoding="utf-8"))
    communities = []
    for a in traj["actions"]:
        f, l = a.get("features", {}), a.get("labels", {})
        status, fail_mode = classify_action(a)
        communities.append({
            "id": a["action_id"], "kind": "community", "source": "s6_trajectory",
            "family": a.get("family"), "domain": a.get("domain"),
            "strategy": a.get("strategy"), "exploratory": a.get("exploratory"),
            "contains_shrinkage_anchor": a.get("contains_shrinkage_anchor"),
            "observables": list(a.get("terms", [])),
            "term_source": a.get("term_source"),
            "searched": True,
            "outcome": {
                "status": status, "failure_mode": fail_mode,
                "hits": f.get("scopus_total_hits", 0), "new": f.get("new_vs_S5", 0),
                "relevant": l.get("RELEVANT", 0), "uncertain": l.get("UNCERTAIN", 0),
                "R_plus_U": l.get("R_plus_U", 0),
            },
            "novelty_gate": None, "novelty_risk": None, "qa": None,
            "decision": "KEEP" if status == "KEEP" else "SKIP",
            "decision_reason": (None if status == "KEEP"
                                else f"S6 检索失败 {fail_mode}"),
        })
    assert len(communities) == len(traj["actions"]) == 17, "S6 action 数不匹配"

    # ── 1.5 S7.3 execute → community 层（EX 组，2026-09-07 新增源）────────
    ex_comms = execute_communities()
    if ex_comms:
        ex_comms = merge_community_qa(ex_comms)
        ex_comms = merge_candidate_rewards(ex_comms)
        ex_comms = merge_ex_small_qa(ex_comms)
        communities += ex_comms
        print(f"  execute groups: {len(ex_comms)} 组"
              f"（EXECUTED_PENDING_QA "
              f"{sum(1 for c in ex_comms if c['outcome']['status']=='EXECUTED_PENDING_QA')}"
              f" / ZERO_HIT "
              f"{sum(1 for c in ex_comms if c['outcome']['status']=='ZERO_HIT')}"
              f" / NO_NEW "
              f"{sum(1 for c in ex_comms if c['outcome']['status']=='NO_NEW')}"
              f" / ABANDONED(未跑) "
              f"{sum(1 for c in ex_comms if c.get('decision')=='ABANDONED')}）"
              + (f" | 簇级 QA 回灌 "
                 f"{sum(1 for c in ex_comms if 'community_qa' in c)} 组"
                 if any("community_qa" in c for c in ex_comms) else ""))

    # ── 2. 多轮 S7 candidates × QA → relation 层（pair 级去重）──────────
    rounds = discover_rounds()
    proposals, first_by_pair, dup_records = [], {}, []
    qa_by_round = []
    for r in rounds:
        cands = json.load(open(os.path.join(T, r["cands"]),
                               encoding="utf-8"))["candidates"]
        qa = json.load(open(os.path.join(T, r["qa"]),
                            encoding="utf-8"))["labels"]
        assert len(cands) == len(qa), f"round{r['round']} 候选/QA 数不匹配"
        qa_by_round.append(r["round"])
        qa_sum = Counter(l["plausibility"] for l in qa.values())
        nv_sum = Counter(l["novelty"] for l in qa.values())
        print(f"  round{r['round']}: n={len(cands)} QA "
              f"VALID{qa_sum['VALID']}/INV{qa_sum['INVALID']} "
              f"novelty H{nv_sum['HIGH']}/M{nv_sum['MEDIUM']}/L{nv_sum['LOW']}")
        for c in sorted(cands, key=lambda x: x["candidate_id"]):
            cid = c["candidate_id"]
            lab = qa[cid]
            risk = gm.compute_novelty_risk(dict(c), a_used_obs)
            derived = proposal_decision(lab)
            coarse = {"RUN": "RUN"}.get(derived, "SKIP")
            assert coarse == lab.get("search_action"), (
                f"round{r['round']} {cid} 裁决派生不一致")
            pk = pair_key(c["concept_A"], c["concept_B"])
            rec = {
                "id": f"R{r['round']}-{cid}", "round": r["round"],
                "kind": "proposal", "source": f"s7_qa_round{r['round']}",
                "concept_A": c["concept_A"],
                "concept_A_status": c["concept_A_source"],
                "concept_B": c["concept_B"],
                "concept_B_status": c["concept_B_source"],
                "relation_type": c["relation_type"], "domain": c.get("domain"),
                "recall_layer": c["recall_layer"],
                "why_S5_failed": c["why_S5_failed"],
                "rationale": c.get("rationale"),
                "searched": False,
                "novelty_gate": c["novelty_gate"],
                "novelty_risk": risk,
                "qa": {"plausibility": lab["plausibility"],
                       "novelty": lab["novelty"], "reason": lab.get("reason")},
            }
            if pk in first_by_pair:   # 跨轮同款 → duplicate（保留首见裁决）
                first = first_by_pair[pk]
                rec["duplicate"] = True
                rec["duplicate_of"] = first["id"]
                rec["decision"] = "DUPLICATE"
                rec["decision_reason"] = (f"与 {first['id']} 同款 pair，"
                                          f"首见裁决 {first['decision']} 为准")
                dup_records.append({"round": r["round"], "candidate_id": cid,
                                    "pair": list(pk),
                                    "duplicate_of": first["id"],
                                    "first_verdict": first["decision"],
                                    "re_verdict": derived})
                proposals.append(rec)
                continue
            rec["decision"] = derived
            rec["decision_reason"] = (
                "QA 盲评 VALID∧novelty∈{H,M} → 排队真实检索" if derived == "RUN"
                else ("QA 盲评 INVALID（科学无效）→ RL 负样本" if derived == "SKIP_INVALID"
                      else "QA 盲评 LOW（已扫社区语义变体）→ RL 负样本"))
            first_by_pair[pk] = rec
            proposals.append(rec)

    firsts = [p for p in proposals if not p.get("duplicate")]
    print(f"  total instances={len(proposals)} unique pairs={len(firsts)} "
          f"duplicates={len(dup_records)}")

    # RUN proposal 检索回填（run_id ∈ EX 组）
    proposals = mark_proposals_searched(proposals, ex_comms)
    n_ran = sum(1 for p in proposals if p.get("searched"))
    if n_ran:
        print(f"  RUN proposal 已真实检索回填: {n_ran} 条 (searched=True)")

    # ── 3. summary / agent_state / policy ────────────────────────────────
    dom_cov = defaultdict(lambda: {"n_searched": 0, "R": 0, "U": 0})
    typ_cov = defaultdict(lambda: {"n_searched": 0, "n_fail": 0,
                                   "R": 0, "U": 0, "R_plus_U": 0,
                                   "qa_n": 0, "qa_run": 0})
    for c in communities:
        o = c["outcome"]
        if o.get("relevant") is None:   # EX 组（EXECUTED_PENDING_QA/NOT_EXECUTED）
            continue                     # 不进 S6 searched 统计，等 QA 回填后并入
        d, t = c["domain"], c["strategy"]
        dom_cov[d]["n_searched"] += 1
        typ_cov[t]["n_searched"] += 1
        dom_cov[d]["R"] += o["relevant"]; dom_cov[d]["U"] += o["uncertain"]
        typ_cov[t]["R"] += o["relevant"]; typ_cov[t]["U"] += o["uncertain"]
        typ_cov[t]["R_plus_U"] += o["R_plus_U"]
        if o["status"] == "FAIL":
            typ_cov[t]["n_fail"] += 1
    for p in firsts:
        st = p["relation_type"]
        typ_cov[st]["qa_n"] += 1
        if p["decision"] == "RUN":
            typ_cov[st]["qa_run"] += 1
    swept_a = sorted(a_used_obs)
    ctx = render_context(communities, firsts, dict(dom_cov), dict(typ_cov),
                         swept_a, dup_records, qa_by_round)

    relations = communities + proposals
    dec_sum = Counter(p["decision"] for p in firsts)
    risk_sum = Counter(tuple(p["novelty_risk"].get("flags", []))
                       for p in firsts)
    out = {
        "schema_version": "2.0",
        "role": "S7 relation memory — unified explored/failed/queued relation space "
                "(community + relation 双层，多轮合并)；生成器/query composer/RL 共用状态",
        "built_at": __import__("datetime").datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "rebuild_policy": "每轮确定性重建（只读冻结源）；跨轮同款 pair 只留首见裁决",
        "rounds": [{"round": r, "file": r["cands"], "qa_file": r["qa"]}
                   for r in rounds],
        "sources": {
            "s6_trajectory": {"n_actions": len(communities)
                              - len(ex_comms)},
            "s7_qa": {"n_instances": len(proposals),
                      "n_unique_pairs": len(firsts),
                      "n_duplicates": len(dup_records)},
            "s7_execute": {"n_groups": len(ex_comms),
                           "file": os.path.basename(EXECUTE_RECORDS)},
            "novelty_risk": "compute_novelty_risk 复用 generator v2 模块重算",
        },
        "summary": {
            "communities": {"n": len(communities),
                            "KEEP": sum(1 for c in communities
                                        if c["outcome"]["status"] == "KEEP"),
                            "FAIL": sum(1 for c in communities
                                        if c["outcome"]["status"] == "FAIL"),
                            "EXECUTED_PENDING_QA": sum(
                                1 for c in ex_comms
                                if c["outcome"]["status"] == "EXECUTED_PENDING_QA"),
                            "NOT_EXECUTED": sum(
                                1 for c in ex_comms
                                if c["outcome"]["status"] == "NOT_EXECUTED"),
                            "ZERO_HIT": sum(
                                1 for c in ex_comms
                                if c["outcome"]["status"] == "ZERO_HIT"),
                            "NO_NEW": sum(
                                1 for c in ex_comms
                                if c["outcome"]["status"] == "NO_NEW"),
                            "ABANDONED": sum(
                                1 for c in ex_comms
                                if c.get("decision") == "ABANDONED")},
            "proposals": dict(dec_sum),
            "duplicates": dup_records,
            "qa_plausibility": dict(Counter(p["qa"]["plausibility"]
                                            for p in firsts)),
            "qa_novelty": dict(Counter(p["qa"]["novelty"] for p in firsts)),
            "novelty_risk_flags": {str(k): v for k, v in risk_sum.items()},
            "domain_coverage": {k: dict(v) for k, v in sorted(dom_cov.items())},
            "relation_type_coverage": {
                k: {kk: vv for kk, vv in v.items()} for k, v in sorted(typ_cov.items())},
        },
        "agent_state": {   # RL Phase 2 状态特征原型
            "swept_observables_A": swept_a,
            "failed_observables": sorted({o
                                          for c in communities
                                          if c["outcome"]["status"] == "FAIL"
                                          for o in c["observables"]}),
            "run_queue": ctx["run_queue"],
            "domain_coverage": {k: dict(v) for k, v in sorted(dom_cov.items())},
            "relation_type_coverage": {
                k: {kk: vv for kk, vv in v.items()} for k, v in sorted(typ_cov.items())},
        },
        "generator_context": ctx,
        "relations": relations,
    }
    out_path = os.path.join(T, args.out)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print(f"[ok] {out_path}")
    print(f"  community {len(communities)} (KEEP {out['summary']['communities']['KEEP']}"
          f" / FAIL {out['summary']['communities']['FAIL']}"
          f" / EX-PENDING-QA "
          f"{out['summary']['communities']['EXECUTED_PENDING_QA']}"
          f" / NOT-EXEC "
          f"{out['summary']['communities']['NOT_EXECUTED']})")
    print(f"  proposals: instances {len(proposals)} → unique {len(firsts)} "
          f"{dict(dec_sum)} | dup {len(dup_records)}")
    print(f"  run_queue {len(ctx['run_queue'])} (已执行 {n_ran} 条 RUN 出队) "
          f"/ deny_pairs {len(ctx['deny_pairs'])}")
    print(f"  prompt_ready {len(ctx['prompt_ready'])} chars")
    print(f"  integrity asserts: S6 17 / EX {len(ex_comms)} / 各轮 QA 对齐 "
          f"/ 裁决双射 全部通过")


if __name__ == "__main__":
    main()
