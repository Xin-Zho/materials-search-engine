"""Phase 2.1b P2.3 staging pipeline（用户定 2026-08-26）：STAGED paper → 知识候选。

固定流程（用户定）：
    STAGED papers → relevance screening（三态）→ 只丢 IRRELEVANT
      → RELEVANT + UNCERTAIN → Extractor → new edges
      → merge into discovery knowledge layer → scanner rerun
      → candidate_before vs candidate_after → new_candidate_not_seen_before

5 个 invariant（用户定）：
    ① STAGED 不能直接进 extractor 结论层，必须先过 relevance 三态
    ② UNCERTAIN 不丢，继续进 extraction（recall-first）
    ③ 新 edge 保留完整 discovery provenance（promoted node / query / new paper）
    ④ scanner 比较 candidate_before / candidate_after，旧候选重新发现不算成功
    ⑤ 核心指标单独记录：new_relevant_papers / new_edges / new_candidates /
       new_candidate_not_seen_before

复用现有组件（用户定：不发明新架构）：
    relevance → screen_relevance（规则第一版；screener_fn 可注入换 LLM）
    extractor → KnowledgeExtractor.extract_many（异步；测试注入 mock extract_fn）
    scanner   → scan_kb（喂 DiscoveryLayerView：KB records + discovery edges 合成）
"""

from __future__ import annotations

import json
import os

from .query_registry import normalize_query, load_registry
from .discovery_retriever import load_staging, STAGING_PATH

DISCOVERY_EDGES_PATH = "data/exports/discovery_edges.json"

# 核心指标（invariant ⑤）
METRIC_KEYS = (
    "new_relevant_papers", "new_edges", "new_candidates",
    "new_candidate_not_seen_before",
)

_STOPWORDS = {"of", "the", "and", "for", "in", "with", "based", "on", "a", "an",
              "bulk", "fill", "composite", "formulation"}


# ── ① relevance screening（STAGED → 三态）──

def query_core_terms(query_text: str, source_node: str = "") -> list[str]:
    """query 核心词：query 分词 - source_node 词 - 停用词。

    例：query "bulk-fill filler surface treatment shrinkage stress"、
    node "bulk-fill composite formulation" → [filler, surface, treatment, shrinkage, stress]
    """
    node_tokens = set(normalize_query(source_node).split()) if source_node else set()
    tokens = [w for w in normalize_query(query_text).split()
              if w not in _STOPWORDS and w not in node_tokens]
    return tokens


def _paper_field(paper, name: str, default: str = "") -> str:
    if isinstance(paper, dict):
        return paper.get(name, default) or default
    return getattr(paper, name, default) or default


def screen_relevance(paper, query_text: str, source_node: str = "") -> str:
    """论文级三态（规则第一版；recall-first——只有明确无关才 IRRELEVANT）。

    title 命中核心词 ≥2 → RELEVANT
    title 命中 1        → UNCERTAIN
    title 0 + abstract 有词 → UNCERTAIN（recall-first，不丢）
    title 0 + abstract 0   → IRRELEVANT（防御：检索按短语匹配，正常不出现）
    """
    title = _paper_field(paper, "title")
    abstract = _paper_field(paper, "abstract")
    core = query_core_terms(query_text, source_node)
    if not core:
        return "UNCERTAIN"
    hit_title = sum(1 for w in core if w in title.lower())
    if hit_title >= 2:
        return "RELEVANT"
    if hit_title == 1:
        return "UNCERTAIN"
    hit_abs = sum(1 for w in core if w in abstract.lower())
    return "UNCERTAIN" if hit_abs > 0 else "IRRELEVANT"


def screen_staging(staging: list[dict], registry: list[dict],
                   screener_fn=None) -> dict:
    """对 STAGED 论文跑三态（invariant ①：必须过 screening 才能进 extraction）。

    每篇 staging 论文用它的第一个 query 判定（query→relevance 映射按 query_id
    从 registry 查）。screener_fn(paper, query_text, source_node) 可注入（LLM 版）。
    返回统计：{RELEVANT: n, UNCERTAIN: n, IRRELEVANT: n}；staging 原地更新
    relevance_status（RELEVANT/UNCERTAIN/IRRELEVANT）。
    """
    by_query = {r.get("query_id"): r for r in registry}
    counts = {"RELEVANT": 0, "UNCERTAIN": 0, "IRRELEVANT": 0}
    fn = screener_fn or screen_relevance
    for p in staging:
        if p.get("relevance_status") != "STAGED":
            continue
        qid = (p.get("query_ids") or [""])[0]
        q = by_query.get(qid, {})
        paper = {"title": p.get("title", ""), "paper_id": p.get("paper_id", "")}
        verdict = fn(paper, q.get("query_text", ""), q.get("source_node", ""))
        p["relevance_status"] = verdict
        counts[verdict] = counts.get(verdict, 0) + 1
    return counts


# ── ② extractor 集成（只收 RELEVANT + UNCERTAIN，丢 IRRELEVANT）──

def assert_all_screened(staging: list[dict]) -> None:
    """invariant ①（硬断言，fail-fast）：STAGED 论文必须全部三态化后才能 extractor。

    用户定：存在 STAGED/None/UNKNOWN → 直接报错停止，**宁可不 extraction，
    也不要悄悄继续**（曾出现 87 STAGED → 0/0/0 → 85 extracted 的违规路径）。
    """
    unscreened = [p.get("paper_id") for p in staging
                  if p.get("relevance_status") not in
                  ("RELEVANT", "UNCERTAIN", "IRRELEVANT")]
    if unscreened:
        statuses = sorted({str(p.get("relevance_status")) for p in staging})
        raise AssertionError(
            f"ERROR: {len(unscreened)} papers remain unscreened "
            f"(当前 status 分布: {statuses})——STAGED 必须先过 relevance 三态，"
            f"禁止未 screening 直接 extractor（fail-fast，用户 invariant）")


def extract_candidates(staging: list[dict], extract_fn,
                       keep=("RELEVANT", "UNCERTAIN")) -> list:
    """RELEVANT + UNCERTAIN 进 extraction（invariant ②：UNCERTAIN 不丢）。

    extract_fn(papers: list[Paper]) -> list[KnowledgeRecord]（真实 =
    KnowledgeExtractor.extract_many 异步包装；测试注入 mock）。
    staging dict → Paper 对象（extractor 契约 = Paper，含 title/abstract）。
    返回 KnowledgeRecord 列表（含 route_mechanism_edges）。
    """
    papers = [p for p in staging if p.get("relevance_status") in keep]
    if not papers:
        return []
    from ..models import Paper
    paper_objs = [
        Paper(paper_id=p.get("paper_id", ""),
              title=p.get("title", ""),
              abstract=p.get("abstract", ""))
        for p in papers
    ]
    records = extract_fn(paper_objs)
    return [r for r in records if r is not None]


# ── ③ discovery edge layer（完整 provenance）──

def edges_to_discovery_layer(records, provenance_map: dict) -> list[dict]:
    """KnowledgeRecord.route_mechanism_edges → discovery edges（带 discovery_provenance）。

    provenance_map: paper_id → {promoted_node, query_id, query_text, query_family,
    origin_round}（从 staging/provenance 记录取）。edge 保留原字段 + provenance。
    """
    edges = []
    for rec in records:
        pid = getattr(rec, "paper_id", "") or ""
        prov = provenance_map.get(pid, {})
        for e in getattr(rec, "route_mechanism_edges", []) or []:
            edges.append({
                "paper_id": pid,
                "raw_route": e.raw_route, "canonical_route": e.canonical_route,
                "raw_mechanism": e.raw_mechanism,
                "canonical_mechanism": e.canonical_mechanism,
                "evidence": e.evidence, "confidence": e.confidence,
                "relation_type": e.relation_type,
                "discovery_provenance": {
                    "origin": "knowledge_expansion",
                    **prov,
                    "paper_id": pid,
                },
            })
    return edges


def save_discovery_edges(edges: list[dict], path: str = DISCOVERY_EDGES_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"phase": "2.1b-discovery-edges", "edges": edges},
                  f, ensure_ascii=False, indent=2)


def load_discovery_edges(path: str = DISCOVERY_EDGES_PATH) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("edges", [])
    except Exception:
        return []


# ── ④ scanner rerun + candidate before/after diff ──

class DiscoveryLayerView:
    """合成视图：KB records + discovery edges → scan_kb 可扫（get_all 接口兼容）。

    discovery edges 按 paper_id 附加到 KB 已有 record；新 paper 的 edges 构造
    新 KnowledgeRecord。**不写回已确认 KB**（discovery layer 独立）。
    """

    def __init__(self, kb, discovery_edges: list[dict]):
        self.kb = kb
        self.discovery_edges = discovery_edges

    def get_all(self):
        from ..models import KnowledgeRecord, RouteMechanismEvidenceEdge
        recs = list(self.kb.get_all())
        by_paper: dict[str, list] = {}
        for e in self.discovery_edges:
            by_paper.setdefault(e.get("paper_id", ""), []).append(e)
        for rec in recs:
            pid = getattr(rec, "paper_id", "")
            extra = by_paper.pop(pid, None)
            if extra:
                rec.route_mechanism_edges = list(rec.route_mechanism_edges or []) + [
                    RouteMechanismEvidenceEdge(
                        paper_id=e.get("paper_id", ""),
                        raw_route=e.get("raw_route", ""),
                        canonical_route=e.get("canonical_route", ""),
                        raw_mechanism=e.get("raw_mechanism", ""),
                        canonical_mechanism=e.get("canonical_mechanism", ""),
                        evidence=e.get("evidence", ""),
                        confidence=e.get("confidence", 0.0),
                        relation_type=e.get("relation_type", "direct"),
                    ) for e in extra]
        for pid, items in by_paper.items():
            recs.append(KnowledgeRecord(
                paper_id=pid,
                route_mechanism_edges=[RouteMechanismEvidenceEdge(
                    paper_id=e.get("paper_id", ""),
                    raw_route=e.get("raw_route", ""),
                    canonical_route=e.get("canonical_route", ""),
                    raw_mechanism=e.get("raw_mechanism", ""),
                    canonical_mechanism=e.get("canonical_mechanism", ""),
                    evidence=e.get("evidence", ""),
                    confidence=e.get("confidence", 0.0),
                    relation_type=e.get("relation_type", "direct"),
                ) for e in items],
            ))
        return recs


def candidate_diff(before_ids: set[str], after_ids: set[str]) -> dict:
    """candidate before/after diff（invariant ④：旧候选重新发现不算成功）。

    new_candidates = after - before；new_candidate_not_seen_before 同口径
    （before 含全部历史池——未见过 = 不在 before 中）。
    """
    new = sorted(after_ids - before_ids)
    return {
        "new_candidates": new,
        "new_candidate_not_seen_before": new,
        "new_candidate_count": len(new),
    }


# ── candidate 审计（用户 invariant：56 个 hash ≠ 56 个新概念）──

def canonical_identity(c: dict) -> str:
    """canonical identity：canonical_name > canonical_match > raw_name（小写归一）。"""
    return (c.get("canonical_name") or c.get("canonical_match")
            or c.get("raw_name", "")).lower().strip()


def audit_new_candidates(new_candidates: list[dict],
                         provenance: list[dict] | None = None) -> list[dict]:
    """new candidates 审计表（用户要求）：raw_name/canonical_name/candidate_type/
    canonical_match/source_paper/query_family——56 个 hash 不等于 56 个新概念。"""
    provs = provenance or []
    rows = []
    for c in new_candidates:
        paper = (c.get("source_papers") or [None])[0]
        fams = sorted({r.get("query_family", "?") for r in provs
                       if r.get("paper_id") == paper})
        rows.append({
            "raw_name": c.get("raw_name"),
            "canonical_name": c.get("canonical_name"),
            "canonical_identity": canonical_identity(c),
            "candidate_type": c.get("candidate_type"),
            "canonical_match": c.get("canonical_match"),
            "source_paper": paper,
            "query_family": fams,
        })
    return rows


def canonical_candidates(before_pool: list[dict],
                         after_pool: list[dict]) -> dict:
    """canonical-before/after 比较（用户 invariant：看 canonical identity 而非
    candidate_id——reduced shrinkage stress / reduced polymerization shrinkage stress
    不能算 3 个新方向）。

    返回 new_raw_candidates（新 id）与 new_canonical_candidates（新 canonical 身份）。
    Phase 2.1 最终验收只看 new_canonical_candidate_not_seen_before。
    """
    before_ids = {c["candidate_id"] for c in before_pool}
    after_ids = {c["candidate_id"] for c in after_pool}
    new_raw = sorted(after_ids - before_ids)
    before_canon = {canonical_identity(c) for c in before_pool}
    after_canon = {canonical_identity(c) for c in after_pool}
    new_canon = sorted(after_canon - before_canon)
    return {
        "new_raw_candidates": new_raw,
        "new_canonical_candidates": new_canon,
        "new_canonical_candidate_not_seen_before": new_canon,
    }


def rerun_scanner(scan_fn, kb, discovery_edges: list[dict],
                  pool: list[dict]) -> tuple[int, list[dict]]:
    """scanner rerun：合成层扫描 → type/filter → merge_pool。返回 (scanned, merged_pool)。

    scan_kb 返回 RawCandidate——需先过 typer/filter（_build_candidate，与 controller
    同逻辑）转 DiscoveryCandidate 才能 merge_pool。
    """
    from .candidate import merge_pool
    from .controller import _build_candidate
    view = DiscoveryLayerView(kb, discovery_edges)
    raws = scan_fn(view)
    built = [_build_candidate(r) for r in raws]   # merge_pool 契约 = DiscoveryCandidate 对象
    merged = merge_pool(pool, built)
    return len(raws), merged


# ── trace（未来 RL trajectory 雏形）──

def build_trace(origin_promotion: str = "", query_id: str = "",
                query_text: str = "", query_family: str = "",
                paper_id: str = "", relevance: str = "",
                edge: dict | None = None, candidate: str | None = None,
                seen_before: bool = False,
                promotion_id: str = "") -> dict:
    """一条完整链 trace（用户定格式）：
    origin promotion → query → NEW paper → relevance → new edge → new candidate → seen_before。

    **trace_complete 硬判定**（用户 invariant：不能'尽量填'）：
        只有 origin_promotion / query_id / paper_id / relevance / edge / candidate
        全部非空才为 True——只有 complete 的 trace 才能计入 successful_closed_loop，
        否则标记 INCOMPLETE_TRACE。
    """
    edge_str = None
    if edge:
        # 方向修正（2026-08-27 审计）：edge 数据模型是 raw_route → raw_mechanism
        # （route=主语/条件，mechanism=宾语/结果，knowledge_extractor 契约）。
        # 旧实现 src=raw_mechanism、tgt=raw_route 把方向显示反了（曾输出
        # "increased modulus --direct--> filler loading"，实际存储是
        # "filler loading --direct--> increased modulus"）。
        src = edge.get("raw_route") or edge.get("raw_mechanism") or ""
        tgt = edge.get("raw_mechanism") or edge.get("raw_route") or ""
        edge_str = f"{src} --{edge.get('relation_type', '?')}--> {tgt}" if src and tgt else None
    trace = {
        "origin_promotion": origin_promotion,
        "promotion_id": promotion_id,
        "query_id": query_id,
        "query_family": query_family,
        "query": query_text,
        "new_paper": paper_id,
        "relevance": relevance,
        "new_edge": edge_str,
        "new_candidate": candidate,
        "candidate_seen_before": seen_before,
        "trace_complete": bool(origin_promotion and query_id and paper_id
                               and relevance and edge_str and candidate),
    }
    return trace
