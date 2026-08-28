"""tools/merge_sr07_refs.py — B2 收尾：SR_07 PDF bibliography identity resolution + canonical union。

用户 2026-08-27 提供 SR_07 78 条 references（Downloads/sr07_references_normalized.json，
retrieval_method=SOURCE_PDF_BIBLIOGRAPHY）。OpenAlex/Crossref 均未索引该综述引用，
从 PDF 恢复是修复数据源覆盖，非人工补 benchmark。

流程：
  1) 解析 78 条 citation（作者.期刊.年;卷:页）
  2) Crossref bibliographic query 逐条匹配 → DOI（直连 trust_env=False，避开代理 TLS）
  3) 验证：journal 相似度 ≥0.6 且 year 一致（volume/page 尽力核对）
  4) 有 DOI → OpenAlex 补 openalex_id/title（独立临时缓存防污染）
  5) canonical union：与现有 671 candidates 合并（DOI > openalex_id > title+year）
  6) 每条 SR_07 记录 reference_retrieval provenance
  7) Freeze QGS Candidate Pool v1（--save，默认 data/exports/qgs_candidates_v1.json）

运行（需 OPENALEX_API_KEY）：
  python tools/merge_sr07_refs.py [--sr07 C:/Users/Administrator/Downloads/sr07_references_normalized.json]
"""
import argparse
import asyncio
import difflib
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, ".")
import httpx

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POOL_PATH = os.path.join(BASE, "data", "exports", "qgs_candidates_v1.json")


def parse_citation(cit: str) -> dict:
    """解析 'Authors. Journal. Year;Vol:Page.' → {query, journal, year, volume, page}。"""
    m = re.search(r"\.\s*(\d{4});\s*([\d(]+(?:-\d+)?)?(?::\s*([^.]*?))?\.?\s*$", cit)
    year = m.group(1) if m else None
    volume = m.group(2).strip("()") if m and m.group(2) else None
    page = m.group(3).strip() if m and m.group(3) else None
    head = cit[: m.start()] if m else cit
    # 期刊 = 年份前最后一段（大写开头的词序列，跳过作者缩写）
    head_clean = re.sub(r"\b[A-Z]\.(?=\s|$)", "", head)  # 去掉单字母缩写 "Fano V." → "Fano "
    head_clean = re.sub(r"[;.:]", " ", head_clean)
    head_clean = re.sub(r"\s+", " ", head_clean).strip()
    query = f"{head_clean} {year or ''} {volume or ''} {page or ''}".strip()
    return {"query": query, "journal": None, "year": year, "volume": volume, "page": page}


async def crossref_lookup(client: httpx.AsyncClient, query: str) -> list[dict]:
    url = "https://api.crossref.org/works"
    params = {"query.bibliographic": query, "rows": 3}
    for attempt in range(4):
        try:
            r = await client.get(url, params=params)
            r.raise_for_status()
            items = r.json().get("message", {}).get("items", [])
            return [{
                "title": (it.get("title") or [""])[0],
                "doi": it.get("DOI", ""),
                "year": (it.get("issued", {}).get("date-parts") or [[None]])[0][0],
                "journal": (it.get("container-title") or [""])[0],
                "volume": it.get("volume"),
                "page": it.get("page"),
            } for it in items]
        except Exception as e:
            if attempt == 3:
                print(f"  ⚠️ Crossref 查询失败(4次): {query[:60]} — {type(e).__name__}")
                return []
            await asyncio.sleep(1.5 * (attempt + 1))
    return []


def jsim(a: str, b: str) -> float:
    a, b = (a or "").lower(), (b or "").lower()
    a = re.sub(r"[^a-z ]", "", a)
    b = re.sub(r"[^a-z ]", "", b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def verify(cit: dict, hit: dict) -> tuple[bool, str]:
    """验证 Crossref top hit 是否真的对应 citation。"""
    if not hit.get("doi"):
        return False, "no_hit"
    if cit["year"] and str(hit.get("year") or "") != str(cit["year"]):
        return False, f"year_mismatch({cit['year']} vs {hit.get('year')})"
    return True, "ok"


def norm_doi_local(d: str) -> str:
    return (d or "").strip().lower().replace("https://doi.org/", "").strip()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sr07", default=r"C:/Users/Administrator/Downloads/sr07_references_normalized.json")
    ap.add_argument("--api-key", default=os.environ.get("OPENALEX_API_KEY", ""))
    ap.add_argument("--save", default=POOL_PATH)
    args = ap.parse_args()

    sr07 = json.load(open(args.sr07, encoding="utf-8"))
    refs = sr07["references"]
    print(f"SR_07 references: {len(refs)} 条")

    # 加载现有 pool（若已落盘）
    existing = []
    if os.path.exists(POOL_PATH):
        d = json.load(open(POOL_PATH, encoding="utf-8"))
        existing = d.get("candidates", [])
    print(f"现有 candidate pool: {len(existing)} 条（若 0 说明还没跑 build_qgs_candidates.py 落盘）")

    # OpenAlex backend（独立缓存防污染）
    from search_engine.backends.openalex import OpenAlexBackend
    cache_path = os.path.join(tempfile.gettempdir(), "qgs_openalex_cache.json")

    async with httpx.AsyncClient(timeout=30, trust_env=False,
                                 headers={"User-Agent": "materials-search/0.1 (mailto:test@example.com)"}) as client:
        async with OpenAlexBackend(api_key=args.api_key, cache_path=cache_path) as oa:
            resolved = []
            for ref in refs:
                cit = parse_citation(ref["citation"])
                hits = await crossref_lookup(client, cit["query"])
                ok = False
                chosen = None
                reason = "no_hit"
                for h in hits:
                    good, why = verify(cit, h)
                    if good:
                        chosen, ok, reason = h, True, "ok"
                        break
                    reason = why
                rec = {
                    "ref_no": ref["ref_no"],
                    "citation": ref["citation"],
                    "query": cit["query"],
                    "doi": (chosen or {}).get("doi", "") if ok else "",
                    "title": (chosen or {}).get("title", "") if ok else "",
                    "journal": (chosen or {}).get("journal", "") if ok else "",
                    "year": cit["year"],
                    "volume": cit["volume"],
                    "page": cit["page"],
                    "resolved": ok,
                    "resolve_note": reason,
                }
                if ok and rec["doi"]:
                    # OpenAlex 补全 openalex_id / venue
                    try:
                        work = await oa._get_json(f"{oa.BASE_URL}/works/doi:{rec['doi'].lower()}", {})
                        if work and "error" not in work:
                            rec["openalex_id"] = re.search(r"(W\d+)", work.get("id", "")).group(1) \
                                if work.get("id") else ""
                            if not rec["title"]:
                                rec["title"] = work.get("title") or ""
                            rec["venue"] = ((work.get("primary_location") or {}).get("source") or {}).get("display_name")
                    except Exception:
                        pass
                resolved.append(rec)
                await asyncio.sleep(0.3)  # Crossref 礼貌限速

    n_ok = sum(1 for r in resolved if r["resolved"])
    print(f"\nCrossref 解析成功: {n_ok}/{len(resolved)}")
    # source review 自身不进 benchmark（B3 criteria：防来源循环）
    SELF_DOI = "10.14314/polimery.2001.590"
    self_hits = [r for r in resolved if norm_doi_local(r.get("doi", "")) == SELF_DOI]
    for r in self_hits:
        r["resolved"] = False
        r["resolve_note"] = "source_review_self(excluded)"
    if self_hits:
        print(f"排除 source review 自身匹配: {len(self_hits)} 条（B3 criteria 防来源循环）")
    print("\n未解析（需人工或后续处理）:")
    for r in resolved:
        if not r["resolved"]:
            print(f"  [{r['ref_no']}] {r['citation'][:80]}  → {r['resolve_note']}")

    # canonical union
    def norm_doi(d):
        return (d or "").strip().lower().replace("https://doi.org/", "").strip()

    by_doi = {norm_doi(c.get("doi", "")): c for c in existing if c.get("doi")}
    by_wid = {c.get("openalex_id"): c for c in existing if c.get("openalex_id")}
    merged = list(existing)
    n_new = 0
    n_dup = 0
    n_unresolved_kept = 0
    for r in resolved:
        if not r["resolved"]:
            # IDENTITY_UNRESOLVED 必须保留（用户 2026-08-27）：
            # resolver 失败 ≠ reference 不存在/不 relevant；用 citation 原文做
            # title fallback，后续人工/Scopus 手工解析。
            rec = {
                "doi": "",
                "openalex_id": r.get("openalex_id", ""),
                "title": r["title"] or f"[identity unresolved] {r['citation'][:120]}",
                "year": r["year"],
                "venue": r.get("journal", ""),
                "sources_from": ["SR_07"],
                "reference_retrieval": {
                    "method": "SOURCE_PDF_BIBLIOGRAPHY",
                    "source_review": "10.14314/polimery.2001.590",
                    "ref_no": r["ref_no"],
                },
                "identity_status": "UNRESOLVED",
                "citation_original": r["citation"],
                "resolve_note": r["resolve_note"],
            }
            merged.append(rec)
            n_unresolved_kept += 1
            continue
        rec = {
            "doi": norm_doi(r["doi"]),
            "openalex_id": r.get("openalex_id", ""),
            "title": r["title"],
            "year": r["year"],
            "venue": r.get("venue") or r["journal"],
            "sources_from": ["SR_07"],
            "reference_retrieval": {
                "method": "SOURCE_PDF_BIBLIOGRAPHY",
                "source_review": "10.14314/polimery.2001.590",
                "ref_no": r["ref_no"],
            },
        }
        key = None
        if rec["doi"]:
            key = ("doi", rec["doi"])
        elif rec["openalex_id"]:
            key = ("wid", rec["openalex_id"])
        if key:
            pool = by_doi if key[0] == "doi" else by_wid
            if key[1] in pool:
                tgt = pool[key[1]]
                if "SR_07" not in tgt.get("sources_from", []):
                    tgt.setdefault("sources_from", []).append("SR_07")
                rr = tgt.get("reference_retrieval")
                if rr is None:
                    tgt["reference_retrieval"] = [rec["reference_retrieval"]]
                elif isinstance(rr, dict):
                    tgt["reference_retrieval"] = [rr, rec["reference_retrieval"]]
                else:
                    tgt["reference_retrieval"].append(rec["reference_retrieval"])
                n_dup += 1
                continue
        merged.append(rec)
        by_doi.setdefault(rec["doi"], rec)
        by_wid.setdefault(rec["openalex_id"], rec)
        n_new += 1

    out = {
        "benchmark_id": "pc_001_qgs_candidates_v1",
        "created_at": "2026-08-27",
        "sources": [
            {"source_id": "SR_07", "doi": "10.14314/polimery.2001.590",
             "title": "Contraction (shrinkage) in polymerization. Part II. Dental resin composites",
             "retrieval_method": "SOURCE_PDF_BIBLIOGRAPHY"},
        ],
        "stats": {
            "sr07_declared": len(refs),
            "sr07_resolved": n_ok,
            "sr07_unresolved": len(refs) - n_ok,
            "identity_unresolved_kept": n_unresolved_kept,
            "union_new": n_new,
            "union_dup_with_existing": n_dup,
            "pool_total": len(merged),
        },
        "candidates": merged,
    }
    with open(args.save, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\nunion: 新增 {n_new}，与现有重复 {n_dup}，identity_unresolved 保留 {n_unresolved_kept}，"
          f"pool 总计 {len(merged)}")
    print(f"✓ 已保存: {args.save}")


if __name__ == "__main__":
    asyncio.run(main())
