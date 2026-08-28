"""tools/adjudicate_unresolved.py — UNRESOLVED 111 篇 adjudication 辅助（用户定 2026-08-27）。

B4 完成后还有 111 篇 UNRESOLVED（92 INSUFFICIENT_METADATA / 17 FULLTEXT_REQUIRED /
2 IDENTITY_UNCERTAIN）。它们不能静默排除出 B_total——先补信息再让 Reviewer 第二轮
判断，最终 B_total = RELEVANT + 新增 relevant。

做法：
  1) 读已完成 screening 的 manifest（默认 Downloads 的 completed 版）
  2) 过滤 decision=UNRESOLVED
  3) 补 abstract：OpenAlex 批量（独立缓存防污染）→ Crossref 兜底（DOI）
  4) 输出 adjudication 清单（含新 abstract + 空 decision/reason），Reviewer 填回

用法：
  python tools/adjudicate_unresolved.py --api-key KEY
      [--manifest C:/Users/Administrator/Downloads/qgs_screening_manifest_completed.json]
      [--save data/exports/qgs_unresolved_adjudication.json]
"""
import argparse
import asyncio
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, ".")
import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_MANIFEST = r"C:/Users/Administrator/Downloads/qgs_screening_manifest_completed.json"
DEFAULT_SAVE = os.path.join(BASE, "data", "exports", "qgs_unresolved_adjudication.json")


def norm_wid(pid: str) -> str:
    m = re.search(r"(W\d+)", pid or "")
    return m.group(1) if m else ""


def norm_doi(d: str) -> str:
    return (d or "").strip().lower().replace("https://doi.org/", "").replace("http://doi.org/", "").strip()


async def fetch_abstracts(entries: list[dict], api_key: str) -> dict:
    """OpenAlex 批量补 abstract（独立临时缓存）。"""
    from search_engine.backends.openalex import OpenAlexBackend
    cache_path = os.path.join(tempfile.gettempdir(), "qgs_openalex_cache.json")
    wid_to_i = {}
    for i, e in enumerate(entries):
        wid = norm_wid(e.get("openalex_id", ""))
        if wid:
            wid_to_i.setdefault(wid, i)
    abstracts = {}
    async with OpenAlexBackend(api_key=api_key, cache_path=cache_path) as oa:
        wids = list(wid_to_i)
        for i in range(0, len(wids), 50):
            chunk = wids[i:i + 50]
            data = await oa._get_json(
                f"{oa.BASE_URL}/works",
                {"filter": "openalex_id:" + "|".join(chunk), "per-page": min(len(chunk), 200)})
            for w in data.get("results", []):
                ab = w.get("abstract_inverted_index")
                if ab:
                    pos = []
                    for word, idxs in ab.items():
                        for ix in idxs:
                            pos.append((ix, word))
                    pos.sort()
                    abstracts[wid_to_i[norm_wid(w.get("id", ""))]] = " ".join(x for _, x in pos)
        print(f"OpenAlex 补 abstract: {len(abstracts)}/{len(entries)}")
    return abstracts


async def crossref_abstracts(entries: list[dict], no_ab: set[int]) -> dict:
    """Crossref 兜底补 abstract（对 OpenAlex 没有的）。"""
    out = {}
    async with httpx.AsyncClient(timeout=30, trust_env=False,
                                 headers={"User-Agent": "materials-search/0.1 (mailto:test@example.com)"}) as client:
        for i in sorted(no_ab):
            e = entries[i]
            doi = norm_doi(e.get("doi", ""))
            if not doi:
                continue
            try:
                r = await client.get(f"https://api.crossref.org/works/{doi}")
                r.raise_for_status()
                msg = r.json().get("message", {})
                ab = (msg.get("abstract") or "").strip()
                # 去 HTML 标签
                ab = re.sub(r"<[^>]+>", " ", ab)
                ab = re.sub(r"\s+", " ", ab).strip()
                if ab:
                    out[i] = ab[:1500]
            except Exception:
                pass
            await asyncio.sleep(0.2)
    print(f"Crossref 兜底补 abstract: {len(out)}")
    return out


async def s2_abstracts(entries: list[dict], no_ab: set[int]) -> dict:
    """Semantic Scholar 兜底补 abstract（免费 API，dental 老论文覆盖比 OpenAlex 好）。

    无 key 限速约 1 req/s，111 篇顺序查 ≈ 2-3 分钟；429 退避重试。
    """
    out = {}
    async with httpx.AsyncClient(timeout=30, trust_env=False,
                                 headers={"User-Agent": "materials-search/0.1"}) as client:
        for i in sorted(no_ab):
            e = entries[i]
            doi = norm_doi(e.get("doi", ""))
            if not doi:
                continue
            for attempt in range(3):
                try:
                    r = await client.get(
                        f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}",
                        params={"fields": "abstract,title"})
                    if r.status_code == 429:
                        await asyncio.sleep(5 * (attempt + 1))
                        continue
                    if r.status_code == 404:
                        break
                    r.raise_for_status()
                    ab = (r.json().get("abstract") or "").strip()
                    if ab:
                        out[i] = ab[:1500]
                    break
                except Exception:
                    await asyncio.sleep(2 * (attempt + 1))
            await asyncio.sleep(1.1)  # 无 key 限速
    print(f"Semantic Scholar 兜底补 abstract: {len(out)}")
    return out


async def pubmed_abstracts(entries: list[dict], no_ab: set[int]) -> dict:
    """PubMed eutils 兜底补 abstract（dental 老论文 PubMed 覆盖好，免费无 key）。

    DOI → esearch → PMID → efetch(abstract)。3 req/s 限速，429 退避。
    """
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    out = {}
    async with httpx.AsyncClient(timeout=30, trust_env=False,
                                 headers={"User-Agent": "materials-search/0.1"}) as client:
        for i in sorted(no_ab):
            e = entries[i]
            doi = norm_doi(e.get("doi", ""))
            if not doi:
                continue
            try:
                r = await client.get(f"{base}/esearch.fcgi",
                                     params={"db": "pubmed", "term": f"{doi}[doi]",
                                             "retmode": "json"})
                if r.status_code == 429:
                    await asyncio.sleep(4)
                    continue
                r.raise_for_status()
                ids = (r.json().get("esearchresult", {}).get("idlist") or [])
                if ids:
                    r2 = await client.get(f"{base}/efetch.fcgi",
                                          params={"db": "pubmed", "id": ids[0],
                                                  "rettype": "abstract", "retmode": "text"})
                    r2.raise_for_status()
                    txt = r2.text.strip()
                    # 去掉开头标题行，只留摘要体
                    lines = txt.splitlines()
                    body = "\n".join(l for l in lines if l.strip())
                    if body:
                        out[i] = body[:1500]
            except Exception:
                pass
            await asyncio.sleep(0.4)
    print(f"PubMed 兜底补 abstract: {len(out)}")
    return out


def run_interactive(manifest_path: str, save_path: str):
    """逐篇人工判定：显示 title/year/venue/abstract/prev_reason，[r]/[i]/[u] +
    reason code，每判立即保存，随时退出（--continue-from 续跑）。

    决策权完全在 human reviewer（用户定：边界案例最终标签记 human
    adjudication，程序不做自动批量判断）。
    """
    import sys as _sys
    adjud = json.load(open(manifest_path, encoding="utf-8"))
    papers = adjud["papers"]
    codes = adjud.get("reason_codes", {})
    n = len(papers)
    i = 0
    while i < n:
        p = papers[i]
        if p.get("decision"):
            i += 1
            continue
        decided = sum(1 for x in papers if x.get("decision"))
        print("\n" + "=" * 74)
        print(f"[{i + 1} / {n}]  已决策 {decided} 篇  (human adjudication)")
        print("=" * 74)
        print(f"Title:   {p['title']}")
        print(f"Year:    {p.get('year')}   Venue: {p.get('venue')}")
        print(f"DOI:     {p.get('doi') or '-'}")
        if p.get("prev_reason"):
            print(f"首轮标签: {p.get('prev_reason')}")
        if p.get("abstract"):
            ab = p["abstract"]
            print(f"Abstract: {ab[:550]}{'...' if len(ab) > 550 else ''}")
        else:
            print("Abstract: （无——需全文）")
        print()
        key = input("  [r] RELEVANT  [i] IRRELEVANT  [u] 仍需全文  [q] 保存退出  [b] 回退: ").strip().lower()
        if key == "q":
            print("已保存退出")
            break
        if key == "b":
            i = max(0, i - 2)
            continue
        decision = {"r": "RELEVANT", "i": "IRRELEVANT", "u": "UNRESOLVED"}.get(key)
        if not decision:
            print("  无效输入")
            continue
        opts = codes.get(decision, [])
        print(f"  reason code ({decision}):")
        for j, c in enumerate(opts, 1):
            print(f"    {j}. {c}")
        rk = input("  选编号（回车=跳过）: ").strip()
        p["decision"] = decision
        p["reason_code"] = opts[int(rk) - 1] if rk.isdigit() and 1 <= int(rk) <= len(opts) else ""
        note = input("  note（可选，回车跳过）: ").strip()
        p["note"] = note
        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(adjud, f, ensure_ascii=False, indent=1)
        i += 1
    print(f"✓ 已保存进度（{sum(1 for x in papers if x.get('decision'))}/{n}）: {save_path}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=DEFAULT_MANIFEST)
    ap.add_argument("--api-key", default=os.environ.get("OPENALEX_API_KEY", ""))
    ap.add_argument("--save", default=DEFAULT_SAVE)
    ap.add_argument("--interactive", action="store_true",
                    help="逐篇人工判定（读已 enriched 的清单，写回 decision）")
    args = ap.parse_args()

    if args.interactive:
        # ⚠️ interactive 必须读 enrich 后的 adjudication 清单：enrich 把 Scopus
        # abstract 写回 data/exports/qgs_unresolved_adjudication.json（111 条中
        # 101 篇有 abstract）。默认 manifest 指向 740 条原始筛选清单——其中
        # UNRESOLVED 的 abstract 是 enrich 前状态（只有 49 篇），不修会白判
        # （已踩坑 2026-08-27 路径断裂）。
        enriched = DEFAULT_SAVE  # data/exports/qgs_unresolved_adjudication.json
        if args.manifest == DEFAULT_MANIFEST and os.path.exists(enriched):
            args.manifest = enriched
            print(f"interactive 读取 enrich 后清单: {enriched}")
        run_interactive(args.manifest, args.save)
        return

    manifest = json.load(open(args.manifest, encoding="utf-8"))
    papers = manifest["papers"]
    unres = [p for p in papers if p.get("decision") == "UNRESOLVED"]
    print(f"manifest {len(papers)} 条，UNRESOLVED {len(unres)} 条")

    # manifest 无 openalex_id 字段（screen_qgs build_manifest 未存）——
    # 从 pool 按 DOI 反查补上（已踩坑：直接 fetch_abstracts 0/111）
    pool_path = os.path.join(BASE, "data", "exports", "qgs_candidates_v1.json")
    if os.path.exists(pool_path):
        pool = json.load(open(pool_path, encoding="utf-8"))["candidates"]
        pool_by_doi = {norm_doi(c.get("doi", "")): c for c in pool}
        n_backfill = 0
        for p in unres:
            if not p.get("openalex_id"):
                c = pool_by_doi.get(norm_doi(p.get("doi", "")))
                if c and c.get("openalex_id"):
                    p["openalex_id"] = c["openalex_id"]
                    n_backfill += 1
        print(f"从 pool 反查 openalex_id: {n_backfill}/{len(unres)}")

    # 补 abstract（保留已存在的）：OpenAlex → Crossref → S2 → PubMed
    abstracts = await fetch_abstracts(unres, args.api_key) if args.api_key else {}
    no_ab = {i for i, p in enumerate(unres) if not p.get("abstract") and i not in abstracts}
    if no_ab:
        abstracts.update(await crossref_abstracts(unres, no_ab))
    no_ab = {i for i, p in enumerate(unres) if not p.get("abstract") and i not in abstracts}
    if no_ab:
        abstracts.update(await s2_abstracts(unres, no_ab))
    no_ab = {i for i, p in enumerate(unres) if not p.get("abstract") and i not in abstracts}
    if no_ab:
        abstracts.update(await pubmed_abstracts(unres, no_ab))

    adjud = {
        "benchmark_id": "pc_001_external_qgs_v1",
        "stage": "B4_adjudication",
        "instructions": "第二轮判断：结合新补的 abstract 重新给 decision（RELEVANT/"
                        "IRRELEVANT/UNRESOLVED）+ reason_code。仍无法判断可保持 UNRESOLVED。",
        "reason_codes": manifest.get("reason_codes"),
        "papers": [
            {"idx": p["idx"], "rank": p.get("rank"), "title": p["title"], "year": p["year"],
             "venue": p["venue"], "doi": p["doi"], "sources_from": p["sources_from"],
             "identity_status": p.get("identity_status"),
             "citation_original": p.get("citation_original", ""),
             "abstract": p.get("abstract") or abstracts.get(i, ""),
             "prev_reason": p.get("reason_code", ""),
             "decision": "", "reason_code": "", "note": ""}
            for i, p in enumerate(unres)
        ],
    }
    with open(args.save, "w", encoding="utf-8") as f:
        json.dump(adjud, f, ensure_ascii=False, indent=1)
    with_ab = sum(1 for p in adjud["papers"] if p["abstract"])
    print(f"\n✓ adjudication 清单已生成: {args.save}")
    print(f"  {len(adjud['papers'])} 条，其中 {with_ab} 条已有 abstract（可直接判断），"
          f"{len(adjud['papers']) - with_ab} 条仍需看全文")
    print("  填完 decision 后发我，合并回 benchmark 得到最终 B_total")


if __name__ == "__main__":
    asyncio.run(main())
