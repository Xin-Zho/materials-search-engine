"""P1-A2: 按 topic 自动合成 S6 bridge spec（通用算法，禁 topic 特判）。

从归一 TermBank（role=mechanism/observable/lexical/context）确定性推导 bridge
组合候选——机制→可测后果 / 体系锚→后果 / 跨域 transfer。产物是 spec 资产
（与 bridge_spec.json 同构），供 bridge_compiler.compile_spec_asset 编译成
Scopus Boolean actions。

合成原则（对任意主题通用）：
  1. 同 domain 配对优先（机制与其同域可测后果最相关），general observable 兜底；
  2. lexical 体系锚 = role==lexical ∧ domain!="general"（general 域 lexical 为
     主题泛锚，如 thermochromism/polymerization shrinkage，不作左锚）；
  3. 全 CANDIDATE 词表生成的 spec 标 exploratory=False 但 term_source 明示
     candidate 词源（VERIFIED 冻结留给 S6 词源三层）；
  4. 产出量受 cap 参数控制（每 mech/lex 的 obs 上限），防笛卡尔爆炸。

输出 spec entries（同 compile 消费 schema）:
    {family, domain, strategy, mech?|left?, obs[], note, exploratory, synthetic: true}
"""
from __future__ import annotations

from .termbank import TermBank, TermEntry


def _split_roles(tb: TermBank):
    by = tb.by_role
    mechs = by.get("mechanism", [])
    obss = by.get("observable", [])
    lexs = by.get("lexical", [])
    ctxs = by.get("context", [])
    # 体系锚：lexical ∧ domain != general（general 域 lexical = 主题泛锚）
    system_anchors = [e for e in lexs if e.domain != "general"]
    # observable 分组（同域 + general 兜底）
    obs_by_dom: dict[str, list[TermEntry]] = {}
    general_obs: list[TermEntry] = []
    for o in obss:
        if o.domain == "general":
            general_obs.append(o)
        else:
            obs_by_dom.setdefault(o.domain, []).append(o)
    return mechs, obss, system_anchors, ctxs, obs_by_dom, general_obs


def _candidate_obs(dom: str, obs_by_dom: dict, general_obs: list,
                   cap: int, fallback_all: list[TermEntry]) -> list[TermEntry]:
    """机制/体系的候选 observable：同域优先；同域空则 general；仍空则全池兜底。"""
    same = obs_by_dom.get(dom, [])
    cand = same + general_obs
    if not cand:
        cand = fallback_all
    # 去重保序 + 截断（同域优先天然在前）
    seen, out = set(), []
    for o in cand:
        if o.term not in seen:
            seen.add(o.term)
            out.append(o)
    return out[:cap]


def synthesize_specs(tb: TermBank, *,
                     cap_obs_per_mech: int = 4,
                     cap_obs_per_lex: int = 3,
                     max_transfer: int = 6) -> list[dict]:
    """termbank → bridge spec entries（与 frozen spec 同 schema 子集）。"""
    mechs, obss, anchors, ctxs, obs_by_dom, general_obs = _split_roles(tb)
    specs: list[dict] = []

    # ── C family: mechanism → observable（机制桥，主力）──
    for m in mechs:
        for o in _candidate_obs(m.domain, obs_by_dom, general_obs,
                                cap_obs_per_mech, obss):
            specs.append({
                "family": "C", "domain": m.domain,
                "strategy": "mechanism_to_observable",
                "mech": [m.term], "obs": [o.term], "add_process": False,
                "note": (f"synth C: {m.domain} 域机制→后果（词源 CANDIDATE，"
                         f"S6 词源三层后冻结）"),
                "synthetic": True,
            })

    # ── A-lex: lexical 体系锚 × observable（体系→后果面）──
    # 体系锚词面（如 vanadium dioxide）作 term_left（left_terms），非模板。
    for lx in anchors:
        for o in _candidate_obs(lx.domain, obs_by_dom, general_obs,
                                cap_obs_per_lex, obss):
            specs.append({
                "family": "A", "domain": lx.domain,
                "strategy": "lexical_to_observable",
                "left_terms": [lx.term],
                "obs": [o.term],
                "note": (f"synth A-lex: {lx.domain} 体系锚 {lx.term!r} → 可测后果"
                         f"（CANDIDATE 词源）"),
                "synthetic": True,
            })

    # ── D-transfer: 跨域机制→observable（探索，限量；C 型槽位 + exploratory）──
    transfered = 0
    for m in mechs:
        if transfered >= max_transfer:
            break
        other = [o for o in obss
                 if o.domain not in (m.domain, "general") and o.domain != ""]
        for o in other[:2]:
            specs.append({
                "family": "D", "domain": f"{m.domain}_to_{o.domain}",
                "strategy": "mechanism_transfer",
                "mech": [m.term], "obs": [o.term],
                "note": "synth D: 跨域机制 transfer（exploratory，低 precision 不代表机制失败）",
                "exploratory": True, "synthetic": True,
            })
            transfered += 1
            if transfered >= max_transfer:
                break

    return specs


def build_synth_spec_asset(topic_id: str, tb: TermBank, *,
                           anchor: str = "", **kw) -> dict:
    """合成完整 spec 资产（bridge_spec.json 同构），供 compile_spec_asset 编译。"""
    specs = synthesize_specs(tb, **kw)
    return {
        "topic_id": topic_id,
        "version": f"{topic_id.upper()}_BRIDGE_SPEC_SYNTH",
        "mode": "synthesized",
        "frozen_at": None,
        "process_clause": None,          # 无 process 锚主题（有则 topic.yaml 声明）
        "shrink_lex_clause": None,
        "domain_context": {},
        "context_source": "TERMBANK_ROLE_CONTEXT",
        "source_labels": {"process": None, "shrink_lex": None,
                          "context": "TERMBANK_ROLE_CONTEXT"},
        "query_form_map": {},
        "anchor": anchor,
        "specs": specs,
        "synth_note": ("自动合成候选（CANDIDATE 词源）；非 VERIFIED 冻结。"
                       "S6 词源三层验证后须人工审并升 frozen spec。"),
        "term_source_label": f"{topic_id.upper()}_TERMBANK_CANDIDATE",
    }
