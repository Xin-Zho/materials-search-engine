"""Candidate Typer：10 类启发式分类 + domain relevance 分级（本地规则，不调 LLM）。

10 类（用户定 2026-08-26）：ROUTE / SUB_ROUTE / MECHANISM / PROCESS_STRATEGY /
FORMULATION_STRATEGY / EFFECT / MATERIAL_CAPABILITY / CONTEXT_TERM / ALIAS / UNKNOWN。

CONTEXT_TERM：领域/过程/材料背景概念（photopolymerization / resin / composite）——
真实概念，但不是需要扩展 ontology 的知识节点（novel discovery = no）。
只对"独立词精确匹配"判 CONTEXT_TERM（photopolymerization ✓；cationic ring-opening
polymerization 是机制组合 ✗，保留 MECHANISM 路径）。

分类是"这到底是什么"，先由 canonical_filter 判 ALIAS，再走规则；拿不准 UNKNOWN 交人工。
"""

from __future__ import annotations

import re

_EFFECT_PREFIX = ("reduced ", "increased ", "improved ", "enhanced ",
                  "decreased ", "lower ", "higher ", "high ", "low ",
                  "superior ", "delayed ", "accelerated ")
_EFFECT_SUFFIX = ("shrinkage", "stress", "conversion", "modulus", "stiffness",
                  "strength", "toughness", "transparency", "stability",
                  "density", "temperature", "dispersity", "resistance")
_STRATEGY_WORDS = ("curing", "formulation", "processing", "protocol", "strategy",
                   "procedure", "regime", "schedule", "program")
_PROCESS_HINTS = ("incremental", "sequential", "gradual", "layered", "pulsed",
                  "stepwise", "post-cure", "postcure", "thermal", "two-stage")
_MECHANISM_HINTS = ("bond", "exchange", "reaction", "rearrangement", "isomerism",
                    "dissociation", "association", "cleavage", "topology",
                    "mobility", "diffusion", "relaxation", "transition",
                    "self-healing", "click", "polymerization", "crosslink")
_CAPABILITY_HINTS = ("self-healing", "antimicrobial", "transparency", "adhesion",
                     "remineralisation", "biocompatib", "recyclab", "reprocessab")
_DESIGN_VARIABLES = ("molecular weight", "molar volume", "double bond",
                     "reactive group", "functional group")
# CONTEXT_TERM：领域/过程/材料背景概念（精确独立词匹配）
_CONTEXT_TERMS = {
    "photopolymerization": "领域过程背景",
    "polymerization": "领域过程背景",
    "photocuring": "领域过程背景",
    "uv curing": "领域过程背景",
    "uv-curing": "领域过程背景",
    "photopolymer": "材料背景",
    "resin": "材料背景",
    "composite": "材料背景",
    "curing": "过程背景",
}


def _norm(t: str) -> str:
    t = (t or "").strip().lower()
    t = re.sub(r"\s+", " ", t)
    t = t.replace(" -", "-").replace("- ", "-")
    return t


def type_candidate(text: str, is_alias: bool = False) -> str:
    """10 类启发式分类。is_alias=True 时直接 ALIAS（由 canonical_filter 判定）。"""
    if is_alias:
        return "ALIAS"
    t = _norm(text)
    if t.startswith(_EFFECT_PREFIX) or t.endswith(_EFFECT_SUFFIX):
        return "EFFECT"
    # FORMULATION_STRATEGY（组合词；裸 "composite" 是 CONTEXT_TERM，不在这里吸走）
    if ("formulation" in t or "bulk-fill" in t or "reinforc" in t
            or "filler" in t or "nanoparticle" in t):
        return "FORMULATION_STRATEGY"
    # CONTEXT_TERM：精确独立词（先于 MECHANISM_HINTS，防 photopolymerization 被吸走）
    if t in _CONTEXT_TERMS:
        return "CONTEXT_TERM"
    if any(w in t for w in _PROCESS_HINTS) and any(w in t for w in _STRATEGY_WORDS):
        return "PROCESS_STRATEGY"
    if any(w in t for w in _CAPABILITY_HINTS):
        return "MATERIAL_CAPABILITY"
    if any(w in t for w in _MECHANISM_HINTS):
        return "MECHANISM"
    if any(w in t for w in _DESIGN_VARIABLES):
        return "SUB_ROUTE"
    if "agent" in t or "monomer" in t or "oligomer" in t:
        return "SUB_ROUTE"
    if any(w in t for w in _STRATEGY_WORDS):
        return "PROCESS_STRATEGY"
    return "UNKNOWN"


# domain relevance 核心词（anchor: polymerization shrinkage & stress）
_CORE_TERMS = ("shrinkage", "stress", "polymerization", "photopolym", "network",
               "gel", "cure", "volume", "monomer", "crosslink", "chain",
               "bond", "exchange")


def domain_relevance_level(text: str, evidence_list: list[str]) -> tuple[str, float]:
    """domain relevance 分级（HIGH ≥0.75 / MEDIUM ≥0.5 / LOW ≥0.25 / UNKNOWN <0.25）。

    返回 (level, score)。score = 核心词命中数 / 4，封顶 1.0。
    """
    pool = " ".join([text] + (evidence_list or [])).lower()
    hits = sum(1 for w in _CORE_TERMS if w in pool)
    score = round(min(1.0, hits / 4), 2)
    if score >= 0.75:
        return "HIGH", score
    if score >= 0.5:
        return "MEDIUM", score
    if score >= 0.25:
        return "LOW", score
    return "UNKNOWN", score
