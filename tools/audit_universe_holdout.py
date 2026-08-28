"""tools/audit_universe_holdout.py — DEV/HOLDOUT 泛化诊断（用户定 2026-08-27）。

方法学问题：拿 67 篇 known relevant 一路调到 containment 100% 只是训练集
accuracy。Universe 构建必须验证泛化能力——对"没参与构网"的论文的兜底能力。

做法：
  ① 67 篇按 route 分层 → DEV(~45) / HOLDOUT(~22)
     HOLDOUT 不参与 query 设计、不作为 citation seed（后续构建 v4 时强制）
  ② 纯离线重建 U_dev = CORE ∪ SUPPLEMENTAL ∪ BACKWARD(DEV seeds)
     ∪ FORWARD(DEV seeds)（缓存已含全部 seed work 引用关系与 cites 分页，
     无需重跑网络——invariant：CORE/SUPP 与 seeds 无关，只有 citation 通道变）
  ③ HoldoutContainment = |HOLDOUT ∩ U_dev| / |HOLDOUT|
     对照 DEVContainment = |DEV ∩ U_dev| / |DEV|
     解读：DEV≈100%（seeds 自证）+ HOLDOUT 高 → 有泛化能力；
           DEV≈100% + HOLDOUT 低 → 只是拟合已知答案。

分层：canonical_route 主标签分组，组内按 seed 分层随机取 ~1/3 进 HOLDOUT
（保证每个 route 都有留出代表）。无 route 标签的篇独立成组随机。
固定 --seed 可复现。
"""
import argparse
import json
import os
import random
import re
import sqlite3
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CACHE_PATH = os.path.join(BASE, "data", "cache", "openalex_cache.json")
SNAP_PATH = os.path.join(BASE, "data", "exports", "completeness_universes.json")
DB_PATH = os.path.join(BASE, "data", "cache", "knowledge_base.db")
SEED_WORK_RE = re.compile(r"https://api\.openalex\.org/works/(W\d+)\?\{\}")
CITES_RE = re.compile(r'"filter": "cites:(W\d+)"')


def norm(w: str) -> str:
    m = re.search(r"(W\d+)", w or "")
    return m.group(1) if m else (w or "").strip()


def load_routes() -> dict:
    """paper_id → 主 route（canonical_route 首个）。"""
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT paper_id, canonical_route FROM route_mechanism_edges "
            "WHERE canonical_route IS NOT NULL AND canonical_route != '' "
            "ORDER BY id").fetchall()
    finally:
        con.close()
    routes: dict[str, str] = {}
    for pid, route in rows:
        w = norm(pid)
        if w and w not in routes:
            routes[w] = route
    return routes


def stratified_split(found: list[str], routes: dict, holdout_frac: float,
                     seed: int) -> tuple[list[str], list[str]]:
    """按主 route 分层：组内随机取 ~1/3 进 HOLDOUT，保证 route 代表性。"""
    rng = random.Random(seed)
    groups: dict[str, list[str]] = {}
    for w in found:
        groups.setdefault(routes.get(w, "NO_ROUTE"), []).append(w)
    dev, holdout = [], []
    for route, members in sorted(groups.items()):
        members = list(members)
        rng.shuffle(members)
        n_hold = max(1, round(len(members) * holdout_frac)) if len(members) > 1 else 0
        # 单篇 route 全进 DEV（HOLDOUT 单点无统计意义）
        n_hold = min(n_hold, len(members) - 1) if len(members) > 1 else 0
        holdout.extend(members[:n_hold])
        dev.extend(members[n_hold:])
    return dev, holdout


def rebuild_u_dev(cache: dict, dev: list[str]) -> dict:
    """U_dev = CORE ∪ SUPP ∪ BACKWARD(DEV) ∪ FORWARD(DEV)，纯离线。"""
    # CORE/SUPP：与 seeds 无关（从 v3 query_hits 重建）
    snaps = json.load(open(SNAP_PATH, encoding="utf-8"))
    v3 = snaps[1]
    core_supp: set[str] = set()
    for k, v in v3["source_breakdown"]["query_hits"].items():
        if k.startswith("BACKWARD") or k.startswith("FORWARD") or k.startswith("REVIEW"):
            continue
        core_supp |= {norm(x) for x in v}
    # seed works 缓存
    seed_works = {}
    for k, v in cache.items():
        m = SEED_WORK_RE.match(k)
        if m and isinstance(v, dict):
            seed_works[norm(m.group(1))] = v
    dev_set = set(dev)
    backward: set[str] = set()
    for seed in dev:
        work = seed_works.get(seed)
        if work and work.get("referenced_works"):
            backward |= {norm(x) for x in work["referenced_works"]}
    forward: set[str] = set()
    for k, v in cache.items():
        m = CITES_RE.search(k)
        if not m or not isinstance(v, dict):
            continue
        if m.group(1) not in dev_set:
            continue
        for w in v.get("results", []):
            wid = norm((w or {}).get("id", ""))
            if wid:
                forward.add(wid)
    u_dev = core_supp | backward | forward
    return {"U_DEV": u_dev, "CORE_SUPP": core_supp,
            "BACKWARD": backward, "FORWARD": forward}


def _title_from_cache(cache: dict, w: str) -> str:
    """从缓存 work 对象取标题（seed work / cites 结果 / refs 结果）。"""
    for k, v in cache.items():
        if not isinstance(v, dict):
            continue
        if isinstance(v.get("results"), list):
            for r in v["results"]:
                if norm((r or {}).get("id", "")) == w:
                    return r.get("title") or ""
        elif norm((v.get("id") or "")) == w:
            return v.get("title") or ""
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--holdout-frac", type=float, default=1 / 3)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--save-split", default="",
                    help="保存 split 文件路径（默认 data/exports/pc_001_dev_holdout.json）")
    ap.add_argument("--dev-failures", action="store_true",
                    help="输出 DEV_outside 的 failure classification（离线，按引用连通性）")
    args = ap.parse_args()

    cache = json.load(open(CACHE_PATH, encoding="utf-8"))
    snaps = json.load(open(SNAP_PATH, encoding="utf-8"))
    v3 = snaps[1]
    found = sorted({norm(x) for x in v3["source_breakdown"]["found_relevant"]})
    routes = load_routes()

    print("=" * 72)
    print(f"known relevant = {len(found)} | route 标签覆盖 = "
          f"{sum(1 for w in found if w in routes)}/{len(found)}")
    from collections import Counter
    print("route 分布:", dict(Counter(routes.get(w, "NO_ROUTE") for w in found)))
    print("=" * 72)

    dev, holdout = stratified_split(found, routes, args.holdout_frac, args.seed)
    dev, holdout = sorted(dev), sorted(holdout)
    print(f"\nsplit (seed={args.seed}, holdout_frac={args.holdout_frac}):")
    print(f"  DEV     = {len(dev)}")
    print(f"  HOLDOUT = {len(holdout)}")
    print("  HOLDOUT route 分布:",
          dict(Counter(routes.get(w, "NO_ROUTE") for w in holdout)))

    # 离线重建 U_dev 并测 containment
    rebuilt = rebuild_u_dev(cache, dev)
    u_dev = rebuilt["U_DEV"]
    cur_ids = set(v3["paper_ids"])
    dev_in = sum(1 for w in dev if w in u_dev)
    hold_in = sum(1 for w in holdout if w in u_dev)
    print()
    print(f"[U_dev 重建] |U_dev| = {len(u_dev)}"
          f" (CORE+SUPP={len(rebuilt['CORE_SUPP'])}, "
          f"BACKWARD(DEV)={len(rebuilt['BACKWARD'])}, "
          f"FORWARD(DEV)={len(rebuilt['FORWARD'])})")
    print(f"  DEVContainment     = {dev_in}/{len(dev)} = {dev_in/len(dev)*100:.1f}%"
          f"  ← seeds 自证（参考）")
    print(f"  HoldoutContainment = {hold_in}/{len(holdout)} = {hold_in/len(holdout)*100:.1f}%"
          f"  ← 泛化能力")
    print(f"  （对照：v3 全 67 篇 containment = {len(set(found) & cur_ids)}/67 = "
          f"{len(set(found) & cur_ids)/67*100:.1f}%）")
    print()
    print("HOLDOUT 逐篇（在 U_dev 里? 被哪个通道兜住）:")
    for w in holdout:
        chs = [ch for ch in ("CORE_SUPP", "BACKWARD", "FORWARD")
               if w in rebuilt[ch]]
        tag = "IN" if w in u_dev else "OUT"
        print(f"  {w}: {tag}  {'/'.join(chs) if chs else '—'}")

    # 保存 split 文件（后续 v4 构建强制 DEV-only seeds）
    if args.save_split:
        path = args.save_split or os.path.join(
            BASE, "data", "exports", "pc_001_dev_holdout.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"topic_id": "pc_001", "seed": args.seed,
                       "holdout_frac": args.holdout_frac,
                       "dev": dev, "holdout": holdout,
                       "routes": routes,
                       "created_at": "2026-08-27"}, f, ensure_ascii=False, indent=2)
        print(f"\n✓ split 已保存: {path}（后续构建 v4 时用 --citation-seeds 文件强制 DEV-only）")

    # ── DEV failure classification（P0-2 前置，用户 2026-08-27 定）──
    # 只基于 DEV_outside 分类；分类依据：是否被 DEV-only 引用网络兜住
    #   QUERY_LEXICAL_GAP      —— 引用连得上（BACKWARD/FORWARD 任一），词面是唯一缺口
    #   CITATION_DISCONNECTED  —— 引用 1-hop 断开（词面+引用都够不着）
    # 标题从缓存 work 对象取（--dev-failures 时补充打印 title，便于设计 query family）。
    if args.dev_failures:
        dev_set = set(dev)
        dev_outside = sorted(set(dev) - u_dev)
        print("\n" + "=" * 72)
        print(f"[DEV failure] DEV={len(dev)}  in={dev_in}  outside={len(dev_outside)}")
        print("=" * 72)
        for w in dev_outside:
            conn_b = w in rebuilt["BACKWARD"]
            conn_f = w in rebuilt["FORWARD"]
            cls = ("QUERY_LEXICAL_GAP" if (conn_b or conn_f)
                   else "CITATION_DISCONNECTED")
            title = _title_from_cache(cache, w)
            print(f"  {w}: {cls:<22} BACKWARD={'连' if conn_b else '断'} "
                  f"FORWARD={'连' if conn_f else '断'}  {title[:64]!r}")
        from collections import Counter
        cnt = Counter(
            ("QUERY_LEXICAL_GAP" if
             (w in rebuilt["BACKWARD"] or w in rebuilt["FORWARD"])
             else "CITATION_DISCONNECTED") for w in dev_outside)
        print("  类别汇总:", dict(cnt))


if __name__ == "__main__":
    main()
