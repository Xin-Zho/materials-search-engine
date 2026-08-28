"""Phase 3 P1 — Gold set 分层 recall diagnostic（用户定 2026-08-27）。

只回答一个问题：**我们事先明确知道的重要论文，系统有没有找回来？**
- 分层输出（foundational / representative / frontier / must_hit），**不输出总加权分**
  （总分会掩盖 "foundational 漏了一半" 这类严重问题）
- missing 必须精确列出 DOI/title，供人工核查
- 只作 diagnostic：没有停止权（停止权唯一属于 recall_bound 的 STATISTICAL_STOP）

role → layer 映射（gold set role 是中文，映射到用户定的英文分层）：
  奠基        → foundational
  代表        → representative
  综述        → frontier（综述代表领域前沿认知）
  3D打印代表  → frontier（新材料体系代表前沿方向）
"""

import json
import os
from dataclasses import dataclass, field, asdict

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GOLD_PATH = os.path.join(BASE, "benchmarks", "benchmarks_v1.json")

ROLE_TO_LAYER = {
    "奠基": "foundational",
    "代表": "representative",
    "综述": "frontier",
    "3D打印代表": "frontier",
}

LAYER_ORDER = ["foundational", "representative", "frontier"]

# diagnostic status（用户定：AVAILABLE / INSUFFICIENT_DATA / INVALID_ASSUMPTION）
AVAILABLE = "AVAILABLE"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


def normalize_doi(doi: str) -> str:
    """DOI 归一化：小写、去 https://doi.org/ 前缀、去空白。

    匹配键必须两边同规——gold set 与 KB/检索结果的 DOI 格式可能不同
    （"10.1016/S0300-5712(96)00063-2" vs "https://doi.org/10.1016/s0300-5712(96)00063-2"）。
    """
    return (doi or "").strip().lower().replace("https://doi.org/", "").replace("http://doi.org/", "").strip()


def load_gold_set(path: str = GOLD_PATH) -> list[dict]:
    """加载 pc_001 key_papers（含 role/must_hit/doi/title）。"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    papers = []
    for q in data.get("questions", []):
        papers.extend(q.get("key_papers", []))
    return papers


def layer_of(paper: dict) -> str:
    return ROLE_TO_LAYER.get(paper.get("role", ""), "frontier")


def goldset_report(gold_papers: list[dict], known_dois: set[str],
                   known_by_title: dict[str, str] | None = None) -> dict:
    """分层 recall diagnostic。

    参数：
      gold_papers     —— benchmarks key_papers（doi/title/role/must_hit）
      known_dois      —— 系统已知论文的 DOI 集合（KB + candidate pool + staging）
      known_by_title  —— 可选：归一化标题 → paper 标识（title-only 论文的兜底匹配）

    返回（无总加权分）：
      layers:   {foundational: {found, total, recall}, ...}
      must_hit: {found, total, recall}
      missing_papers: [{doi, title, role, layer, must_hit}]
      status
    """
    known = {normalize_doi(d) for d in known_dois}
    title_index = {_norm_title(t): pid for t, pid in (known_by_title or {}).items()}

    layers = {L: {"found": 0, "total": 0, "recall": 0.0} for L in LAYER_ORDER}
    must = {"found": 0, "total": 0, "recall": 0.0}
    missing: list[dict] = []

    for p in gold_papers:
        layer = layer_of(p)
        layers[layer]["total"] += 1
        if p.get("must_hit"):
            must["total"] += 1
        # found：DOI 匹配优先；title 兜底（gold 无 DOI 或 KB 无 DOI 的论文）
        doi_hit = normalize_doi(p.get("doi", "")) in known
        title_hit = _norm_title(p.get("title", "")) in title_index
        found = bool(doi_hit or title_hit)
        if found:
            layers[layer]["found"] += 1
            if p.get("must_hit"):
                must["found"] += 1
        else:
            missing.append({
                "doi": p.get("doi", ""),
                "title": p.get("title", ""),
                "role": p.get("role", ""),
                "layer": layer,
                "must_hit": bool(p.get("must_hit")),
            })

    for L in layers.values():
        L["recall"] = round(L["found"] / L["total"], 4) if L["total"] else 0.0
    must["recall"] = round(must["found"] / must["total"], 4) if must["total"] else 0.0

    if not gold_papers:
        return {"layers": layers, "must_hit": must, "missing_papers": [],
                "status": INSUFFICIENT_DATA, "reason": "gold set 为空"}
    return {"layers": layers, "must_hit": must, "missing_papers": missing,
            "status": AVAILABLE}


def _norm_title(title: str) -> str:
    """标题归一化：小写、去标点、压缩空白（title 兜底匹配用）。"""
    import re
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", (title or "").lower())).strip()
