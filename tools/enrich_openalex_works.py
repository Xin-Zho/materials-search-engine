"""tools/enrich_openalex_works.py — 泛化 OpenAlex 元数据 enrichment（用户 2026-08-28 定稿）。

输入任意含 W-id 的 JSON（citation bridge hop1/hop2/hop3... 通用），输出统一
enrichment 缓存。**不做 hop 专用逻辑**。

  python tools/enrich_openalex_works.py \
    --input data/exports/citation_bridge_hop2.json \
    --output data/cache/openalex_hop2_enriched.json

策略（用户冻结）：
  W-id
   → local openalex_cache.json（已有 work → CACHE_HIT，不发请求）
   → 已有 --output 缓存（幂等跳过）
   → OpenAlex API works?filter=ids.openalex:...（批量 50）→ API_ENRICHED
   → 不在 OpenAlex → NOT_FOUND；请求失败 → API_FAILED
  enrichment 失败不删 neighbor（输入 graph 不动，缓存只是附加层）

每篇统一字段：
  openalex_id / doi / title / year / abstract / source / concepts / topics /
  referenced_works / cited_by_count /
  enrichment_status / enrichment_source / abstract_available / references_available

覆盖指标（以输入 W-id 总数为分母）：
  MetadataCoverage = N_title_available / N_input
  AbstractCoverage = N_abstract_available / N_input
"""
import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.parse

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

LOCAL_CACHE = os.path.join(BASE, "data", "cache", "openalex_cache.json")
API = "https://api.openalex.org/works"
SELECT = ("id,doi,display_name,publication_year,abstract_inverted_index,"
          "concepts,topics,primary_location,referenced_works,cited_by_count")
BATCH = 50
SLEEP = 0.6


def abstract_text(inv: dict | None) -> str:
    if not inv:
        return ""
    pos: list[tuple[int, str]] = []
    for word, idxs in inv.items():
        for i in idxs:
            pos.append((i, word))
    pos.sort()
    return " ".join(w for _, w in pos)


def extract_wids(data: dict | list) -> list[str]:
    """从任意输入 JSON 抽 W-id（支持 candidates[].wid / 顶层 list / openalex_id）。"""
    out: list[str] = []
    if isinstance(data, dict):
        cands = data.get("candidates") or data.get("papers") or data.get("seeds") or []
        for c in cands:
            if isinstance(c, dict):
                w = c.get("wid") or c.get("openalex_id") or c.get("id") or ""
                if isinstance(w, str) and w.startswith("W"):
                    out.append(w)
    elif isinstance(data, list):
        for c in data:
            if isinstance(c, dict):
                w = c.get("wid") or c.get("openalex_id") or c.get("id") or ""
                if isinstance(w, str) and w.startswith("W"):
                    out.append(w)
    # 去重保序
    seen = set()
    return [w for w in out if not (w in seen or seen.add(w))]


def load_local_cache() -> dict[str, dict]:
    """openalex_cache.json 里的 works → {wid: 元数据}。"""
    out = {}
    if not os.path.exists(LOCAL_CACHE):
        return out
    cache = json.load(open(LOCAL_CACHE, encoding="utf-8"))
    for entry in cache.values():
        for w in (entry.get("results") or []):
            wid = (w.get("id") or "").rsplit("/", 1)[-1]
            if not wid:
                continue
            src = w.get("primary_location") or {}
            out[wid] = {
                "openalex_id": wid,
                "doi": (w.get("doi") or "").replace("https://doi.org/", ""),
                "title": w.get("display_name") or "",
                "year": w.get("publication_year"),
                "abstract": abstract_text(w.get("abstract_inverted_index")),
                "source": (src.get("source") or {}).get("display_name") or "",
                "concepts": [c.get("display_name") for c in (w.get("concepts") or [])][:8],
                "topics": [t.get("display_name") for t in (w.get("topics") or [])][:8],
                "refs": [str(x).rsplit("/", 1)[-1] for x in (w.get("referenced_works") or [])],
                "cited_by_count": w.get("cited_by_count"),
            }
    return out


def fetch_batch(wids: list[str]) -> list[dict]:
    url = f"{API}?filter=ids.openalex:{urllib.parse.quote('|'.join(wids))}" \
          f"&per-page={BATCH}&select={SELECT}"
    req = urllib.request.Request(url, headers={"User-Agent": "materials-kb-enrich/0.2 (research)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("results", [])


def finalize(rec: dict, status: str, source: str) -> dict:
    rec["enrichment_status"] = status
    rec["enrichment_source"] = source
    rec["abstract_available"] = bool(rec.get("abstract"))
    rec["references_available"] = bool(rec.get("refs"))
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="含 W-id 的 JSON（bridge hop 输出）")
    ap.add_argument("--output", required=True, help="enrichment 缓存输出路径")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    data = json.load(open(args.input, encoding="utf-8"))
    all_wids = extract_wids(data)
    print(f"输入 W-id 总数 = {len(all_wids)}（{os.path.basename(args.input)}）")

    # 分层缓存
    local = load_local_cache()
    enriched: dict[str, dict] = {}
    if os.path.exists(args.output):
        enriched = json.load(open(args.output, encoding="utf-8"))
        print(f"已有 output 缓存 = {len(enriched)}（幂等跳过）")

    todo = []
    for w in all_wids:
        if w in enriched:
            continue                      # 幂等：本缓存已有
        if w in local:
            enriched[w] = finalize(dict(local[w]), "CACHE_HIT", "openalex_cache")
            continue
        todo.append(w)
    print(f"本地缓存命中 = {len(enriched)} | 待 API = {len(todo)}")

    if args.dry_run:
        print(f"将发起 {len(todo) // BATCH + 1} 个批量请求（每批 {BATCH}）")
        return

    n_ok = n_missing = n_fail = 0
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        try:
            results = fetch_batch(batch)
        except Exception as e:
            print(f"  ✗ 批次 {i // BATCH + 1} 失败: {e}；重试一次")
            time.sleep(2)
            try:
                results = fetch_batch(batch)
            except Exception as e2:
                print(f"  ✗ 批次 {i // BATCH + 1} 重试仍失败: {e2}")
                for w in batch:
                    enriched[w] = finalize({"openalex_id": w, "title": "",
                                            "abstract": ""}, "API_FAILED", "api")
                    n_fail += 1
                continue
        found = {w["id"].rsplit("/", 1)[-1]: w for w in results}
        for w in batch:
            wd = found.get(w)
            if not wd:
                enriched[w] = finalize({"openalex_id": w, "title": "",
                                        "abstract": ""}, "NOT_FOUND", "api")
                n_missing += 1
                continue
            src = wd.get("primary_location") or {}
            enriched[w] = finalize({
                "openalex_id": w,
                "doi": (wd.get("doi") or "").replace("https://doi.org/", ""),
                "title": wd.get("display_name") or "",
                "year": wd.get("publication_year"),
                "abstract": abstract_text(wd.get("abstract_inverted_index")),
                "source": (src.get("source") or {}).get("display_name") or "",
                "concepts": [c.get("display_name") for c in (wd.get("concepts") or [])][:8],
                "topics": [t.get("display_name") for t in (wd.get("topics") or [])][:8],
                "refs": [str(x).rsplit("/", 1)[-1] for x in (wd.get("referenced_works") or [])],
                "cited_by_count": wd.get("cited_by_count"),
            }, "API_ENRICHED", "api")
            n_ok += 1
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(enriched, f, ensure_ascii=False, indent=1)
        print(f"  ✓ 批次 {i // BATCH + 1}（{len(batch)} ids）: "
              f"累计 ok {n_ok} / missing {n_missing} / fail {n_fail}")
        time.sleep(SLEEP)

    # 覆盖指标（以输入总数为分母）
    n_in = len(all_wids)
    n_title = sum(1 for w in all_wids if enriched.get(w, {}).get("title"))
    n_abs = sum(1 for w in all_wids if enriched.get(w, {}).get("abstract"))
    from collections import Counter
    status = Counter(enriched.get(w, {}).get("enrichment_status") for w in all_wids)
    print(f"\n完成: 输入 {n_in} | 缓存总 {len(enriched)}（ok {n_ok} / missing {n_missing} / fail {n_fail}）")
    print(f"  enrichment_status: {dict(status)}")
    print(f"  MetadataCoverage = {n_title}/{n_in} = {n_title/n_in*100:.1f}%")
    print(f"  AbstractCoverage = {n_abs}/{n_in} = {n_abs/n_in*100:.1f}%")
    print(f"✓ 已写: {args.output}")


if __name__ == "__main__":
    main()
