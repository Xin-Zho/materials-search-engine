#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/generate_relation_candidates_v2.py — S7 relation generator v2→v3（memory-guided）
（2026-09-07：QA v2 schema 冻结；v3 = round2 起注入关系记忆：prompt 文本 deny 摘要
 + hard_filter 程序 deny（S7 已裁决 pair 全量 + 运行时增长挡 PLAN 内重复）。
round2 实证：纯 prompt 提示挡不住 LLM 重复已裁决组合（6 条跨轮同款 + 14 条轮内
同款），故 deny 必须进 hard filter 程序层。

v1 失败根因：输入 = VERIFIED 40 + S6 trajectory → LLM 生成的是「63 篇已有论文关系
的回声」→ QA v2 判 VALID 17 / novelty LOW 18（VALID ≠ RUN：关系科学上对，但 S6
已扫过该 observable 社区 → 无探索价值）。20 候选真新仅 ~5-7。

v2 设计（用户裁决 2026-09-07）：
  ┌ 词源分层：s7_term_layers.json（R_main = VERIFIED 40 ∪ VERIFIED_NORMALIZED 8；
  │           R_discovery = SEMANTIC_CANDIDATE 8）——SEMANTIC 词不再整体禁入：
  │           可作 term_B 提议，报告时 R_main / R_discovery 分层，不污染正式 recall
  ├ novelty gate = 程序（用户 Q2 裁决）：按 term_B 词源层 + S6 A 类已用 observable
  │           覆盖度派生 exploration_level，不信 LLM 自报
  ├ LLM 只写 gap 论证 why_S5_failed（为什么 S5/S6 query 会漏 → QUERY_EXPRESSIVITY
  │           类 miss taxonomy 驱动），不写 novelty
  └ relation_type v2 枚举（用户 4 类，去 v1 回声源）：
       process_to_observable（主力）/ process_to_domain_observable /
       observable_transfer_with_process（D 类教训：必须 process 第三约束）/
       mechanism_to_observable（低优先，C 类失败教训）

输出：s7_relation_candidates_v2.json
  每候选：concept_A/B + *_source + relation_type + domain + rationale +
           why_S5_failed + novelty_gate{verdict,exploration_level,b_status,reason}
           + recall_layer(R_main|R_discovery)   ← query composer 分流用
QA 兼容：字段对齐 run_s7_relation_qa.py v2 输入（candidate_id/concept_A/B/source/
         relation_type/domain/rationale）。

用法：
  python tools/generate_relation_candidates_v2.py --plan-only     # 词源/池自检
  python tools/generate_relation_candidates_v2.py --dry-samples  # 样例走 filter+gate
  python tools/generate_relation_candidates_v2.py                 # LLM 生成
"""
import argparse
import asyncio
import json
import os
import sys
from collections import Counter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T = os.path.join(BASE, "data", "exports", "terminology")
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tools"))
sys.path.insert(0, os.path.join(BASE, "search_engine"))

# P1-A2: 冻结常量改从 pc001 topic 决策资产读（完整 --topic 化列入 P1-B 前置）。
import json as _json
_BSPEC = _json.load(open(os.path.join(
    BASE, "topics", "photopolymerization_shrinkage", "bridge_spec.json"),
    encoding="utf-8"))
ROLE_CORRECTION = _BSPEC.get("role_correction") or {}  # noqa: E402  role 修正（冻结）

DEFAULT_OUT = os.path.join(T, "s7_relation_candidates_v2.json")

# ── relation_type v2 枚举（用户 4 类；tier 预算权重）─────────────────────────
RELATION_TYPE_V2 = {
    "process_to_observable": {"tier": "T1", "prior": 1.0},
    "process_to_domain_observable": {"tier": "T1", "prior": 1.0},
    "observable_transfer_with_process": {"tier": "T2", "prior": 0.7},
    "mechanism_to_observable": {"tier": "T2", "prior": 0.4},   # 低优先
}

# 生成预算（轮次 × 每轮 raw）：~130 raw → 吸收 filter 损耗后 valid 尽量 >60
PLAN = [
    ("process_to_observable", 4, 12),
    ("process_to_domain_observable", 4, 12),
    ("observable_transfer_with_process", 2, 10),
    ("mechanism_to_observable", 2, 8),
]

# why_S5_failed 枚举（miss taxonomy 驱动，非 LLM 自由发挥）
WHY_FAILED_SET = {
    "QUERY_EXPRESSIVITY",   # S5/S6 Boolean 无 shrinkage 词面表达不到该 relation
    "NORMALIZED_FORM",      # 词以连字符/复合/间隔形态出现，exact 匹配漏
    "DOMAIN_DRIFT",         # 跨 domain 无 process 桥（S6 D 类教训）
    "MECHANISM_MEDIATED",   # 需 mechanism 中介，S5 仅 process 锚表达不到
    "LOW_YIELD_EXPECTED",   # 预期低产但可低成本试（exploratory）
}

# process 锚（冻结模板；A 端 process 类）
PROCESS_ANCHORS = ["curing", "polymeriz*", "photocuring", "photopolymeriz*"]
# 可迁移 domain 目标（observable_transfer 第三约束用；非裸 domain 标签）
DOMAINS = ["dental", "composites", "sla", "coatings", "optics", "packaging", "general"]


def load(name):
    return json.load(open(os.path.join(T, name), encoding="utf-8"))


def build_pools():
    """词源唯一入口 = s7_term_layers.json（不直接读 term_bank，防漂移）。
    A/B 池按 role + ROLE_CORRECTION 组装。"""
    layers = load("s7_term_layers.json")
    verified = layers["layers"]["R_main_verified"]     # 40
    norm = layers["layers"]["R_main_normalized"]       # 8（升级）
    disc = layers["layers"]["R_discovery"]             # 8（探索）

    def by_role(items, role, exclude_context=True):
        out = []
        for e in items:
            if e["role"] != role:
                continue
            if exclude_context and ROLE_CORRECTION.get(e["term"]) == "context":
                continue  # optics 4 词不当 observable（S6 教训）
            out.append(e)
        return out

    obs_v = by_role(verified, "observable")   # 15（剔除 context 4）
    obs_n = by_role(norm, "observable")       # 5（marginal gap/post-op/warpage/deformation/crack）
    obs_d = by_role(disc, "observable")       # 2（dimensional accuracy/delamination）
    mech_v = by_role(verified, "mechanism")   # 13
    mech_n = by_role(norm, "mechanism")       # 3（polym-shrink-stress/internal stress/stress development）
    mech_d = by_role(disc, "mechanism")       # 3（residual stress/cure-induced stress/crosslinking density）
    lex_v = by_role(verified, "lexical")      # 8（保留：mechanism 语境/对照，不直接作 B 回声源）

    def pack(items, layer):
        return [{"term": e["term"], "domain": e["domain"],
                 "source_status": e["source_status"], "recall_layer": layer} for e in items]

    pools = {
        "A_process": [{"term": t, "domain": "general", "source_status": "FROZEN_CONTEXT",
                       "recall_layer": "R_main"} for t in PROCESS_ANCHORS],
        "A_observable": pack(obs_v, "R_main") + pack(obs_n, "R_main"),
        "A_mechanism": pack(mech_v, "R_main") + pack(mech_n, "R_main"),
        # B 池三层全开（R_discovery 词可作 B，报告层分流）
        "B_observable": pack(obs_v, "R_main") + pack(obs_n, "R_main")
                        + pack(obs_d, "R_discovery"),
        "B_mechanism": pack(mech_v, "R_main") + pack(mech_n, "R_main")
                       + pack(mech_d, "R_discovery"),
        # 展示用（domain context 提示；第三约束候选池）
        "domain_ctx": _mat_ctx(),
    }
    return pools


def _mat_ctx():
    """material context（第三约束候选；与 v1 build_vocab 对齐）"""
    return {
        "dental": ["dental", "dentistry", "restorative", "composite", "resin composite",
                   "tooth", "enamel", "dentin", "adhesive"],
        "3dp": ["3d print", "additive manufacturing", "stereolithograph", "sla",
                "vat photopolymerization", "dlp", "resin printing"],
        "optics": ["holograph", "optical", "lithograph", "photoresist", "recording"],
        "packaging": ["molding compound", "encapsulant", "epoxy", "semiconductor"],
        "coatings": ["coating", "film", "paint", "varnish"],
        "composites": ["composite", "fiber", "filler"],
    }


def load_s6_feedback():
    """S6 trajectory → (used_terms, failed_terms, A_used_observables)。
    A_used_observables 是 novelty gate 的核心参照：S6 A 类已扫过的 observable。"""
    traj = load("s6_trajectory.json")
    used, failed = [], []
    a_used_obs = set()
    for a in traj["actions"]:
        terms = list(a.get("terms", []))
        q = a.get("query_string", "")
        # 按 anchor 展开左锚词（v1 build_feedback 同逻辑）
        SHRINK = ["polymerization shrinkage", "polymerization contraction",
                  "volume contraction", "shrinkage"]
        PROC = ["curing", "polymeriz*", "photocuring", "photopolymeriz*"]
        if a.get("contains_shrinkage_anchor"):
            terms += [t for t in SHRINK if t.lower() in q.lower()]
        elif "polymeriz" in q or "photocuring" in q or "photopolymeriz" in q:
            terms += [t for t in PROC if t.lower() in q.lower()]
        used.append(terms)
        f = a.get("features", {})
        l = a.get("labels", {})
        fr = None
        if f.get("scopus_total_hits") == 0:
            fr = "ZERO_HIT"
        elif f.get("new_vs_S5") == 0:
            fr = "NO_NEW"
        elif l.get("R_plus_U") == 0:
            fr = "RELATION_EXPRESSION_ABSENT"
        if fr:
            failed.append(terms)
        if a.get("family") == "A":
            a_used_obs.update(t for t in a.get("terms", []))
    return used, failed, a_used_obs


def load_miss_taxonomy():
    """r05 miss failure modes → LLM 的 gap 方向（只作提示，不作字面词）"""
    try:
        d = load("r05_miss_failure_modes.json")
        dist = d.get("pattern_distribution", {})
        bp = d.get("break_pairs", {})
        top = sorted(bp.items(), key=lambda x: -x[1])[:3]
        return (f"R05 43 miss 主因：QUERY_EXPRESSIVITY——miss 标题不含 shrinkage 词面，"
                f"36 条 S5 query 没指向它。break_pairs 前 3: "
                + "; ".join(f"{k}({v})" for k, v in top))
    except Exception:
        return "R05 miss 主因 QUERY_EXPRESSIVITY：miss 论文标题不含 shrinkage 词面。"


def load_relation_memory():
    """P1/P2 关系记忆（build_s7_relation_memory.py 产物）→ dict。
    含 generator_context.prompt_ready（prompt 注入文本）与 deny_pairs
    （全部已裁决 pair —— hard filter 程序 deny）。文件缺失返回 None。"""
    try:
        return load("s7_relation_memory.json")
    except Exception:
        return None


def memory_deny_set(mem):
    """memory deny_pairs → set[(A_norm, B_norm)]（程序 deny 判据）。"""
    deny = set()
    if not mem:
        return deny
    for row in mem.get("generator_context", {}).get("deny_pairs", []):
        a, b = row[0], row[1]
        deny.add((a.strip().lower().rstrip("*"),
                  b.strip().lower().rstrip("*")))
    return deny


# ── novelty gate（程序；用户 Q2 裁决：不信 LLM 自报）────────────────────────
def novelty_gate(b_term, b_source, b_layer, rt, a_used_obs):
    """按 B 端词源层 + S6 A 类覆盖度派生 exploration_level。
    verdict:
      FRESH_NORMALIZED  — B ∈ VERIFIED_NORMALIZED 升级词（S6 从未用其作 observable 右锚）
      DISCOVERY_ONLY     — B ∈ SEMANTIC_CANDIDATE（R_discovery 报告，不污染 R_main）
      S6_UNUSED_VERIFIED — B ∈ VERIFIED 40 但 S6 A 类未扫过
      CLUSTER_DUP        — B ∈ S6 A 类已用 observable → 低价值（回声）
    mechanism 类 relation 整体降档（C 类失败先例）。"""
    if b_source == "VERIFIED_NORMALIZED":
        return {"verdict": "FRESH_NORMALIZED", "exploration_level": "HIGH",
                "reason": "B=条件升级词，S6 从未用其作 observable 右锚 → 词面新社区"}
    if b_source == "SEMANTIC_CANDIDATE":
        return {"verdict": "DISCOVERY_ONLY", "exploration_level": "HIGH",
                "reason": "B=SEMANTIC_CANDIDATE → R_discovery 报告层，正式 recall 不混入"}
    if b_term in a_used_obs or b_term.lower() in {o.lower() for o in a_used_obs}:
        lvl = "LOW" if rt.startswith("process") else "MEDIUM"
        return {"verdict": "CLUSTER_DUP", "exploration_level": lvl,
                "reason": f"B∈S6 A 类已用 observable → cluster 回声；{rt} 下探索价值低"}
    return {"verdict": "S6_UNUSED_VERIFIED", "exploration_level": "MEDIUM",
            "reason": "B∈VERIFIED 但 S6 A 类未扫 → 中等；依赖 QA 复核 cluster 级 novelty"}


def hard_filter(cands, pools, used, failed, s7_deny=None):
    """v2/v3 filter：
    ① 词源：A/B ∈ 对应池（按 relation_type 允许集合）
    ② S6 已用/失败 term-set 子集去重
    ③ S7 已裁决 pair deny（memory 全量 deny_pairs；round2 教训：LLM 会重复已裁决
       组合——RC01/RC25/RC22 等 6 条跨轮同款 + 14 条轮内同款，纯 prompt 挡不住）
    ④ pair 重复（大小写不敏感、顺序不敏感）
    ⑤ why_S5_failed ∈ 枚举
    source_status/recall_layer 由程序重算（不信 LLM 输入）。
    s7_deny：set[(A_norm, B_norm)]（A/B 已 strip/lower/rstrip('*')）。
    返回 (kept, reasons)。"""
    kept, reasons = [], []
    sem_pool = {p["term"].lower() for p in pools["B_observable"] + pools["B_mechanism"]
                if p["source_status"] == "SEMANTIC_CANDIDATE"}
    seen_pairs = set()

    def pool_terms(key):
        return {p["term"].lower() for p in pools[key]}

    allowed = {
        "process_to_observable": ("A_process", "B_observable"),
        "process_to_domain_observable": ("A_process", "B_observable"),
        "observable_transfer_with_process": ("A_observable", "B_observable"),
        "mechanism_to_observable": ("A_mechanism", "B_observable"),
    }

    def lookup(pool_key, raw):
        raw_s = raw.strip().lower()
        for p in pools[pool_key]:
            if p["term"].lower() == raw_s:
                return p
        # 兼容 QUERY_FORM_MAP 类（词尾泛化复数裁剪后匹配）
        return None

    for c in cands:
        rt = c.get("relation_type")
        if rt not in RELATION_TYPE_V2:
            reasons.append((str(c.get("concept_A")) + "|" + str(c.get("concept_B")),
                            f"未知 relation_type {rt}")); continue
        a_pool, b_pool = allowed[rt]
        pa = lookup(a_pool, c.get("concept_A", ""))
        pb = lookup(b_pool, c.get("concept_B", ""))
        if pa is None:
            reasons.append((f"{c.get('concept_A')}|{c.get('concept_B')}",
                            f"词源违规 A [{rt}]")); continue
        if pb is None:
            reasons.append((f"{c.get('concept_A')}|{c.get('concept_B')}",
                            f"词源违规 B [{rt}]")); continue
        if pa["term"].lower() == pb["term"].lower():
            reasons.append((f"{pa['term']}|{pb['term']}", "A==B")); continue
        wf = c.get("why_S5_failed")
        if wf not in WHY_FAILED_SET:
            reasons.append((f"{pa['term']}|{pb['term']}", f"why_S5_failed 非法: {wf}")); continue
        # S6 去重（子集包含）
        pair_set = {pa["term"].lower(), pb["term"].lower()}
        seen_used = [frozenset(t) for t in used]
        seen_failed = [frozenset(t) for t in failed]
        if any(pair_set <= ts for ts in seen_used):
            reasons.append((f"{pa['term']}|{pb['term']}", "S6 已用组合（增量≈0）")); continue
        if any(pair_set <= ts for ts in seen_failed):
            reasons.append((f"{pa['term']}|{pb['term']}", "S6 已失败 relation")); continue
        # S7 已裁决 deny（v3：memory 全量已裁决 pair——RUN 已排队、SKIP 已判死）
        if s7_deny:
            dk = (pa["term"].lower().rstrip("*"), pb["term"].lower().rstrip("*"))
            if dk in s7_deny or (dk[1], dk[0]) in s7_deny:
                reasons.append((f"{pa['term']}|{pb['term']}",
                                "S7 已裁决（memory deny）")); continue
        key = (pa["term"].lower(), pb["term"].lower())
        if key in seen_pairs or (key[1], key[0]) in seen_pairs:
            reasons.append((f"{pa['term']}|{pb['term']}", "pair 重复")); continue
        seen_pairs.add(key)
        # 程序重算 source / layer（不信 LLM 自填 status）
        c["concept_A"] = pa["term"]
        c["concept_A_source"] = pa["source_status"]
        c["concept_B"] = pb["term"]
        c["concept_B_source"] = pb["source_status"]
        c["recall_layer"] = pb["recall_layer"]
        kept.append(c)
    return kept, reasons


# ── prompt 组装 ──────────────────────────────────────────────────────────────
def fmt(pool):
    return " | ".join(f"{p['term']}" for p in pool)


def build_prompt(pools, used, failed, a_used_obs, rt, count, miss_dir, rnd,
                 mem_txt=None):
    """v2 prompt：novelty 指令 = 打开语言缺口（升级词/Discovery 词），不是找合理关系。
    给 term_B 候选三层：B 端明确允许 SEMANTIC 升级/discovery 词——它们才是未覆盖社区。
    mem_txt：P1 关系记忆（s7_relation_memory.json prompt_ready）；有则替换 S6 ALREADY
    SWEPT 纯词面段——记忆含真实产出（R/U/new）、失败模式与 QA 裁决，比 used_txt
    更强的 deny-set + 成功参照。None 时回退旧路径（兼容测试/旧调用）。"""
    tier = RELATION_TYPE_V2[rt]["tier"]
    a_pool, b_pool = {
        "process_to_observable": ("A_process", "B_observable"),
        "process_to_domain_observable": ("A_process", "B_observable"),
        "observable_transfer_with_process": ("A_observable", "B_observable"),
        "mechanism_to_observable": ("A_mechanism", "B_observable"),
    }[rt]

    if rt == "process_to_observable":
        rules = ("A=process 锚，B=observable。目标：无 shrinkage 字面地表达固化过程的"
                 "可观察后果，突破 S5/S6 已扫社区。PRIORITIZE 标 * 的 B 候选"
                 "（升级词/discovery 词，S6 从未扫过它们）。")
    elif rt == "process_to_domain_observable":
        rules = ("A=process 锚，B=observable，domain 是该 observable 的**自然语言领域**"
                 "（论文作者所属学科词汇），不是松散标签。B 优先选标 * 的新词。")
    elif rt == "observable_transfer_with_process":
        rules = ("A=源 observable（已在某 domain 验证），B=目标 observable。必须带 process "
                 "第三约束（curing/polymeriz* 等）作为桥——S6 D 类裸 domain transfer 失败教训。")
    else:
        rules = ("A=mechanism，B=observable（低优先但允许）。仅当 A 机制在真实文献中直接"
                 "导致 B 才提议；S6 C 类 mechanism×observable 大面积失败，若无强 rationale 不要凑。")

    b_list = [f"{p['term']}{'*' if p['source_status'] in ('VERIFIED_NORMALIZED', 'SEMANTIC_CANDIDATE') else ''}"
              for p in pools[b_pool]]
    used_txt = ("；".join(" AND ".join(t) for t in used[-8:]) or "无")[:700]
    # P1 关系记忆注入：deny-set（已扫+已失败+QA SKIP 变体）+ RUN 排队禁重提 + thin domain
    memory_block = (mem_txt if mem_txt
                    else f"S6 ALREADY SWEPT (do not repeat these observable "
                         f"clusters): {used_txt}")
    prompt = f"""You are a materials-science retrieval strategist for a HIGH-RECALL discovery agent.
Propose {count} SCIENTIFIC RELATIONS that could reach paper communities S5/S6 did NOT cover.
Rubric: S7 relation proposal v2, relation_type={rt}, round {rnd}.

RESEARCH FRAME: polymerization shrinkage / shrinkage stress in photopolymerization & light-curing
systems, and observable consequences (deformation, dimensional error, interface defects, printing
artifacts...). CRITICAL: express relations WITHOUT writing "shrinkage" — S5/S6 已证该词面饱和。

TERM SOVEREIGNTY: pick term strings EXACTLY from the pools. Never invent/rephrase.
  - A candidates: {fmt(pools[a_pool])}
  - B candidates (* = S6 未扫过的新词面，PRIORITIZE): { ' | '.join(b_list) }
  - domain 提示（仅作语境，勿作 term）: {json.dumps(pools['domain_ctx'], ensure_ascii=False)[:600]}

RELATION RULES: {rules}

MISS TAXONOMY (why relations below must be tried — gap direction): {miss_dir}

{memory_block}

Per relation output a gap argument why_S5_failed ∈ {sorted(WHY_FAILED_SET)} —
it must explain the RETRIEVAL failure (why a literal S5/S6 query missed), not just scientific truth.

Output STRICT JSON array only (no prose/fences), {count} objects:
[{{"concept_A": "<exact>", "concept_B": "<exact>", "relation_type": "{rt}",
   "domain": "<target domain for query context>", "rationale": "<1 sentence: why authors write both>",
   "why_S5_failed": "<from enum>", "plausibility": 0.0-1.0}}]
No A==B, no duplicates of swept clusters, plausibility > 0.6."""
    return prompt


# ── LLM 调用（复用 v1 的 _parse_list/backend 模式）──────────────────────────
def _parse_list(text):
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


def backend_of(args):
    from search_engine.llm import create_backend
    key = args.api_key or os.environ.get("DEEPSEEK_API_KEY") or ""
    return create_backend(args.provider, api_key=key, model=args.model)


# ── v3 novelty_risk 层（2026-09-07 calibration；verdict 不动，只作 risk 标记）──
# QA v2 盲评 40 条显示：FRESH_NORMALIZED 19 条里 10 条 QA=LOW，全死于 B 端词面是
# S6 已扫 observable 的变体/同族（warpage~structural deformation、marginal gap~
# marginal leakage）。v2 gate 只按词源层判 verdict 抓不到变体。
# v3 双通道（用户 Q1 裁决）：① 词典通道=build_s7_variant_families.py 冻结同族表；
# ② embedding 通道（可选，--embed）对词典未覆盖 B 端算与 S6 已扫 observables 相似度。
# 设计原则：novelty_risk 只作排序/降档参考，verdict 不变——QA 是终审
# （RC26 delayed gel point→warpage QA=HIGH 证明变体命中≠必死，词典误杀会丢真新）。

_VARIANT_CACHE = None


def _variant_families():
    global _VARIANT_CACHE
    if _VARIANT_CACHE is None:
        try:
            _VARIANT_CACHE = load("s7_variant_families.json")["families"]
        except Exception:
            _VARIANT_CACHE = []
    return _VARIANT_CACHE


def _domain_saturation():
    try:
        return load("s7_variant_families.json").get("domain_saturation", {})
    except Exception:
        return {}


def _family_of_term(term, domain):
    """term → 命中的 family_id 列表（含 domain 语境过滤）。
    family.domain is None → 通用族（语言层，不限 domain）；
    family.domain 非空 → 候选 domain ∈ family.domain 才命中（防跨 domain 复活误杀，
    如 crack@coatings QA=MEDIUM——F03 crack 族 domain=dental，coating 不命中）。"""
    fams = _variant_families()
    dom_sat = _domain_saturation()
    hit = []
    for f in fams:
        members = [f["canonical"].lower()] + [v.lower() for v in f["variants"]]
        if term.lower().rstrip("*").strip() not in members:
            continue
        fdom = f.get("domain")
        if fdom is None or domain in fdom:
            hit.append(f["family_id"])
    return hit


def compute_novelty_risk(c, a_used_obs):
    """三层 risk 标记（verdict 不变；QA 终审）：
    variant_dup  — B 端词面命中同族词典（domain 语境过滤）
    a_side_swept — A 端 ∈ S6 A 类已扫 observable（已扫关系网内的后果链回声）
    domain_sat   — 仅作 amplifier：与其他 flag 同现才输出（domain 高饱和语境强化
                   variant/a-side 风险；dental post-op sensitivity 单标会被误降——
                   QA 4/5 MEDIUM RUN 证明饱和≠必死）
    返回 {"flags": [...], "family_ids": [...], "note": ...} 或 {"flags": []}。
    """
    flags, fam_ids, notes = [], [], []
    B = (c.get("concept_B") or "").lower().rstrip("*").strip()
    dom = c.get("domain")
    fams_hit = _family_of_term(B, dom)
    if fams_hit:
        flags.append("variant_dup")
        fam_ids.extend(fams_hit)
        notes.append(f"B端词典命中 {fams_hit} (domain={dom})")
    A = (c.get("concept_A") or "").lower().rstrip("*").strip()
    a_used_low = {x.lower() for x in a_used_obs}
    if A in a_used_low:
        flags.append("a_side_swept")
        notes.append(f"A端∈S6已扫 observable（关系网内回声风险）: {c.get('concept_A')}")
    sat = _domain_saturation().get(dom, 0.0)
    if sat >= 0.6 and flags:
        flags.append("domain_sat")
        notes.append(f"domain {dom} QA LOW率 {sat:.0%}（饱和，amplifier）")
    risk = {"flags": flags, "family_ids": fam_ids}
    if notes:
        risk["note"] = "; ".join(notes)
    return risk


def annotate_risks(cands, a_used_obs):
    """给候选批量附加 novelty_risk（就地）。"""
    for c in cands:
        c["novelty_risk"] = compute_novelty_risk(c, a_used_obs)
    return cands


async def run_llm(prompt, args):
    backend = backend_of(args)
    SYS = ("You are a rigorous materials-science retrieval strategist. "
           "Follow the user instruction exactly; output STRICT JSON only.")
    for attempt in range(3):
        try:
            resp = await backend.chat(SYS, prompt, temperature=0.4, max_tokens=8192)
            parsed = _parse_list(resp)
            if parsed:
                return parsed
            print(f"  attempt{attempt + 1}: 解析空，前 300 字: {repr(resp[:300])}")
        except Exception as e:
            print(f"  attempt{attempt + 1} 失败: {str(e)[:120]}")
            await asyncio.sleep(3 * (attempt + 1))
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--plan-only", action="store_true")
    ap.add_argument("--dry-samples", action="store_true")
    ap.add_argument("--provider", default="deepseek")
    ap.add_argument("--model", default=None)
    ap.add_argument("--api-key", default=None)
    args = ap.parse_args()

    pools = build_pools()
    used, failed, a_used_obs = load_s6_feedback()
    miss_dir = load_miss_taxonomy()

    if args.plan_only:
        print("[plan-only] v2 词源池:")
        for k in ("A_process", "A_observable", "A_mechanism",
                  "B_observable", "B_mechanism"):
            src = Counter(p["source_status"] for p in pools[k])
            print(f"  {k:14s} n={len(pools[k]):3d}  {dict(src)}")
        print("  S6 A 类已用 observable（CLUSTER_DUP 参照）:", sorted(a_used_obs))
        print("  plan:", [(rt, r, p) for rt, r, p in PLAN],
              "≈", sum(r * p for _, r, p in PLAN), "raw")
        return

    if args.dry_samples:
        samples = [
            # 应 KEEP + FRESH_NORMALIZED：warpage 升级词（S6 未扫过）——v2 核心新源
            {"concept_A": "curing", "concept_B": "warpage",
             "relation_type": "process_to_observable", "domain": "sla",
             "rationale": "x", "why_S5_failed": "NORMALIZED_FORM", "plausibility": 0.85},
            # 应 KEEP + DISCOVERY_ONLY：dimensional accuracy → R_discovery 报告
            {"concept_A": "polymeriz*", "concept_B": "dimensional accuracy",
             "relation_type": "process_to_observable", "domain": "sla",
             "rationale": "x", "why_S5_failed": "QUERY_EXPRESSIVITY", "plausibility": 0.8},
            # 应 KEEP + FRESH_NORMALIZED：crack 升级词 observable（SOURCE_MISMATCH 但词真实）
            {"concept_A": "curing", "concept_B": "crack",
             "relation_type": "process_to_observable", "domain": "dental",
             "rationale": "x", "why_S5_failed": "QUERY_EXPRESSIVITY", "plausibility": 0.85},
            # 应 KEEP + S6_UNUSED_VERIFIED：film shrinkage ∈ VERIFIED 40 但 S6 A 类未扫 coatings
            {"concept_A": "curing", "concept_B": "film shrinkage",
             "relation_type": "process_to_observable", "domain": "coatings",
             "rationale": "x", "why_S5_failed": "DOMAIN_DRIFT", "plausibility": 0.8},
            # 应 REJECT：cusp deflection ∈ S6 A 类已用 → CLUSTER_DUP（软降级非拒？）
            # hard_filter 不清 cusp deflection（v1 幸存项）→ 留给 novelty gate 判 LOW
            {"concept_A": "curing", "concept_B": "cusp deflection",
             "relation_type": "process_to_observable", "domain": "dental",
             "rationale": "x", "why_S5_failed": "QUERY_EXPRESSIVITY", "plausibility": 0.9},
            # 应 REJECT：made-up 词（词源违规 A）
            {"concept_A": "made-up term", "concept_B": "warpage",
             "relation_type": "process_to_observable", "domain": "sla",
             "rationale": "x", "why_S5_failed": "QUERY_EXPRESSIVITY", "plausibility": 0.9},
            # 应 REJECT：S6 已失败 relation（stress relaxation × internal gap 属 C-11 系）
            {"concept_A": "stress relaxation", "concept_B": "internal gap",
             "relation_type": "mechanism_to_observable", "domain": "dental",
             "rationale": "x", "why_S5_failed": "MECHANISM_MEDIATED", "plausibility": 0.8},
            # 应 REJECT：SEMANTIC 词 residual stress 作 A 端（A 必须 VERIFIED/FROZEN；升级门只管 B）
            {"concept_A": "residual stress", "concept_B": "warpage",
             "relation_type": "process_to_observable", "domain": "sla",
             "rationale": "x", "why_S5_failed": "QUERY_EXPRESSIVITY", "plausibility": 0.8},
            # 应 KEEP + DISCOVERY_ONLY：residual stress（CORPUS_ABSENT）作 B，R_discovery
            {"concept_A": "curing", "concept_B": "residual stress",
             "relation_type": "process_to_observable", "domain": "sla",
             "rationale": "x", "why_S5_failed": "QUERY_EXPRESSIVITY", "plausibility": 0.75},
            # 应 REJECT：why_S5_failed 非法值
            {"concept_A": "curing", "concept_B": "marginal gap",
             "relation_type": "process_to_observable", "domain": "dental",
             "rationale": "x", "why_S5_failed": "MAYBE_GOOD", "plausibility": 0.8},
        ]
        kept, reasons = hard_filter(samples, pools, used, failed)
        print(f"[dry] 输入 {len(samples)} → 保留 {len(kept)} / 拒 {len(reasons)}")
        for k, r in reasons:
            print(f"  [REJECT] {k}: {r}")
        for c in kept:
            ng = novelty_gate(c["concept_B"], c["concept_B_source"], c["recall_layer"],
                              c["relation_type"], a_used_obs)
            c["novelty_gate"] = ng
            risk = compute_novelty_risk(c, a_used_obs)
            c["novelty_risk"] = risk
            print(f"  [KEEP] {c['relation_type']:30s} {c['concept_A']} × {c['concept_B']}"
                  f"  src={c['concept_B_source']}  layer={c['recall_layer']}"
                  f"  [{ng['verdict']}/{ng['exploration_level']}]"
                  f"  risk={risk['flags'] or '-'}")
        return

    # ── 正式 LLM 生成（v3 memory-guided）─────────────────────────────────
    all_reasons = []
    cands = []
    mem = load_relation_memory()
    deny_set = memory_deny_set(mem)      # 初始 = 全部已裁决 pair
    deny_initial_n = len(deny_set)
    mem_txt = None
    if mem:
        mem_txt = mem["generator_context"]["prompt_ready"]
        print(f"[memory] 注入 prompt ({len(mem_txt)} chars) + hard deny "
              f"{len(deny_set)} 条已裁决 pair")
    else:
        print("[memory] 无 s7_relation_memory.json → 回退旧路径（无 deny）")
    for rt, rounds, per_round in PLAN:
        for rnd in range(1, rounds + 1):
            print(f"\n=== {rt} 轮 {rnd}/{rounds}（目标 {per_round}）===")
            prompt = build_prompt(pools, used, failed, a_used_obs, rt,
                                  per_round, miss_dir, rnd, mem_txt)
            got = asyncio.run(run_llm(prompt, args))
            kept_r, reasons_r = hard_filter(got, pools, used, failed,
                                            s7_deny=deny_set)
            for c in kept_r:
                c["novelty_gate"] = novelty_gate(
                    c["concept_B"], c["concept_B_source"], c["recall_layer"],
                    c["relation_type"], a_used_obs)
                c["novelty_risk"] = compute_novelty_risk(c, a_used_obs)
                # deny 集运行时增长：挡 PLAN 内跨调用重复（round1 RC30/RC40 教训）
                deny_set.add((c["concept_A"].lower().rstrip("*"),
                              c["concept_B"].lower().rstrip("*")))
            cands.extend(kept_r)
            all_reasons.extend(reasons_r)
            print(f"  本轮 raw {len(got)} → filter 保留 {len(kept_r)}"
                  f"（deny 累积 {len(deny_set)}）")

    tax = dict(Counter(r for _, r in all_reasons))
    print(f"\nLLM raw ≈ {len(cands) + len(all_reasons)} → hard filter 保留 {len(cands)}")
    print("=== reject taxonomy ===")
    for reason, n in sorted(tax.items(), key=lambda x: -x[1]):
        print(f"  {n:3d}  {reason}")
    print("=== novelty gate 分布 ===")
    from collections import Counter as C2
    vd = C2(c["novelty_gate"]["verdict"] for c in cands)
    el = C2(c["novelty_gate"]["exploration_level"] for c in cands)
    rl = C2(c["recall_layer"] for c in cands)
    rf = C2(tuple(c.get("novelty_risk", {}).get("flags", [])) for c in cands)
    print(f"  verdict: {dict(vd)}")
    print(f"  exploration_level: {dict(el)}")
    print(f"  recall_layer: {dict(rl)}")
    print(f"  novelty_risk flags: {dict(rf)}")

    for i, c in enumerate(cands):
        c["candidate_id"] = f"RC{i + 1:02d}"
    out = {
        "version": "s7_relation_candidates_v2",
        "generated_at": __import__("datetime").datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "status": "DEVELOPMENT_PILOT",
        "contract": (
            "QA v2 schema 冻结（2026-09-07）；novelty gate=程序（VERIFIED_NORMALIZED→HIGH/"
            "SEMANTIC_CANDIDATE→R_discovery HIGH/CLUSTER_DUP→LOW），LLM 只写 why_S5_failed；"
            "R_main 词源=VERIFIED∪VERIFIED_NORMALIZED(48)，R_discovery 单独报告；"
            "relation_type 枚举=用户 v2 四类（process_to_observable/process_to_domain_"
            "observable/observable_transfer_with_process/mechanism_to_observable低优先）"),
        "relation_type_v2": {k: v["tier"] for k, v in RELATION_TYPE_V2.items()},
        "why_S5_failed_enum": sorted(WHY_FAILED_SET),
        "plan": [{"relation_type": rt, "rounds": r, "per_round": p}
                 for rt, r, p in PLAN],
        "novelty_gate_distribution": {
            "verdict": dict(vd), "exploration_level": dict(el),
            "recall_layer": dict(rl)},
        "novelty_risk_distribution": {str(k): v for k, v in rf.items()},
        "memory_guided": {
            "memory_file": "s7_relation_memory.json",
            "deny_initial": deny_initial_n,
            "deny_grown": len(deny_set),   # 含本轮运行时增长
        },
        "reject_taxonomy": tax,
        "candidates": cands,
        "rejected": [{"pair": k, "reason": r} for k, r in all_reasons],
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n[ok] {args.out} ({len(cands)} candidates)")


if __name__ == "__main__":
    main()
