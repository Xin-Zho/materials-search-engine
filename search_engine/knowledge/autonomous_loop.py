"""AutonomousLoop — Phase 1.7 自主知识循环。

闭环：Knowledge Base → coverage 分析 → 缺口检测 → gap query 生成 → 搜索 →
相关性筛选 → 知识抽取 → 入库 → 重算 coverage，直到缺口下降停滞或达上限轮次。

第一版是有界 loop（不做 bandit/动态预算/复杂停止条件）：

    max_rounds = 3
    max_gap_queries_per_round = 10   （gap targets，每 target 内部 fallback 链不计配额）
    max_results_per_query = 20

每轮四件事：
    ① 找当前缺失 route × mechanism（MechanismCoverageAnalyzer + GapDetector）
    ② 生成 gap targets（每个含 L0→L3 分级 fallback query 链，带 provenance）
    ③ 分级搜索（L0 strict R+M+A → L1 strict R+M → L2 fulltext → L3 fulltext）
       + 去重 + 筛 relevant
    ④ Target Evidence Gate → 入库 + 记 paper_traces + query_traces 漏斗 → 重算 coverage

指标（每轮）：
    new_relevant_papers       本轮 overall relevant 论文数（RelevanceFilter 判定）
    new_gap_candidates        gate 通过数（title/abstract 含 target mechanism）
    new_covered_mechanisms    本轮新覆盖的机制数（= gaps_before - gaps_after）
    remaining_gaps            本轮结束后剩余缺口数
    query_gap_hit_rate        hit>0 的 query 占比（Gap Closure Efficiency）
    paper_gap_hit_rate        target_mechanism_hit=True 的 paper 占比

query_traces（每个 gap target 的检索漏斗，供 analyze_gap_failures.py 定位流失层）：
    originating_gap / query / level_used(L0-3, None=全空) /
    total_hits(数据源报告) → retrieved(进入候选) → gap_gate(通过 evidence gate) →
    relevant(RelevanceFilter) → hit(extract 后机制命中)

paper_traces（每篇 detail）：
    paper_id / title / score / originating_gap / query / assigned_route /
    extracted_mechanisms / target_evidence_gate / target_mechanism_hit

停止条件（工程循环，非统计 completeness proof）：
    no_new_relevant_papers | no_gap_reduction | max_rounds

真正的 completeness proof 留给 Phase 3 的独立 statistical audit。
"""

import logging

from ..llm import LLMBackend
from ..route_normalizer import RouteNormalizer
from ..route_ontology import RouteOntology
from ..mechanism_normalizer import MechanismNormalizer
from ..route_mechanism_ontology import (
    assign_route,
    route_match_type,
    CoverageMatcher,
    knowledge_status,
    missing_reason,
)
from ..relevance import RelevanceFilter
from ..knowledge_extractor import KnowledgeExtractor
from ..knowledge_base import KnowledgeBase
from ..backends import SearchBackend
from .coverage import MechanismCoverageAnalyzer
from .gap_detector import GapDetector
from .gap_query_generator import GapQueryGenerator

logger = logging.getLogger(__name__)


class AutonomousLoop:
    """有界自主知识循环。"""

    def __init__(
        self,
        llm_backend: LLMBackend,
        search_backend: SearchBackend,
        knowledge_base: KnowledgeBase,
        *,
        max_rounds: int = 3,
        max_gap_queries_per_round: int = 10,
        max_results_per_query: int = 20,
        relevance_threshold: int = 70,
        anchor: str = "polymerization shrinkage",
        gap_threshold: int = 1,
        initial_gap_targets: set | None = None,
        mode: str = "completeness",
    ):
        """initial_gap_targets: 冻结的 TRUE_OPEN baseline（phase17_baseline manifest 的
        initial_true_open）。提供后 loop 只针对这些 gap 搜索（新发现的 gap 单独统计，
        不进入本轮 target 生成），不提供则 round 1 自动冻结当前 missing。

        mode（用户定 2026-08-25，按 KNOWLEDGE_STATUS + MISSING_REASON 分流）：
          completeness —— 只搜 confirmed_missing/SEARCH_GAP（补漏，提高 coverage；
                          EXTRACTION_GAP 走 extractor/人工修复，不让搜索 agent 浪费时间）
          discovery    —— 只搜 hypothesis（探索，发现新机制）
          all          —— 不过滤（全部 missing 缺口）
        """
        self.llm = llm_backend
        self.search = search_backend
        self.kb = knowledge_base
        self.max_rounds = max_rounds
        self.max_gap_queries_per_round = max_gap_queries_per_round
        self.max_results_per_query = max_results_per_query
        self.relevance_threshold = relevance_threshold
        self.anchor = anchor
        self.initial_gap_targets = (
            set(initial_gap_targets) if initial_gap_targets is not None else None
        )
        if mode not in ("completeness", "discovery", "all"):
            raise ValueError(f"mode 必须为 completeness/discovery/all，got {mode!r}")
        self.mode = mode

        # 内部构建组件（loop 是编排者，不拥有算法）
        self.normalizer = RouteNormalizer(llm_backend)
        self.ontology = RouteOntology(llm_backend)
        self.analyzer = MechanismCoverageAnalyzer(llm_backend, self.normalizer, self.ontology)
        self.detector = GapDetector(gap_threshold)
        self.generator = GapQueryGenerator(anchor)
        self.extractor = KnowledgeExtractor(llm_backend)
        self.relevance_filter = RelevanceFilter(llm_backend)
        self.mech_normalizer = MechanismNormalizer()  # target 别名匹配

    async def run(self, research_question: str) -> dict:
        """跑有界 loop，返回每轮指标（含 query_traces / paper_traces）+ 停止原因。"""
        history: list[dict] = []
        stop_reason = "max_rounds"
        final_gaps = 0
        initial_gaps: set | None = None  # 冻结 round 1 的 gap universe

        for round_i in range(1, self.max_rounds + 1):
            logger.info("=== Round %d ===", round_i)

            # ① 找当前缺失 route × mechanism
            records = self.kb.get_all()
            cov = await self.analyzer.analyze_route_coverage(records)
            missing = self.detector.detect_missing_mechanisms(cov["mechanism_coverage"])
            gaps_before = sum(len(v) for v in missing.values())
            existing_routes = list(cov.get("route_coverage", {}).keys())
            before_set = {(r, m) for r, mechs in missing.items() for m in mechs}
            if initial_gaps is None:
                if self.initial_gap_targets is not None:
                    # Phase 1.7 baseline：只搜冻结的 TRUE_OPEN targets（∩ 当前 missing，
                    # 防 baseline 已关闭/已覆盖的 gap 重复计入）
                    initial_gaps = set(self.initial_gap_targets) & before_set
                    logger.info("使用冻结 baseline %d 个 TRUE_OPEN targets (∩ missing=%d)",
                                len(self.initial_gap_targets), len(initial_gaps))
                else:
                    initial_gaps = set(before_set)  # 冻结单次 run 的 initial gap universe
            logger.info("Round %d: %d records, %d 缺口 (initial=%d)", round_i, len(records), gaps_before, len(initial_gaps))

            # ② 生成 gap targets（每个含 L0→L3 fallback 链）
            gap_targets = self._build_gap_targets(missing)
            if len(gap_targets) < 2 and self.mode == "all":
                # 策略级回退（无 mechanism 维度）——仅 all 模式；completeness/discovery
                # 按 KNOWLEDGE_STATUS 分流，不用策略级 gap 破坏模式纯度
                strat = await self.analyzer.analyze(records)
                strat_gaps = self.detector.detect_gaps(strat["coverage"])
                for g in strat_gaps:
                    gap_targets.append({
                        "route": g, "mechanism": None,
                        "fallback": self.generator.generate_fallback_queries(g, None, self.anchor),
                    })
                gap_targets = gap_targets[: self.max_gap_queries_per_round]
            if not gap_targets:
                logger.info("Round %d: 无缺口可生成 query，停止", round_i)
                history.append({
                    "round": round_i, "mode": self.mode,
                    "new_relevant_papers": 0,
                    "new_gap_candidates": 0, "new_covered_mechanisms": 0,
                    "remaining_gaps": gaps_before, "gap_queries": 0,
                    "query_gap_hit_rate": 0.0, "paper_gap_hit_rate": 0.0,
                    "query_traces": [], "paper_traces": [],
                })
                stop_reason = "no_gaps"
                final_gaps = gaps_before
                break

            # ③ 分级搜索（fallback 链）+ 去重 + 筛 relevant
            existing_ids = {self._paper_key(r.paper_id) for r in records}
            search_out = await self._search_and_filter(
                gap_targets, research_question, existing_ids, existing_routes
            )
            relevant = search_out["relevant"]
            raw_query_traces = search_out["query_traces"]

            # Target Evidence Gate：title/abstract 是否含 target mechanism（本地文本，不调 LLM）
            for item in relevant:
                item["gate_pass"] = self._target_evidence_gate(
                    item["scored"].paper, item["gap"].get("mechanism")
                )
            gap_candidates = sum(1 for item in relevant if item["gate_pass"])

            # ④ 入库 + 记 paper_traces
            ingested, paper_traces = await self._ingest_and_trace(relevant)
            query_traces = self._summarize_query_traces(raw_query_traces, relevant, paper_traces)
            hit_papers = sum(1 for t in paper_traces if t["target_mechanism_hit"])
            paper_hit_rate = hit_papers / len(paper_traces) if paper_traces else 0.0
            query_hit_rate = sum(1 for qt in query_traces if qt["hit"] > 0) / len(query_traces) if query_traces else 0.0

            # 重算 coverage（matrix 级，LLM 归并）
            records_after = self.kb.get_all()
            cov_after = await self.analyzer.analyze_route_coverage(records_after)
            missing_after = self.detector.detect_missing_mechanisms(cov_after["mechanism_coverage"])
            gaps_after = sum(len(v) for v in missing_after.values())
            after_set = {(r, m) for r, mechs in missing_after.items() for m in mechs}
            mech_cov_after = cov_after.get("mechanism_coverage", {})
            matrix_closed = before_set - after_set        # matrix 确认关闭（LLM 归并）
            discovered = after_set - before_set           # 新发现 gap（知识发现）
            closed_deltas = []
            for (route, mech) in sorted(matrix_closed):
                entry = mech_cov_after.get(route, {}).get(mech, {})
                closed_deltas.append({
                    "route": route, "mechanism": mech,
                    "evidence": (entry.get("evidence") or "")[:100],
                    "confidence": entry.get("confidence", 0),
                })

            # coverage commit：对全部 open gaps global rematch（统一 CoverageMatcher）
            # 原则：query provenance ≠ knowledge destination——论文因 gap A 被搜到，
            # 但可能关闭 gap B（CROSS_GAP_HIT）。
            matcher = CoverageMatcher(self.mech_normalizer)
            commit_traces = []
            for item in relevant:
                rec = item.get("rec")
                sp = item["scored"]
                gap = item["gap"]
                g_route, g_mech = gap.get("route"), gap.get("mechanism")
                paper_routes = self._paper_canonical_routes(rec) if rec else set()
                closed_by_paper = []
                if rec:
                    # Phase 1.8: coverage 只认 route_mechanism_edges（edge_supports_gap）。
                    # 旧记录（无 edges）→ closed_by_paper 空 → NO_COVERAGE_GAIN（等重抽）。
                    # 禁止 paper.routes × paper.mechanisms 笛卡尔组合凭空生成关系。
                    for (cr, cm) in sorted(before_set):
                        if not cm:
                            continue
                        st = "NO_MATCH"
                        for e in rec.route_mechanism_edges:
                            s = matcher.edge_supports_gap(e, cr, cm)
                            if s in ("DIRECT_MODEL", "DIRECT_HUMAN", "INHERITED"):
                                st = s
                                break
                        if st != "NO_MATCH":
                            closed_by_paper.append({
                                "route": cr, "mechanism": cm, "match_type": st,
                            })
                target_closed = (
                    any(c["route"] == g_route and c["mechanism"] == g_mech for c in closed_by_paper)
                    if g_mech else None
                )
                cross = [c for c in closed_by_paper
                         if not (c["route"] == g_route and c["mechanism"] == g_mech)]
                verdict = ("TARGET_GAP_HIT" if target_closed
                           else ("CROSS_GAP_HIT" if cross else "NO_COVERAGE_GAIN"))
                commit_traces.append({
                    "paper_id": sp.paper.paper_id,
                    "title": (sp.paper.title or "")[:80],
                    "originating_gap": {"route": g_route, "mechanism": g_mech},
                    "canonical_routes": sorted(paper_routes),
                    "closed_gaps": closed_by_paper,
                    "target_closed": target_closed,
                    "verdict": verdict,
                    "gate_pass": item.get("gate_pass"),
                    "persisted": bool(rec and (rec.strategy_routes or rec.physical_mechanisms)),
                    # mechanism debug（dual-curing 类样本定位用）
                    "target_mech": g_mech,
                    "extracted_mechanisms": [
                        m.canonical or m.mechanism
                        for m in (rec.physical_mechanisms if rec else [])
                    ][:6],
                    "mechanism_evidence": [
                        ((m.canonical or m.mechanism), (m.evidence or "")[:40])
                        for m in (rec.physical_mechanisms if rec else [])
                    ][:3],
                })

            # unique closure accounting：
            #   predicted = 本地 rematch 预测关闭（union(paper.closed_gaps) & round_start_open）
            #   confirmed = matrix before/after diff（coverage matrix 是唯一 authoritative truth）
            predicted = set()
            for c in commit_traces:
                for cg in c.get("closed_gaps", []):
                    predicted.add((cg["route"], cg["mechanism"]))
            predicted &= before_set
            confirmed = matrix_closed
            new_covered = len(confirmed)                  # unique gap 首次关闭数（matrix 确认）
            target_paper_hits = sum(1 for c in commit_traces if c["verdict"] == "TARGET_GAP_HIT")
            cross_paper_hits = sum(1 for c in commit_traces if c["verdict"] == "CROSS_GAP_HIT")

            # 硬 invariant：predicted（本地 rematch）== confirmed（matrix）必须一致
            commit_consistent = (predicted == confirmed)
            initial_remaining = len(initial_gaps & after_set)
            newly_discovered_total = len(after_set - initial_gaps)
            if not commit_consistent:
                logger.error(
                    "COVERAGE_COMMIT_INCONSISTENCY: commit_closed=%s vs matrix_closed=%s",
                    sorted(newly_closed), sorted(matrix_closed))
                history.append({
                    "round": round_i,
                    "new_relevant_papers": len(relevant),
                    "new_covered_mechanisms": new_covered,
                    "target_paper_hits": target_paper_hits,
                    "cross_paper_hits": cross_paper_hits,
                    "unique_gaps_closed": new_covered,
                    "newly_discovered": len(discovered),
                    "initial_remaining": initial_remaining,
                    "total_open": len(after_set),
                    "commit_consistent": False,
                    "remaining_gaps": gaps_after,
                    "query_traces": query_traces,
                    "paper_traces": paper_traces,
                    "commit_traces": commit_traces,
                })
                stop_reason = "coverage_commit_inconsistency"
                final_gaps = gaps_after
                break

            delta_consistent = (new_covered == len(closed_deltas))
            # ── Phase 1.7 核心指标（用户定：只看搜索系统产生价值的证据）──
            initial_true_open_before = len(initial_gaps & before_set)
            initial_true_open_after = len(initial_gaps & after_set)
            # new_direct_model_edges：本轮新入库 paper 的非 human edges（DIRECT_MODEL 来源）
            new_direct_model_edges = 0
            for item in relevant:
                rec = item.get("rec")
                if rec:
                    new_direct_model_edges += sum(
                        1 for e in rec.route_mechanism_edges
                        if (e.provenance or "") != "manual_audit"
                        and e.relation_type != "human_verified"
                    )
            # target_gaps_closed / cross_gaps_closed（gap 级，initial 内）：
            # closed gap 被 TARGET_GAP_HIT paper 关闭 → target；否则 cross
            closed_in_initial = confirmed & initial_gaps
            target_closed_gaps: set = set()
            cross_closed_gaps: set = set()
            for ct in commit_traces:
                for cg in ct.get("closed_gaps", []):
                    key = (cg["route"], cg["mechanism"])
                    if key not in closed_in_initial:
                        continue
                    if ct.get("verdict") == "TARGET_GAP_HIT":
                        target_closed_gaps.add(key)
                    else:
                        cross_closed_gaps.add(key)
            for key in closed_in_initial - target_closed_gaps:
                cross_closed_gaps.add(key)

            logger.info(
                "Round %d: relevant=%d ingested=%d init_true_open=%d→%d "
                "target_closed=%d cross_closed=%d new_direct_edges=%d discovered=%d open=%d",
                round_i, len(relevant), ingested,
                initial_true_open_before, initial_true_open_after,
                len(target_closed_gaps), len(cross_closed_gaps),
                new_direct_model_edges, len(discovered), len(after_set),
            )

            history.append({
                "round": round_i,
                "mode": self.mode,
                "new_relevant_papers": len(relevant),
                "new_gap_candidates": gap_candidates,
                "new_papers_ingested": ingested,
                "new_covered_mechanisms": new_covered,
                "target_paper_hits": target_paper_hits,
                "cross_paper_hits": cross_paper_hits,
                "unique_gaps_closed": new_covered,
                "initial_true_open_before": initial_true_open_before,
                "initial_true_open_after": initial_true_open_after,
                # Phase 1.7 open 拆分（用户定：不要混淆）：
                #   TRUE_OPEN_INITIAL   = 冻结 baseline 大小（不变）
                #   TRUE_OPEN_REMAINING = baseline 中仍 open 的（= initial_true_open_after）
                #   OTHER_OPEN          = missing 中非 baseline 的（EXTRACTION_MISS 机制 + 新发现）
                "true_open_initial": len(initial_gaps),
                "true_open_remaining": initial_true_open_after,
                "other_open": len(after_set - initial_gaps),
                "target_gaps_closed": len(target_closed_gaps),
                "cross_gaps_closed": len(cross_closed_gaps),
                "new_direct_model_edges": new_direct_model_edges,
                "newly_discovered": len(discovered),
                "initial_remaining": initial_remaining,
                "total_open": len(after_set),
                "closed_deltas": closed_deltas,
                "coverage_delta_consistent": delta_consistent,
                "commit_consistent": True,
                "remaining_gaps": gaps_after,
                "gap_queries": len(gap_targets),
                "query_gap_hit_rate": round(query_hit_rate, 3),
                "paper_gap_hit_rate": round(paper_hit_rate, 3),
                "query_traces": query_traces,
                "paper_traces": paper_traces,
                "commit_traces": commit_traces,
            })
            final_gaps = gaps_after

            # 停止条件
            if len(relevant) == 0:
                stop_reason = "no_new_relevant_papers"
                break
            # no_gap_reduction 看本轮是否关闭了任何 gap（unique 毛关闭，非净）
            if new_covered == 0:
                stop_reason = "no_gap_reduction"
                break

        return {
            "rounds": history,
            "stop_reason": stop_reason,
            "final_remaining_gaps": final_gaps,
            "total_rounds": len(history),
        }

    def _build_gap_targets(self, missing: dict) -> list[dict]:
        """机制级 gap targets（带 L0→L3 fallback 链），限量 max_gap_queries_per_round。

        按 self.mode 过滤（用户定 2026-08-25，KNOWLEDGE_STATUS + MISSING_REASON 分流）：
          completeness —— 只留 confirmed_missing 且 missing_reason==SEARCH_GAP（补漏，
                         代表搜索能力；EXTRACTION_GAP 走 extractor/人工，不让搜索浪费）
          discovery    —— 只留 KNOWLEDGE_STATUS == hypothesis（探索新机制）
          all          —— 不过滤；unknown 状态只在 all 下进入
        """
        targets = []
        for route, mechs in missing.items():
            for mech in mechs:
                if self.mode == "completeness":
                    if knowledge_status(route, mech) != "confirmed_missing":
                        continue
                    if missing_reason(route, mech) != "SEARCH_GAP":
                        continue
                elif self.mode == "discovery":
                    if knowledge_status(route, mech) != "hypothesis":
                        continue
                targets.append({
                    "route": route, "mechanism": mech,
                    "fallback": self.generator.generate_fallback_queries(route, mech, self.anchor),
                })
        return targets[: self.max_gap_queries_per_round]

    async def _search_fallback(self, fallback_queries: list[dict]) -> tuple[list, dict]:
        """执行 L0→L3 fallback 链。

        返回 (results, info)：results 非空 = 第一个有结果的 level 的候选；
        info["level"]=None = 整条链 0 命中。info 记录执行细节供 trace 漏斗：
        query_text / query_mode(scope) / request_mode / backend_status / meta_count / results_len。
        """
        info: dict = {"level": None, "scope": None, "query_text": "", "request_mode": "",
                      "backend_status": None, "meta_count": 0, "results_len": 0}
        for fq in fallback_queries:
            info = {
                "level": None, "scope": fq["scope"], "query_text": fq["query"],
                "request_mode": "filter" if fq["scope"] == "strict" else "search",
                "backend_status": None, "meta_count": 0, "results_len": 0,
            }
            try:
                if fq["scope"] == "strict":
                    results = await self.search.search_strict(fq["query"], limit=self.max_results_per_query)
                else:
                    results = await self.search.search(fq["query"], limit=self.max_results_per_query)
                info["backend_status"] = 200
            except Exception as e:
                logger.warning("search 失败 %r: %s", fq["query"], e)
                results = []
                info["backend_status"] = f"error:{type(e).__name__}"
            info["meta_count"] = getattr(self.search, "last_total_hits", 0) or 0
            info["results_len"] = len(results)
            if results:
                info["level"] = fq["level"]
                logger.info("gap %s → L%d(%s) %d 篇 (count %d)",
                            fq["query"][:45], fq["level"], fq["scope"], len(results), info["meta_count"])
                return results, info
        return [], info

    async def _search_and_filter(
        self,
        gap_targets: list[dict],
        research_question: str,
        existing_ids: set[str],
        existing_routes: list[str],
    ) -> dict:
        """每个 target 跑 fallback 链 → 跨 target 去重 → 相关性筛选。

        返回 {"relevant": [{"scored", "gap", "gate_pass"}], "query_traces": [...]}。
        """
        candidates: dict[str, dict] = {}  # key -> {"paper": Paper, "gap": target}
        query_traces: list[dict] = []
        for gq in gap_targets:
            results, finfo = await self._search_fallback(gq["fallback"])
            retrieved = 0
            for p in results:
                if not getattr(p, "title", None):
                    continue
                key = self._paper_key(getattr(p, "doi", None) or getattr(p, "paper_id", ""))
                if key and key not in existing_ids and key not in candidates:
                    candidates[key] = {"paper": p, "gap": gq}
                    retrieved += 1
            query_traces.append({
                "originating_gap": {"route": gq["route"], "mechanism": gq["mechanism"]},
                "query": gq["fallback"][0]["query"] if gq["fallback"] else "",
                # 执行细节（backend 真实做了什么）
                "query_level": finfo.get("level"),               # None = 整条链 0 命中
                "query_mode": finfo.get("scope"),                # strict / fulltext
                "query_text": finfo.get("query_text", ""),       # 实际发给 backend 的 query
                "request_mode": finfo.get("request_mode", ""),   # filter / search
                "backend_status": finfo.get("backend_status"),   # 200 / error:xxx
                "meta_count": finfo.get("meta_count", 0),        # 数据源报告命中数
                "results_len": finfo.get("results_len", 0),      # backend 实际返回数
                # 兼容字段（analyze 沿用）
                "level_used": finfo.get("level"),
                "total_hits": finfo.get("meta_count", 0),
                "retrieved": retrieved,                          # 去重后进入候选
                "gap_gate": 0, "relevant": 0, "hit": 0,          # 稍后 _summarize 填充
            })
            if len(candidates) >= self.max_results_per_query * 3:
                break

        papers = [c["paper"] for c in candidates.values()]
        if not papers:
            return {"relevant": [], "query_traces": query_traces}

        scored = await self.relevance_filter.filter(
            papers,
            research_question,
            threshold=self.relevance_threshold,
            top_k=self.max_results_per_query,
            existing_routes=existing_routes,
        )
        relevant = []
        for s in scored:
            if s.score >= self.relevance_threshold:
                key = self._paper_key(getattr(s.paper, "doi", None) or getattr(s.paper, "paper_id", ""))
                gap = candidates.get(key, {}).get("gap", {})
                relevant.append({"scored": s, "gap": gap})
        return {"relevant": relevant, "query_traces": query_traces}

    @staticmethod
    def _summarize_query_traces(query_traces: list[dict], relevant: list[dict],
                                paper_traces: list[dict]) -> list[dict]:
        """把 gate/relevant/hit 按 originating gap 归组填进 query_traces 漏斗。"""
        for item in relevant:
            key = (item["gap"].get("route"), item["gap"].get("mechanism"))
            for qt in query_traces:
                if (qt["originating_gap"]["route"], qt["originating_gap"]["mechanism"]) == key:
                    qt["relevant"] += 1
                    if item.get("gate_pass"):
                        qt["gap_gate"] += 1
                    break
        for t in paper_traces:
            if not t["target_mechanism_hit"]:
                continue
            key = (t["originating_gap"]["route"], t["originating_gap"]["mechanism"])
            for qt in query_traces:
                if (qt["originating_gap"]["route"], qt["originating_gap"]["mechanism"]) == key:
                    qt["hit"] += 1
                    break
        return query_traces

    async def _ingest_and_trace(self, relevant: list[dict]) -> tuple[int, list[dict]]:
        """入库 + 记 paper_traces。返回 (ingested_count, paper_traces)。"""
        ingested = 0
        traces: list[dict] = []
        for item in relevant:
            sp = item["scored"]
            gap = item["gap"]
            try:
                rec = await self.extractor.extract(sp.paper)
                if rec and (rec.strategy_routes or rec.physical_mechanisms):
                    self.kb.store(rec)
                    ingested += 1
                item["rec"] = rec if (rec and (rec.strategy_routes or rec.physical_mechanisms)) else None
                extracted_mechs = [
                    m.canonical or m.mechanism
                    for m in (rec.physical_mechanisms if rec else [])
                ][:8]
                target_hit = self._mechanism_hit(rec, gap.get("mechanism"))
                traces.append({
                    "paper_id": sp.paper.paper_id,
                    "title": (sp.paper.title or "")[:80],
                    "score": sp.score,
                    "originating_gap": {
                        "route": gap.get("route"),
                        "mechanism": gap.get("mechanism"),
                    },
                    "query": (gap.get("fallback") or [{}])[0].get("query", ""),
                    "assigned_route": getattr(sp, "category", None) or getattr(sp, "route", None),
                    "canonical_route": assign_route(rec.strategy_routes) if rec else None,
                    "extracted_mechanisms": extracted_mechs,
                    "target_evidence_gate": item.get("gate_pass"),
                    "target_mechanism_hit": target_hit,
                })
            except Exception as e:
                logger.warning("extract 失败 %s: %s", sp.paper.paper_id, e)
        return ingested, traces

    @staticmethod
    def _paper_canonical_routes(rec) -> set[str]:
        """论文的全部 canonical routes（multi-label，逐 strategy_route 归并）。"""
        routes: set[str] = set()
        for phrase in (rec.strategy_routes or []):
            r = assign_route([phrase])
            if r:
                routes.add(r)
        return routes

    def _target_evidence_gate(self, paper, target_mechanism: str | None) -> bool:
        """Target Evidence Gate：title/abstract 是否出现 target mechanism 的 canonical/aliases。

        overall relevant（RelevanceFilter）≠ gap candidate（含 target evidence）。
        gate 通过 = 论文文本层面至少讨论 target mechanism，才值得 extract/入库。
        """
        if not target_mechanism:
            return True  # 策略级 gap 无 mechanism，视为候选
        aliases = self.mech_normalizer.aliases_for(target_mechanism) or [target_mechanism.lower()]
        text = " ".join(
            t for t in (
                getattr(paper, "title", None) or "",
                getattr(paper, "abstract", None) or "",
            ) if t
        ).lower()
        if not text:
            return False
        return any(a.lower() in text for a in aliases)

    def _mechanism_hit(self, rec, target_mechanism: str | None) -> bool | None:
        """判断 rec 是否覆盖了 target mechanism（别名匹配 rec 的 mechanism 文本）。

        无 target mechanism（策略级 gap）返回 None。
        """
        if not target_mechanism:
            return None
        aliases = self.mech_normalizer.aliases_for(target_mechanism)
        if not aliases:
            aliases = [target_mechanism.lower()]
        for m in (rec.physical_mechanisms if rec else []):
            text = " ".join(
                t for t in (m.canonical, m.cause, m.mechanism, m.effect, m.evidence) if t
            ).lower()
            if any(a.lower() in text for a in aliases):
                return True
        return False

    @staticmethod
    def _paper_key(ident: str) -> str:
        """归一化 paper 标识（doi / paper_id）用于去重。"""
        if not ident:
            return ""
        k = ident.lower().strip()
        for pfx in ("https://doi.org/", "http://dx.doi.org/", "doi:"):
            if k.startswith(pfx):
                k = k[len(pfx):]
                break
        return k
