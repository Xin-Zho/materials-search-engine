"""search_engine/audit/miss_expansion.py — v3.0 Miss Expansion Agent（Repair-first 主链）。

设计（用户 2026-08-29 定稿）：
  主链 = Audit miss -> Miss Expansion Agent -> Repair Search
  Failure Classifier 降级为辅助记录/解释层（Repair first, classification for
  auditability）。漏检论文本身就是最有价值的搜索线索：
    每篇 miss 围绕它展开 term / citation / community / historical 四类扩展，
    生成 repair query 候选（DRY_RUN，不执行）。

统计纪律（唯一必须守住的原则）：
  漏检论文用于修复之后 = 开发数据（validation 变 training）；下一次证明效果
  必须用**新的独立 Audit Round 样本**（Search S0 -> Audit A0 -> Repair ->
  Search S1 -> 新独立 Audit A1）。核心曲线 = p_hat_miss^(t) + 95% Upper Bound
  持续下降，不是 QGS 百分比。

术语用途分类（rule-based，deterministic；补上 P/M/R/C 缺的 Method slot）：
  METHOD / MATERIAL / PROBLEM / REACTION / CONTEXT / HISTORICAL / UNKNOWN
  （HISTORICAL = year<2006 ∧ 系统未掌握；与其他类可共存）
"""
from __future__ import annotations

import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tools"))

from build_round3_expansion_terms import canonicalize_phrase, extract_verbatim  # noqa: E402

HISTORICAL_YEAR = 2006

# ── 术语用途词典（第一版规则；命中=子串包含，取最高分主类）──
_LEXICON = {
    "METHOD": ("meter", "metry", "metric", "scope", "graphy", "graph", "espi", "speckle",
               "measurement", "measuring", "sensor", "detector", "profilometer",
               "microscopy", "spectroscopy", "spectrometry", "calorimetry", "dilatometry",
               "analysis", "evaluation", "testing", "test", "assay", "recording",
               "strain gauge", "interferometry", "holograph", "photogrammetry"),
    "MATERIAL": ("resin", "composite", "monomer", "filler", "matrix", "polymer",
                 "methacrylate", "acrylate", "epoxy", "glass", "ceramic", "adhesive",
                 "cement", "ionomer", "restorative", "sealant", "coating", "gel",
                 "oligomer", "urethane", "silane", "bis", "mono", "di", "tri", "dimethacrylate",
                 "polyol", "bisphenol", "microfilled", "hybrid", "flowable", "unfilled",
                 "dental", "tooth", "enamel", "dentin"),
    "PROBLEM": ("shrinkage", "shrink", "stress", "contraction", "strain", "debonding",
                "gap formation", "leakage", "fracture", "cracking", "distortion",
                "warpage", "failure", "fatigue", "wear", "erosion", "sag", "deflection",
                "dimensional change", "deformation"),
    "REACTION": ("polymerization", "curing", "photopolymerization", "initiation",
                 "conversion", "crosslink", "cross-link", "reaction", "hardening",
                 "setting", "cure", "degradation", "oxidation", "irradiation",
                 "exposure", "photo", "wavelength", "intensity", "light", "shrinkage light"),
    "CONTEXT": ("dental", "clinical", "restorative", "orthodontic", "prosthetic",
                "intraoral", "in vivo", "in vitro", "tooth", "cavity", "marginal",
                "posterior", "anterior", "occlusal", "intracoronal", "class", "cavity",
                "proximal", "gingival"),
}


def classify_term(canon: str, year=None) -> dict:
    """术语用途分类（可多标签）：{classes: [...], primary: ...}。"""
    hits = []
    for cls, words in _LEXICON.items():
        if any(w in canon for w in words):
            hits.append(cls)
    if not hits:
        hits = ["UNKNOWN"]
    primary = hits[0] if len(hits) == 1 else "MIXED"
    return {"classes": hits, "primary": primary}


def expand_miss(ev: dict, ctx) -> dict:
    """对一篇 miss 的 MissEvidence 生成搜索扩展候选（不执行搜索）。

    返回四类扩展 + 候选 query（DRY_RUN）：
      term_candidates        特征术语（用途分类后，排除 anchor）
      backward_candidates    后向引用中不在 found 的论文（enriched refs）
      forward_candidates     前向引用（数据缺失 -> PENDING 标注）
      community_candidates   venue/年份 邻近线索
      historical_terms       year<2006 且系统未掌握
      query_candidates       [anchor AND term] 候选（按用途分类组织）
    """
    title = ev.get("title") or ""
    year = ev.get("year")
    te = ev.get("term_evidence", {})
    id_ev = ev.get("identity_evidence", {})
    wid = id_ev.get("wid")

    # ── term mining + 用途分类 ──
    rows = extract_verbatim([{"wid": wid or "?", "title": title,
                              "abstract": ""}])
    terms: dict[str, dict] = {}
    for r in rows:
        canon = canonicalize_phrase(r["phrase"])
        if not canon or canon == canonicalize_phrase("polymerization shrinkage"):
            continue
        if canon not in terms:
            terms[canon] = {"surface": r["phrase"],
                            "in_system": canon in ctx.terms,
                            **classify_term(canon, year)}
    term_list = [{"canonical": c, **v} for c, v in sorted(terms.items())]

    # ── backward citation（enriched refs 中不在 found 的）──
    backward = []
    if wid and wid in ctx.enriched:
        for ref in ctx.enriched[wid].get("referenced_works") or []:
            if ref not in ctx.found_wids:
                meta = ctx.enriched.get(ref, {})
                backward.append({"wid": ref, "title": (meta.get("title") or "")[:120],
                                 "year": meta.get("year"),
                                 "doi": meta.get("doi") or None})

    # ── forward citation（数据缺）──
    forward = {"available": False,
               "note": "forward/cited_by 数据源未接入（OpenAlex cited_by / Scopus 第二来源待补）"}

    # ── community / venue 线索（第一版：年份 + 是否 bridge 节点）──
    community = {
        "represented": ev.get("community_evidence", {}).get("represented", False),
        "community_ids": ev.get("community_evidence", {}).get("community_ids", []),
        "known_bridge_node": ev.get("citation_evidence", {}).get("known_bridge_node", False),
        "bridge_count": ev.get("citation_evidence", {}).get("bridge_count"),
        "year": year,
    }

    # ── historical terms ──
    historical = [t for t in term_list
                  if not t["in_system"]
                  and year is not None and str(year).isdigit()
                  and int(year) < HISTORICAL_YEAR]

    # ── query 候选（DRY_RUN：anchor AND term，按用途分类）──
    anchor = "polymerization shrinkage"
    by_class: dict[str, list[str]] = {}
    for t in term_list:
        if t["in_system"]:
            continue
        cls = t["primary"]
        if cls == "MIXED":
            cls = t["classes"][0]
        by_class.setdefault(cls, []).append(t["surface"])
    query_candidates = []
    for cls in ("METHOD", "MATERIAL", "PROBLEM", "REACTION", "CONTEXT", "UNKNOWN"):
        for surf in (by_class.get(cls) or [])[:5]:
            query_candidates.append({
                "class": cls, "term": surf,
                "query": f'TITLE-ABS-KEY("{anchor}" AND "{surf}")',
                "dry_run": True,
            })

    return {
        "paper_id": ev.get("paper_id"),
        "term_candidates": term_list,
        "backward_candidates": backward,
        "forward_candidates": forward,
        "community_evidence": community,
        "historical_terms": historical,
        "query_candidates": query_candidates,
        "note": "Repair-first：漏检论文自身是搜索线索；所有 query 候选 DRY_RUN 不执行；"
                "用于修复后该 miss 即成为开发数据，下一轮效果须用新独立 Audit Round 验证",
    }
