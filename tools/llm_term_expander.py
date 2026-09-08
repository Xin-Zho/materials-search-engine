#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/llm_term_expander.py — LLM TermFamily 扩展器 V2（domain-conditioned，2026-09-01 用户拍板）。

V2 升级（用户 2026-09-01 17:01）：
  1. **双轴数据结构**：term 不再用 `type + application` 单轴，改为
     semantic_role（lexical / mechanism / observable）× domain（general / dental /
     composites / sla / coatings / optics / packaging）
     ex: {"term": "cusp deflection", "semantic_role": "observable", "domain": "dental"}
  2. **三层验证**：VERIFIED_EXACT（evidence paper 有原字符串——正式 S6 唯一允许进 query）
     / SEMANTIC_CANDIDATE（LLM 语义归纳但原 paper 无原字符串——不删，保留并全 corpus
     反查真实语言证据）/ REJECTED（evidence id 编造且全 corpus 无该词）
     LLM 提出概念，corpus validation 找真实语言证据。
  3. **domain-conditioned expansion**：63 篇按 domain 分批读（每批一个领域，问该领域
     如何描述 cure shrinkage 的机制/观测/表达）——不再一次性生成以 shrinkage 为中心的
     29 个词；目标是几十到几百条有语义差异的 S6 TermBank。
  4. **日志守恒**：raw_terms / dedup_dropped / validated_terms / VERIFIED_EXACT /
     SEMANTIC_CANDIDATE / REJECTED 全打印（防静默丢失）。

V1 保留项：六条 prompt 约束（evidence_paper_id / 四分类 / 禁 R05 miss 词 / 严格 JSON /
不生成 query）；鲁棒 JSON 解析；raw 响应落盘。

后续（不在本工具）：
  - semantic bridge query：允许 (curing OR polymerization OR photocuring) AND
    (warpage OR cusp deflection ...) AND (context)——不再强制 AND shrinkage
    （否则把刚获得的 expressivity 杀掉；77% MATCHES_NO_QUERY 的根源之一）
  - TermBank → query composition → pilot → freeze S6 → fresh R06；RL 之后

用法：
  python tools/llm_term_expander.py --provider deepseek --model deepseek-chat
  python tools/llm_term_expander.py --provider tencent --model hy4-preview --api-key sk-...
  python tools/llm_term_expander.py --plan-only
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

from build_cross_layer_queries import _load_oa_with_abstract  # noqa: E402

T = os.path.join(BASE, "data", "exports", "terminology")
DEFAULT_OUT = os.path.join(T, "s6_term_bank.json")

# ── domain 标注规则（从 LAYER_RULES["APPLICATION"] 细化；论文可多标签）──
DOMAIN_RULES = {
    "dental": ("dental", "dentistry", "restorative", "denture", "tooth", "filling",
               "sealant", "marginal", "cement", "orthodontic", "endodontic"),
    "sla": ("stereolithograph", "sla", "3d print", "additive manufacturing",
            "vat photopolymerization", "digital light processing", "dlp", "slm"),
    "composites": ("composite", "laminate", "fiber reinforced", "carbon fiber",
                   "glass fiber", "thermoset", "fibre"),
    "coatings": ("coating", "paint", "film", "varnish"),
    "optics": ("optical", "holograph", "photoresist", "data storage", "grating",
               "refractive"),
    "packaging": ("packaging", "molding compound", "encapsulation", "insulator",
                  "electronic", "semiconductor", "microelectronic", "multi-chip",
                  "mold compound", "warpage of package"),
}
DOMAIN_ORDER = ("dental", "composites", "sla", "coatings", "optics", "packaging")


def tag_domain(paper: dict) -> list[str]:
    """论文 → domain 标签（可多标签；无命中 → general）。"""
    text = f"{paper.get('title', '')} {paper.get('abstract', '') or ''}".lower()
    tags = [d for d, kws in DOMAIN_RULES.items() if any(k in text for k in kws)]
    return tags or ["general"]


def domain_prompts(corpus_texts: dict) -> list[tuple[str, dict]]:
    """按 domain 分批：返回 [(domain, {paper_id: paper})]。论文可出现在多批。"""
    batches = {}
    for pid, p in corpus_texts.items():
        for d in tag_domain(p):
            batches.setdefault(d, {})[pid] = p
    # 顺序固定（DOMAIN_ORDER 优先，general 最后）
    ordered = [d for d in DOMAIN_ORDER if d in batches]
    ordered += [d for d in batches if d not in DOMAIN_ORDER]
    return [(d, batches[d]) for d in ordered]


SYSTEM_PROMPT = """You are a materials-science literature search expert. Your ONLY job
is DOMAIN-CONDITIONED CONCEPT EXPANSION: given papers from ONE research domain, list the
terms that domain actually uses to describe the mechanism **polymerization / curing
induced volume change → stress / deformation** and its observable outcomes.

## Output schema (STRICT JSON, single object)

{
  "domain": "dental|composites|sla|coatings|optics|packaging",
  "terms": [
    {"term": "...", "semantic_role": "lexical|mechanism|observable",
     "evidence_paper_id": "W..."}
  ]
}

semantic_role definition:
  lexical    — direct synonyms: shrinkage / contraction / volume change
  mechanism  — mechanistic reframings: cure-induced stress / residual strain / internal stress
  observable — measurable outcomes: warpage / cusp deflection / marginal gap / void formation

## Six hard constraints

1. Every term MUST be grounded in the provided papers: it must literally appear in the
   title/abstract of at least one provided paper, and you MUST cite that paper's id.
2. Each term MUST carry an evidence_paper_id (use the [PAPER W...] ids given).
3. Classify every term into exactly one semantic_role (lexical / mechanism / observable).
   Do NOT invent new roles.
4. Do NOT use any term absent from the provided papers. Do NOT invent paper ids.
5. Output ONLY valid JSON — no markdown, no prose, no query strings.
6. Do NOT construct Scopus queries. Term lists only.

Produce 5-15 terms. Prefer terms that are characteristic of THIS domain's literature
(observable outcomes and domain-specific expressions), not generic shrinkage synonyms."""
# ──────────────────────────────────────────────────────────


def build_user_message(domain: str, papers: dict) -> str:
    blob = []
    for pid, t in papers.items():
        title = t.get("title", "")
        ab = (t.get("abstract") or "")[:450]
        blob.append(f"[PAPER {pid}] {title}\n{ab}")
    return (f"## Domain: {domain}\n"
            f"## Core phenomenon\npolymerization / curing induced volume change → "
            f"stress / deformation (photopolymerization & thermosets)\n"
            f"## Papers in this domain (the ONLY evidence source)\n"
            + "\n\n".join(blob)
            + f"\n## Task\nHow does the {domain} literature describe the causes, "
              f"mechanisms, and measurable outcomes of cure-induced volume change / "
              f"stress / deformation? List the terms (5-15), each with semantic_role "
              f"and evidence_paper_id. Output the strict JSON object only.")


def parse_llm_json(response: str) -> dict:
    def _try(s: str) -> dict | None:
        s = s.strip()
        try:
            v = json.loads(s)
            if isinstance(v, dict):
                return v
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v[0]
        except (json.JSONDecodeError, ValueError):
            pass
        return None

    text = response.strip()
    for cand in (text,):
        r = _try(cand)
        if r is not None:
            return r
    if text.startswith("```"):
        lines = text.split("\n")
        body = "\n".join(lines[1:])
        if body.endswith("```"):
            body = body[:-3]
        r = _try(body)
        if r is not None:
            return r
    try:
        start, end = text.index("{"), text.rindex("}") + 1
        r = _try(text[start:end])
        if r is not None:
            return r
    except ValueError:
        pass
    return {}


def extract_terms(obj: dict, batch_domain: str) -> list[dict]:
    out = []
    for e in obj.get("terms", []) or []:
        if isinstance(e, dict) and e.get("term"):
            role = e.get("semantic_role", "observable")
            if role not in ("lexical", "mechanism", "observable"):
                role = "observable"
            out.append({"term": e["term"], "semantic_role": role,
                        "domain": e.get("domain") or batch_domain,
                        "evidence_paper_id": e.get("evidence_paper_id")})
        elif isinstance(e, str):
            out.append({"term": e, "semantic_role": "observable",
                        "domain": batch_domain, "evidence_paper_id": None})
    return out


def validate_terms(terms: list[dict], corpus_texts: dict) -> tuple[list, list, list]:
    """三层验证：
      VERIFIED_EXACT    — evidence paper 的 title/abstract 有原字符串（可进 query）
      SEMANTIC_CANDIDATE— evidence paper 无原字符串（LLM 语义归纳）——保留；全 corpus
                          反查真实证据（corpus_hit 或 none）
      REJECTED          — evidence id 编造（不存在）且全 corpus 无该词
    返回 (verified_exact, semantic_candidates, rejected)。
    """
    verified, candidates, rejected = [], [], []
    for e in terms:
        term = e["term"].strip().strip('"').rstrip("*")
        if len(term) < 3:
            rejected.append({**e, "reason": "term 过短"})
            continue
        pid = e["evidence_paper_id"]
        # 反查全 corpus 的工具函数
        def corpus_hit(t: str) -> tuple[str, str] | None:
            for pp, pt in corpus_texts.items():
                title = pt.get("title", "") or ""
                ab = pt.get("abstract", "") or ""
                for field, text in (("title", title), ("abstract", ab)):
                    if t.lower() in text.lower():
                        i = text.lower().index(t.lower())
                        win = text[max(0, i - 50): i + len(t) + 70].replace("\n", " ")
                        return (pp, win)
            return None

        if pid and pid in corpus_texts:
            t = corpus_texts[pid]
            full = f"{t.get('title', '')} {t.get('abstract', '') or ''}"
            if term.lower() in full.lower():
                i = full.lower().index(term.lower())
                win = full[max(0, i - 50): i + len(term) + 70].replace("\n", " ")
                verified.append({**e, "source_paper": pid, "source_sentence": win,
                                 "verdict": "VERIFIED_EXACT"})
                continue
        # 未在 evidence paper 命中 → SEMANTIC_CANDIDATE（全 corpus 反查）
        hit = corpus_hit(term)
        if hit is not None:
            candidates.append({**e, "source_paper": hit[0], "source_sentence": hit[1],
                               "verdict": "SEMANTIC_CANDIDATE",
                               "note": "evidence paper 无原词，corpus 其他论文命中"})
        elif pid and pid not in corpus_texts:
            rejected.append({**e, "reason": "evidence_paper_id 编造且全 corpus 无该词",
                             "verdict": "REJECTED"})
        else:
            candidates.append({**e, "source_paper": None, "source_sentence": None,
                               "verdict": "SEMANTIC_CANDIDATE",
                               "note": "evidence paper 无原词，corpus 也未命中——"
                                       "保留待更大 TermBank 反查"})
    return verified, candidates, rejected


def dedup(terms: list[dict]) -> list[dict]:
    seen = {}
    for e in terms:
        key = e["term"].strip().lower().strip('"').rstrip("*")
        if key in seen:
            continue
        seen[key] = e
    return list(seen.values())


async def main_async(args):
    from search_engine.llm import create_backend

    # ── corpus（唯一词源；R05 miss 不参与）──
    if args.corpus:
        corpus = json.load(open(args.corpus, encoding="utf-8"))
        if isinstance(corpus, dict):
            corpus = corpus.get("papers", corpus.get("labels", []))
    else:
        from search_engine.completeness.universe_builder import build_agent_seen_pool
        pool = build_agent_seen_pool()
        corpus = _load_oa_with_abstract(pool["found_relevant"])
    corpus_texts = {p.get("paper_id") or p.get("wid"): p for p in corpus}
    print(f"[corpus] KnownRelevantKnowledge: {len(corpus)} papers（唯一词源）")

    # ── domain 分批 ──
    batches = domain_prompts(corpus_texts)
    print(f"[domain] 分批: {[(d, len(p)) for d, p in batches]}")

    api_key = args.api_key or os.environ.get("DEEPSEEK_API_KEY") or \
        os.environ.get("TENCENT_MAAS_API_KEY")
    if args.provider == "deepseek" and not api_key:
        raise SystemExit("✗ DeepSeek 需要 API key：--api-key 或 DEEPSEEK_API_KEY"
                         "（或 --provider tencent / --provider ollama）")
    backend = create_backend(provider=args.provider, api_key=api_key, model=args.model)
    print(f"[llm] provider={args.provider} model={args.model or 'default'} "
          f"| rounds={len(batches)}（每 domain 一批）")

    if args.plan_only:
        print("\n[plan-only] 将按 domain 分批调用 LLM + 三层验证（不写盘）")
        return

    # ── 逐 domain 调用 ──
    raw_terms_all = []
    for i, (domain, papers) in enumerate(batches, 1):
        user_msg = build_user_message(domain, papers)
        response = await backend.chat(system_prompt=SYSTEM_PROMPT,
                                      user_message=user_msg,
                                      temperature=args.temperature, max_tokens=4096)
        raw_path = args.out.replace(".json", f".raw_{domain}.txt")
        with open(raw_path, "w", encoding="utf-8") as f:
            f.write(response)
        obj = parse_llm_json(response)
        if not obj:
            print(f"[WARN] domain={domain} 响应非 JSON（raw 已存 {raw_path}）——跳过该批")
            continue
        batch_terms = extract_terms(obj, domain)
        raw_terms_all.extend(batch_terms)
        print(f"  [{i}/{len(batches)}] {domain}: raw {len(batch_terms)} "
              f"（corpus 命中句示例: {response[:40].strip()[:40]!r}）")

    # ── 守恒日志：raw → dedup → validate ──
    n_raw = len(raw_terms_all)
    deduped = dedup(raw_terms_all)
    n_dedup_dropped = n_raw - len(deduped)
    verified, candidates, rejected = validate_terms(deduped, corpus_texts)
    n_validated = len(deduped)
    print(f"\n[counts] raw_terms={n_raw} dedup_dropped={n_dedup_dropped} "
          f"validated_terms={n_validated}")
    print(f"[counts] VERIFIED_EXACT={len(verified)} "
          f"SEMANTIC_CANDIDATE={len(candidates)} REJECTED={len(rejected)}")
    assert n_validated == len(verified) + len(candidates) + len(rejected), \
        "守恒失败：validated != verified + candidates + rejected"

    from collections import Counter
    role_dist = Counter(e["semantic_role"] for e in verified)
    dom_dist = Counter(e["domain"] for e in verified)

    out = {
        "version": "s6_term_bank_v1",
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "architecture": "V2 domain-conditioned expansion（用户 2026-09-01）：双轴 "
                        "semantic_role × domain；三层验证 VERIFIED_EXACT / "
                        "SEMANTIC_CANDIDATE / REJECTED",
        "disciplines": {
            "r05_miss_papers_as_term_source": False,
            "llm_generates_query": False,
            "query_gate": "正式 S6 query 只允许 VERIFIED_EXACT；SEMANTIC_CANDIDATE "
                          "保留待更大 TermBank 反查",
            "bridge_query_note": "后续 query 允许 (curing OR polymerization OR "
                                 "photocuring) AND (observable terms) AND (context)——"
                                 "不强制 AND shrinkage（防杀 expressivity）",
        },
        "inputs": {"corpus_n": len(corpus), "domains": [d for d, _ in batches],
                   "llm": {"provider": args.provider, "model": args.model or "default"}},
        "counts": {"raw_terms": n_raw, "dedup_dropped": n_dedup_dropped,
                   "validated_terms": n_validated,
                   "VERIFIED_EXACT": len(verified),
                   "SEMANTIC_CANDIDATE": len(candidates),
                   "REJECTED": len(rejected)},
        "semantic_role_distribution": dict(role_dist),
        "domain_distribution": dict(dom_dist),
        "terms_verified_exact": verified,
        "terms_semantic_candidates": candidates,
        "terms_rejected": rejected,
        "next": "TermBank → semantic bridge query composition → novelty/specificity gate"
                " → pilot → quality gate → freeze S6 → fresh R06；RL 在动作空间丰富之后",
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print(f"\n[OK] {args.out}")
    print(f"  VERIFIED_EXACT 分类: {dict(role_dist)} | domain: {dict(dom_dist)}")
    print("\n=== VERIFIED_EXACT terms ===")
    for e in verified[:25]:
        print(f"  [{e['semantic_role']:<10}][{e['domain']}] {e['term']}")
        print(f"      src={e['source_paper']} | {e['source_sentence'][:65]}")
    print(f"\n=== SEMANTIC_CANDIDATE（{len(candidates)}，不进正式 query）===")
    for e in candidates[:15]:
        print(f"  [{e['semantic_role']:<10}][{e['domain']}] {e['term']} "
              f"| {'corpus_hit ' + e['source_paper'] if e.get('source_paper') else 'no_corpus_evidence'}")


def main():
    ap = argparse.ArgumentParser(description="LLM TermFamily 扩展器 V2（domain-conditioned）")
    ap.add_argument("--corpus", default="")
    ap.add_argument("--provider", default="deepseek",
                    choices=["deepseek", "ollama", "tencent"])
    ap.add_argument("--model", default="")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--temperature", type=float, default=0.4)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--plan-only", action="store_true")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
