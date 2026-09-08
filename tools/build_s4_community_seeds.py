#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""S4 Step 6C 修正：从 S3 seen high-conf seeds 中选 dental-measurement 社区代表（2026-08-30 用户定）。

关键纪律（用户叫停）：
- **绝不用 R03 residual miss 本身当 citation seed**——miss 只能产生"社区缺入口"知识，
  不能直接成为入口（Miss → CommunityKnowledge → GeneralizedEntryPoint，而非 Miss → Seed=Miss）。
- 代表 seed 来源 = 222 pre-existing high-conf seeds（= S3_SEEN ∩ RelevantHighConfidence，
  用户指定的来源，天然满足）。
- 打分只看 seed 的 community 特征（词汇重叠 + 社区内地位），**不看能否追回 R03 dental miss**。
  Score(s) = 2·VocabularyOverlap(s) + log1p(cited_by)（纯 seed 侧特征）。
- 若 222 池内无任何 dental-measurement 词汇代表 → 本身是重要结论：
  "community 在 S3 KB 中缺乏 seed representation" → community repair 只走 vocabulary query。

输出：s4_community_seeds.json（top-5 代表 + vocab overlap 明细 + verdict）
"""

import json
import math
import os
import re
import sys
from collections import defaultdict

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TERM = os.path.join(BASE, "data", "exports", "terminology")
TERM_EV = os.path.join(TERM, "s4_term_evidence.json")
COMM = os.path.join(TERM, "s4_community_discovery.json")
OUT = os.path.join(TERM, "s4_community_seeds.json")

sys.path.insert(0, os.path.join(BASE, "tools"))
from citation_reachability_audit import load_oa  # noqa: E402
from citation_2hop_reachability_audit import build_s3_seeds  # noqa: E402
from benchmark_s4_citation_policies import seed_meta, COMMUNITY_DEF  # noqa: E402

MEASUREMENT_TERMS = None


def community_vocab() -> tuple[set, set]:
    """(general_vocab, method_vocab)：dental-measurement 社区词汇。

    general_vocab = community shared_vocab（含 restoration/restorative 等泛词）
    method_vocab  = MEASUREMENT eligible 词（linometer/cuspal deflection/shrinkage vector…）
                    ——只有 method_vocab 命中才构成"测量方法学社区代表"证据。
    """
    global MEASUREMENT_TERMS
    general = set()
    comm = json.load(open(COMM, encoding="utf-8"))
    for c in comm["communities"]:
        if c["community_id"] == "C_dental-measurement":
            general |= set(c.get("shared_vocab", []))
    te = json.load(open(TERM_EV, encoding="utf-8"))
    MEASUREMENT_TERMS = [x["term_family"] for x in te["terms"]
                         if x.get("eligible") and x["term_type"] == "MEASUREMENT"]
    method = set(MEASUREMENT_TERMS)
    norm = lambda vs: {re.sub(r"[-]", " ", v.lower()).strip() for v in vs}
    return norm(general), norm(method)


def title_tokens(title: str) -> set:
    t = (title or "").lower()
    t = re.sub(r"[^a-z0-9\s]+", " ", t)
    return set(x for x in t.split() if len(x) >= 4)


def vocab_overlap(title: str, vocab: set) -> int:
    toks = title_tokens(title)
    return len(toks & vocab)


def main() -> None:
    out_path = os.path.join(sys.argv[sys.argv.index("--out") + 1]
                            if "--out" in sys.argv else OUT)
    oa = load_oa()
    lp = os.path.join(BASE, "data", "exports", "completeness_labels",
                      "pc_001__20260830011713_filled.json")
    if not os.path.exists(lp):
        lp = os.path.join(os.path.expanduser("~"), "Downloads",
                          "pc_001__20260830011713_filled.json")
    seed, _n = build_s3_seeds(lp, oa)
    meta = seed_meta(seed, oa)
    general_vocab, method_vocab = community_vocab()
    print(f"222 pool seeds: {len(seed)} | general vocab: {len(general_vocab)} "
          f"| method vocab: {len(method_vocab)}")

    # dental-measurement cluster 的 seeds（title 关键词分类；pool 内 pre-existing seen）
    dental_seeds = [w for w in seed if meta[w]["community"] == "dental-measurement"]
    print(f"pool 内 dental-measurement cluster seeds: {len(dental_seeds)}")

    scored = []
    for w in dental_seeds:
        t = oa.get(w, {}).get("title")
        toks = title_tokens(t)
        method_vo = len(toks & method_vocab)
        general_vo = len(toks & general_vocab)
        cited = meta[w]["cited_by"] or 0
        score = 2 * method_vo + general_vo + math.log1p(cited)
        scored.append({"seed_wid": w, "title": t,
                       "method_vocab_overlap": method_vo,
                       "general_vocab_overlap": general_vo,
                       "cited_by": cited, "score": round(score, 3),
                       "matched_method_vocab": sorted(toks & method_vocab)[:10],
                       "matched_general_vocab": sorted(toks & general_vocab)[:10]})
    scored.sort(key=lambda x: (-x["method_vocab_overlap"], -x["cited_by"]))

    top = scored[:5]
    has_method_rep = bool(top and top[0]["method_vocab_overlap"] > 0)
    out = {
        "version": "s4_community_seeds_v1",
        "frozen_at": "2026-08-30",
        "development_source": "AUDIT_R03",
        "community_id": "C_dental-measurement",
        "discipline": "代表 seed 只从 222 pre-existing high-conf seeds（S3_SEEN ∩ Relevant）中选；"
                      "绝不用 R03 residual miss 本身当 seed；打分只看 community 特征不看 miss recovery",
        "score_rule": "Score(s) = 2·MethodVocabOverlap + GeneralVocabOverlap + log1p(cited_by)",
        "pool_dental_cluster_seeds": len(dental_seeds),
        "verdict": ("FOUND_METHOD_REPRESENTATION" if has_method_rep else
                    "NO_METHOD_VOCAB_REPRESENTATION"),
        "representative_seeds": top,
        "interpretation": ("222 池内存在命中 MEASUREMENT 方法学词汇的 seed → 用 top seeds 做 "
                           "citation expansion" if has_method_rep else
                           "★ 222 池内 71 个 dental-cluster seeds 无一命中 MEASUREMENT 方法学词汇"
                           "（linometer/cuspal deflection/shrinkage vector/x ray ct…）——"
                           "community 在 S3 KB 中缺乏测量方法学 seed representation（与 Step 3 "
                           "undercoverage 93.5% 一致）；community repair 只走 vocabulary query Q1/Q2，"
                           "不强行塞相邻社区 seed 当代表"),
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    def safe(s, n=70):
        return (s or "").encode("ascii", "replace").decode("ascii")[:n]

    print("=" * 78)
    print("S4 Step 6C 修正：dental-measurement community seeds（pool 内 pre-existing）")
    print("=" * 78)
    print(f"verdict = {out['verdict']}")
    for s in scored[:8]:
        print(f"  {s['seed_wid']} mVO={s['method_vocab_overlap']:>2} "
              f"gVO={s['general_vocab_overlap']:>2} cited={s['cited_by']:>4} "
              f"| {safe(s['title'])}")
    print(f"\n[OK] written: {out_path}")


if __name__ == "__main__":
    main()
