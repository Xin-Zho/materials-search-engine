#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/llm_cross_layer_generator.py — 受约束 LLM Query Generator v1.1（2026-09-01 用户拍板）。

v1.1 升级（用户 2026-09-01 16:10）：
  - LLM 只输出 term-level 证据（expression_family 数组 + application + expansion_type
    + rationale），**不直接输出裸 query**——query 由工具从 verified terms 组装
  - **每个 term 必须通过 corpus 验证**：term 子串必须真实出现在 KnownRelevantKnowledge
    某篇论文的 title/abstract → 记录 {term, semantic_role, application_domain,
    source_paper, source_sentence}；验证失败的 term 标 UNVERIFIED 且不进 query
    （防 LLM 编造；可追溯）
  - query 组装：SHRINKAGE_CORE AND verified family terms（OR 组内）——保证 query
    每个词都有 corpus 证据

背景（R05 taxonomy，用户跑 scopus_reachability_check.py 确认）：
  - 43 miss：QUERY_EXPRESSIVITY 19（44.2%）/ RETRIEVAL_OR_TOKEN_SEMANTICS 3（7.0%）
    / BACKEND_UNREACHABLE 11（25.6%）/ UNKNOWN 10（23.3%）
  - Scopus-reachable 的 22 篇 miss 中 19/22 = 86.4% 死于 query expressivity
  - 主指标：Recall_resolved = 37/80 = 46.25%；37/99 = conservative end-to-end recall
  - 理论空间：仅修 19 篇 QUERY_EXPRESSIVITY → 70.0%（development 上限，非 S6 预测）

纪律（写死）：
  - term source = KnownRelevantKnowledge（found_relevant + openalex abstract）
  - R05 miss papers 不提供词（gap hint 只给方向：Application-centered reframing）
  - 已执行 query（S4+S5 36 条）进 prompt 防重复 + 规则 novelty gate
  - 输出走现有 pipeline：pilot → quality gate → candidate QA → freeze S6 → fresh R06

用法：
  python tools/llm_cross_layer_generator.py --provider deepseek --model deepseek-chat
  python tools/llm_cross_layer_generator.py --provider ollama --model qwen3:8b
  python tools/llm_cross_layer_generator.py --plan-only
"""
import argparse
import asyncio
import datetime
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tools"))

from build_cross_layer_queries import (  # noqa: E402
    LAYER_RULES, FAMILY_ALIASES, _norm, _load_oa_with_abstract,
    extract_paper_tags, SHRINKAGE_CORE,
)

T = os.path.join(BASE, "data", "exports", "terminology")
DEFAULT_EXECUTED = os.path.join(T, "s5_final_actions.json")
DEFAULT_OUT = os.path.join(T, "s6_llm_query_candidates.json")

GENERIC_BLACKLIST = ("resin", "polymer", "composite", "material", "surface",
                     "process", "application", "study", "investigation", "effect")

SYSTEM_PROMPT = """You are a materials-science literature search expert. Your job is to
identify how the SAME underlying mechanism — **polymerization / curing induced volume
change → stress / deformation** — is expressed across DIFFERENT research domains, and to
produce term families that would find such papers in Scopus.

## The four expansion types (produce families across all four)

1. LEXICAL — direct synonyms: polymerization shrinkage / curing shrinkage / cure shrinkage /
   volume contraction / volumetric contraction / chemical shrinkage
2. MECHANISM — mechanistic reframings: cure-induced stress / residual strain /
   process-induced deformation / internal strain / contraction stress
3. OBSERVABLE — measurable outcomes: warpage / dimensional error / dimensional instability /
   shape distortion / curl distortion / cuspal deflection / positional drift / spring-in
4. DOMAIN — domain-specific terminology for the SAME mechanism:
   dental → cuspal deflection, marginal gap, contraction stress
   composite manufacturing → spring-in, warpage, residual strain
   stereolithography / 3DP → curl distortion, dimensional accuracy, compensation factor
   optical assembly → positional drift, angular displacement
   electronics packaging → package warpage, molding compound deformation
   coatings / adhesives → volume shrinkage, delamination, bond-line strain

## Hard rules

- EVERY term you output MUST be present in the CORPUS_TERMINOLOGY below (the term or a
  clear lexical variant of it appears in known relevant papers). Do NOT invent terms that
  are absent from the corpus.
- Prefer OBSERVABLE and DOMAIN terms (this is the current coverage gap).
- Output ONLY valid JSON — an array of objects:
  [{"expression_family": ["term1", "term2"], "application": "dental|composites|sla|optical|packaging|coatings|general",
    "expansion_type": "lexical|mechanism|observable|domain", "rationale": "one sentence"}]"""


def build_user_message(corpus_texts: dict, executed_queries: list[str],
                       gap_hint: str) -> str:
    """corpus_texts: paper_id -> {title, abstract}（LLM 只能从这里取词）。"""
    parts = []
    parts.append("## Core phenomenon\npolymerization / curing induced volume change "
                 "→ stress / deformation (photopolymerization & thermosets)")
    # corpus 全文（截断控制）——LLM 的唯一词源
    corpus_blob = []
    for pid, t in list(corpus_texts.items()):
        title = t.get("title", "")
        ab = (t.get("abstract") or "")[:600]
        corpus_blob.append(f"[PAPER {pid}] {title}\n{ab}")
    parts.append("## CORPUS_TERMINOLOGY (AUTHORITATIVE — every output term MUST appear "
                 "here; cite the paper id)\n" + "\n\n".join(corpus_blob))
    parts.append("## Executed queries (already searched — your families must NOT merely "
                 "reproduce these)\n" + "\n".join(f"- {q[:100]}" for q in executed_queries[:36]))
    parts.append(f"## Gap hint (direction only, no miss terms)\n{gap_hint}")
    parts.append("## Task\nGenerate 12 term families across the four expansion types, "
                 "favoring OBSERVABLE and DOMAIN. Each family = 3-8 terms that all appear "
                 "in the corpus. Output JSON array only.")
    return "\n\n".join(parts)


def parse_llm_json(response: str) -> list[dict]:
    text = response.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:])
        if text.endswith("```"):
            text = text[:-3]
    try:
        start, end = text.index("["), text.rindex("]") + 1
        arr = json.loads(text[start:end])
        if isinstance(arr, list):
            return [a for a in arr if isinstance(a, dict) and a.get("expression_family")]
    except (json.JSONDecodeError, ValueError):
        pass
    return []


def verify_term(term: str, corpus_texts: dict) -> dict | None:
    """term 在 corpus 某论文 title/abstract 中真实出现 → 返回证据；否则 None。
    source_sentence = term 所在上下文窗口（≤160 字符）。"""
    tl = term.lower().strip('"').rstrip("*")
    if len(tl) < 3:
        return None
    for pid, t in corpus_texts.items():
        title = t.get("title", "") or ""
        ab = t.get("abstract", "") or ""
        for field, text in (("title", title), ("abstract", ab)):
            if tl in text.lower():
                i = text.lower().index(tl)
                win = text[max(0, i - 60): i + len(tl) + 80].replace("\n", " ")
                return {"term": term, "source_paper": pid, "field": field,
                        "source_sentence": win}
    return None


def assemble_query(family_terms: list[str]) -> str:
    """SHRINKAGE_CORE AND (verified terms OR 组)——保证 query 每个词有 corpus 证据。"""
    quoted = []
    for t in family_terms:
        t2 = t.strip()
        if not t2:
            continue
        if " " in t2 and not t2.startswith('"'):
            quoted.append(f'"{t2}"')
        else:
            quoted.append(t2)
    if not quoted:
        return ""
    return f"TITLE-ABS-KEY({SHRINKAGE_CORE} AND ({' OR '.join(quoted)}))"


def novelty_check(query: str, executed_queries: list[str]) -> bool:
    q_tokens = set(re.findall(r"[a-z]{3,}", query.lower()))
    for eq in executed_queries:
        eq_tokens = set(re.findall(r"[a-z]{3,}", eq.lower()))
        inter = q_tokens & eq_tokens
        if len(inter) / max(len(q_tokens), 1) >= 0.7:
            return False
    return True


def specificity_check(query: str) -> bool:
    ql = query.lower()
    domain_markers = ("warpage", "spring-in", "curl", "deflect", "packag", "molding",
                      "stereolith", "3d print", "sla ", "coating", "adhesive", "optical",
                      "holograph", "encapsul", "dental", "composite", "laminate", "epoxy",
                      "acrylate", "crosslink", "network", "silica", "filler", "strain",
                      "stress", "distortion", "displacement", "accuracy", "deformation")
    return any(m in ql for m in domain_markers)


async def main_async(args):
    from search_engine.llm import create_backend

    # ── 1. corpus（词源 = KnownRelevantKnowledge；R05 miss 不参与）──
    if args.corpus:
        corpus = json.load(open(args.corpus, encoding="utf-8"))
        if isinstance(corpus, dict):
            corpus = corpus.get("papers", corpus.get("labels", []))
    else:
        from search_engine.completeness.universe_builder import build_agent_seen_pool
        pool = build_agent_seen_pool()
        corpus = _load_oa_with_abstract(pool["found_relevant"])
    corpus_texts = {p.get("paper_id") or p.get("wid"): p for p in corpus}
    print(f"[corpus] KnownRelevantKnowledge: {len(corpus)} papers（term 唯一词源）")

    # ── 2. 已执行 queries ──
    executed = []
    if args.executed:
        acts = json.load(open(args.executed, encoding="utf-8"))["actions"]
        executed = [a["query_string"] for a in acts]
    print(f"[executed] {len(executed)} queries（S5 frozen）")

    api_key = args.api_key or os.environ.get("DEEPSEEK_API_KEY") or \
        os.environ.get("TENCENT_MAAS_API_KEY")
    if args.provider == "deepseek" and not api_key:
        raise SystemExit("✗ DeepSeek 需要 API key：--api-key sk-... 或设置 DEEPSEEK_API_KEY"
                         "（或用 --provider ollama / --provider tencent）")
    backend = create_backend(provider=args.provider, api_key=api_key, model=args.model)
    print(f"[llm] provider={args.provider} model={args.model or 'default'}")

    gap_hint = ("Application-centered semantic reframing：R05 诊断显示当前 query 只覆盖"
                "shrinkage 核心词族，缺 OBSERVABLE（warpage/dimensional error/deflection）"
                "与 DOMAIN（SLA curl distortion、packaging warpage、dental cuspal "
                "deflection、optical positional drift）类表达——优先生成这两类。"
                "（方向提示，不含 R05 miss 术语）")

    if args.plan_only:
        print("\n[plan-only] 将调用 LLM 生成 term families + corpus 验证（不写盘）：")
        print(f"  per_round={args.per_round} | executed 防重复={len(executed)}")
        print("  → 每个 term 必须 corpus 命中（source_paper+source_sentence）")
        print("  → query 由 verified terms 组装（SHRINKAGE_CORE AND family）")
        return

    user_msg = build_user_message(corpus_texts, executed, gap_hint)
    response = await backend.chat(system_prompt=SYSTEM_PROMPT, user_message=user_msg,
                                  temperature=0.4, max_tokens=4096)
    fams = parse_llm_json(response)
    print(f"[llm] 原始 families {len(fams)} 组")

    # ── 3. term 级 corpus 验证 ──
    all_terms = []
    for f in fams:
        fam = f.get("expression_family", [])
        if isinstance(fam, str):
            fam = [fam]
        for t in fam:
            all_terms.append({"term": t, "application": f.get("application", "general"),
                              "expansion_type": f.get("expansion_type", "unknown"),
                              "rationale": f.get("rationale", "")})

    verified, unverified = [], []
    for e in all_terms:
        ev = verify_term(e["term"], corpus_texts)
        if ev:
            ev.update({"application": e["application"],
                       "expansion_type": e["expansion_type"],
                       "rationale": e["rationale"]})
            verified.append(ev)
        else:
            unverified.append(e)
    print(f"[verify] term 级 corpus 验证：verified {len(verified)} / "
          f"unverified {len(unverified)}（unverified 不进 query）")
    for u in unverified[:10]:
        print(f"  [UNVERIFIED] {u['term']}")

    # ── 4. 组装 query（按 expansion family 分组）──
    actions = []
    seen_q = set()
    for i, e in enumerate(verified, 1):
        q = assemble_query([e["term"]])
        if not q or q in seen_q:
            continue
        seen_q.add(q)
        if not novelty_check(q, executed):
            e["novelty_pass"] = False
            continue
        if not specificity_check(q):
            e["specificity_pass"] = False
            continue
        e["novelty_pass"] = True
        e["specificity_pass"] = True
        actions.append({
            "action_id": f"LLM_{i:03d}",
            "type": "QUERY_FAMILY",
            "query_string": q,
            "expansion_type": e["expansion_type"],
            "application": e["application"],
            "rationale": e["rationale"],
            "term": e["term"],
            "source_paper": e["source_paper"],
            "source_sentence": e["source_sentence"],
            "bridge_type": "LLM",
        })
    print(f"[gate] 过双门 {len(actions)} 条 query（novelty/specificity 拒 "
          f"{len(verified) - len(actions)}）")

    out = {
        "version": "s6_llm_query_candidates_v1.1",
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "policy": "受约束 LLM Query Generator v1.1（用户 2026-09-01）：LLM 输出 term-level "
                  "证据，工具 corpus 验证（term 必须在 known relevant 论文中出现），"
                  "query 由 verified terms 组装——每个词可追溯",
        "disciplines": {
            "r05_miss_papers_as_term_source": False,
            "term_verification": "corpus 子串命中（source_paper + source_sentence）",
            "unverified_terms_dropped": len(unverified),
            "evaluation": "S6 必须 fresh R06 paired；R05 只作 development evidence",
        },
        "inputs": {"corpus_n": len(corpus), "executed_queries": len(executed),
                   "llm": {"provider": args.provider, "model": args.model or "default"}},
        "term_evidence": verified,
        "unverified_terms": unverified,
        "n_candidates": len(actions),
        "actions": actions,
        "next": "run_s3_query_pilot.py --actions s6_llm_query_candidates.json "
                "--new-basis s4（dev 旁路）→ quality gate → candidate QA → freeze S6 → R06",
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print(f"\n[OK] {args.out}（{len(actions)} 条 query）")
    for a in actions[:15]:
        print(f"  {a['action_id']} [{a['expansion_type']:<9}] {a['query_string'][:78]}")
        print(f"      src={a['source_paper']} | {a['source_sentence'][:70]}")


def main():
    ap = argparse.ArgumentParser(description="受约束 LLM Query Generator v1.1")
    ap.add_argument("--corpus", default="")
    ap.add_argument("--executed", default=DEFAULT_EXECUTED)
    ap.add_argument("--provider", default="deepseek",
                    choices=["deepseek", "ollama", "tencent"])
    ap.add_argument("--model", default="")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--per-round", type=int, default=12)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--plan-only", action="store_true")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
