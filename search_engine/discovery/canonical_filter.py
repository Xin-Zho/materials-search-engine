"""Existing Knowledge Filter：alias / existing-knowledge 清洗（用户定 2026-08-26）。

novelty 是知识新颖性，不是字符串新颖性。过滤链：
    canonical exact → alias dictionary → semantic candidate match → existing ontology hierarchy

只认确定性匹配（拼写变体/归一化），不自动合并语义近似
（reduced shrinkage stress ≠ reduced shrinkage strain——不能合并）。
"""

from __future__ import annotations

import re

from ..route_mechanism_ontology import (
    get_mechanisms, CORE_ROUTE_MECHANISMS,
)

_CHECKLIST_MECHS = {m for r in CORE_ROUTE_MECHANISMS for m in get_mechanisms(r)}
_CHECKLIST_ALIASES = {
    a.lower() for r in CORE_ROUTE_MECHANISMS for a in (CORE_ROUTE_MECHANISMS[r].get("aliases") or [])
}


def _norm(t: str) -> str:
    t = (t or "").strip().lower()
    t = re.sub(r"\s+", " ", t)
    t = t.replace(" -", "-").replace("- ", "-")
    return t


def canonical_match(text: str) -> str | None:
    """确定性 alias 匹配：route canonical key / aliases / canonical mechanism（归一后）。

    返回匹配到的已有 node 名（None = 非 alias）。
    """
    t = _norm(text)
    for r in CORE_ROUTE_MECHANISMS:
        if _norm(r) == t:
            return r
    if t in _CHECKLIST_ALIASES:
        for r in CORE_ROUTE_MECHANISMS:
            if t in {a.lower() for a in CORE_ROUTE_MECHANISMS[r].get("aliases") or []}:
                return r
    for m in _CHECKLIST_MECHS:
        if _norm(m) == t:
            return m
    dashed = t.replace(" ", "-")
    for r in CORE_ROUTE_MECHANISMS:
        if dashed == _norm(r) or dashed in {a.lower() for a in CORE_ROUTE_MECHANISMS[r].get("aliases") or []}:
            return r
    for m in _CHECKLIST_MECHS:
        if _norm(m) == dashed:
            return m
    return None


def existing_knowledge_match(text: str) -> str | None:
    """semantic candidate match：EFFECT 语义家族检测（收紧版，用户定 2026-08-26）。

    只认两种：
      1) 核心名词完全相等（如 "reduced shrinkage" vs "reduced shrinkage"）
      2) 核心名词 = "修饰语 + 已有 effect 名词" 且无维度后缀
         （"polymerization shrinkage" endswith "shrinkage" → family）

    严格不合并（共享 "shrinkage" 不等于同一知识）：
      stress / strain / rate / time-to-peak / force —— 维度不同，独立 EFFECT candidate。
      例：reduced shrinkage stress、reduced volumetric shrinkage stress、
          increased shrinkage rate、reduced time to maximum shrinkage force rate
      → 全部独立保留（≠ reduced shrinkage，也 ≠ reduced shrinkage strain）。
    """
    t = _norm(text)
    prefix = re.compile(
        r"^(reduced|increased|improved|enhanced|decreased|lower|higher|high|low|"
        r"superior|delayed|accelerated)\s+")
    core = prefix.sub("", t)
    if not core or core == t:
        return None
    # 维度后缀拦截：一旦含这些词就不归 "shrinkage magnitude" 家族
    _DIMENSION = ("stress", "strain", "rate", "force", "time")
    for m in _CHECKLIST_MECHS:
        m_core = prefix.sub("", _norm(m))
        if not m_core:
            continue
        # 1) 完全相等
        if core == m_core:
            return m
        # 2) 修饰语 + 已有 effect 名词（无维度后缀）
        if core.endswith(m_core) and core != m_core and not any(d in core for d in _DIMENSION):
            return m
    return None
