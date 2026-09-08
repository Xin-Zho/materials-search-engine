"""S8 抽取试抽（3 篇代表，评估 2.0-edges 离线抽取质量与成本）。

选文策略：不同社区代表
  1. EX-04 dim-accuracy@3dp（S7 最大 R 社区 73 篇）
  2. S6 family A（dental shrinkage，S6 最大 R 族 49 篇）
  3. EX-05 warpage 或 EX-03 delamination（S7 中量 R）

用法：需 DEEPSEEK_API_KEY
    python tools/run_s8_extraction_probe.py
"""
import asyncio
import json
import os
import sys
import time

from search_engine.llm import DeepSeekBackend
from search_engine.models import Paper
from search_engine.knowledge_extractor import KnowledgeExtractor

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

CATALOG = "data/exports/terminology/s8_finalkb_catalog.json"


def pick_probe(catalog):
    """选 3 篇代表（EX-04 / S6-A / EX-05 或 EX-03）。"""
    rs = [p for p in catalog["papers"]
          if p["label"] == "RELEVANT" and p["abstract"]]
    ex04 = [p for p in rs if "EX-04" in (p["evidence"].get("ex_sources") or [])]
    s6a = [p for p in rs if p["source"] == "S6"
           and "A" in (p["evidence"].get("families") or [])]
    ex05 = [p for p in rs if "EX-05" in (p["evidence"].get("ex_sources") or [])]
    picks = []
    for pool, tag in ((ex04, "EX-04 dim-accuracy@3dp"),
                      (s6a, "S6-A dental shrinkage"),
                      (ex05, "EX-05 warpage@3dp")):
        if pool:
            picks.append((tag, pool[0]))
        elif rs:
            picks.append((tag, rs[len(picks) % len(rs)]))
    return picks[:3]


async def main():
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        print("未设 DEEPSEEK_API_KEY", file=sys.stderr)
        return 1
    catalog = json.load(open(CATALOG, encoding="utf-8"))
    picks = pick_probe(catalog)

    llm = DeepSeekBackend(api_key=key)
    ex = KnowledgeExtractor(llm, extractor_version="2.0-edges")

    for tag, p in picks:
        paper = Paper(paper_id=p["key"], title=p["title"],
                      year=None, abstract=p["abstract"], doi=p["doi"])
        t0 = time.time()
        rec = await ex.extract(paper)
        dt = time.time() - t0
        print(f"\n{'='*70}\n[{tag}] {p['key']}  ({dt:.0f}s)")
        print(f"  {p['title'][:100]}")
        if rec is None:
            print("  ✗ 抽取失败")
            continue
        print(f"  problem      : {rec.problem[:90]}")
        print(f"  routes       : {rec.strategy_routes[:5]}")
        print(f"  mechanisms   : {[m.canonical or m.mechanism for m in rec.physical_mechanisms][:6]}")
        print(f"  materials    : {rec.materials[:5]}")
        print(f"  edges        : {len(rec.route_mechanism_edges)}")
        for e in rec.route_mechanism_edges[:5]:
            print(f"     {e.raw_route or '(unbound)'} → {e.raw_mechanism or e.canonical_mechanism}"
                  f"  [{e.relation_type} conf={e.confidence:.2f}]")
            print(f"        evid: {(e.evidence or '')[:100]}")
        print(f"  hypotheses   : {len(rec.search_hypotheses)}")
        print(f"  extraction_status: {rec.extraction_status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
