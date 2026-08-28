"""tools/enrich_neighbor_metadata.py — OpenAlex 批量补 citation neighbor 元数据（用户 2026-08-28 定稿）。

背景：Citation Bridge v1 的 850 个 neighbor 里 475 个无 title/year（refs 里的 W id
不在现有 openalex_cache.json 中）。term_community.py 需要 title + abstract 做
phrase extraction——缺一半以上节点会把边缘 community 砍掉。本脚本批量补齐。

目标字段（用户定，abstract 最重要）：
  W_id / DOI / title / publication_year / abstract（inverted index → 正文）/
  concepts / topics / primary_location.source / referenced_works / cited_by_count

实现：
  - 输入 citation_bridge_v1.json 的 candidates（W id）
  - OpenAlex GET /works?filter=ids.openalex:W1|W2|...（每批 50 个 id）
  - select 只取需要的字段（减小响应）
  - 幂等：已有 enriched 缓存的 wid 跳过
  - 输出 data/cache/openalex_neighbors_enriched.json

用法：
  python tools/enrich_neighbor_metadata.py            # 联网补全
  python tools/enrich_neighbor_metadata.py --dry-run  # 只报告要查多少，不发请求
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

BRIDGE_PATH = os.path.join(BASE, "data", "exports", "citation_bridge_v1.json")
OUT_PATH = os.path.join(BASE, "data", "cache", "openalex_neighbors_enriched.json")

API = "https://api.openalex.org/works"
SELECT = ("id,doi,display_name,publication_year,abstract_inverted_index,"
          "concepts,topics,primary_location,referenced_works,cited_by_count")
BATCH = 50          # OpenAlex 单个 filter 的 OR 上限
SLEEP = 0.6         # 礼貌限速（OpenAlex 建议 mailto + 限速）


def abstract_text(inv: dict | None) -> str:
    """abstract_inverted_index → 正文字符串。"""
    if not inv:
        return ""
    pos: list[tuple[int, str]] = []
    for word, idxs in inv.items():
        for i in idxs:
            pos.append((i, word))
    pos.sort()
    return " ".join(w for _, w in pos)


def fetch_batch(wids: list[str]) -> list[dict]:
    url = f"{API}?filter=ids.openalex:{urllib.parse.quote('|'.join(wids))}" \
          f"&per-page={BATCH}&select={SELECT}"
    req = urllib.request.Request(url, headers={"User-Agent": "materials-kb-enrich/0.1 (research)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("results", [])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只报告待查数量，不发请求")
    ap.add_argument("--batch", type=int, default=BATCH)
    args = ap.parse_args()

    bridge = json.load(open(BRIDGE_PATH, encoding="utf-8"))
    all_wids = [c["wid"] for c in bridge["candidates"]]
    print(f"citation bridge neighbors 总数 = {len(all_wids)}")

    # 幂等：加载已有 enriched
    enriched: dict[str, dict] = {}
    if os.path.exists(OUT_PATH):
        enriched = json.load(open(OUT_PATH, encoding="utf-8"))
        print(f"已有 enriched 缓存 = {len(enriched)}（幂等跳过）")
    todo = [w for w in all_wids if w not in enriched]
    print(f"待查 = {len(todo)}（{len(all_wids) - len(todo)} 已缓存）")

    if args.dry_run:
        print(f"将发起 {len(todo) // args.batch + 1} 个批量请求（每批 {args.batch}）")
        return

    n_ok = n_missing = n_fail = 0
    for i in range(0, len(todo), args.batch):
        batch = todo[i:i + args.batch]
        try:
            results = fetch_batch(batch)
        except Exception as e:
            print(f"  ✗ 批次 {i // args.batch + 1} 失败: {e}；重试一次")
            time.sleep(2)
            try:
                results = fetch_batch(batch)
            except Exception as e2:
                print(f"  ✗ 批次 {i // args.batch + 1} 重试仍失败: {e2}；跳过")
                n_fail += len(batch)
                continue
        found = {w["id"].rsplit("/", 1)[-1]: w for w in results}
        for w in batch:
            wd = found.get(w)
            if not wd:
                n_missing += 1
                enriched[w] = {"wid": w, "missing": True}
                continue
            src = wd.get("primary_location") or {}
            enriched[w] = {
                "wid": w,
                "doi": (wd.get("doi") or "").replace("https://doi.org/", ""),
                "title": wd.get("display_name") or "",
                "year": wd.get("publication_year"),
                "abstract": abstract_text(wd.get("abstract_inverted_index")),
                "concepts": [c.get("display_name") for c in (wd.get("concepts") or [])][:8],
                "topics": [t.get("display_name") for t in (wd.get("topics") or [])][:8],
                "source": (src.get("source") or {}).get("display_name") or "",
                "refs": [str(x).rsplit("/", 1)[-1] for x in (wd.get("referenced_works") or [])],
                "cited_by_count": wd.get("cited_by_count"),
            }
            n_ok += 1
        # 每批落盘（中断可续）
        with open(OUT_PATH, "w", encoding="utf-8") as f:
            json.dump(enriched, f, ensure_ascii=False, indent=1)
        print(f"  ✓ 批次 {i // args.batch + 1}（{len(batch)} ids）: "
              f"累计成功 {n_ok} / missing {n_missing} / fail {n_fail}")
        time.sleep(SLEEP)

    n_with_abs = sum(1 for v in enriched.values() if v.get("abstract"))
    print(f"\n完成: 缓存总 {len(enriched)}（新增 {n_ok}，missing {n_missing}，fail {n_fail}）")
    print(f"其中含 abstract 的: {n_with_abs}（{n_with_abs/len(enriched)*100:.1f}%）")
    print(f"✓ 已写: {OUT_PATH}")


if __name__ == "__main__":
    main()
