"""Phase 2.0 candidate 验证 CLI：对 VERIFYING 候选回答 Q1/Q2/Q3。

流程（用户定 2026-08-26 修正版）：
  1. 本地 Q1（canonical filter）：concept_independent / novel_to_ontology
  2. **统一验证语料**：candidate.source_papers/evidence（scanner 已发现，绝不能丢）
     + 验证搜索新论文（Candidate lexical family × Target lexical family）
  3. 本地概念相关判定：语料中提及候选词族的论文数（concept_related_paper_count）
  4. LLM 分析（DeepSeek）：Q2 相关性 + Q3 causal chain + DIRECT 三层计数
  5. decide_verdict → 更新候选 status（含 SEARCH_INCONCLUSIVE——retrieval failure ≠ 领域无关）

无 key / --plan-only 时只输出验证计划（Q1 本地 + 词族查询），不调外部。

用法:
    python tools/verify_candidate.py --name "incremental curing" --plan-only
    python tools/verify_candidate.py --name "incremental curing"            # 完整验证（需 key）
"""

import argparse
import asyncio
import json
import os
import re
import sys

from search_engine.discovery import DiscoveryCandidate
from search_engine.discovery.verifier import (
    VerificationResult, build_verification_queries, build_candidate_family,
    q1_concept_check, decide_verdict, verification_sufficiency_level,
)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

POOL_PATH = "data/exports/phase2_candidates.json"
VERIFY_RUNS_PATH = "data/exports/candidate_verification_runs.json"


def _load_pool() -> list[dict]:
    with open(POOL_PATH, encoding="utf-8") as f:
        return json.load(f).get("candidates", [])


def _save_pool(cands: list[dict]):
    with open(POOL_PATH, encoding="utf-8") as f:
        data = json.load(f)
    data["candidates"] = cands
    with open(POOL_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _log_run(run: dict):
    runs = []
    if os.path.exists(VERIFY_RUNS_PATH):
        try:
            runs = json.load(open(VERIFY_RUNS_PATH, encoding="utf-8"))
        except Exception:
            runs = []
    runs.append(run)
    with open(VERIFY_RUNS_PATH, "w", encoding="utf-8") as f:
        json.dump(runs, f, ensure_ascii=False, indent=2)


def _concept_related_count(terms: list[str], papers: list[dict]) -> int:
    """本地判定：论文集（title/abstract）命中任一 term 的篇数（词边界匹配）。"""
    fam = [t.lower() for t in terms]
    n = 0
    for p in papers:
        text = " ".join(t for t in (p.get("title", ""), p.get("abstract", "")) if t).lower()
        if not text:
            continue
        if any(re.search(rf"\b{re.escape(f)}\b", text) for f in fam):
            n += 1
    return n


def _retrieval_quality(search_concept: int, searched_n: int) -> str:
    """检索质量（用户定 2026-08-26：只描述搜索本身，lexical precision，不否决 verdict）：
    INVALID —— 新搜论文 0 篇命中候选概念（candidate lexical hit rate = 0）
    PARTIAL —— 部分命中（36% 这种：检索结果有点脏但可能有足够证据）
    GOOD   —— 候选概念论文有效召回（≥50%）
    """
    if searched_n == 0:
        return "INVALID"
    hit_rate = search_concept / searched_n
    if hit_rate >= 0.5:
        return "GOOD"
    return "PARTIAL" if hit_rate > 0 else "INVALID"


def _seed_corpus(c: dict) -> tuple[list[dict], int]:
    """候选自带证据 → corpus 论文（scanner 已发现，必须进验证语料）。

    用 provenance.evidence_samples 的**原始 evidence 文本**作 seed 论文 abstract
    （paper_id + 原文可追溯，用户定：DIRECT 必须 raw-source traceable——
    不用占位符/模型总结，否则 LLM 会把结构化总结句误当 DIRECT）。
    """
    papers, n_ev = [], 0
    samples = (c.get("provenance") or {}).get("evidence_samples", [])
    for s in samples:
        pid, ev = s.get("paper", ""), s.get("evidence", "")
        if pid and ev:
            papers.append({"paper_id": pid, "title": c.get("raw_name", ""),
                           "abstract": f"[KB seed evidence] {ev}", "seed": True})
            n_ev += 1
    # 兜底：source_papers 中没有 evidence_samples 映射的论文（无原文可追溯 → 不算 DIRECT 证据源）
    return papers, n_ev


async def _llm_analyze(key: str, candidate_name: str, family: list[str], query_plan: list[dict],
                       corpus: list[dict], seed_evidence: list[str]) -> dict:
    """DeepSeek 分析统一语料 → Q2 相关性 + Q3 causal chain + DIRECT 三层计数。

    三层计数（用户定）：
      direct_concept_evidence_count  —— 论文直接证明候选概念本身
      direct_relation_evidence_count —— 论文直接证明概念→机制链
      direct_target_evidence_count   —— 论文直接证明概念→降低光固化收缩应力
    每层同时输出 *_paper_count（evidence count ≠ paper count，独立性按论文去重）。
    **DIRECT 必须 raw-source traceable**：只能引用语料中给出的原文片段（paper_id + 引文）；
    模型 paraphrase / 结构化总结句只能标 INFERRED / STRUCTURED_SUMMARY，不能进 DIRECT 计数。
    candidate_validated（节点值不值得加入 ontology）≠ causal_status（是否带来新机制）：
      causal_status ∈ NOVEL_CAUSAL_CHAIN / EXISTING_MECHANISM_COMPOSITION /
                     PARTIAL_CAUSAL_EVIDENCE / NO_CAUSAL_EVIDENCE
    """
    from search_engine.llm import DeepSeekBackend
    llm = DeepSeekBackend(api_key=key)
    sys_prompt = (
        "你是材料学知识验证助手。候选概念可能是降低光固化聚合收缩应力的新知识方向。"
        "对给定验证语料（含候选自带 seed evidence 和检索论文），严格区分："
        "论文原文明确陈述（DIRECT，必须引用语料中给出的原文片段 + paper_id）"
        "vs 你的推测或结构化总结（INFERRED / STRUCTURED_SUMMARY，不能进 DIRECT 计数）。"
        "禁止'关键词共现即建链'；禁止把模型总结句当 DIRECT 证据。\n"
        "输出 JSON：{relevance: HIGH|MEDIUM|LOW, relevance_evidence: [引文], "
        "causal_chain: [{step, evidence, paper_id, evidence_type: DIRECT|INFERRED|STRUCTURED_SUMMARY}], "
        "direct_concept_evidence_count: int, direct_concept_paper_count: int, "
        "direct_relation_evidence_count: int, direct_relation_paper_count: int, "
        "direct_target_evidence_count: int, direct_target_paper_count: int, "
        "candidate_validated: bool, causal_status: NOVEL_CAUSAL_CHAIN|EXISTING_MECHANISM_COMPOSITION|"
        "PARTIAL_CAUSAL_EVIDENCE|NO_CAUSAL_EVIDENCE, "
        "supporting_papers: [paper_id], note: str}"
    )
    corpus_text = []
    for p in corpus[:10]:
        tag = "[KB seed]" if p.get("seed") else "[search]"
        corpus_text.append(
            f"- {tag} {p['paper_id']}: {p.get('title', '')}\n  {p.get('abstract', '')[:500]}")
    user_msg = (
        f"候选概念: {candidate_name}\n候选词族: {json.dumps(family, ensure_ascii=False)}\n"
        f"验证查询计划: {json.dumps(query_plan, ensure_ascii=False)}\n"
        f"统一验证语料（{len(corpus)} 篇）:\n" + "\n".join(corpus_text)
    )
    resp = await llm.chat(system_prompt=sys_prompt, user_message=user_msg)
    m = re.search(r"\{.*\}", resp, re.S)
    if not m:
        return {"note": f"LLM 输出非 JSON: {resp[:200]}", "direct_target_evidence_count": 0}
    try:
        return json.loads(m.group(0))
    except Exception as e:
        return {"note": f"LLM JSON 解析失败: {e}", "direct_target_evidence_count": 0}


async def _search_papers(family: list[str], target_family: list[str],
                         mailto: str | None) -> list[dict]:
    """OpenAlex 验证搜索：候选词族 × target 词族短语 AND + relevance 排序。

    为什么不用普通 search：`sort=cited_by_count:desc` 会把候选相关论文挤出 top-N
    （bulk-fill dental 论文被引低于 hydrogel/MOF 综述——实测 49 篇全被挤掉）。
    search_relevance 用 filter=title_and_abstract.search（候选词/目标词 required）
    + sort=relevance_score:desc，候选匹配度决定排序。
    """
    from search_engine.backends import OpenAlexBackend
    seen, papers = set(), []
    queries = [f'"{f}" AND "{t}"' for f in family[:3] for t in target_family]
    async with OpenAlexBackend(mailto=mailto) as search:
        for query in queries:
            try:
                results = await search.search_relevance(query, limit=10)
            except Exception:
                continue
            for p in results:
                pid = getattr(p, "paper_id", "") or ""
                if pid in seen:
                    continue
                seen.add(pid)
                papers.append({
                    "paper_id": pid,
                    "title": getattr(p, "title", "") or "",
                    "abstract": getattr(p, "abstract", "") or "",
                })
    return papers


async def _run_verify(args) -> None:
    cands = _load_pool()
    hits = [c for c in cands
            if args.name.lower() in c["raw_name"].lower() or c["candidate_id"] == args.name]
    if not hits:
        print(f"✗ 找不到候选: {args.name}")
        return
    if len(hits) > 1:
        print("匹配到多个，请精确:")
        for c in hits:
            print(f"  {c['candidate_id']}  {c['raw_name']}  [{c['status']}]")
        return
    c = hits[0]
    if c["status"] not in ("VERIFYING", "NEED_MORE_EVIDENCE", "SEARCH_INCONCLUSIVE") \
            and not args.plan_only:
        print(f"✗ 候选不在验证中: {c['raw_name']} status={c['status']}"
              f"（先 review_candidates --set-status VERIFYING；--plan-only 可预览计划）")
        return

    name = c["raw_name"]
    family = build_candidate_family(name)
    print(f"验证候选: {name}  [{c['candidate_type']}, {c['source']}, {c['domain_relevance']}]")
    print(f"候选词族: {family}")

    indep, novel, matched = q1_concept_check(name)
    print(f"\nQ1 Concept: concept_independent={indep}  novel_to_ontology={novel}"
          + (f"  matched={matched}" if matched else ""))

    plan = build_verification_queries(name)
    print("\nQ 查询计划（词族 × target 家族）:")
    for q in plan:
        print(f"  [{q['type']}] {q['purpose']}")
        for query in q["queries"][:5]:
            print(f"      · {query}")

    if args.plan_only or not os.environ.get("DEEPSEEK_API_KEY"):
        print("\n[plan-only] 未执行外部搜索/LLM（需要 DEEPSEEK_API_KEY 完整验证）。"
              "完整验证: python tools/verify_candidate.py --name \"%s\"" % name)
        return

    # 统一验证语料：seed（scanner 已发现，绝不丢）+ 新搜论文（seed 与 search 统计完全分开）
    seed_papers, seed_ev_count = _seed_corpus(c)
    print(f"\n搜索 OpenAlex + 分析中...（seed evidence {seed_ev_count} 条 + 新搜）")
    target_family = ["polymerization shrinkage", "polymerization shrinkage stress",
                     "shrinkage stress", "photopolymerization stress"]
    searched = await _search_papers(family, target_family,
                                    args.mailto or os.environ.get("OPENALEX_MAILTO") or None)
    seed_concept = _concept_related_count(family, seed_papers)
    search_concept = _concept_related_count(family, searched)
    search_target = _concept_related_count(target_family, searched)
    quality = _retrieval_quality(search_concept, len(searched))
    hit_rate = (search_concept / len(searched)) if searched else 0.0
    print(f"新搜 {len(searched)} 篇 | candidate lexical hit rate = {hit_rate:.0%} "
          f"({search_concept} 篇命中候选词族) | target 命中 {search_target} | "
          f"retrieval_quality = {quality}")
    print(f"seed 命中候选概念 {seed_concept}（只证明概念存在，不能证明本次检索有效）")

    # S0 短路：本次检索连候选概念论文都没召回 → 直接 SEARCH_INCONCLUSIVE，不送 LLM（省调用）
    if search_concept == 0 and search_target == 0:
        result = VerificationResult(
            candidate_id=c["candidate_id"], candidate_name=name,
            concept_independent=indep, novel_to_ontology=novel,
            domain_relevance=c["domain_relevance"],
            corpus_total=len(seed_papers) + len(searched),
            seed_evidence_count=seed_ev_count,
            seed_concept_related_count=seed_concept,
            search_concept_related_count=0,
            search_target_related_count=0,
            retrieval_quality="INVALID",
            verification_sufficiency="INSUFFICIENT",
            note=f"S0 retrieval failure：新搜 {len(searched)} 篇均未命中候选词族"
                 f"（seed {seed_concept} 篇只证明概念存在）。retrieval failure ≠ 领域无关，"
                 f"先修搜索再验证。",
        )
        result.verdict = decide_verdict(result)
        print(f"\n结果: {name}  verdict = {result.verdict}")
        print(f"  note = {result.note}")
        c["status"] = result.verdict
        c.setdefault("review_log", []).append({
            "to": result.verdict, "by": "verifier", "verdict": result.verdict,
            "note": result.note,
        })
        c["provenance"]["verification"] = result.to_dict()
        _save_pool(cands)
        _log_run({"candidate_id": c["candidate_id"], "candidate": name,
                  "verdict": result.verdict, "result": result.to_dict()})
        print(f"\n✓ 已更新: {c['raw_name']} → {result.verdict}"
              f"（回 VERIFYING: review_candidates --set-status VERIFYING --direct --reason ...）")
        return

    analysis = await _llm_analyze(os.environ["DEEPSEEK_API_KEY"], name, family, plan,
                                  corpus=seed_papers + searched,
                                  seed_evidence=c.get("evidence", []))
    result = VerificationResult(
        candidate_id=c["candidate_id"], candidate_name=name,
        concept_independent=indep, novel_to_ontology=novel,
        domain_relevance=analysis.get("relevance", c["domain_relevance"]),
        relevance_evidence=analysis.get("relevance_evidence", []),
        causal_chain=analysis.get("causal_chain", []),
        direct_concept_evidence_count=analysis.get("direct_concept_evidence_count", 0),
        direct_relation_evidence_count=analysis.get("direct_relation_evidence_count", 0),
        direct_target_evidence_count=analysis.get("direct_target_evidence_count", 0),
        direct_concept_paper_count=analysis.get("direct_concept_paper_count", 0),
        direct_relation_paper_count=analysis.get("direct_relation_paper_count", 0),
        direct_target_paper_count=analysis.get("direct_target_paper_count", 0),
        candidate_validated=analysis.get("candidate_validated"),
        causal_status=analysis.get("causal_status", "NO_CAUSAL_EVIDENCE"),
        supporting_papers=analysis.get("supporting_papers", []),
        corpus_total=len(seed_papers) + len(searched),
        seed_evidence_count=seed_ev_count,
        seed_concept_related_count=seed_concept,
        search_concept_related_count=search_concept,
        search_target_related_count=search_target,
        retrieval_quality=quality,
        note=analysis.get("note", ""),
    )
    # verification_sufficiency：证据充分性（retrieval_quality 只作 caveat）
    result.verification_sufficiency = verification_sufficiency_level(result)
    result.verdict = decide_verdict(result)
    print(f"\n结果: {name}")
    print(f"  retrieval_quality = {result.retrieval_quality}  "
          f"verification_sufficiency = {result.verification_sufficiency}")
    print(f"  Q2 relevance = {result.domain_relevance}  "
          f"relevance_evidence={len(result.relevance_evidence)} 条")
    print(f"  Q3 causal_chain = {len(result.causal_chain)} 步  "
          f"causal_status = {result.causal_status}")
    print(f"  candidate_validated = {result.candidate_validated}")
    print(f"  DIRECT 三层 (evidence/paper): "
          f"concept={result.direct_concept_evidence_count}/{result.direct_concept_paper_count}  "
          f"relation={result.direct_relation_evidence_count}/{result.direct_relation_paper_count}  "
          f"target={result.direct_target_evidence_count}/{result.direct_target_paper_count}"
          f"（effective concept = {result.effective_concept_paper_count}，向上蕴含）")
    print(f"  corpus={result.corpus_total}  seed_hit={result.seed_concept_related_count}  "
          f"search_hit={result.search_concept_related_count}  "
          f"search_target={result.search_target_related_count}  "
          f"independent_support={result.total_independent_support}")
    for step in result.causal_chain[:4]:
        print(f"      [{step.get('evidence_type', '?')}] {step.get('step')}  "
              f"← {str(step.get('evidence', ''))[:55]}")
    print(f"  verdict = {result.verdict}")
    if result.note:
        print(f"  note = {result.note}")

    c["status"] = result.verdict
    c.setdefault("review_log", []).append({
        "to": result.verdict, "by": "verifier", "verdict": result.verdict,
        "note": result.note or "",
    })
    c["provenance"]["verification"] = result.to_dict()
    _save_pool(cands)
    _log_run({"candidate_id": c["candidate_id"], "candidate": name,
              "verdict": result.verdict, "result": result.to_dict()})
    print(f"\n✓ 已更新: {c['raw_name']} → {result.verdict}"
          f"（SEARCH_INCONCLUSIVE/NEED_MORE_EVIDENCE 可回 VERIFYING 重跑）")


def main():
    ap = argparse.ArgumentParser(description="Phase 2.0 candidate 验证（Q1/Q2/Q3）")
    ap.add_argument("--name", required=True, help="候选名（raw_name 子串或 candidate_id）")
    ap.add_argument("--plan-only", action="store_true", help="只输出验证计划（Q1 本地 + 词族查询）")
    ap.add_argument("--mailto", default="", help="OpenAlex polite pool email")
    args = ap.parse_args()
    asyncio.run(_run_verify(args))


if __name__ == "__main__":
    main()
