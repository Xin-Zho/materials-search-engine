"""tools/audit_universe_diagnose.py — Phase 3 离线缺口诊断（用户定 2026-08-27）。

回答三个问题（全部可离线/低消耗，不重跑全量扩网）：
  ① 上一版(42 篇)漏网里救回的 20 篇，分别被哪个 channel 兜住？
     —— 归属矩阵 + exclusive_recovered_gaps（多 channel 同时兜住也算）
  ② 剩余 22 篇的 failure classification：
     QUERY_LEXICAL_GAP      —— 引用网络连得上 known relevant，但 CORE/SUPP 词面没匹配
     CITATION_DISCONNECTED  —— 1-hop 引用网络完全断开（不引用/不被引用任何 known relevant）
     SOURCE_INDEX_GAP       —— OpenAlex 里查不到（id 失效/未收录）
     SOURCE_METADATA_GAP    —— OpenAlex 有但 metadata 异常（无 title/abstract）
     DATE/TYPE_FILTER_GAP   —— 被客观过滤规则挡掉
     CROSS_DOMAIN_GAP       —— 连得上引用但属于未覆盖子领域（需新 query 词）
  ③ 本版 channel 完整集合重建（修复 query_hits 只存前 100 的缺口）：
     CORE/SUPP   —— v3 query_hits 完整聚合（精确）
     BACKWARD    —— 缓存 67 seed work 的 referenced_works（引用关系，精确）
     FORWARD     —— 缓存 cites:{seed} 分页重放（中断可能不完整，标记 partial）

在线消耗：仅 22 篇元数据 singleton 请求（≈22 credits），可选 --skip-metadata 跳过。
"""
import argparse
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

CACHE_PATH = os.path.join(BASE, "data", "cache", "openalex_cache.json")
SNAP_PATH = os.path.join(BASE, "data", "exports", "completeness_universes.json")
SEED_WORK_RE = re.compile(r"https://api\.openalex\.org/works/(W\d+)\?\{\}")
CITES_RE = re.compile(r'"filter": "cites:(W\d+)"')


def norm(w: str) -> str:
    m = re.search(r"(W\d+)", w or "")
    return m.group(1) if m else (w or "").strip()


def load_snapshot(idx: int):
    from search_engine.completeness.universe import UniverseSnapshot
    snaps = json.load(open(SNAP_PATH, encoding="utf-8"))
    return UniverseSnapshot.from_dict(snaps[idx]), snaps


def rebuild_channels(cache: dict, snap, definition) -> dict:
    """从 snapshot query_hits + 缓存重建各 channel 完整 id 集合。

    CORE/SUPP  —— query_hits 的 key 是裸 query 文本，用 definition.channels 匹配
                 （v3 起 query_hits 存完整列表，精确）
    BACKWARD   —— seed work.referenced_works ∪ refs-batch 查询 key 里的 id
                 （引用关系双向等价：X 出现在批量取回 ⟺ X ∈ 某 seed 的
                 referenced_works，精确；修 seed work 缓存缺失导致的漏归因）
    FORWARD    —— cites:{seed} 分页结果 union（中断则 PARTIAL，可能低估）
    """
    sb = snap.source_breakdown
    channels: dict[str, set] = {}
    for ch in ("CORE_UMBRELLA", "SUPPLEMENTAL_ROUTE"):
        ids = set()
        queries = (definition.channels.get(ch) or [])
        qh = sb.get("query_hits", {})
        for q in queries:
            ids |= {norm(x) for x in qh.get(q, [])}
        channels[ch] = ids
    # BACKWARD：只用 snapshot citation_seed_map 记录的 seeds（排除探测期残留缓存）。
    # seed work.referenced_works（精确）∪ 对应 refs-batch key id。
    seed_map = sb.get("citation_seed_map", {})
    seeds = set()
    for ch in ("BACKWARD_CITATION", "FORWARD_CITATION"):
        seeds |= {norm(x) for x in (seed_map.get(ch) or [])}
    seed_works = {}
    for k, v in cache.items():
        m = SEED_WORK_RE.match(k)
        if m and isinstance(v, dict):
            seed_works[norm(m.group(1))] = v
    backward: set[str] = set()
    missing_seed_works: list[str] = []
    for seed in seeds:
        work = seed_works.get(seed)
        if work is None:
            missing_seed_works.append(seed)
        elif work.get("referenced_works"):
            backward |= {norm(x) for x in work["referenced_works"]}
    # refs-batch 缓存补缺失 seed work（key 里含完整引用 id 列表，无法精确归属
    # 到 seed，但只并入"与任一 seed work referenced_works 同源"的引用——
    # 残留探测期缓存会高估 ~5%，见 [③] 偏差注记）
    if missing_seed_works:
        refs_batch_ids: set[str] = set()
        for k in cache:
            if "openalex_id:" not in k:
                continue
            m = re.search(r'"filter": "openalex_id:([^"]+)"', k)
            if m:
                refs_batch_ids |= {norm(x) for x in m.group(1).split("|")}
        backward |= refs_batch_ids
    channels["BACKWARD_CITATION"] = backward
    channels["_MISSING_SEED_WORKS"] = missing_seed_works
    # FORWARD：cites:{seed} 分页结果 union（缓存覆盖检查：有 next_cursor 但
    # 下一页 key 不在缓存才是真 PARTIAL；正常跑完的最后一页 next_cursor 指向
    # 空结果也带值，不能误标）
    forward: set[str] = set()
    partial = False
    cited_by_ids = [m.group(1) for m in (CITES_RE.search(k) for k in cache) if m]
    for k, v in cache.items():
        if "cites:" not in k:
            continue
        if isinstance(v, dict):
            for w in v.get("results", []):
                wid = norm((w or {}).get("id", ""))
                if wid:
                    forward.add(wid)
            nc = (v.get("meta") or {}).get("next_cursor")
            if nc:
                import urllib.parse
                params = json.loads(k.split("?", 1)[1])
                params["cursor"] = nc
                next_key = (k.split("?", 1)[0] + "?"
                            + json.dumps(params, sort_keys=True))
                if next_key not in cache:
                    partial = True
    channels["FORWARD_CITATION"] = forward
    channels["_FORWARD_PARTIAL"] = {("PARTIAL" if partial else "COMPLETE")}
    return channels


def classify_gaps(gap_ids: list[str], channels: dict, metadata: dict,
                  skip_meta: bool = False) -> dict:
    """22 篇漏网 failure classification。

    metadata 缺失时（--skip-metadata / 拉取失败）降级为引用连通性预判：
      CONNECTED_NOT_IN_META   —— 引用连得上（QUERY_LEXICAL_GAP 候选）
      DISCONNECTED            —— 引用 1-hop 断开（CITATION_DISCONNECTED 候选）
    只有明确拉取到元数据后才给最终分类；OpenAlex 明确报 missing/error 才是
    SOURCE_INDEX_GAP。
    """
    backward = channels.get("BACKWARD_CITATION", set())
    forward = channels.get("FORWARD_CITATION", set())
    core_supp = (channels.get("CORE_UMBRELLA", set())
                 | channels.get("SUPPLEMENTAL_ROUTE", set()))
    classes: dict[str, str] = {}
    notes: dict[str, str] = {}
    for w in gap_ids:
        md = metadata.get(w)
        if md is None:
            if skip_meta:
                connected = (w in backward) or (w in forward)
                classes[w] = ("CONNECTED_NOT_IN_META" if connected
                              else "DISCONNECTED")
                notes[w] = (f"引用: BACKWARD={'连' if w in backward else '断'} "
                            f"FORWARD={'连' if w in forward else '断'}（未拉元数据，待确认）")
            else:
                classes[w] = "UNKNOWN_METADATA"
                notes[w] = "元数据拉取缺失（重跑 --api-key）"
            continue
        if md.get("missing", False):
            classes[w] = "SOURCE_INDEX_GAP"
            notes[w] = md.get("reason", "OpenAlex 查不到")
            continue
        has_meta = bool(md.get("title") and (md.get("abstract") or md.get("venue") or md.get("year")))
        if not has_meta:
            classes[w] = "SOURCE_METADATA_GAP"
            notes[w] = f"title={md.get('title')!r} abstract={'有' if md.get('abstract') else '无'} venue={md.get('venue')!r}"
            continue
        connected = (w in backward) or (w in forward)
        lexical = w in core_supp
        if not connected:
            classes[w] = "CITATION_DISCONNECTED"
            notes[w] = f"1-hop 引用网络断开（BACKWARD={'连' if w in backward else '断'} FORWARD={'连' if w in forward else '断'}）"
        elif not lexical:
            classes[w] = "QUERY_LEXICAL_GAP"
            notes[w] = f"引用连得上但词面不匹配（title={md.get('title', '')[:60]!r}）"
        else:
            classes[w] = "CROSS_DOMAIN_GAP"
            notes[w] = "词面+引用都连上但仍漏（需人工复核）"
    return classes, notes


def fetch_metadata(gap_ids: list[str], api_key: str) -> dict:
    """拉 22 篇漏网元数据（singleton，≈1 credit/篇）。"""
    import asyncio
    from search_engine.backends.openalex import OpenAlexBackend

    async def run():
        async with OpenAlexBackend(api_key=api_key) as oa:
            out = {}
            for w in gap_ids:
                try:
                    data = await oa._get_json(f"{oa.BASE_URL}/works/{w}", {})
                except Exception as e:  # noqa: BLE001
                    out[w] = {"missing": True, "reason": f"{type(e).__name__}: {e}"}
                    continue
                if not data or "error" in data:
                    out[w] = {"missing": True, "reason": data.get("error", "empty")}
                    continue
                out[w] = {
                    "title": data.get("title") or "",
                    "abstract": bool(data.get("abstract_inverted_index")),
                    "year": data.get("publication_year"),
                    "venue": ((data.get("primary_location") or {}).get("source") or {}).get("display_name"),
                    "cited_by": data.get("cited_by_count"),
                    "type": data.get("type"),
                }
            return out, oa.credit_summary()

    return asyncio.run(run())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prev-idx", type=int, default=0, help="上一版 snapshot 下标（默认 0=v2）")
    ap.add_argument("--cur-idx", type=int, default=1, help="当前版 snapshot 下标（默认 1=v3）")
    ap.add_argument("--api-key", default=os.environ.get("OPENALEX_API_KEY", ""),
                    help="拉漏网元数据用（≈22 credits）；缺省则跳过在线部分")
    ap.add_argument("--skip-metadata", action="store_true", help="不拉元数据，只做离线分析")
    args = ap.parse_args()

    cache = json.load(open(CACHE_PATH, encoding="utf-8"))
    prev, _ = load_snapshot(args.prev_idx)
    cur, _ = load_snapshot(args.cur_idx)

    from search_engine.completeness.universe_builder import load_definition
    definition = load_definition("pc_001")
    found = set(norm(x) for x in cur.source_breakdown.get("found_relevant", []))
    prev_outside = set(prev.found_relevant_outside())   # 42 篇
    cur_outside = set(cur.found_relevant_outside())     # 22 篇
    recovered = prev_outside - cur_outside              # 20 篇
    channels = rebuild_channels(cache, cur, definition)

    print("=" * 72)
    print(f"snapshot: {prev.universe_id} (v2, {prev.total_count}) → "
          f"{cur.universe_id} (v3, {cur.total_count})")
    print(f"known relevant = {len(found)} | 上一版漏网 = {len(prev_outside)} | "
          f"当前漏网 = {len(cur_outside)} | 救回 = {len(recovered)}")
    print("=" * 72)

    # ── ① 20 篇救回归属矩阵 ──
    print("\n[①] 救回 20 篇的 channel 归属（一篇可被多 channel 兜住）:")
    owners: dict[str, list[str]] = {}
    for w in sorted(recovered):
        chs = [ch for ch in ("CORE_UMBRELLA", "SUPPLEMENTAL_ROUTE",
                             "BACKWARD_CITATION", "FORWARD_CITATION")
               if w in channels.get(ch, set())]
        owners[w] = chs
    n_excl = 0
    for w, chs in owners.items():
        tag = " ★独占" if len(chs) == 1 else ""
        if len(chs) == 1:
            n_excl += 1
        print(f"  {w}: {', '.join(chs) or '— 未归因（缓存缺口）'}{tag}")
    print(f"\n  独立救回篇数(exclusive): {n_excl}/{len(recovered)}")

    print("\n  channel 恢复统计:")
    print(f"  {'Channel':<22} {'recovered':>10} {'exclusive':>10}")
    for ch in ("CORE_UMBRELLA", "SUPPLEMENTAL_ROUTE", "BACKWARD_CITATION", "FORWARD_CITATION"):
        rec = sum(1 for chs in owners.values() if ch in chs)
        excl = sum(1 for chs in owners.values() if chs == [ch])
        print(f"  {ch:<22} {rec:>10} {excl:>10}")
    fwd_state = list(channels.get("_FORWARD_PARTIAL", set()))[0]
    print(f"  (FORWARD 缓存完整性: {fwd_state}——PARTIAL 时 FORWARD 归属可能低估)")

    # ── ② 22 篇 failure classification ──
    print(f"\n[②] 当前 {len(cur_outside)} 篇漏网分类:")
    metadata: dict = {}
    credits = None
    if not args.skip_metadata and args.api_key:
        metadata, credits = fetch_metadata(sorted(cur_outside), args.api_key)
    elif not args.skip_metadata:
        print("  ⚠️ 未提供 --api-key，跳过元数据拉取（分类将退化为纯离线判定）")
    classes, notes = classify_gaps(sorted(cur_outside), channels, metadata,
                                   skip_meta=args.skip_metadata)
    from collections import Counter
    cnt = Counter(classes.values())
    for cls, n in cnt.most_common():
        print(f"  {cls:<24} {n}")
    for w in sorted(cur_outside):
        print(f"    {w}: {classes[w]} — {notes.get(w, '')[:90]}")
    if credits:
        print(f"  (在线消耗 credits: {credits['total']})")

    # ── ③ channel 集合重建验证 ──
    print("\n[③] 重建 channel 集合 vs snapshot 计数:")
    missing_seeds = list(channels.get("_MISSING_SEED_WORKS", set()))
    for ch in ("CORE_UMBRELLA", "SUPPLEMENTAL_ROUTE", "BACKWARD_CITATION", "FORWARD_CITATION"):
        rebuilt = len(channels.get(ch, set()))
        recorded = cur.source_breakdown.get("channel_contribution", {}).get(ch, 0)
        ok = abs(rebuilt - recorded) <= max(50, recorded * 0.05)
        note = "[一致]" if ok else "[偏差]"
        if ch == "BACKWARD_CITATION" and missing_seeds:
            note += f"（{len(missing_seeds)} 个 seed work 缓存缺失: {missing_seeds[:3]}）"
        elif ch == "BACKWARD_CITATION":
            note += "（refs-batch 兜底可能含探测期残留，高估）"
        print(f"  {ch:<22} rebuilt={rebuilt:>7} recorded={recorded:>7} {note}")

    # ── ④ 未归因 recovered 深挖（缓存缺口定位）──
    unattributed = [w for w in recovered if not owners[w]]
    if unattributed:
        print("\n[④] 未归因救回篇的缓存线索（FORWARD PARTIAL 时可能是缺页）:")
        for w in unattributed:
            in_cites_pages = any(w in {norm(x.get("id", "")) for x in v.get("results", [])}
                                 for k, v in cache.items() if "cites:" in k and isinstance(v, dict))
            in_refs_batch = any(w in k for k in cache if "openalex_id:" in k)
            in_prev = w in prev.paper_ids
            print(f"  {w}: cites缓存出现={in_cites_pages} refs批量出现={in_refs_batch} "
                  f"在v2里={in_prev}")


if __name__ == "__main__":
    main()
