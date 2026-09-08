"""tools/run_search_s4.py — Search S4 正式执行（2026-08-30 用户冻结口径）。

S4_SEEN = S3_SEEN ∪ CitationExpansion(64 seeds) ∪ QueryResults(18 queries)
  Citation: 64 frozen seeds（P2_DIVERSE_64）→ backward ∪ forward 1-hop → 全部 canonical candidates
  Query:    18 frozen queries → depth=1000, skip_cache=True（防 depth 缓存串味）

纪律（用户定 2026-08-30，写死不可改）：
- 只读冻结配置 s4_final_config.json（status=FROZEN）；**绝不读 residual miss 决定保留哪些结果**
- 不用 miss-specific set cover 选 citation seeds；query 按 quality gate 冻结（非 R03 recovery）
- R03 dev diagnostic（37 residual）仅作 DEVELOPMENT_ONLY 输出，不参与正式决策
- 正式执行结束只宣布 Search S4 snapshot = FROZEN / S4_SEEN_SET = N，
  然后进入 fresh R04（R04 才能回答 S4 真实独立 recall）

方向语义（与 QA 一致，避免 pilot 历史命名混乱）：
  BACKWARD = candidate 引用 seed（candidate.referenced_works ∋ seed）
  FORWARD  = seed 引用 candidate（seed.referenced_works ∋ candidate）

输出：
  s4_candidate_snapshot.json / s4_seen_set.json / s4_citation_records.json /
  s4_query_records.json / s4_delta_vs_s3.json

用法：
  python tools/run_search_s4.py --plan-only         # 冻结校验（不发送不写盘）
  python tools/run_search_s4.py                     # 正式执行（citation 本地 + query engine）
  python tools/run_search_s4.py --skip-queries      # 只跑 citation 层（断点/诊断）
"""
import argparse
import asyncio
import datetime
import hashlib
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tools"))

from pilot_round3_query_utility import (  # noqa: E402
    IdentityResolver, build_r_old, connect_cache_ro, paper_key_and_info,
)
from build_r02_seen import _norm_doi, _norm_title  # noqa: E402
from citation_reachability_audit import load_oa  # noqa: E402
from build_r03_seen import build_s3_found_sets  # noqa: E402
from run_search_s1 import is_usable  # noqa: E402

T = os.path.join(BASE, "data", "exports", "terminology")
CONFIG = os.path.join(T, "s4_final_config.json")
S3_SEEN = os.path.join(T, "s3_seen_set.json")
DEFAULT_SNAP = os.path.join(T, "s4_candidate_snapshot.json")
DEFAULT_SEEN = os.path.join(T, "s4_seen_set.json")
DEFAULT_CIT_REC = os.path.join(T, "s4_citation_records.json")
DEFAULT_Q_REC = os.path.join(T, "s4_query_records.json")
DEFAULT_DELTA = os.path.join(T, "s4_delta_vs_s3.json")
DEFAULT_DEPTH = 1000

CIT_EXPECTED = 64
Q_EXPECTED = 18
S3_EXPECTED = 13430


def config_hash(cfg: dict) -> str:
    """对冻结的 citation/query action 关键字段计算 sha256（防篡改）。

    S4 的 citation actions 无 pilot 新值（new_vs_S3=None），hash 用
    (action_id, seed_wid, direction)；query 用 (action_id, query_string, new_vs_S3)。
    """
    cit = [(a["action_id"], a["seed_wid"], a["direction"], a.get("new_vs_S3"))
           for a in cfg["citation_actions"]]
    q = [(a["action_id"], a["query_string"], a.get("new_vs_S3"))
         for a in cfg["query_actions"]]
    blob = json.dumps({"citation": cit, "query": q}, sort_keys=True,
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def main():
    ap = argparse.ArgumentParser(description="Search S4 正式执行（64 citation + 18 query）")
    ap.add_argument("--config", default=CONFIG)
    ap.add_argument("--s3-seen", default=S3_SEEN)
    ap.add_argument("--depth", type=int, default=DEFAULT_DEPTH)
    ap.add_argument("--snapshot", default=DEFAULT_SNAP)
    ap.add_argument("--seen-set", default=DEFAULT_SEEN)
    ap.add_argument("--citation-records", default=DEFAULT_CIT_REC)
    ap.add_argument("--query-records", default=DEFAULT_Q_REC)
    ap.add_argument("--delta", default=DEFAULT_DELTA)
    ap.add_argument("--engine-data-dir", default="data")
    ap.add_argument("--skip-queries", action="store_true",
                    help="只跑 citation 层（断点/诊断，不写正式 snapshot）")
    ap.add_argument("--plan-only", action="store_true")
    args = ap.parse_args()

    # ── 冻结校验（用户定，写死）──
    cfg = json.load(open(args.config, encoding="utf-8"))
    assert cfg["status"] == "FROZEN", "s4_final_config 未 FROZEN"
    cit_acts = cfg["citation_actions"]
    q_acts = cfg["query_actions"]
    assert len(cit_acts) == CIT_EXPECTED, f"citation actions != {CIT_EXPECTED}"
    assert len(q_acts) == Q_EXPECTED, f"query actions != {Q_EXPECTED}"
    assert args.depth == 1000, "S4 正式 depth 必须 = 1000（基础设施参数冻结）"
    ch = config_hash(cfg)
    s3_keys = set(json.load(open(args.s3_seen, encoding="utf-8"))["keys"])
    assert len(s3_keys) == S3_EXPECTED, f"S3 seen != {S3_EXPECTED}（实测 {len(s3_keys)}）"

    print("=" * 78)
    print("Search S4 正式执行（冻结口径校验通过）")
    print("=" * 78)
    print(f"S4 citation actions = {len(cit_acts)}（P2_DIVERSE_64，双向 1-hop 展开）")
    print(f"S4 query actions    = {len(q_acts)}（quality gate 冻结）")
    print(f"query depth         = {args.depth}")
    print(f"base S3 seen        = {len(s3_keys)}")
    print(f"config hash         = {ch[:16]}...")
    print(f"development_source  = AUDIT_R03（R03 已 closed development data）")

    oa = load_oa()
    print(f"openalex meta       = {len(oa)} works（citation 1-hop 数据源；"
          f"forward 依赖缓存覆盖度——已知限制，与 S3 同源）")

    if args.plan_only:
        print("\n[plan-only] 冻结校验通过，将执行（不发送不写盘）：")
        print("  CITATION:")
        for a in cit_acts:
            print(f"    {a['action_id']:<10} seed={a['seed_wid']} dir={a['direction']}")
        print("  QUERY (depth=1000, skip_cache=True):")
        for q in q_acts:
            print(f"    {q['action_id']:<18} {q['query_string'][:72]}")
        print("\n[plan-only] 结束：未发送任何请求，未写盘。")
        return

    # ── Citation 层（本地，openalex_cache；backward ∪ forward 1-hop）──
    s3_found = build_s3_found_sets()   # S3 三通道（eids/dois/titles）——new_vs_s3 判定口径
    cit_records, cit_keys, cit_seen_map, cit_identity_unknown, cit_prov_count = (
        run_citation_layer(cit_acts, oa, s3_found))

    # ── Query 层（engine，depth=1000 skip_cache=True）──
    q_rows_by_query = {}
    if args.skip_queries:
        print("[WARN] --skip-queries：本运行只产出 citation 诊断，不写正式 snapshot")
    else:
        q_rows_by_query = asyncio.run(run_query_layer(q_acts, args, s3_keys))

    # ── 汇总 ──
    q_keys = set()
    for rows in q_rows_by_query.values():
        for r in rows:
            if r.get("key"):
                q_keys.add(r["key"])
    # citation 新于 S3 = 三通道判定未见的（其 key 才加入 S4_SEEN；已见论文由 S3 的
    # EID key 代表，citation key 只在 records 里留 provenance，避免同一论文双 key）
    # 2026-08-30 修复：三通道（dois/titles）对 title/doi 缺失的论文覆盖不全（WID-only
    # 论文）→ 同一论文会以 WID/DOI key 重复计入并撞 S3 canonical keys（实测 28 个）。
    # 追加 key 级过滤 k not in s3_keys：S3 已收录的 key 一律不计入 S4 增量。
    cit_new_keys = {k for k in cit_keys
                    if not cit_seen_map.get(k, False) and k not in s3_keys}
    key_overlap_excluded = len(cit_keys) - len(cit_new_keys) - \
        sum(1 for k in cit_keys if cit_seen_map.get(k, False))
    q_new = q_keys - s3_keys
    cit_q_overlap = cit_keys & q_keys
    s4_new = cit_new_keys | q_new
    s4_keys = s3_keys | s4_new

    aggregate = {
        "s3_seen_unique": len(s3_keys),
        "citation_union_unique": len(cit_keys),
        "citation_new_vs_s3": len(cit_new_keys),
        "citation_already_seen_s3": len(cit_keys) - len(cit_new_keys) - key_overlap_excluded,
        "citation_key_overlap_excluded": key_overlap_excluded,
        "query_union_unique": len(q_keys),
        "query_new_vs_s3": len(q_new),
        "citation_query_overlap": len(cit_q_overlap),
        "s4_new_vs_s3": len(s4_new),
        "search_s4_union_unique": len(s4_keys),
        "identity_unknown_total": cit_identity_unknown,
        "citation_provenance_edges": cit_prov_count,
    }
    # 硬校验（用户定）：|S4| == |S3| + |S4\S3|（key 级过滤保证 s3_keys ∩ s4_new = ∅）
    overlap = len(s3_keys & s4_new)
    assert overlap == 0, f"unexpected overlap with S3 keys: {overlap}"
    assert len(s4_keys) == len(s3_keys) + len(s4_new), \
        f"union invariant broken: {len(s4_keys)} != {len(s3_keys)} + {len(s4_new)}"
    print(f"\n[assert] search_s4_union_unique == {len(s3_keys)} + {len(s4_new)} "
          f"= {len(s3_keys) + len(s4_new)} ✓（key_overlap_excluded={key_overlap_excluded}）")

    snap_id = f"S4_{datetime.datetime.now():%Y%m%d_%H%M%S}_{ch[:8]}"
    now = datetime.datetime.now().isoformat(timespec="seconds")

    # R03 dev diagnostic（DEVELOPMENT_ONLY，写死自 config，不重算）
    dev = {
        "r03_dev_diagnostic": cfg.get("expected_dev_recovery"),
        "development_source": "AUDIT_R03",
        "DEVELOPMENT_ONLY": True,
        "note": "S4 不用 miss-specific set cover / R03 dev recovery 决策；"
                "formal 执行器绝不读 residual miss 决定保留哪些结果；"
                "S4 真实独立 recall 由 fresh R04 判定",
    }

    delta = {
        "s3_seen": len(s3_keys), "s4_seen": len(s4_keys),
        "citation_new_vs_s3": len(cit_new_keys),
        "query_new_vs_s3": len(q_new),
        "s4_new_vs_s3": len(s4_new),
        "s3_only": len(s3_keys - s4_keys),
        "note": "S4 = S3 ∪ CitationExpansion(64, P2_DIVERSE_64) ∪ QueryResults(18)；"
                "S3_SEEN 保留冻结不动",
    }

    snapshot = {
        "search_snapshot_id": snap_id,
        "version": "S4", "status": "FROZEN",
        "config_file": os.path.basename(args.config),
        "config_hash": ch,
        "retrieval_budget": {"query_depth": args.depth, "pagination": "Scopus cursor 25/page",
                             "citation_hop": 1, "citation_direction": "BACKWARD ∪ FORWARD",
                             "citation_note": "全部 canonical candidates 入 S4_SEEN；"
                                              "forward 依赖 openalex cache 覆盖度"},
        "execution_timestamp": now,
        "execution_mode": ("citation_only" if args.skip_queries else "citation+engine"),
        "development_source": "AUDIT_R03",
        "eligible_for_r04_evaluation": False,
        "identity_rule": "query: EID 优先 + DOI fallback；citation: DOI 主 key，无 DOI 用 WID"
                         "（R04 found sets 由 s4_citation_records/s4_query_records 三通道构建）",
        "aggregate": aggregate,
        "delta_vs_s3": delta,
        "r03_dev_diagnostic": dev,
        "direction_semantics": "BACKWARD = candidate 引用 seed；FORWARD = seed 引用 candidate"
                               "（QA 口径；pilot 历史命名相反，本文件统一）",
        "interpretation": "S4 是 quality-gate + diverse-policy 决策（防 development overfit："
                          "① 无 miss-specific set cover；② diverse seed policy；"
                          "③ query 按质量 gate 非 R03 recovery；④ community discovery 与 query "
                          "formulation 分离；⑤ 新候选多不自动视为好 search action）；"
                          "snapshot 冻结后不再改 82 actions；正式召回率必须 fresh R04",
    }
    seen_set = {
        "search_snapshot_id": snap_id, "config_hash": ch,
        "frozen_at": now,
        "definition": "SEARCH_S4_SEEN_UNION = S3_SEEN(13430) ∪ CitationExpansion(64, 1-hop 双向) "
                      "∪ QueryResults(18, depth=1000)；R04 agent_seen 唯一依据",
        "size": len(s4_keys), "keys": sorted(s4_keys),
    }
    for path, obj in ((args.snapshot, snapshot), (args.seen_set, seen_set),
                      (args.citation_records, {
                          "search_snapshot_id": snap_id, "config_hash": ch,
                          "frozen_at": now,
                          "direction_semantics": snapshot["direction_semantics"],
                          "n_records": len(cit_records),
                          "records": cit_records}),
                      (args.query_records, {
                          "search_snapshot_id": snap_id, "config_hash": ch,
                          "frozen_at": now,
                          "n_queries": len(q_rows_by_query),
                          "records_by_query": q_rows_by_query}),
                      (args.delta, {"search_snapshot_id": snap_id,
                                    "delta_vs_s3": delta,
                                    "r03_dev_diagnostic": dev})):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)

    print(f"\n[OK] snapshot          : {args.snapshot}")
    print(f"[OK] seen set          : {args.seen_set}")
    print(f"[OK] citation records  : {args.citation_records}")
    print(f"[OK] query records     : {args.query_records}")
    print(f"[OK] delta vs S3       : {args.delta}")
    print("\n=== S4 aggregate ===")
    for k, v in aggregate.items():
        print(f"  {k:<26} {v}")
    print("\n=== Search S4 = FROZEN ===")
    print(f"S4_SEEN_SET = {len(s4_keys)}")
    print("→ 下一步：fresh R04（universe → exclude R01+R02+R03 samples → SRS → "
          "blind relevance → S4 seen join）")


# ──────────────────────────────────────────────────────────────
# Citation 层
# ──────────────────────────────────────────────────────────────
def run_citation_layer(actions: list[dict], oa: dict, s3_found: dict):
    """64 seeds → backward ∪ forward 1-hop → 全部 canonical candidates + provenance。

    方向（QA 口径）：
      FORWARD  = seed 引用 candidate（candidate ∈ seed.referenced_works）
      BACKWARD = candidate 引用 seed（seed ∈ candidate.referenced_works）
    key：DOI 主（_norm_doi）；无 DOI → WID 本身（identity 来源标 WID-only）。
    provenance 保留多来源（同一 candidate 被多个 seed 命中时数组累计）。
    already_seen_s3：候选论文在 S3 三通道（eids/dois/titles）中是否已见——
      S3 canonical keys 是 Scopus EID（DOI 仅 fallback），citation keys 是 DOI/WID，
      两套 key 体系不可直接比较，new_vs_s3 必须用 found sets 判定（与 R04 seen 同口径）。
    """
    records = {}          # key -> record
    seen_map = {}         # key -> already_seen_s3
    prov_count = 0        # (key, seed) 边数
    seed_action = {a["seed_wid"]: a for a in actions}   # 64 seeds 唯一
    for act in actions:
        seed = act["seed_wid"]
        m = oa.get(seed)
        if not m:
            print(f"  [WARN] {act['action_id']} seed {seed} 不在 oa 元数据")
            continue
        # FORWARD：seed 引用 candidate
        for c in m.get("referenced_works", []):
            add_candidate(records, seen_map, oa, act, seed, c, "FORWARD", s3_found)
            prov_count += 1
    # BACKWARD：candidate 引用 seed——一次遍历 oa + seed 倒排（64 seeds 时避免
    # oa.items() 遍历 64 次；命中集合与逐 seed 遍历完全一致，纯性能优化不改语义）
    for c, cm in oa.items():
        refs = cm.get("referenced_works", []) or []
        for s in refs:
            act = seed_action.get(s)
            if act is not None:
                add_candidate(records, seen_map, oa, act, s, c, "BACKWARD", s3_found)
                prov_count += 1
    n_unknown = sum(1 for r in records.values() if r["identity_unknown"])
    return list(records.values()), set(records.keys()), seen_map, n_unknown, prov_count


def add_candidate(records: dict, seen_map: dict, oa: dict, act: dict, seed: str,
                  cand: str, direction: str, s3_found: dict) -> None:
    cm = oa.get(cand, {})
    doi = _norm_doi(cm.get("doi"))
    key = doi or cand           # DOI 主 key；无 DOI 用 WID
    rec = records.get(key)
    if rec is None:
        # 三通道已见判定（与 R04 seen 同口径）：DOI 命中优先，无 DOI 用 title
        if doi and doi in s3_found["dois"]:
            already = True
        elif not doi and cm.get("title") and \
                _norm_title(cm.get("title")) in s3_found["titles"]:
            already = True
        else:
            already = False
        rec = {
            "key": key, "wid": cand, "doi": doi or None,
            "title": cm.get("title"),
            "identity_unknown": not bool(doi),
            "already_seen_s3": already,
            "provenance": [],
        }
        records[key] = rec
        seen_map[key] = already
    rec["provenance"].append({
        "action_id": act["action_id"], "seed_wid": seed,
        "direction": direction,
    })


# ──────────────────────────────────────────────────────────────
# Query 层（复用 S3 engine 模式，基准改 S3）
# ──────────────────────────────────────────────────────────────
async def run_query_layer(q_acts: list[dict], args, s3_keys: set) -> dict:
    from search_engine.engine import ScopusSearchEngine
    resolver, r_old_keys = build_r_old()
    engine = ScopusSearchEngine(data_dir=args.engine_data_dir)
    await engine.start()
    out = {}
    try:
        for i, q in enumerate(q_acts, 1):
            try:
                # 强制 skip_cache：防 depth<1000 缓存命中（S1 depth=50 教训）
                res = await engine.search(q["query_string"], limit=args.depth,
                                          skip_cache=True)
            except Exception as e:
                print(f"    [WARN] {q['action_id']}: S4 检索失败 {e}")
                q["error"] = str(e)
                await asyncio.sleep(1)
                continue
            papers = res.papers
            total_hits = getattr(res, "total_count", len(papers))
            rows = []
            for p in papers:
                key, info = paper_key_and_info(p, resolver, r_old_keys)
                rows.append({"key": key, "eid": info["eid"], "doi": info["doi"],
                             "title": (getattr(p, "title", None) or "").strip(),
                             "usable": is_usable(
                                 (getattr(p, "title", None) or "").strip(),
                                 (getattr(p, "abstract", None) or "").strip(),
                                 bool(key))})
            keys = {r["key"] for r in rows if r["key"]}
            q.update({
                "total_hits": total_hits, "raw_returned": len(papers),
                "unique_returned": len(keys),
                "identity_unknown": sum(1 for r in rows if not r["key"]),
                "usable_returned": sum(1 for r in rows if r["usable"]),
                "new_vs_S3": len(keys - s3_keys),
                "depth_saturated": (len(papers) >= args.depth
                                    and total_hits > args.depth),
            })
            out[q["query_string"]] = rows
            cen = "C" if q["depth_saturated"] else " "
            print(f"  [{i}/{len(q_acts)}]{cen} {q['action_id']:<18} "
                  f"hits={total_hits:>6} raw={len(papers):>4} uniq={len(keys):>4} "
                  f"new_S3={len(keys - s3_keys):>4} {q['query_string'][:46]}")
            await asyncio.sleep(1)
    finally:
        await engine.close()
    return out


if __name__ == "__main__":
    main()
