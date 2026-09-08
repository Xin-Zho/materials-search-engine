#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/generate_relation_candidates.py — S7 Query Planner v1: LLM relation proposal
（2026-09-07 用户终裁 Tier 冻结后实现）。

架构：LLM 提议 **concept relation**（非 query）→ deterministic 程序组装 query → pilot。
本工具只做前半：从冻结词表产出 relation candidates + hard filter + soft score。

输入：
  - s6_term_bank.json      VERIFIED_EXACT 40（词源唯一来源；SEMANTIC 16 禁入）
  - s6_trajectory.json     S6 17 条（失败模式 → 黑名单 + 反馈）
  - build_s6_bridge_queries.py 冻结常量（PROCESS/SHRINK_LEX/CTX/ROLE_CORRECTION）
输出：
  - s7_relation_candidates.json（filter 后 + score 排序）

relation_type Tier（2026-09-07 终裁，勿改）：
  T1: process_to_observable / lexical_to_observable        —— 默认生成（主力）
  T2: mechanism_to_observable（须 rationale）/ observable_transfer（须 process/material
      第三约束，禁裸 domain）                                —— 受控生成
  T3: mechanism_to_mechanism                               —— 默认关闭，主动探索才允许
generation budget（初始 exploration）：70/20/10 → 总量 60 时 T1=42 T2=12 T3=6

hard filter（程序，不信 LLM 自报）：
  ① term 两端 ∈ 冻结词表（SEMANTIC 词自然排除）② 与 S6 已有/失败 relation 去重
  ③ term-set Jaccard 重复拒
soft score（只排序不学习）：
  tier_prior + 0.3*LLM plausibility + 0.3*coverage_gap − 0.5*hist_fail

用法：
  python tools/generate_relation_candidates.py --plan-only          # 词表/管线自检
  python tools/generate_relation_candidates.py --dry-samples        # 内置样例走 filter+score
  python tools/generate_relation_candidates.py                      # LLM 生成（需 DEEPSEEK_API_KEY）
"""
import argparse
import asyncio
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T = os.path.join(BASE, "data", "exports", "terminology")
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tools"))
sys.path.insert(0, os.path.join(BASE, "search_engine"))

from build_s6_bridge_queries import (  # noqa: E402  冻结常量（词源纪律）
    PROCESS_CLAUSE, SHRINK_LEX_CLAUSE, CTX, ROLE_CORRECTION, QUERY_FORM_MAP)

DEFAULT_OUT = os.path.join(T, "s7_relation_candidates.json")
# 生成计划（轮次 × 每轮 raw 目标）：DeepSeek max_tokens=2048 默认会截断长 JSON 数组
# → 拆小批（每轮 ≤12 条）防截断；raw 总量略高于用户 50 目标以吸收 filter 损耗。
# 预算比例保持 70/20/10（用户终裁）。
TIER_PLAN = [("T1", 4, 12), ("T2", 2, 8), ("T3", 1, 6)]  # (tier, rounds, per_round)

# soft score 权重（v1，只排序；学习参数留给 bandit 阶段）
W = {"tier_prior": {"T1": 1.0, "T2": 0.7, "T3": 0.4},
     "plausibility": 0.3, "coverage_gap": 0.3, "hist_fail": 0.5}

RELATION_TYPE_TIER = {
    "process_to_observable": "T1",
    "lexical_to_observable": "T1",
    "mechanism_to_observable": "T2",
    "observable_transfer": "T2",
    "mechanism_to_mechanism": "T3",
}


def load(name):
    return json.load(open(os.path.join(T, name), encoding="utf-8"))


def build_vocab():
    """冻结词表：按 role/domain 分组 + ROLE_CORRECTION（context 词从 observable 剔除）。"""
    tb = load("s6_term_bank.json")
    verified = tb["terms_verified_exact"]
    semantic = tb.get("terms_semantic_candidates", [])
    obs, mech, lex = [], [], []
    for e in verified:
        t = e["term"]
        role = e["semantic_role"]
        if role == "observable" and ROLE_CORRECTION.get(t) == "context":
            continue  # 组装角色修正：context 词不当 observable 用（S6 教训）
        (obs if role == "observable" else mech if role == "mechanism" else lex)\
            .append({"term": t, "domain": e["domain"], "role": role})
    # process 锚（冻结模板，非内容词）
    proc_terms = ["curing", "polymeriz*", "photocuring", "photopolymeriz*"]
    # material context（S5 冻结 APPLICATION 层，D transfer 第三约束用；domain 标签只作提示）
    mat_ctx = {
        "dental": ["dental", "dentistry", "restorative", "composite", "resin composite",
                   "tooth", "enamel", "dentin", "adhesive"],
        "3dp": ["3d print", "additive manufacturing", "stereolithograph", "sla",
                "vat photopolymerization", "dlp", "resin printing"],
        "optics": ["holograph", "optical", "lithograph", "photoresist", "recording"],
        "packaging": ["molding compound", "encapsulant", "epoxy", "semiconductor"],
        "coatings": ["coating", "film", "paint", "varnish"],
        "composites": ["composite", "fiber", "filler"],
    }
    return {"observable": obs, "mechanism": mech, "lexical": lex,
            "process": proc_terms, "material_ctx": mat_ctx,
            "all_verified": {e["term"] for e in verified},
            "semantic": {e["term"] for e in semantic}}


def build_feedback(traj):
    """S6 失败/已有 relation → 黑名单 + 反馈文本。
    terms 只记 obs/mech，须按 anchor 标志展开左锚（process/shrink 词族）才能完整去重。"""
    PROC_TOKENS = ["curing", "polymeriz*", "photocuring", "photopolymeriz*"]
    SHRINK_TOKENS = ["polymerization shrinkage", "polymerization contraction",
                     "volume contraction", "shrinkage"]
    failed, used = [], []
    for a in traj["actions"]:
        terms = list(a.get("terms", []))
        q = a.get("query_string", "")
        if a.get("contains_shrinkage_anchor"):
            terms += [t for t in SHRINK_TOKENS if t.lower() in q.lower()]
        elif "polymeriz" in q or "photocuring" in q or "photopolymeriz" in q:
            terms += [t for t in PROC_TOKENS if t.lower() in q.lower()]
        used.append(terms)
        ru = a["labels"]["R_plus_U"]
        hits = a["features"]["scopus_total_hits"]
        new = a["features"]["new_vs_S5"]
        fr = None
        if hits == 0:
            fr = "ZERO_HIT"
        elif new == 0:
            fr = "NO_NEW"
        elif ru == 0:
            fr = "RELATION_EXPRESSION_ABSENT"
        if fr:
            failed.append(terms)
    return used, failed


def build_prompt(vocab, used, failed, tier, count, miss_dir):
    """单 Tier prompt：从冻结词表 pick 组合，LLM 只输出关系+理由，不自由造词。"""
    def fmt(items):
        # 只给 term 字符串，不带 [domain] 后缀——防 LLM 把展示标记当 term 复制
        return " | ".join(i["term"] for i in items)

    if tier == "T1":
        rules = [
            "process_to_observable：A=process 锚词（curing/polymeriz*/photocuring/"
            "photopolymeriz*），B=observable。目标：无 shrinkage 字面地表达'固化过程的"
            "可观察后果'，进入 S5 语言覆盖不到的论文社区。",
            "lexical_to_observable：A=lexical core（shrinkage 族），B=observable。"
            "高精度补充入口，非开新社区。",
            "每条必须说明该 B（observable）在论文中为何由 A 的固化过程导致。",
        ]
    elif tier == "T2":
        rules = [
            "mechanism_to_observable：A=mechanism，B=observable。必须给 scientific "
            "rationale：论文作者为何会同时讨论二者（真实论文表达，非概念推演）。"
            "禁止牵强组合。",
            "observable_transfer：一个 domain 的 observable 迁到另一 domain。"
            "必须带第三约束：process/material context 词（material_ctx 表），"
            "禁止裸用 domain 标签（论文作者不用 domain 分类）。",
        ]
    else:
        rules = [
            "mechanism_to_mechanism：仅当你确信某材料体系中两个机制直接耦合时才提议"
            "（如 AFCT 与 stress relaxation 同现）。默认不生成；无强证据不要凑。",
        ]

    failed_txt = ("；".join(" AND ".join(t) for t in failed[-6:]) or "无")[:600]
    used_txt = ("；".join(" AND ".join(t) for t in used[-10:]) or "无")[:800]
    prompt = f"""You are a materials-science retrieval strategist. Propose {count} SCIENTIFIC RELATIONS (term pairs) that a paper author might actually write about. Rubric: S7 relation proposal, tier {tier}.

RESEARCH FRAME: polymerization shrinkage / shrinkage stress in photopolymerization & light-curing systems, and its observable consequences (deformation, dimensional error, interface defects...). Goal: express relations WITHOUT always writing "shrinkage".

RULE — TERM SOVEREIGNTY: You must pick term strings EXACTLY from the vocabulary below. Never invent, pluralize, hyphenate or rephrase terms. Non-listed words → candidate rejected.

VOCABULARY:
- OBSERVABLE (candidate B): {fmt(vocab['observable'])}
- MECHANISM (candidate A in T2/T3): {fmt(vocab['mechanism'])}
- LEXICAL core (candidate A in T1-lexical): {fmt(vocab['lexical'])}
- PROCESS anchor (candidate A in T1-process): {' | '.join(vocab['process'])}
- MATERIAL/process context (3rd constraint in observable_transfer): {json.dumps(vocab['material_ctx'], ensure_ascii=False)[:500]}

TIER RULES:
{chr(10).join(' - ' + r for r in rules)}

KNOWN HISTORY (do not repeat; these were already searched or failed):
- already used: {used_txt}
- failed (0 relevant): {failed_txt}

MISS DIRECTION (gap direction only, do not use these as literal terms unless in vocab above): {miss_dir}

Output STRICT JSON array only (no prose, no fences), {count} objects:
[{{"term_A": "<exact vocab string>", "term_B": "<exact vocab string>",
   "relation_type": "{'|'.join(k for k,v in RELATION_TYPE_TIER.items() if v==tier)}",
   "domain": "<target domain for the QUERY context, one of dental/composites/sla/coatings/optics/packaging/general>",
   "rationale": "<one sentence: why authors would write both>",
   "plausibility": 0.0-1.0,
   "expected_failure_mode": "ZERO_HIT|NO_NEW|RELATION_EXPRESSION_ABSENT|DOMAIN_DRIFT|LOW_YIELD|REDUNDANT"}}]
Ensure term_A != term_B, no exact duplicates of known history. All plausibility > 0.6."""
    return prompt


def _strip_domain_tag(s):
    """剥离 LLM 误复制的 [domain] 展示后缀（词表曾带 [dental] 等标记）。"""
    if "[" in s and s.endswith("]"):
        head = s[:s.index("[")].strip()
        if head:
            return head
    return s


def hard_filter(cands, vocab, used, failed):
    """①词源/source 校验（SEMANTIC_SECONDARY 禁入 query）②S6 去重 ③两两 Jaccard。
    kept 候选补 concept source 字段。返回 (kept, reasons)。"""
    kept, reasons = [], []
    v_obs = {i["term"] for i in vocab["observable"]}
    v_mech = {i["term"] for i in vocab["mechanism"]}
    v_lex = {i["term"] for i in vocab["lexical"]}
    v_proc = set(vocab["process"])
    v_mat = {w for lst in vocab["material_ctx"].values() for w in lst}
    v_sem = vocab["semantic"]
    # source 分层（schema v1.1）：VERIFIED_TERM / FROZEN_CONTEXT / SEMANTIC_SECONDARY
    def source_of(x):
        if x in v_obs | v_mech | v_lex:
            return "VERIFIED_TERM"
        if x in v_proc | v_mat:
            return "FROZEN_CONTEXT"
        if x in v_sem:
            return "SEMANTIC_SECONDARY"
        return None
    # 允许集合按 role（term 端允许的宽集合——program 校验核心在两端不可都是新词/非表词）
    def allowed_A(rt):
        if rt == "process_to_observable":
            return v_proc
        if rt == "lexical_to_observable":
            return v_lex
        if rt == "mechanism_to_observable":
            return v_mech
        if rt == "observable_transfer":
            return v_obs  # A=源 observable
        return v_mech  # mechanism_to_mechanism
    def allowed_B(rt):
        if rt in ("process_to_observable", "lexical_to_observable",
                  "mechanism_to_observable"):
            return v_obs
        if rt == "observable_transfer":
            return v_obs | v_mech  # B 可 observable 或 mechanism（作为桥）
        return v_mech

    seen_used = [frozenset(t) for t in used]
    seen_failed = [frozenset(t) for t in failed]
    seen_pairs = set()
    for c in cands:
        # 兜底：剥离 LLM 可能复制的 [domain] 展示后缀
        c["term_A"] = _strip_domain_tag(c.get("term_A", ""))
        c["term_B"] = _strip_domain_tag(c.get("term_B", ""))
        a, b = c.get("term_A", "").strip().lower(), c.get("term_B", "").strip().lower()
        rt = c.get("relation_type")
        if rt not in RELATION_TYPE_TIER:
            reasons.append((f"{a}|{b}", "unknown relation_type")); continue
        if a == b:
            reasons.append((f"{a}|{b}", "A==B")); continue
        # 词表（大小写不敏感但存原词；先按原词匹配，失败再小写匹配表内）
        def in_vocab(x, allow):
            if x in allow:
                return True
            return any(x.lower() == y.lower() for y in allow)
        # SEMANTIC_SECONDARY 禁入 query（evidence gate 纪律，即使词表匹配也不放行）
        if c["term_A"] in v_sem or c["term_B"] in v_sem or \
                c["term_A"].lower() in {s.lower() for s in v_sem} or \
                c["term_B"].lower() in {s.lower() for s in v_sem}:
            reasons.append((f"{a}|{b}", "SEMANTIC_SECONDARY 禁入 query")); continue
        if not (in_vocab(c["term_A"], allowed_A(rt)) and
                in_vocab(c["term_B"], allowed_B(rt))):
            reasons.append((f"{a}|{b}", f"词源违规 [{rt}]")); continue
        # S6 已用/失败去重：candidate 两端 ⊆ 某条 query terms → 该组合已被覆盖/已失败
        pair_set = {c["term_A"].lower(), c["term_B"].lower()}
        if any(pair_set <= ts for ts in seen_used):
            reasons.append((f"{a}|{b}", "S6 已用组合（增量≈0）")); continue
        if any(pair_set <= ts for ts in seen_failed):
            reasons.append((f"{a}|{b}", "S6 已失败 relation（禁重复）")); continue
        if (a, b) in seen_pairs or (b, a) in seen_pairs:
            reasons.append((f"{a}|{b}", "pair 重复")); continue
        seen_pairs.add((a, b))
        # schema v1.1：补 concept source（程序判定，不信 LLM）
        c["concept_A"] = c.pop("term_A", "")
        c["concept_A_source"] = source_of(c["concept_A"])
        c["concept_B"] = c.pop("term_B", "")
        c["concept_B_source"] = source_of(c["concept_B"])
        kept.append(c)
    return kept, reasons


def soft_score(c, vocab, used):
    """coverage_gap：observable 端若已被 S6 A 类用过→gap 小。返回组件 dict。"""
    tier = RELATION_TYPE_TIER[c["relation_type"]]
    b = (c.get("term_B") or c.get("concept_B") or "").lower()
    a_used_obs = {t.lower() for terms in used
                  for t in terms if t.lower() in
                  {i["term"].lower() for i in vocab["observable"]}}
    gap = 0.3 if b in a_used_obs else 1.0
    plaus = float(c.get("plausibility", 0.5))
    return {"tier_prior": W["tier_prior"][tier],
            "coverage_gap": gap,
            "plausibility": plaus,
            "hist_fail": 0.0,
            "score": round(W["tier_prior"][tier] + W["plausibility"] * plaus
                           + W["coverage_gap"] * gap, 3)}


# ── LLM 调用 ──
def backend_of(args):
    from search_engine.llm import create_backend
    key = args.api_key or os.environ.get("DEEPSEEK_API_KEY") or ""
    return create_backend(args.provider, api_key=key, model=args.model)


def _parse_list(text):
    """局部数组解析：parse_llm_json 是单对象语义（TermBank 用），relation 需要 list。
    兼容：纯数组 / fence / dict 包裹 / 前后文字截取。"""
    t = (text or "").strip()
    if t.startswith("```"):
        body = "\n".join(t.split("\n")[1:])
        if body.endswith("```"):
            body = body[:-3]
        t = body
    for cand in (t,):
        try:
            v = json.loads(cand)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
            if isinstance(v, dict):
                for k in ("candidates", "relations", "items", "results"):
                    if isinstance(v.get(k), list):
                        return [x for x in v[k] if isinstance(x, dict)]
                return []
        except (json.JSONDecodeError, ValueError):
            pass
    try:
        s, e = t.index("["), t.rindex("]") + 1
        v = json.loads(t[s:e])
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
    except (ValueError, json.JSONDecodeError):
        pass
    return []


async def run_llm_propose(prompts_by_tier, args):
    backend = backend_of(args)
    SYS = ("You are a rigorous materials-science retrieval strategist. "
           "Follow the user instruction exactly; output STRICT JSON only.")
    out = []
    for tier, prompt in prompts_by_tier.items():
        for attempt in range(3):
            try:
                resp = await backend.chat(SYS, prompt, temperature=0.4,
                                          max_tokens=8192)  # 防长数组被 2048 截断
                parsed = _parse_list(resp)
                if parsed:
                    out.extend(parsed)
                    print(f"  [tier {tier}] LLM 返回 {len(parsed)} candidates")
                    break
                print(f"  [tier {tier}] attempt{attempt+1}: 解析空/非 list，"
                      f"响应前 400 字: {repr(resp[:400])}")
            except Exception as e:
                print(f"  [tier {tier}] attempt{attempt+1} 失败: {str(e)[:100]}")
                await asyncio.sleep(3 * (attempt + 1))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--plan-only", action="store_true")
    ap.add_argument("--dry-samples", action="store_true",
                    help="内置 8 条样例走 filter+score（不调 LLM）")
    ap.add_argument("--provider", default="deepseek")
    ap.add_argument("--model", default=None)
    ap.add_argument("--api-key", default=None)
    args = ap.parse_args()

    vocab = build_vocab()
    traj = load("s6_trajectory.json")
    used, failed = build_feedback(traj)

    if args.plan_only:
        print("[plan-only] 词表规模:")
        print(f"  observable={len(vocab['observable'])} mechanism={len(vocab['mechanism'])}"
              f" lexical={len(vocab['lexical'])} process={len(vocab['process'])}")
        print("  plan:", TIER_PLAN, "(轮次×每轮 raw) → 默认输出 s7_relation_candidates.json")
        print("  词表样例 observable:", [i['term'] for i in vocab['observable']][:6])
        return

    if args.dry_samples:
        samples = [
            {"term_A": "curing", "term_B": "internal gap", "relation_type":
             "process_to_observable", "domain": "dental", "rationale": "d",
             "plausibility": 0.9, "expected_failure_mode": "LOW_YIELD"},
            {"term_A": "polymerization shrinkage", "term_B": "cusp deflection",
             "relation_type": "lexical_to_observable", "domain": "dental",
             "rationale": "d", "plausibility": 0.85,
             "expected_failure_mode": "REDUNDANT"},
            {"term_A": "photopolymeriz*", "term_B": "fabrication error",
             "relation_type": "process_to_observable", "domain": "optics",
             "rationale": "d", "plausibility": 0.8, "expected_failure_mode": "LOW_YIELD"},
            {"term_A": "stress relaxation", "term_B": "structural deformation",
             "relation_type": "mechanism_to_observable", "domain": "general",
             "rationale": "d", "plausibility": 0.7,
             "expected_failure_mode": "RELATION_EXPRESSION_ABSENT"},
            {"term_A": "fabrication error", "term_B": "printability",
             "relation_type": "observable_transfer", "domain": "sla",
             "rationale": "d", "plausibility": 0.75, "expected_failure_mode": "DOMAIN_DRIFT"},
            {"term_A": "made-up term", "term_B": "internal gap",
             "relation_type": "process_to_observable", "domain": "dental",
             "rationale": "x", "plausibility": 0.9, "expected_failure_mode": "LOW_YIELD"},
            {"term_A": "curing kinetics", "term_B": "volume holographic recording",
             "relation_type": "mechanism_to_observable", "domain": "optics",
             "rationale": "x", "plausibility": 0.6, "expected_failure_mode": "NO_NEW"},
            {"term_A": "addition-fragmentation chain transfer", "term_B":
             "stress relaxation", "relation_type": "mechanism_to_mechanism",
             "domain": "packaging", "rationale": "x", "plausibility": 0.65,
             "expected_failure_mode": "RELATION_EXPRESSION_ABSENT"},
        ]
        kept, reasons = hard_filter(samples, vocab, used, failed)
        print(f"[dry] 输入 {len(samples)} → 保留 {len(kept)} / 拒 {len(reasons)}")
        for k, r in reasons:
            print(f"  [REJECT] {k}: {r}")
        for c in kept:
            sc = soft_score(c, vocab, used)
            print(f"  [KEEP] {c['relation_type']:26s} {c.get('concept_A', c.get('term_A'))}"
                  f" × {c.get('concept_B', c.get('term_B'))}  score={sc['score']}")
        # 期望：made-up term 拒(词源)、fabrication error×printability 若 domain=sla 保留?
        return

    # ── 正式 LLM 生成（分批：TIER_PLAN 轮次 × 每轮 raw，防 max_tokens 截断）──
    try:
        miss_doc = load("r05_miss_query_reachability.json")
        miss_dir = str(miss_doc)[:400]
    except Exception:
        miss_dir = "主要缺陷 QUERY_EXPRESSIVITY：miss 论文标题不含 shrinkage 词面。"
    all_reasons = []
    cands = []
    for tier, rounds, per_round in TIER_PLAN:
        for rnd in range(1, rounds + 1):
            print(f"\n=== tier {tier} 轮 {rnd}/{rounds}（目标 {per_round}）===")
            prompt = build_prompt(vocab, used, failed, tier, per_round, miss_dir)
            got = asyncio.run(run_llm_propose({tier: prompt}, args))
            kept_r, reasons_r = hard_filter(got, vocab, used, failed)
            cands.extend(kept_r)
            all_reasons.extend(reasons_r)
            print(f"  本轮 raw {len(got)} → filter 保留 {len(kept_r)}")
    # reject taxonomy（按原因聚合）
    from collections import Counter
    tax = dict(Counter(r for _, r in all_reasons))
    print(f"\nLLM 原始 {len(cands) + len(all_reasons)} → hard filter 保留 {len(cands)}")
    print("=== reject taxonomy ===")
    for reason, n in sorted(tax.items(), key=lambda x: -x[1]):
        print(f"  {n:3d}  {reason}")
    for c in cands:
        sc = soft_score(c, vocab, used)
        c["score_components"] = sc
    out = {"version": "s7_relation_candidates_v1.1",
           "generated_at": __import__("datetime").datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
           "relation_tier": RELATION_TYPE_TIER,
           "plan": [{"tier": t, "rounds": r, "per_round": p} for t, r, p in TIER_PLAN],
           "soft_score_weights": W,
           "note": "schema v1.1：concept source ∈ VERIFIED_TERM/FROZEN_CONTEXT；"
                   "SEMANTIC_SECONDARY 禁入 query；排序仅用于 pilot 预算分配，非学习。",
           "reject_taxonomy": tax,
           "candidates": sorted(cands, key=lambda x: -x.get("score_components", {}).get("score", 0)),
           "rejected": [{"pair": k, "reason": r} for k, r in all_reasons]}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"[ok] {args.out}  ({len(cands)} candidates)")


if __name__ == "__main__":
    main()
