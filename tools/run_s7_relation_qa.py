#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/run_s7_relation_qa.py — S7 relation candidate QA（v2 二维 schema，2026-09-07）。

v1（KEEP/DROP/UNCERTAIN 单标签）问题：把"科研合理性"与"搜索价值"混成一个指标
→ KEEP=0/DROP=17，而 S6 已证 process→observable 是唯一有效 relation，QA 却全拒。
用户终裁：拆二维，**不 hard DROP**（低价值 relation 是 RL 负样本，须保存）。

rubric（v2）：
  Q1/Q2 → plausibility：A→B 是否科学合理 + 作者是否会用 A 词面表达？
  Q3/Q4 → novelty：相对 S6 已探索空间是否真新？（term-pair 新 ≠ relation 新 ≠ 探索价值；
    注意 S6 覆盖 curing+cusp deflection 不代表 curing+marginal leakage 无价值——novelty
    看 relation cluster 而非精确 term pair）
输出每候选：
  plausibility: VALID | INVALID
  novelty:      HIGH | MEDIUM | LOW
  search_action:（程序派生，非 LLM 自由判）
      RUN  = VALID ∧ novelty ∈ {HIGH, MEDIUM}（pilot 便宜，MEDIUM 也可低成本试）
      SKIP = INVALID ∨ novelty == LOW（保存为负样本/观察，不丢）

blind 纪律：只给 relation + rationale，不给 tier/source/score/family。
产物：s7_relation_qa.json {candidate_id: {plausibility, novelty, search_action, reason, q1..q4}}
"""
import argparse
import asyncio
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T = os.path.join(BASE, "data", "exports", "terminology")
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tools"))
sys.path.insert(0, os.path.join(BASE, "search_engine"))

S6_SUMMARY = """S6 已探索空间（供 novelty 判定，勿重复）：
- A(anchor-free process→observable，已验证有效 43R)：process 锚 × FREE observable
  ——cusp deflection / enamel crack propagation / marginal leakage / internal gap /
  marginal discoloration / void formation / structural deformation / softening effect /
  printability / fabrication error(optics)
- B(shrink-anchor，对照组，已证相关层 ⊂ A)：shrinkage 词族 × 上述 dental observable
- C(mechanism×observable，失败 0R)：stress relaxation×structural deformation、
  modulus development/gel point/curing kinetics×gap/void、AFCT×low shrink stress、
  spatial-temporal control×low shrink stress → "机制词直接 AND observable"多失败
- D(cross-domain，drift)：fabrication error→3DP 无相关；structural deformation→3DP 仅 1R
- shrink 系后果词（low-shrinkage/reduced shrinkage/film shrinkage/low shrinkage stress）
  已作 anchored outcome 在 B/C 使用"""

RUBRIC_SYSTEM = """You are a rigorous literature-discovery reviewer. Judge a proposed SCIENTIFIC
RELATION (concept_A → concept_B) as a SEARCH HYPOTHESIS.

Research frame: polymerization shrinkage / shrinkage stress and observable consequences in
photopolymerization / light-curing systems.

Answer FOUR yes/no questions per relation:
 Q1 plausibility:  Is the A→B relation scientifically real (papers study B as caused by / linked to A)?
 Q2 expression:    Would authors write this relation using A's literal words (not only conceptually)?
 Q3 novelty:       Relative to the KNOWN EXPLORED SPACE below, would a query on this relation reach
                   a community S6 did NOT already sweep?  NOTE: term-pair novelty is NOT enough —
                   S6 covering "curing AND cusp deflection" does NOT mean "curing AND marginal
                   leakage" has no value. Judge at the relation-cluster level (different observable
                   consequences reach different papers even under the same process anchor).
 Q4 discovery:     Is there plausible discovery potential beyond S6's explored relations?

Then output two dimensions (do NOT collapse them into one):
 plausibility: "VALID" (Q1&Q2 true) | "INVALID" (implausible or authors would never write it with A's words)
 novelty:      "HIGH" (Q3&Q4 true: reaches unexplored community)
               | "MEDIUM" (partially new: e.g. new observable consequence, new domain, new mechanism anchor)
               | "LOW" (echo of explored space / cluster duplicate)

search_action is derived programmatically, NOT by you.

KNOWN EXPLORED SPACE (novelty reference):
%s
Output STRICT JSON object only: {"<id>": {"plausibility": "VALID|INVALID",
"novelty": "HIGH|MEDIUM|LOW", "reason": "<one short sentence>",
"q1": true/false, "q2": true/false, "q3": true/false, "q4": true/false}, ...}""" % S6_SUMMARY


def derive_action(p, n):
    """程序派生 search_action（用户 schema）：RUN = VALID ∧ novelty∈{HIGH,MEDIUM}；
    SKIP = INVALID ∨ LOW——LOW/INVALID 不丢，保留为 RL 负样本/观察。"""
    if p == "VALID" and n in ("HIGH", "MEDIUM"):
        return "RUN"
    return "SKIP"


def _parse_obj(text):
    t = (text or "").strip()
    if t.startswith("```"):
        body = "\n".join(t.split("\n")[1:])
        if body.endswith("```"):
            body = body[:-3]
        t = body
    try:
        v = json.loads(t)
        if isinstance(v, dict):
            return v
    except (json.JSONDecodeError, ValueError):
        pass
    try:
        s, e = t.index("{"), t.rindex("}") + 1
        v = json.loads(t[s:e])
        if isinstance(v, dict):
            return v
    except (ValueError, json.JSONDecodeError):
        pass
    return {}


def backend_of(args):
    from search_engine.llm import create_backend
    key = args.api_key or os.environ.get("DEEPSEEK_API_KEY") or ""
    return create_backend(args.provider, api_key=key, model=args.model)


async def run_qa(cands, args, out_path):
    existing = {}
    if os.path.exists(out_path):
        try:
            existing = json.load(open(out_path, encoding="utf-8"))
            existing = existing.get("labels", existing)
            # v1 产物（KEEP/DROP/UNCERTAIN label）无 plausibility → stale，重评
            if existing and not any(isinstance(v, dict) and
                                    "plausibility" in v for v in existing.values()):
                print("[warn] 检测到 v1 schema 旧产物，重置重评")
                existing = {}
        except Exception:
            existing = {}
    todo = [c for c in cands if c["candidate_id"] not in existing]
    print(f"total={len(cands)} 已评={len(cands) - len(todo)} 待评={len(todo)}")
    if not todo:
        return existing
    backend = backend_of(args)
    for i in range(0, len(todo), args.batch):
        chunk = todo[i:i + args.batch]
        user = json.dumps([{"id": c["candidate_id"],
                            "concept_A": c["concept_A"], "concept_B": c["concept_B"],
                            "A_role": c.get("concept_A_source"),
                            "B_role": c.get("concept_B_source"),
                            "relation_type": c["relation_type"],
                            "domain_hint": c.get("domain"),
                            "rationale": c.get("rationale", "")}
                           for c in chunk], ensure_ascii=False)
        user += "\n\nOnly output the JSON object. No explanation, no markdown fences."
        ok = False
        for attempt in range(3):
            try:
                resp = await backend.chat(RUBRIC_SYSTEM, user, temperature=0.0,
                                          max_tokens=4096)
                parsed = _parse_obj(resp)
                got = 0
                for c in chunk:
                    ent = parsed.get(c["candidate_id"])
                    if isinstance(ent, dict) and \
                            ent.get("plausibility") in ("VALID", "INVALID") and \
                            ent.get("novelty") in ("HIGH", "MEDIUM", "LOW"):
                        ent["search_action"] = derive_action(ent["plausibility"],
                                                              ent["novelty"])
                        existing[c["candidate_id"]] = ent
                        got += 1
                ok = True
                if got < len(chunk):
                    miss = [c["candidate_id"] for c in chunk
                            if c["candidate_id"] not in existing]
                    print(f"  batch@{i} 解析 {got}/{len(chunk)} 缺: {miss}")
                break
            except Exception as e:
                print(f"  [WARN] batch@{i} attempt{attempt + 1}: {str(e)[:100]}")
                await asyncio.sleep(3 * (attempt + 1))
        if not ok:
            print(f"  [SKIP] batch@{i}: 3 次失败，留待续跑")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"version": "s7_relation_qa_v2",
                       "schema": "plausibility(VALID/INVALID) × novelty(HIGH/MEDIUM/LOW)"
                                 " → search_action 程序派生；不 hard DROP",
                       "labels": existing}, f, ensure_ascii=False, indent=1)
        await asyncio.sleep(0.2)
    return existing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp",
                    default=os.path.join(T, "s7_relation_candidates.json"))
    ap.add_argument("--out", default=os.path.join(T, "s7_relation_qa.json"))
    ap.add_argument("--batch", type=int, default=5)
    ap.add_argument("--provider", default="deepseek")
    ap.add_argument("--model", default=None)
    ap.add_argument("--api-key", default=None)
    args = ap.parse_args()

    doc = json.load(open(args.inp, encoding="utf-8"))
    cands = doc["candidates"]
    for i, c in enumerate(cands):
        c["candidate_id"] = c.get("candidate_id") or f"RC{i + 1:02d}"
    print(f"candidates: {len(cands)}")
    labels = asyncio.run(run_qa(cands, args, args.out))
    from collections import Counter
    cp = Counter(v["plausibility"] for v in labels.values())
    cn = Counter(v["novelty"] for v in labels.values())
    ca = Counter(v["search_action"] for v in labels.values())
    print(f"\n[OK] plausibility: {dict(cp)}")
    print(f"     novelty:      {dict(cn)}")
    print(f"     search_action:{dict(ca)} | 未评 {len(cands) - len(labels)}")
    print(f"[OK] labels: {args.out}")


if __name__ == "__main__":
    main()
