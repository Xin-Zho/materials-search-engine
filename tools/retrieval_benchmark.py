"""Verifier retrieval regression benchmark：验证搜索必须召回已知 ground-truth 论文。

用户定（2026-08-26）：bulk-fill + shrinkage stress 的直接文献非常容易找到——
如果 verification search 连 2-3 篇已知论文都召不回，说明 retrieval layer 有召回问题
（比继续看 LLM prompt 有价值）。

Ground truth（bulk-fill composite × polymerization shrinkage/stress，用户提供）：
  1. "Polymerization Shrinkage Stress Kinetics and Related Properties of Bulk-fill
     Resin Composites"（Operative Dentistry 39(4), 2014）
  2. "Shrinkage stress and elastic modulus assessment of bulk-fill composites"
     (PubMed 30624465)
  3. "Polymerization shrinkage, modulus, and shrinkage stress related to
     tooth-restoration interfacial debonding in bulk-fill composites" (PubMed 25676178)
  4. "Polymerization Shrinkage and Depth of Cure of Bulk-Fill Resin Composites and
     Highly Filled Flowable Resin"（Operative Dentistry 40(2), 2015）

判定：search_relevance（候选词族 × target 词族短语 AND）的 top-N 召回中，
标题/摘要命中任一 ground-truth 关键词组（bulk-fill/shrinkage/stress/modulus）的论文数
≥ 2 → PASS；否则 FAIL（retrieval layer 修）。

用法（需网络）:
    python tools/retrieval_benchmark.py [--top 30]
"""

import argparse
import asyncio
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

GROUND_TRUTH = {
    "polymerization shrinkage stress kinetics": "Operative Dentistry 39(4) 2014",
    "shrinkage stress and elastic modulus assessment": "PubMed 30624465",
    "interfacial debonding in bulk-fill": "PubMed 25676178",
    "polymerization shrinkage and depth of cure": "Operative Dentistry 40(2) 2015",
}

CANDIDATE_FAMILY = [
    "bulk-fill composite", "bulk fill composite", "bulk-fill resin composite",
    "bulk fill resin-based composite", "bulk-fill composite resin",
]
TARGET_FAMILY = [
    "polymerization shrinkage", "polymerization shrinkage stress",
    "shrinkage stress", "photopolymerization stress",
]


def _hits(papers: list[dict], top_n: int) -> dict[str, int]:
    """每条 ground truth 的召回情况（标题命中关键词组）。"""
    result = {k: 0 for k in GROUND_TRUTH}
    for p in papers[:top_n]:
        t = ((p.get("title", "") or "") + " " + (p.get("abstract", "") or "")).lower()
        for key in GROUND_TRUTH:
            if key in t:
                result[key] += 1
    return result


async def main():
    ap = argparse.ArgumentParser(description="Verifier retrieval regression benchmark（bulk-fill）")
    ap.add_argument("--top", type=int, default=30, help="检查的 top-N 召回（默认 30）")
    args = ap.parse_args()

    from search_engine.backends import OpenAlexBackend
    queries = [f'"{f}" AND "{t}"' for f in CANDIDATE_FAMILY[:3] for t in TARGET_FAMILY]
    seen, papers = set(), []
    async with OpenAlexBackend(mailto=None) as search:
        for q in queries:
            try:
                results = await search.search_relevance(q, limit=15)
            except Exception as e:
                print(f"query {q!r} ERROR: {e}")
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
    print("=" * 70)
    print(f"Retrieval benchmark（bulk-fill candidate，去重召回 {len(papers)} 篇，检查 top-{args.top}）")
    print("=" * 70)
    hits = _hits(papers, args.top)
    n_hit = sum(1 for v in hits.values() if v > 0)
    for key, src in GROUND_TRUTH.items():
        print(f"  [{hits[key]}] {key[:55]}  ({src})")
    print(f"\nground-truth 召回: {n_hit}/{len(GROUND_TRUTH)}")
    print("→ " + ("PASS（retrieval layer 有效）" if n_hit >= 2
                  else "FAIL（retrieval layer 召回不足，先修 backend 再验证）"))
    print("\ntop-10 召回标题:")
    for p in papers[:10]:
        print(f"  {str(p['title'])[:80]}")


if __name__ == "__main__":
    asyncio.run(main())
