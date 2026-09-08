"""search_engine/audit/miss_analyzer.py — v3.0 Miss Analyzer（只收集证据，不做判断）。

输入：独立 Auditor 判定 RELEVANT、但 Search A 未找到（agent_seen=false）的论文。
输出：MissEvidence（结构化诊断证据），分类由 failure_classifier 负责。

证据域（用户 2026-08-29 定稿）：
  identity_evidence     eid/doi/wid 解析状态
  query_evidence        目标出现在哪些已执行 query 的结果集；query_set_contains_target
  citation_evidence     是否已知 bridge 节点；backward links to found；bridge_count
  community_evidence    目标是否在 term community supporting papers；community_ids
  term_evidence         目标 title/abstract 提取短语（复用 v2.1.2 extractor）：
                        shared（系统已掌握）/ novel（系统未掌握）/ historical（旧年份∧novel）
  execution_evidence    相关 query 是否执行；目标是否在导出深度内

数据源（第一版 deterministic，路径可配置 + %TEMP% fallback）：
  query registry / 已执行 query 结果集（Round3 + Round1 + hop2_round2）
  citation_bridge_hop2 / term_communities_hop2 / round3_expansion_B_v5（term registry）
  openalex_hop2_enriched（wid 侧信息 + referenced_works）
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tools"))

from build_round3_expansion_terms import canonicalize_phrase, extract_verbatim  # noqa: E402

_ANCHOR_CANON = canonicalize_phrase("polymerization shrinkage")
HISTORICAL_YEAR = 2006        # v2.0 PRE_2006 口径
DEFAULT_DEPTH = 500           # 正式 Round3 导出深度


def _path(p: str) -> str:
    if os.path.exists(p):
        return p
    alt = os.path.join(os.environ.get("TEMP", ""), os.path.basename(p))
    if os.path.exists(alt):
        return alt
    return None


def _norm_doi(d) -> str | None:
    if not d:
        return None
    x = str(d).strip().lower()
    return x or None


def _norm_eid(e) -> str | None:
    if not e:
        return None
    x = str(e).strip()
    return x or None


class AuditContext:
    """一次性加载系统 registries（只读）。"""

    def __init__(self, data_dir: str | None = None):
        base = data_dir or os.path.join(BASE, "data", "exports")
        self.data_dir = base

        # ── 已执行 query 结果集：eid/doi -> set(query_ids) + query 深度 ──
        self.exec_eid: dict[str, set[str]] = {}
        self.exec_doi: dict[str, set[str]] = {}
        self.query_depth: dict[str, int] = {}
        self._load_retrieval(os.path.join(base, "round3_depth500_retrieval.json"),
                             DEFAULT_DEPTH)
        self._load_retrieval(os.path.join(base, "community_round1_retrieval.json"),
                             500)
        self._load_retrieval(os.path.join(base, "community_hop2_round2_retrieval.json"),
                             500)

        # ── openalex enriched（wid 侧 + referenced_works；先加载供 found 映射）──
        self.enriched: dict[str, dict] = {}
        ep = _path("openalex_hop2_enriched.json")
        if ep:
            self.enriched = json.load(open(ep, encoding="utf-8"))

        # ── found（Search A 已找到论文集合）──
        self.found_eids: set[str] = set()
        self.found_dois: set[str] = set()
        self.found_wids: set[str] = set()
        self._load_found()

        # ── citation graph（hop2 bridge；candidates 无 eid 字段，wid/doi 双索引）──
        self.bridge: dict[str, dict] = {}        # wid -> {doi, bridge_count}
        self.bridge_doi: dict[str, dict] = {}    # doi -> {wid, bridge_count}
        hp = _path(os.path.join(base, "citation_bridge_hop2.json")) or \
            _path("citation_bridge_hop2.json")
        if hp:
            bd = json.load(open(hp, encoding="utf-8"))
            for c in bd.get("candidates", []):
                wid = c.get("wid")
                doi = _norm_doi(c.get("doi"))
                bc = c.get("bridge_count")
                if wid:
                    self.bridge[wid] = {"doi": doi, "bridge_count": bc}
                if doi:
                    self.bridge_doi[doi] = {"wid": wid, "bridge_count": bc}

        # ── term community（supporting papers -> community_ids；语言池）──
        self.paper_comm: dict[str, list[str]] = {}   # eid/wid/doi -> [TC_x]
        self.comm_terms: dict[str, list[str]] = {}   # TC_x -> terms
        tp = _path(os.path.join(base, "term_communities_hop2.json")) or \
            _path("term_communities_hop2.json")
        if tp:
            tc = json.load(open(tp, encoding="utf-8"))
            for c in tc.get("communities", []):
                cid = c.get("community_id")
                if not cid:
                    continue
                self.comm_terms[cid] = list(c.get("terms", []) or [])
                for pid in c.get("supporting_papers", []) or []:
                    self.paper_comm.setdefault(str(pid), []).append(cid)

        # ── term registry（v2.1.2 候选 canonical 集合）──
        self.terms: set[str] = set()
        rp = _path(os.path.join(base, "round3_expansion_B_v5.json")) or \
            _path("round3_expansion_B_v5.json")
        if rp:
            rd = json.load(open(rp, encoding="utf-8"))
            for t in rd.get("all_verbatim_terms", []):
                c = canonicalize_phrase(t.get("term", ""))
                if c:
                    self.terms.add(c)

    # ── 加载 helper ──
    def _load_retrieval(self, path: str, depth: int):
        if not os.path.exists(path):
            return
        d = json.load(open(path, encoding="utf-8"))
        for cid, spec in d.get("communities", {}).items():
            for q in spec.get("queries", []):
                qid = q.get("query_id")
                if not qid:
                    continue
                self.query_depth[qid] = depth
                for rec in q.get("records", []):
                    e = _norm_eid(rec.get("eid"))
                    d2 = _norm_doi(rec.get("doi"))
                    if e:
                        self.exec_eid.setdefault(e, set()).add(qid)
                    if d2:
                        self.exec_doi.setdefault(d2, set()).add(qid)

    def _load_found(self):
        """found = depth run eids ∪ 各 retrieval 结果 eids/dois（Search A 已见）。
        wid 侧：enriched 中 doi 与 found_dois 匹配的论文计入 found_wids。"""
        dp = os.path.join(self.data_dir, "query_family_runs_depth.json")
        if os.path.exists(dp):
            dd = json.load(open(dp, encoding="utf-8"))
            for rs in dd.get("records", {}).values():
                for r in rs:
                    e = _norm_eid(r.get("eid"))
                    if e:
                        self.found_eids.add(e)
        for e in self.exec_eid:
            self.found_eids.add(e)
        for d in self.exec_doi:
            self.found_dois.add(d)
        for wid, m in self.enriched.items():
            d = _norm_doi(m.get("doi"))
            if d and d in self.found_dois:
                self.found_wids.add(wid)


def analyze_miss(miss: dict, ctx: AuditContext) -> dict:
    """收集一篇 miss 的完整证据（不分类）。"""
    eid = _norm_eid(miss.get("eid"))
    doi = _norm_doi(miss.get("doi"))
    wid = miss.get("wid") or None
    title = (miss.get("title") or "").strip()
    abstract = (miss.get("abstract") or "").strip()
    year = miss.get("year")

    # ── identity ──
    identity = {
        "eid": eid, "doi": doi, "wid": wid,
        "resolved": bool(eid or doi or wid),
        "found_in_candidate_db": bool((eid and eid in ctx.found_eids)
                                      or (doi and doi in ctx.found_dois)),
    }

    # ── query coverage ──
    matched = set()
    if eid:
        matched |= ctx.exec_eid.get(eid, set())
    if doi:
        matched |= ctx.exec_doi.get(doi, set())
    query_evidence = {
        "matched_existing_queries": sorted(matched),
        "query_set_contains_target": bool(matched),
    }

    # ── citation ──
    b = ctx.bridge.get(wid) if wid else None
    if b is None and doi:
        b = ctx.bridge_doi.get(doi)
    backward = 0
    if wid and wid in ctx.enriched:
        refs = ctx.enriched[wid].get("referenced_works") or []
        backward = len([r for r in refs if r in ctx.found_wids])
    citation_data_available = bool(ctx.bridge or ctx.bridge_doi) \
        or bool(wid and wid in ctx.enriched)
    citation_evidence = {
        "known_bridge_node": b is not None,
        "bridge_count": (b or {}).get("bridge_count"),
        "backward_links_to_found": backward,
        "forward_links_to_found": None,   # 第一版不做 forward 扫描（成本高，留空标注）
        "_data_available": citation_data_available,
        "note": "forward_links 未计算（v3.0 MVP 第一版省略 forward 扫描）",
    }

    # ── community ──
    comm_ids = set()
    for key in (eid, wid, doi):
        if key and key in ctx.paper_comm:
            comm_ids |= set(ctx.paper_comm[key])
    community_evidence = {
        "represented": bool(comm_ids),
        "community_ids": sorted(comm_ids),
    }

    # ── term overlap（复用 v2.1.2 extractor）──
    rows = extract_verbatim([{"wid": wid or "?", "title": title,
                              "abstract": abstract}])
    canon_set = {canonicalize_phrase(r["phrase"]) for r in rows}
    canon_set.discard(None)
    canon_set.discard(_ANCHOR_CANON)
    shared = sorted(c for c in canon_set if c in ctx.terms)
    novel = sorted(c for c in canon_set if c not in ctx.terms)
    historical = sorted(c for c in novel
                        if year is not None and isinstance(year, (int, str))
                        and str(year).isdigit() and int(year) < HISTORICAL_YEAR)
    term_evidence = {
        "shared_terms_with_found": shared,
        "novel_terms": novel,
        "historical_terms": historical,
        "n_shared": len(shared),
        "n_novel": len(novel),
    }

    # ── execution ──
    execution_evidence = {
        "query_executed": bool(matched),
        "within_depth": None,    # 目标不在任何结果集时无法判定；由 classifier 按 M5 规则看
        "note": "within_depth 仅在 query_set_contains_target 时有意义",
    }

    return {
        "paper_id": miss.get("paper_id") or eid or doi or wid,
        "audit_round": miss.get("audit_round"),
        "audit_label": miss.get("audit_label"),
        "agent_seen": miss.get("agent_seen", False),
        "title": title[:200], "year": year,
        "identity_evidence": identity,
        "query_evidence": query_evidence,
        "citation_evidence": citation_evidence,
        "community_evidence": community_evidence,
        "term_evidence": term_evidence,
        "execution_evidence": execution_evidence,
    }


def _load_labels_meta(audit_id: str) -> dict:
    """从 labels 文件（filled 优先，模板兜底）读 {pid: {title, abstract, year, doi, eid}}。"""
    safe = audit_id.replace(":", "_").replace("/", "_").replace("\\", "_")
    candidates = [
        os.path.join(BASE, "data", "exports", "completeness_labels", f"{safe}_filled.json"),
        os.path.join(BASE, "data", "exports", "completeness_labels", f"{safe}.json"),
    ]
    for p in candidates:
        if not os.path.exists(p):
            continue
        d = json.load(open(p, encoding="utf-8"))
        meta = {}
        for x in d.get("labels", []):
            pid = x.get("paper_id")
            if pid:
                meta[pid] = {"title": x.get("title") or "",
                             "abstract": x.get("abstract") or "",
                             "year": x.get("year"),
                             "doi": x.get("doi"),
                             "eid": x.get("eid")}
        return meta
    return {}


def build_audit_misses(ctx: AuditContext, mode: str = "gold",
                       audit_id: str | None = None) -> tuple[list[dict], str]:
    """从 Phase A 产物导出 misses（Phase A -> Phase B 自动接口）。

    mode='audit'：读 Phase A AuditRecord（audits 必须存在且 labels COMPLETED）：
        misses = {p: label(p)=RELEVANT ∧ p 未被 Search A 找到}
        统计（sample_size/m/CI）直接取 Phase A 数字，Phase B 不重算不改写。
    mode='gold'：用 QGS usable gold（独立人工构建，非概率抽样）∩ agent_seen=false
        ——labels 尚未完成时的降级路径，miss_rate/CI 标注不适用。

    返回 (misses, mode_note)。misses 每项符合用户 schema：
    {paper_id, audit_round, audit_label, agent_seen, title, abstract, year, doi, eid, wid}
    """
    if mode == "audit":
        from search_engine.completeness.audit import find_audit  # noqa: E402
        a = find_audit(audit_id)
        if a is None:
            raise ValueError(
                f"AUDIT_NOT_FOUND: {audit_id}（data/completeness/completeness_audits.json "
                f"无此 audit；先跑 tools/audit_completeness.py --create）")
        if a.status != "COMPLETED":
            raise ValueError(
                f"LABELS_NOT_COMPLETED: {audit_id} status={a.status}——独立 Auditor 标签未完成，"
                f"拒绝导出 misses（宁可不出数，不默认 irrelevant）")
        misses = []
        unknown_ids: list[str] = []
        from search_engine.audit.agent_seen import resolve_agent_seen
        rel_ids = [pid for pid, lab in (a.labels or {}).items()
                   if lab == "RELEVANT"]
        resolved = resolve_agent_seen(rel_ids)
        # 从 labels 模板文件补元数据（title/abstract/year/doi——Miss Expansion 的
        # term mining 依赖 title+abstract；audit record 里只有 label）
        meta = _load_labels_meta(audit_id)
        for pid, lab in (a.labels or {}).items():
            if lab != "RELEVANT":
                continue
            st = resolved.get(pid, {}).get("agent_seen", "UNKNOWN")
            m0 = meta.get(pid, {})
            if st == "FALSE":
                misses.append({"paper_id": pid, "audit_round": audit_id,
                               "audit_label": "RELEVANT", "agent_seen": False,
                               "title": m0.get("title") or "", "abstract": m0.get("abstract") or "",
                               "year": m0.get("year"), "doi": m0.get("doi"),
                               "eid": m0.get("eid"), "wid": pid})
            elif st == "UNKNOWN":
                unknown_ids.append(pid)
        note = (f"Phase A audit {audit_id}（COMPLETED，样本 {a.sample_size}，"
                f"m={a.m}，Recall_LCB={getattr(a, 'recall_lcb', None)} "
                f"[PROVISIONAL]）——misses=RELEVANT ∧ agent_seen=FALSE（三态）；"
                f"agent_seen UNKNOWN {len(unknown_ids)} 篇走 identity repair，"
                f"不计 miss：{unknown_ids[:8]}{'...' if len(unknown_ids) > 8 else ''}")
        return misses, note

    # ── gold 模式（QGS usable，独立人工构建）──
    audit = json.load(open(os.path.join(ctx.data_dir, "research_usability_audit.json"),
                           encoding="utf-8"))
    bench = json.load(open(os.path.join(ctx.data_dir, "pc_001_external_qgs_v1.json"),
                           encoding="utf-8"))
    year_by_eid = {_norm_eid(p.get("scopus_eid")): p.get("year")
                   for p in bench.get("papers", [])}
    misses = []
    for d in audit["qgs_detail"]:
        e = _norm_eid(d.get("eid"))
        doi = _norm_doi(d.get("doi"))
        if not (d.get("usable") and e):
            continue
        if e in ctx.found_eids or (doi and doi in ctx.found_dois):
            continue
        misses.append({"paper_id": f"QGS_{e}", "audit_round": "GOLD_QGS",
                       "audit_label": "RELEVANT", "agent_seen": False,
                       "title": d.get("title") or "", "abstract": "",
                       "year": year_by_eid.get(e), "doi": doi,
                       "eid": e, "wid": None})
    note = ("QGS usable gold（独立人工构建 benchmark，非概率抽样）："
            "miss_rate/CI 不适用，标注 N/A")
    return misses, note
