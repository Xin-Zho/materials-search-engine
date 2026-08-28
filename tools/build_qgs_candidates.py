"""tools/build_qgs_candidates.py — B2 拉取 7 篇 source reviews 的 references，构造 candidate QGS pool。

用户 2026-08-27 冻结 source reviews：
  SR_01 = #4  10.1039/d3py00261f   Photopolymerization shrinkage (2023)
  SR_02 = #7  10.1021/acspolymersau.5c00007   Thiol-epoxy photoclick (2025)
  SR_03 = #10 10.1007/s10853-026-12891-w     Photopolymers for AM (2026)
  SR_04 = #15 10.3390/polym15112524          Cationic photoinitiating (2023)
  SR_05 = #17 10.3390/molecules27196283      Holographic photopolymer (2022)
  SR_06 = #19 10.3390/polym14194182          Flowable dental composites (2022)
  SR_07 = #25 10.14314/polimery.2001.590     Contraction shrinkage Part II (2001)

流程（协议 B2）：
  每篇 → referenced_works → 批量取回引文元数据 → DOI > openalex_id > title+year
  去重 → candidate QGS pool（**不做 relevance 判断**，留给独立 reviewer）。

⚠️ 防污染：使用独立临时缓存（不读项目主 openalex_cache.json——那是 Agent 历史
产物）。relevance 判断由领域专家做，本脚本纯工程。

运行：
  python tools/build_qgs_candidates.py --api-key KEY
  （--save 默认输出到临时目录，之后人工移入 data/exports/）
"""
import argparse
import asyncio
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, ".")
from search_engine.backends.openalex import OpenAlexBackend

SOURCES = [
    ("SR_01", "10.1039/d3py00261f", "Photopolymerization shrinkage: strategies for reduction, measurement methods"),
    ("SR_02", "10.1021/acspolymersau.5c00007", "Photopolymerization Using Thiol-Epoxy Click Reaction"),
    ("SR_03", "10.1007/s10853-026-12891-w", "Review: photopolymers for additive manufacturing"),
    ("SR_04", "10.3390/polym15112524", "Recent Advances and Challenges in Long Wavelength Sensitive Cationic Photoinitiating Systems"),
    ("SR_05", "10.3390/molecules27196283", "Phenanthraquinone-Doped Polymethyl Methacrylate Photopolymer for Holographic Recording"),
    ("SR_06", "10.3390/polym14194182", "Overviews on the Progress of Flowable Dental Polymeric Composites"),
    ("SR_07", "10.14314/polimery.2001.590", "Contraction (shrinkage) in polymerization. Part II. Dental resin composites"),
]


def norm_doi(doi: str) -> str:
    return (doi or "").strip().lower().replace("https://doi.org/", "").replace("http://doi.org/", "").strip()


def norm_wid(pid: str) -> str:
    m = re.search(r"(W\d+)", pid or "")
    return m.group(1) if m else (pid or "").strip()


async def fetch_source_refs(backend, doi: str) -> tuple[list[dict], dict]:
    """拉一篇综述的全部 references（引用论文元数据）。"""
    work = await backend._get_json(f"{backend.BASE_URL}/works/doi:{norm_doi(doi)}", {})
    if not work or "error" in work:
        return [], {"error": work.get("error", "empty")}
    refs = work.get("referenced_works", [])
    ref_wids = [norm_wid(r) for r in refs if norm_wid(r)]
    papers = []
    for i in range(0, len(ref_wids), 50):
        chunk = ref_wids[i:i + 50]
        url = f"{backend.BASE_URL}/works"
        params = {"filter": "openalex_id:" + "|".join(chunk), "per-page": min(len(chunk), 200)}
        data = await backend._get_json(url, params)
        for w in data.get("results", []):
            papers.append({
                "openalex_id": norm_wid(w.get("id", "")),
                "doi": norm_doi(w.get("doi", "")),
                "title": w.get("title") or "",
                "year": w.get("publication_year"),
                "venue": ((w.get("primary_location") or {}).get("source") or {}).get("display_name"),
            })
    return papers, {"referenced_count": len(refs), "retrieved": len(papers)}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api-key", default=os.environ.get("OPENALEX_API_KEY", ""),
                    help="OpenAlex API key（必需；无 key 仅 1000 credits/天）")
    ap.add_argument("--save", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "exports", "qgs_candidates_v1.json"))
    args = ap.parse_args()
    if not args.api_key:
        print("⚠️ 需要 --api-key 或 OPENALEX_API_KEY")
        return

    # 独立临时缓存（防污染：不读项目主缓存）
    cache_path = os.path.join(tempfile.gettempdir(), "qgs_openalex_cache.json")
    async with OpenAlexBackend(api_key=args.api_key, cache_path=cache_path) as oa:
        by_source: dict[str, list[dict]] = {}
        meta = {}
        for sid, doi, _title in SOURCES:
            print(f"拉取 {sid} ({doi}) ...")
            papers, m = await fetch_source_refs(oa, doi)
            by_source[sid] = papers
            meta[sid] = m
            print(f"  → referenced={m.get('referenced_count')} retrieved={m.get('retrieved')}")

        # 去重：DOI > openalex_id > title+year
        by_doi: dict[str, dict] = {}
        by_wid: dict[str, dict] = {}
        by_title_year: dict[tuple, dict] = {}
        order: list[dict] = []
        for sid, papers in by_source.items():
            for p in papers:
                rec = dict(p)
                rec["sources_from"] = [sid]
                key = None
                if rec["doi"]:
                    key = ("doi", rec["doi"])
                    pool = by_doi
                elif rec["openalex_id"]:
                    key = ("wid", rec["openalex_id"])
                    pool = by_wid
                else:
                    key = ("ty", (rec["title"] or "").strip().lower(), rec["year"])
                    pool = by_title_year
                if key in pool:
                    pool[key]["sources_from"].append(sid)
                else:
                    pool[key] = rec
                    order.append(rec)

        # 汇总
        candidates = [{"doi": r["doi"], "openalex_id": r["openalex_id"], "title": r["title"],
                       "year": r["year"], "venue": r["venue"], "sources_from": r["sources_from"]}
                      for r in order]
        out = {
            "benchmark_id": "pc_001_qgs_candidates_v1",
            "created_at": "2026-08-27",
            "sources": [{"source_id": s, "doi": d, "title": t} for s, d, t in SOURCES],
            "stats": {
                "raw_references": sum(len(v) for v in by_source.values()),
                "unique_after_dedup": len(candidates),
                "per_source": {s: {"referenced": m.get("referenced_count"),
                                   "retrieved": m.get("retrieved")} for s, m in meta.items()},
                "sources_per_paper": {str(n): sum(1 for c in candidates if len(c["sources_from"]) == n)
                                      for n in (1, 2, 3, 4)},
            },
            "candidates": candidates,
        }
        with open(args.save, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        cs = oa.credit_summary()
        print()
        print(f"raw references      = {out['stats']['raw_references']}")
        print(f"unique after dedup  = {out['stats']['unique_after_dedup']}")
        print(f"credits used        = {cs['total']} (singleton={cs['singleton']}, list={cs['list']})")
        print(f"✓ 已保存: {args.save}")


if __name__ == "__main__":
    asyncio.run(main())
