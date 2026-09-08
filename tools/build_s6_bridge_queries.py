#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/build_s6_bridge_queries.py — S6 Semantic Bridge deterministic assembler（2026-09-04 用户拍板）。

纪律（写死，用户 2026-09-04 22:51）：
  1. 词源纪律：除冻结 process/domain context vocabulary 外，query 中任何内容词
     必须来自 S6_TERM_BANK VERIFIED_EXACT（40）。禁止手工塞词。
  2. LLM 不输出 Boolean：本工具纯 deterministic（词源已在 term bank 层冻结）。
  3. A 类（anchor-free observable×process）为主力；B 是 shrinkage-anchor 对照组（少量）；
     C 是 mechanism×observable 桥；D 是 cross-domain transfer（探索，少）。
  4. A 的 observable 必须不含 shrink/contraction 词面（anchor-free）；role 不盲信 LLM，
     组装前经 ROLE_CORRECTION 修正（fabrication error→observable；holography 5 词→context）。
  5. query normalization 派生必须在 pilot 前写死（QUERY_FORM_MAP），禁隐性扩词。
  6. provenance 全记录：family/domain/strategy/terms/term_source/context_source/
     contains_shrinkage_anchor/exploratory，供 pilot 后比较 anchor-free vs shrinkage-anchor yield
     （exploratory=True：A7 softening effect、A8 printability、D2 structural deformation→3DP；
     低 precision 只代表该具体迁移弱，不归因 family 失败——用户 2026-09-04 22:58）。
  7. context vocabulary 只来自 S5 冻结 LAYER_RULES（build_cross_layer_queries.py 2026-08-31）
     + s5_final_actions.json（36 actions），不新造。

输入：data/exports/terminology/s6_term_bank.json（FROZEN 2026-09-04）
输出：data/exports/terminology/s6_bridge_queries.json
"""
import datetime
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T = os.path.join(BASE, "data", "exports", "terminology")
TERM_BANK = os.path.join(T, "s6_term_bank.json")
DEFAULT_OUT = os.path.join(T, "s6_bridge_queries.json")

# ─────────────────────────────────────────────────────────────────────────────
# 冻结词汇（全部有据可查；出处写死，禁改）
# ─────────────────────────────────────────────────────────────────────────────

# S6 semantic bridge 冻结 process 模板（2026-09-01 spec；用户示范结构）
PROCESS_CLAUSE = '(curing OR polymeriz* OR photocuring OR photopolymeriz*)'

# S5 冻结左锚 shrinkage 词族（对照组 B 专用；来源 build_cross_layer_queries LAYER_RULES PROPERTY）
SHRINK_LEX_CLAUSE = '("polymerization shrinkage" OR "polymerization contraction" OR "volume contraction" OR shrinkage)'

# S5 冻结 APPLICATION 层 domain context（build_cross_layer_queries.py LAYER_RULES APPLICATION）
# 按域细分并做 alias 规范化（FAMILY_ALIASES: dentistry→dental, holographic→holograph*,
# lithography→lithograph*, "3d printing"→"3d print*" 等）
CTX = {
    "dental": '(dental OR dentistry OR restorative)',
    "optics": '(optical OR holograph* OR lithograph* OR photoresist OR "data storage")',
    "3dp":    '("3d print*" OR "additive manufacturing" OR stereolithograph* OR sla)',
}
CTX_SOURCE = "S5_FROZEN_LAYER_RULES_APPLICATION"

# ─────────────────────────────────────────────────────────────────────────────
# 角色修正（assembler 前；不改词源，只改组装角色。用户 22:51 点名）
#   fabrication error            → observable（强 bridge 词）
#   volume holographic recording → context/application
#   holographic recording ...    → context/application
#   recording media              → context/application
#   data storage capacity        → outcome/application metric
# ─────────────────────────────────────────────────────────────────────────────
ROLE_CORRECTION = {
    "fabrication error": "observable",
    "volume holographic recording": "context",
    "holographic recording characteristics": "context",
    "recording media": "context",
    "data storage capacity": "context",
}

# VERIFIED phrase → query 形态的合法派生（pilot 前写死；仅去掉尾部泛化复数词）
QUERY_FORM_MAP = {
    "two-color photopolymerization chemistries": "two-color photopolymerization",
}

# FREE observable（A/C 右锚；词面无 shrink/contraction；role 修正后仍为 observable）
# 来源 = S6 term_bank VERIFIED_EXACT 40 词，按上述过滤+修正后的人工复核（2026-09-04）
OBSERVABLE_FREE = {
    # dental（evidence 均为 dental 论文原文）
    "cusp deflection": "dental",
    "enamel crack propagation": "dental",
    "marginal leakage": "dental",
    "marginal discoloration": "dental",
    "internal gap": "dental",
    "void formation": "dental",
    # optics（fabrication error 为强桥词；holography 4 词已 role-correct 出 observable）
    "fabrication error": "optics",
    # general / sla
    "structural deformation": "general",
    "softening effect": "general",
    "printability": "sla",   # exploratory，独立使用
}

# mechanism-free（C 左锚；词面无 shrink/contraction）
MECHANISM_FREE = [
    "stress relaxation",
    "modulus development",
    "curing kinetics",
    "delayed gel point",
    "addition-fragmentation chain transfer",
    "spatial and temporal control",
]

# anchored observable（C 右锚少量使用：机制的直接目标/outcome，含 shrink 词面）
OBSERVABLE_ANCHORED = ["low shrinkage stress", "reduced shrinkage", "film shrinkage"]

# ─────────────────────────────────────────────────────────────────────────────
# Query specs（确定性组合；每条的 observable 集合均从 OBSERVABLE_FREE 引用，
# 无手工塞词。组合粒度经用户审阅原则拆分：形变/力学 vs 界面/临床分开）
# ─────────────────────────────────────────────────────────────────────────────

def build_specs():
    s = []
    def A(domain, obs, ctx=None, strategy="process_to_observable", note="",
          exploratory=False):
        # exploratory=True：低置信迁移/过宽词，precision 低不代表 A family 失败
        # （用户 2026-09-04 22:58：A8 printability、A7 softening effect 明确标 exploratory）
        s.append({"family": "A", "domain": domain, "strategy": strategy,
                  "left": "process", "obs": obs, "ctx": ctx, "note": note,
                  "exploratory": exploratory})
    # A1 拆 2 条（用户）：形变/力学 vs 界面/临床
    A("dental", ["cusp deflection", "enamel crack propagation"], "dental",
      note="力学/形变后果，anchor-free")
    A("dental", ["marginal leakage", "marginal discoloration"], "dental",
      note="界面/临床后果，anchor-free")
    A("dental", ["internal gap"], "dental", note="结构缝隙，anchor-free")
    A("dental", ["void formation"], "dental", note="孔隙/缺陷，anchor-free")
    A("optics", ["fabrication error"], "optics",
      note="强 bridge；光学制造误差（holography 语境），anchor-free")
    A("general", ["structural deformation"], None,
      note="源自 dental 理论论文 observable，anchor-free")
    A("general", ["softening effect"], None,
      note="低置信 exploratory（用户 22:58：与 printability 同列 exploratory 观察）",
      exploratory=True)
    A("sla", ["printability"], None,
      note="exploratory（用户点名）；可能太宽，yield 低不代表 A family 失败",
      exploratory=True)
    return s

def build_specs_b():
    # 对照组：同一 observable 右锚，左侧换回 shrinkage —— 测 anchor reversal 增量
    return [
        {"family": "B", "domain": "dental", "strategy": "shrink_lex_to_observable",
         "left": "shrink_lex",
         "obs": ["cusp deflection", "marginal leakage", "internal gap"],
         "ctx": "dental", "note": "对照 A_dental 1-3：右侧同族、左侧 shrinkage-anchor"},
        {"family": "B", "domain": "optics", "strategy": "shrink_lex_to_observable",
         "left": "shrink_lex",
         "obs": ["fabrication error"], "ctx": "optics",
         "note": "对照 A_optics：fabrication error 同右锚、左侧 shrinkage-anchor"},
    ]

def build_specs_c():
    # mechanism × (free-observable | anchored-outcome) × process（机制-现象桥）
    return [
        {"family": "C", "domain": "general", "strategy": "mechanism_to_observable",
         "mech": ["stress relaxation"], "obs": ["structural deformation", "cusp deflection"],
         "add_process": True,
         "note": "机制↔可观察（用户 C1 重写版，relaxation 单用已砍-过宽）"},
        {"family": "C", "domain": "general", "strategy": "mechanism_to_outcome_shrink",
         "mech": ["modulus development", "delayed gel point"],
         "obs": ["internal gap", "low shrinkage stress", "reduced shrinkage"],
         "add_process": True,
         "note": "模量发展/凝胶点延迟 与 收缩-应力理论后果（dental theory 论文语言）"},
        {"family": "C", "domain": "packaging", "strategy": "mechanism_to_outcome_shrink",
         "mech": ["addition-fragmentation chain transfer"], "obs": ["low shrinkage stress"],
         "add_process": False,
         "note": "AFCT 应力松弛机制 → 低收缩应力（W2127319543 原文语境）"},
        {"family": "C", "domain": "dental", "strategy": "mechanism_to_outcome_shrink",
         "mech": ["spatial and temporal control", "two-color photopolymerization chemistries"],
         "obs": ["low shrinkage stress"], "add_process": False,
         "note": "双色/时空控制光化学 → 低收缩应力体系（W4289022570 thiol-ene）"},
        {"family": "C", "domain": "general", "strategy": "mechanism_to_outcome_shrink",
         "mech": ["curing kinetics"], "obs": ["void formation"], "add_process": False,
         "note": "固化动力学 → 孔隙/缺陷 后果（低置信；curing kinetics 已含 curing 词根，不加 process 锚防冗余）"},
    ]

def build_specs_d():
    # cross-domain observable transfer（用户：只留 fabrication error 思路，softening→packaging 砍）
    return [
        {"family": "D", "domain": "optics_to_3dp", "strategy": "observable_transfer",
         "left": "ctx:3dp", "obs": ["fabrication error"], "ctx": None,
         "note": "optics 证据词 fabrication error → SLA/3DP 制造域（核心 transfer 假设）"},
        {"family": "D", "domain": "general_to_3dp", "strategy": "observable_transfer",
         "left": "ctx:3dp", "obs": ["structural deformation"], "ctx": None,
         "note": "general 词 → 制造域；low-confidence exploratory（用户 22:58 点名标 exploratory）",
         "exploratory": True},
    ]

# ─────────────────────────────────────────────────────────────────────────────
# Assembler（纯 deterministic）
# ─────────────────────────────────────────────────────────────────────────────

def q(s):
    return f'"{s}"'

def group_or(items):
    """每个 AND 组一律括号化（Scopus Boolean：AND/OR 不括号会串优先级）。"""
    assert items, "empty group"
    return "(" + " OR ".join(q(t) for t in items) + ")"

def compile_action(spec, idx, term_bank_meta):
    left = spec.get("left")
    ctx = CTX[spec["ctx"]] if spec.get("ctx") else None

    if left == "process":
        left_clause = PROCESS_CLAUSE
    elif left == "shrink_lex":
        left_clause = SHRINK_LEX_CLAUSE
    elif left and left.startswith("ctx:"):
        left_clause = CTX[left.split(":", 1)[1]]
    else:
        left_clause = None

    parts = []
    if spec.get("family") == "C" and spec.get("mech"):
        # C: (mech) AND (observable/outcome) [AND (process)]
        parts.append(group_or([QUERY_FORM_MAP.get(m, m) for m in spec["mech"]]))
        parts.append(group_or([QUERY_FORM_MAP.get(t, t) for t in spec["obs"]]))
        if spec.get("add_process"):
            parts.append(PROCESS_CLAUSE)
    elif spec.get("family") in ("A", "B", "D"):
        if left_clause:
            parts.append(left_clause)
        parts.append(group_or(spec["obs"]))
        if ctx:
            parts.append(ctx)
    else:
        parts.append(left_clause or PROCESS_CLAUSE)
        parts.append(group_or(spec["obs"]))
        if ctx:
            parts.append(ctx)

    query_string = "TITLE-ABS-KEY(" + " AND ".join(parts) + ")"

    # provenance
    terms = []
    if spec.get("mech"):
        terms += [QUERY_FORM_MAP.get(m, m) for m in spec["mech"]]
    terms += [QUERY_FORM_MAP.get(t, t) for t in spec["obs"]]
    contains_shrink = "shrink_lex" in (spec.get("left") or "") or \
        any(("shrink" in t.lower() or "contraction" in t.lower()) for t in spec["obs"])
    return {
        "action_id": f"S6-{spec['family']}-{idx:02d}",
        "type": "scopus_query",
        "query_string": query_string,
        "bridge_type": f"S6-{spec['family']}",
        "family": spec["family"],
        "domain": spec["domain"],
        "strategy": spec["strategy"],
        "terms": terms,
        "term_source": "S6_TERM_BANK_VERIFIED_EXACT",
        "context_source": CTX_SOURCE if spec.get("ctx") or (spec.get("left", "").startswith("ctx:")) else (
            "S6_PROCESS_TEMPLATE_2026-09-01" if spec.get("left") == "process" else
            "S5_FROZEN_SHRINK_LEX" if spec.get("left") == "shrink_lex" else None),
        "contains_shrinkage_anchor": contains_shrink,
        "exploratory": bool(spec.get("exploratory", False)),
        "note": spec.get("note", ""),
    }


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--term-bank", default=TERM_BANK)
    args = ap.parse_args()

    tb = json.load(open(args.term_bank, encoding="utf-8"))
    verified = tb["terms_verified_exact"]
    verified_terms = {e["term"].strip().strip('"').rstrip("*") for e in verified}
    assert len(verified) == 40, f"term bank VERIFIED_EXACT 应=40，实际 {len(verified)}"
    counts = tb["counts"]
    assert counts["VERIFIED_EXACT"] == 40 and counts["SEMANTIC_CANDIDATE"] == 16 and counts["REJECTED"] == 0

    # 词源纪律：spec 里每个 term 必须 ∈ VERIFIED（QUERY_FORM_MAP 派生登记除外）
    specs = (build_specs() + build_specs_b() + build_specs_c() + build_specs_d())
    for sp in specs:
        for t in sp.get("obs", []) + sp.get("mech", []):
            assert t in verified_terms, f"[词源违规] '{t}' 不在 VERIFIED_EXACT，也非已登记派生"
    # 派生登记：QUERY_FORM_MAP 的 key 必须在 VERIFIED
    for k in QUERY_FORM_MAP:
        assert k in verified_terms, f"QUERY_FORM_MAP key '{k}' 不在 VERIFIED"
    # anchor-free 纪律：A/D 的 obs 不含 shrink 词面
    for sp in specs:
        if sp["family"] in ("A", "D"):
            for t in sp["obs"]:
                assert "shrink" not in t.lower() and "contraction" not in t.lower(), \
                    f"[anchor 违规] A/D observable '{t}' 含 shrink/contraction 词面"

    actions = [compile_action(sp, i + 1, tb) for i, sp in enumerate(specs)]
    fam_counts = {}
    for a in actions:
        fam_counts[a["family"]] = fam_counts.get(a["family"], 0) + 1
    anchor_free = sum(1 for a in actions if not a["contains_shrinkage_anchor"])
    expl = [a["action_id"] for a in actions if a["exploratory"]]
    # 用户 22:58 检查点 3：exploratory 只降权解释，不参与 family 成败归因
    assert len(expl) >= 2, f"exploratory 标记缺失: {expl}（应含 A7/A8/D2）"

    out = {
        "version": "S6_BRIDGE_QUERIES_V1",
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "frozen_refs": {
            "term_bank": "S6_TERM_BANK FROZEN 2026-09-04 (40 VERIFIED / 16 SEMANTIC secondary / 0 REJECTED)",
            "process_template": "semantic bridge 2026-09-01 spec: (curing OR polymerization OR photocuring)",
            "context_vocab": CTX_SOURCE,
        },
        "word_source_discipline": "query 内容词 = 40 VERIFIED_EXACT ∪ 冻结 process/domain context ∪ QUERY_FORM_MAP 已登记派生",
        "role_correction": ROLE_CORRECTION,
        "query_form_derivation": QUERY_FORM_MAP,
        "family_counts": fam_counts,
        "total_actions": len(actions),
        "anchor_free_actions": anchor_free,
        "exploratory_actions": expl,
        "actions": actions,
    }
    json.dump(out, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[counts] total={len(actions)} by_family={fam_counts} anchor_free={anchor_free}")
    print(f"[out] {args.out}")
    for a in actions:
        print(f"\n{a['action_id']} [{a['family']}|{a['domain']}|{a['strategy']}]"
              f" shrink_anchor={a['contains_shrinkage_anchor']}")
        print(f"   {a['query_string']}")


if __name__ == "__main__":
    main()
