"""Phase 3 P1.5 — tools/build_audit_universe.py（用户定 2026-08-27）。

外部审计总体构造：定义 → 宽 umbrella 检索 → union → dedup → 冻结 UniverseSnapshot。
与 Search Agent 的 ranking/prioritizer/candidate/ontology 完全无关（高 recall 低
precision——宁可多收垃圾，不把可能相关论文排除在总体外）。

用法：
  python tools/build_audit_universe.py --topic pc_001 [--limit 500] [--save-path ...]
"""

import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)


def _search_backend(mailto: str | None = None, api_key: str | None = None):
    """真实宽检索：OpenAlex 全量分页（cursor pagination）——universe builder 专用。

    ⚠️ 不能用 search_relevance：它 per-page≤200 只取一页，实测
    '"photopolymerization" AND "shrinkage"' 命中 831 只取回 200——
    universe 被截断，已确认 relevant 论文会落在 universe 外（已踩坑 2026-08-27）。

    ⚠️ 配额（2025+ OpenAlex credit 制，已踩坑 2026-08-27）：
      - 无 API key：1000 credits/天。list 请求（filter/search/分页）= 10 credits/
        次——28 宽检索 + 67 seeds citation 1-hop ≈ 4000+ credits，必然打爆。
      - mailto 只进 polite pool（响应更稳定），**不增加配额**。
      - 免费 API key（openalex.org/settings/api 注册即得）：100k credits/天。
      全量扩网必须传 api_key（OPENALEX_API_KEY env 或 --api-key）。
    """
    from search_engine.backends.openalex import OpenAlexBackend
    from search_engine.completeness.universe_builder import fetch_paginated_openalex
    backend = OpenAlexBackend(mailto=mailto, api_key=api_key)

    async def fetch_all(query, limit=100000):
        return await fetch_paginated_openalex(backend, query)
    return fetch_all, backend


def _load_prev_snapshot(topic_id: str, current_id: str):
    """从 completeness_universes.json 取同 topic 的上一版 snapshot（创建时间早于当前）。

    recovered_previous_gaps 的基准：上一版 universe 的 known relevant 漏网集合。
    返回 UniverseSnapshot 或 None（首次构建/找不到时）。
    """
    from search_engine.completeness.universe import UniverseSnapshot, load_snapshots
    try:
        snaps = load_snapshots()
    except Exception:
        return None
    prev = None
    for d in snaps:
        if d.get("topic_id") != topic_id or d.get("universe_id") == current_id:
            continue
        if prev is None or d.get("created_at", "") > prev.get("created_at", ""):
            prev = d
    return UniverseSnapshot.from_dict(prev) if prev else None


def _load_snapshot_by_id(universe_id: str):
    """按 universe_id 精确加载 snapshot。"""
    from search_engine.completeness.universe import UniverseSnapshot, load_snapshots
    try:
        snaps = load_snapshots()
    except Exception:
        return None
    for d in snaps:
        if d.get("universe_id") == universe_id:
            return UniverseSnapshot.from_dict(d)
    return None


def main():
    ap = argparse.ArgumentParser(description="Build External Audit Universe")
    ap.add_argument("--topic", required=True, help="topic_id（如 pc_001）")
    ap.add_argument("--limit", type=int, default=500, help="每个 umbrella query 取多少")
    ap.add_argument("--save-path", default="",
                    help="snapshot 保存路径（默认 data/exports/completeness_universes.json）")
    ap.add_argument("--found-relevant", default="",
                    help="可选：Agent 已确认 relevant 论文 id 文件（每行一个 id）")
    ap.add_argument("--citations", action="store_true",
                    help="开 citation channels（BACKWARD/FORWARD 1-hop，seed=known relevant）")
    ap.add_argument("--review-seeds", default="",
                    help="可选：review seed 论文 id 文件（REVIEW_REFERENCE 通道）")
    ap.add_argument("--citation-seeds", default="",
                    help="可选：citation seed 论文 id 文件（BACKWARD/FORWARD 通道，"
                         "每行一个 id）；缺省 = found relevant 全集（v4a 起必须传 "
                         "DEV-only seeds 文件，见 pc_001_dev_holdout.json）")
    ap.add_argument("--mailto", default=os.environ.get("OPENALEX_MAILTO", ""),
                    help="OpenAlex polite pool 邮箱（响应更稳定；不增加配额）")
    ap.add_argument("--api-key", default=os.environ.get("OPENALEX_API_KEY", ""),
                    help="OpenAlex API key（openalex.org/settings/api 免费申请；"
                         "无 key 仅 1000 credits/天，全量扩网必打爆）")
    ap.add_argument("--prev-universe", default="",
                    help="可选：上一版 snapshot 的 universe_id（比较漏网恢复用；"
                         "缺省自动取 completeness_universes.json 里同 topic 的上一版）")
    args = ap.parse_args()

    from search_engine.completeness.universe_builder import (
        load_definition, build_audit_universe, AGENT_KNOWLEDGE_MAY_EXPAND_NEVER_CONTRACT,
        build_agent_seen_pool, _norm_openalex_id,
    )
    from search_engine.completeness.universe import save_snapshot

    definition = load_definition(args.topic)
    print("=" * 60)
    print(f"Audit Universe Definition — {args.topic}")
    print("=" * 60)
    print(f"sources:     {definition.sources}")
    print(f"date:        {definition.date_start}–{definition.date_end}")
    print(f"definition_version: {definition.definition_version}")
    print(f"fingerprint: {definition.fingerprint()}")
    for ch in sorted(definition.channels):
        qs = definition.channels.get(ch, [])
        print(f"{ch} ({len(qs)} entries):")
        for q in qs:
            print(f"  - {q}")

    # F（found relevant）：Agent 已确认 relevant 的唯一 canonical 论文
    # 优先用 --found-relevant 文件（每行一个 id）；否则自动从 KB（有 edges 的论文，
    # doi 去重）+ candidate pool（VALIDATED/PROMOTED source_papers）计算——
    # F 是审计总体与 Agent 结果的交集，universe 本身仍来自外部定义（不违反独立性）。
    found = []
    if args.found_relevant and os.path.exists(args.found_relevant):
        with open(args.found_relevant, encoding="utf-8") as f:
            found = [line.strip() for line in f
                     if line.strip() and not line.startswith("#")]
    else:
        from search_engine.completeness.universe_builder import build_agent_seen_pool
        found = build_agent_seen_pool()["found_relevant"]
        print(f"· F 自动计算（KB edges + VALIDATED/PROMOTED source_papers）: {len(found)} 篇")
    if not found:
        print("⚠️ F 为空——universe 将全部进入 remaining pool（统计上合法，"
              "但请确认 Agent 已确认 relevant 的论文未被遗漏）")

    print()
    print("正在执行宽 umbrella 检索 + citation 扩展（高 recall，允许低 precision）...")
    search_fn, backend = _search_backend(args.mailto or None, args.api_key or None)
    if not args.api_key:
        print("⚠️  未提供 API key（OPENALEX_API_KEY）——无 key 配额仅 1000 credits/天，"
              "全量扩网（4000+ credits）会在中途 429 中断。建议先申请免费 key。")
    citation_backend = backend if args.citations else None

    review_seeds = []
    if args.review_seeds and os.path.exists(args.review_seeds):
        with open(args.review_seeds, encoding="utf-8") as f:
            review_seeds = [line.strip() for line in f
                            if line.strip() and not line.startswith("#")]

    # citation seeds：优先 --citation-seeds 文件（v4a 起 = DEV-only 44 篇）；
    # 否则默认 known relevant 全集（用户定：以当前 known relevant 为 seed 做 1-hop）
    citation_seeds = []
    if args.citation_seeds and os.path.exists(args.citation_seeds):
        with open(args.citation_seeds, encoding="utf-8") as f:
            citation_seeds = [line.strip() for line in f
                              if line.strip() and not line.startswith("#")]
        print(f"· citation seeds（来自文件）: {len(citation_seeds)} 篇")
    citation_seeds = [w for w in (citation_seeds or found) if "W" in (w or "")] or found
    snap = build_audit_universe(definition, search_fn, found_relevant=found,
                                citation_backend=citation_backend,
                                review_seeds=review_seeds,
                                citation_seeds=citation_seeds)

    print()
    print("=" * 60)
    print("Universe frozen")
    print("=" * 60)
    print(f"universe_id:      {snap.universe_id}")
    print(f"universe_hash:    {snap.universe_hash}")
    print(f"source_type:      {snap.source_type}")
    print(f"total papers:     {snap.total_count}")
    # KnownRelevantContainment 诊断（用户定：正式 audit 前必须 100%）
    total_kr = snap.known_relevant_total()
    f_in = snap.found_relevant_count()
    outside = snap.found_relevant_outside()
    containment = snap.known_relevant_containment()
    print()
    print(f"known_relevant_total        = {total_kr}")
    print(f"known_relevant_in_universe  = {f_in}  (F)")
    print(f"known_relevant_outside      = {len(outside)}")
    print(f"KnownRelevantContainment    = {containment*100:.1f}%  "
          f"({'PASS' if containment >= 1.0 else '⚠️ 要求 100%——先扩 universe 再审计'})")
    if outside:
        print("outside papers:")
        for w in outside[:15]:
            print(f"  - {w}")
        if len(outside) > 15:
            print(f"  ... (+{len(outside) - 15} more)")
    print()
    snap.check_accounting()   # 硬断言 |U| = F + N_remaining
    print(f"remaining pool:   {snap.remaining_pool_size()}  "
          f"(账目校验: {snap.total_count} = {f_in} + {snap.remaining_pool_size()})")

    # Channel 诊断表（用户 2026-08-27 定三口径，修复"找回漏网恒为 0"的口径 bug）：
    #   口径 1  channel_unique_count     —— 该 channel 内部去重后的论文数（不是独有）
    #   口径 2  known_relevant_hits      —— 该 channel 兜住的 known relevant（F 内）
    #   口径 3  recovered_previous_gaps  —— 相对【上一版】snapshot 找回的漏网
    #           （上一版 known relevant 在 U_prev 之外，但进了本版该 channel）
    #   口径 4  exclusive_recovered_gaps —— 口径 3 中只有该 channel 兜住的（独有贡献）
    # ⚠️ 旧口径 `ids & outside_after` 按定义恒为 0（channel_ids ⊆ final_union，不可能
    # 同时属于 final_union 之外的集合）——已废弃。
    # channel 完整 id 集合：优先 source_breakdown["channel_papers"]（本版起持久化）；
    # 旧 snapshot 无此字段时退化为 query_hits 聚合（citation 通道只存前 100，会低估）。
    sb = snap.source_breakdown
    contrib = sb.get("channel_contribution", {})
    channel_papers = sb.get("channel_papers", {})
    qh = sb.get("query_hits", {})
    if not channel_papers:
        for k, ids in qh.items():
            ch = k.split(":")[0]
            if ch in contrib:
                channel_papers.setdefault(ch, set()).update(ids)
    else:
        channel_papers = {ch: set(v) for ch, v in channel_papers.items()}
    seed_map = sb.get("citation_seed_map", {})
    kr = set(_norm_openalex_id(f) for f in found if f)

    # 上一版 snapshot 的漏网集合（口径 3 的基准）
    outside_before: set[str] = set()
    prev_snap = None
    if args.prev_universe:
        prev_snap = _load_snapshot_by_id(args.prev_universe)
    else:
        prev_snap = _load_prev_snapshot(args.topic, snap.universe_id)
    if prev_snap is not None:
        outside_before = set(prev_snap.found_relevant_outside())
    else:
        print("⚠️  未找到上一版 snapshot——recovered_previous_gaps 显示 'N/A'"
              "（首次构建无比较基准，属正常）")

    # 每篇漏网的 channel 归属（去重计数用：同一篇可被多 channel 兜住）
    gap_owners: dict[str, list[str]] = {}
    for ch, ids in channel_papers.items():
        for w in (ids & outside_before):
            gap_owners.setdefault(w, []).append(ch)

    print()
    print("Channel × unique × known-relevant-hits × recovered-gaps（相对上一版）:")
    print(f"  {'Channel':<20} {'unique':>8} {'knownRel':>9} {'recovGap':>9} {'exclRecov':>9}")
    for ch in sorted(contrib):
        ids = channel_papers.get(ch, set())
        unique_n = contrib[ch]
        kr_hits = len(ids & kr)
        recov = len(ids & outside_before) if prev_snap is not None else "N/A"
        excl = sum(1 for w, owners in gap_owners.items()
                   if owners == [ch]) if prev_snap is not None else "N/A"
        print(f"  {ch:<20} {unique_n:>8} {kr_hits:>9} {recov!s:>9} {excl!s:>9}")
    if prev_snap is not None and gap_owners:
        print()
        print("被救回漏网的 channel 归属（一篇可被多 channel 兜住）:")
        for w in sorted(gap_owners):
            print(f"  {w}: {', '.join(gap_owners[w])}")
    print()
    print(f"invariant ⑧: {AGENT_KNOWLEDGE_MAY_EXPAND_NEVER_CONTRACT}")

    # credit 消耗（OpenAlex 2025+ credit 制：singleton=1, list=10 credits/请求）
    cs = backend.credit_summary()
    print()
    print(f"本次 OpenAlex credits 消耗估算: {cs['total']} "
          f"(singleton={cs['singleton']}, list={cs['list']}×10)"
          f"{' — 缓存命中不消耗' if cs['total'] == 0 else ''}")

    path = args.save_path or os.path.join(
        BASE, "data", "exports", "completeness_universes.json")
    save_snapshot(snap, path)
    print(f"\n✓ snapshot 已保存: {path}")
    print()
    print("下一步：")
    print(f"  python tools/audit_completeness.py --topic {args.topic} --create "
          f"--sample-size 500 --confidence 0.95 --target-recall 0.95 --seed 42")


if __name__ == "__main__":
    main()
