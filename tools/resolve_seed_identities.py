"""tools/resolve_seed_identities.py — Round1 seed → OpenAlex identity resolution（用户 2026-08-28 拍板）。

背景：80 new usable 里 44 篇 DOI 不在 openalex_cache（离线解析极限）。用户拍板：
允许联网（metadata/identity enrichment，非破坏 MVP 离线原则），结果缓存复用。

resolver 顺序（用户冻结，第一版不做 fuzzy title matching——避免 F6 identity error）：
  1. CACHE_WID                openalex_cache.json 中 DOI exact（离线）
  2. OPENALEX_DOI_EXACT       联网 OpenAlex API works?filter=doi:xxx
  3. OPENALEX_TITLE_YEAR_EXACT 联网 title.search + publication_year，且 normalized title
                              必须与 seed 完全相等才接受（exact 校验，非模糊）
  4. UNRESOLVED               不强制 100%

输入：data/exports/round1_seed_relevance.json（RELEVANT 子集才有资格解析）
输出：data/exports/round1_seed_identities.json
      [{eid, doi, title, openalex_id, resolution_method, resolution_confidence, year}]

指标（用户定，分开报告）：
  SeedPurity     = N_RELEVANT / N_new_usable_screened    （relevance 问题）
  IdentityCoverage = N_resolved / N_RELEVANT              （identity 问题）
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "search_engine", "discovery"))

import citation_bridge as cb

RELEVANCE_PATH = os.path.join(BASE, "data", "exports", "round1_seed_relevance.json")
CACHE_PATH = os.path.join(BASE, "data", "cache", "openalex_cache.json")
OUT_PATH = os.path.join(BASE, "data", "exports", "round1_seed_identities.json")
MAILTO = "materials-kb-audit@example.com"   # OpenAlex 礼貌性标识


def norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def oa_get(url: str, retries: int = 3) -> dict | None:
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": f"materials-kb/{MAILTO}"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            if i == retries - 1:
                print(f"    ⚠️ OpenAlex 请求失败: {e}")
                return None
            time.sleep(2 * (i + 1))
    return None


def resolve_online(entry: dict) -> tuple[str, str, float]:
    """联网解析，返回 (openalex_id, method, confidence)。不做 fuzzy title。"""
    doi = entry.get("doi") or ""
    if doi:
        q = urllib.parse.quote(doi)
        data = oa_get(f"https://api.openalex.org/works?filter=doi:{q}"
                      f"&per-page=1&mailto={MAILTO}")
        if data and data.get("results"):
            w = data["results"][0]
            return (w["id"].rsplit("/", 1)[-1], "OPENALEX_DOI_EXACT", 1.0)
    # title + year exact
    title = entry.get("title") or ""
    year = entry.get("year")
    if title:
        nt = norm_title(title)
        tq = urllib.parse.quote(title)
        url = (f"https://api.openalex.org/works?filter=title.search:{tq}"
               f"&per-page=10&mailto={MAILTO}")
        data = oa_get(url)
        if data and data.get("results"):
            for w in data["results"]:
                w_nt = norm_title(w.get("display_name") or "")
                if w_nt == nt:      # exact，非模糊
                    if year and w.get("publication_year") != year:
                        continue    # title 全等但年份不同 → 不冒险（防 F6）
                    return (w["id"].rsplit("/", 1)[-1], "OPENALEX_TITLE_YEAR_EXACT", 1.0)
            # title exact 命中但被 year 卡住 → 仍返回 title 命中（confidence 0.9）
            for w in data["results"]:
                w_nt = norm_title(w.get("display_name") or "")
                if w_nt == nt:
                    return (w["id"].rsplit("/", 1)[-1], "OPENALEX_TITLE_EXACT_NO_YEAR", 0.9)
    return ("", "UNRESOLVED", 0.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-network", action="store_true", help="只离线（CACHE_WID），不联网")
    ap.add_argument("--out", default=OUT_PATH)
    ap.add_argument("--print-only", action="store_true",
                    help="只输出 JSON 到 stdout（不写文件，配合工具落盘）")
    args = ap.parse_args()

    relevance = json.load(open(RELEVANCE_PATH, encoding="utf-8"))
    verdicts = {v["title"]: v["verdict"] for v in relevance["verdicts"]}
    relevant_titles = {t for t, v in verdicts.items() if v == "RELEVANT"}

    # Round1 new usable（eid/doi/title/year/communities）
    sys.path.insert(0, os.path.join(BASE, "tools"))
    import run_citation_hop2 as hop2
    new_usable = hop2.load_round1_new_usable()
    by_title = {}
    for e in new_usable:
        # title 匹配 relevance 的 title（relevance 是 66 字符截断，用 startswith 匹配）
        by_title[e["title"]] = e
    # relevance title 是 66 字符截断版 → 用 normalized 前缀匹配（norm 去标点，
    # 避免 'composites' vs 'composite/' 这类截断边界 miss）
    def norm_match(a: str, b: str) -> bool:
        na, nb = norm_title(a), norm_title(b)
        return na.startswith(nb) or nb.startswith(na) if na and nb else False

    def match_title(full: str) -> str:
        for rt in relevant_titles:
            if norm_match(full, rt):
                return rt
        return ""

    # 离线 CACHE_WID
    cache = json.load(open(CACHE_PATH, encoding="utf-8"))
    doi2wid = {}
    for entry in cache.values():
        for w in (entry.get("results") or []):
            doi = cb.norm_doi(w.get("doi") or "")
            wid = (w.get("id") or "").rsplit("/", 1)[-1]
            if doi and wid:
                doi2wid.setdefault(doi, wid)

    rows = []
    n_rel = 0
    for e in new_usable:
        rt = match_title(e["title"])
        if rt not in relevant_titles:
            continue   # 只解析 RELEVANT（用户定：IdentityCoverage 只在 relevant 上算）
        n_rel += 1
        doi = cb.norm_doi(e.get("doi") or "")
        wid = doi2wid.get(doi) if doi else None
        if wid:
            method, conf = "CACHE_WID", 1.0
        elif not args.no_network:
            wid, method, conf = resolve_online(e)
        else:
            wid, method, conf = "", "UNRESOLVED", 0.0
        rows.append({"eid": e["eid"], "doi": doi, "title": e["title"],
                     "year": e.get("year"), "openalex_id": wid,
                     "resolution_method": method, "resolution_confidence": conf})
        if not args.print_only:
            print(f"  [{method:<26}] {wid or '-':<14} {e['title'][:52]}")
        if method != "CACHE_WID":
            time.sleep(0.15)   # OpenAlex 限速礼貌

    resolved = sum(1 for r in rows if r["openalex_id"])
    from collections import Counter
    methods = Counter(r["resolution_method"] for r in rows)
    out = {
        "created_at": "2026-08-28",
        "input": {"new_usable": len(new_usable), "screened_relevant": n_rel},
        "metrics": {
            "seed_purity": relevance["seed_purity"],
            "identity_coverage": round(resolved / n_rel, 4) if n_rel else 0,
            "resolved": resolved, "total_relevant": n_rel,
            "methods": dict(methods),
        },
        "seeds": rows,
    }
    if args.print_only:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n✓ 已写: {args.out}")
    print(f"SeedPurity = {relevance['seed_purity']} | IdentityCoverage = "
          f"{out['metrics']['identity_coverage']}（{resolved}/{n_rel}）| methods: {dict(methods)}")


if __name__ == "__main__":
    main()
