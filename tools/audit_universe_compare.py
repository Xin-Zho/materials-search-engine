"""tools/audit_universe_compare.py — v3 vs v4a 离线对比（用户定 2026-08-27）。

回答：v4a 的泛化提升到底发生在 DEV 还是 HOLDOUT？每个 channel 贡献多少？
每条新增 lexical query 的 ExpansionCost 是多少？

不调用 OpenAlex（纯离线）：
  v3 baseline  —— DEV-only 重建（v3 query_hits + 缓存 BACKWARD/FORWARD(DEV seeds)，
                  与 audit_universe_holdout.py 的 U_dev 一致）
  v4a          —— 真实 DEV-only 构建（snapshot paper_ids + 完整 channel_papers）

输出：
  A. v3 vs v4a 主表（DEV/HOLDOUT containment、universe size、outside 数）
  B. v4a 每 channel：DEV hits / DEV recovered / HOLDOUT hits / HOLDOUT recovered
     （recovered 相对 v3 DEV-only baseline 的 outside 集合）
  C. v4a 新增 16 query 的 ExpansionCost = exclusive_added / DEV_gap_recovered
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

CACHE_PATH = os.path.join(BASE, "data", "cache", "openalex_cache.json")
SNAP_PATH = os.path.join(BASE, "data", "exports", "completeness_universes.json")
SPLIT_PATH = os.path.join(BASE, "data", "exports", "pc_001_dev_holdout.json")
SEED_WORK_RE = re.compile(r"https://api\.openalex\.org/works/(W\d+)\?\{\}")
CITES_RE = re.compile(r'"filter": "cites:(W\d+)"')


def norm(w: str) -> str:
    m = re.search(r"(W\d+)", w or "")
    return m.group(1) if m else (w or "").strip()


def load_split():
    d = json.load(open(SPLIT_PATH, encoding="utf-8"))
    dev = set(d["dev"])
    holdout = set(d["engineering_holdout"])
    return dev, holdout, dev | holdout


def v3_dev_only_baseline(cache: dict, v3: dict, dev: set[str]) -> dict:
    """v3 的 DEV-only 重建（与 audit_universe_holdout.py 一致）。
    U = CORE(v3 query_hits) ∪ SUPP(v3 qh) ∪ BACKWARD(DEV seeds) ∪ FORWARD(DEV seeds)。
    """
    core_supp: set[str] = set()
    for k, v in v3["source_breakdown"]["query_hits"].items():
        if k.startswith(("BACKWARD", "FORWARD", "REVIEW")):
            continue
        core_supp |= {norm(x) for x in v}
    seed_works = {}
    for k, v in cache.items():
        m = SEED_WORK_RE.match(k)
        if m and isinstance(v, dict):
            seed_works[norm(m.group(1))] = v
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
        if m.group(1) not in dev:
            continue
        for w in v.get("results", []):
            wid = norm((w or {}).get("id", ""))
            if wid:
                forward.add(wid)
    u = core_supp | backward | forward
    return {"U": u, "CORE_SUPP": core_supp, "BACKWARD": backward, "FORWARD": forward}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--v3-idx", type=int, default=1)
    ap.add_argument("--v4-idx", type=int, default=2)
    args = ap.parse_args()

    snaps = json.load(open(SNAP_PATH, encoding="utf-8"))
    v3, v4 = snaps[args.v3_idx], snaps[args.v4_idx]
    dev, holdout, found = load_split()
    cache = json.load(open(CACHE_PATH, encoding="utf-8"))

    # ── v3 DEV-only baseline ──
    base = v3_dev_only_baseline(cache, v3, dev)
    u3 = base["U"]
    dev3_in = dev & u3
    hold3_in = holdout & u3
    dev3_out = dev - u3
    hold3_out = holdout - u3

    # ── v4a 真实 ──
    u4 = set(v4["paper_ids"])
    cp4 = {ch: set(ids) for ch, ids in v4["source_breakdown"]["channel_papers"].items()}
    dev4_in = dev & u4
    hold4_in = holdout & u4

    print("=" * 74)
    print("A. v3 (DEV-only baseline)  vs  v4a (DEV-only build)")
    print("=" * 74)
    print(f"  {'':<24}{'v3':>12}{'v4a':>12}")
    print(f"  {'Universe size':<24}{len(u3):>12}{len(u4):>12}")
    print(f"  {'DEV containment':<24}{len(dev3_in):>6}/{len(dev):<5}{len(dev4_in):>6}/{len(dev):<5}")
    print(f"  {'HOLDOUT containment':<24}{len(hold3_in):>6}/{len(holdout):<5}{len(hold4_in):>6}/{len(holdout):<5}")
    print(f"  {'DEV outside':<24}{len(dev3_out):>12}{len(dev - u4):>12}")
    print(f"  {'HOLDOUT outside':<24}{len(hold3_out):>12}{len(holdout - u4):>12}")
    print(f"  DEV containment %      = {len(dev3_in)/len(dev)*100:.1f}%  vs  {len(dev4_in)/len(dev)*100:.1f}%")
    print(f"  HOLDOUT containment % = {len(hold3_in)/len(holdout)*100:.1f}%  vs  {len(hold4_in)/len(holdout)*100:.1f}%")
    d_dev_gain = len(dev4_in) - len(dev3_in)
    d_hold_gain = len(hold4_in) - len(hold3_in)
    print(f"  净变化: DEV {d_dev_gain:+d}  HOLDOUT {d_hold_gain:+d}  Universe {len(u4)-len(u3):+d}")
    print(f"  解读: DEV↑&HOLDOUT↑=泛化成功 | DEV↑&HOLDOUT→=DEV过拟合 | 都低→进 v4b")

    # ── B. v4a 每 channel ──
    print()
    print("=" * 74)
    print("B. v4a 每 channel 贡献（recovered = 相对 v3 DEV-only baseline 的 outside）")
    print("=" * 74)
    print(f"  {'Channel':<22}{'unique':>8}{'DEV_hits':>10}{'DEV_recov':>10}"
          f"{'HOLD_hits':>11}{'HOLD_recov':>11}")
    for ch in ("CORE_UMBRELLA", "SUPPLEMENTAL_ROUTE", "BACKWARD_CITATION",
               "FORWARD_CITATION", "REVIEW_REFERENCE"):
        ids = cp4.get(ch, set())
        if not ids:
            continue
        dev_hits = ids & dev
        dev_recov = ids & dev3_out
        hold_hits = ids & holdout
        hold_recov = ids & hold3_out
        print(f"  {ch:<22}{len(ids):>8}{len(dev_hits):>10}{len(dev_recov):>10}"
              f"{len(hold_hits):>11}{len(hold_recov):>11}")
    print()
    print("  每篇 DEV/HOLDOUT 救回的 channel 归属:")
    all_recovered = sorted((dev3_out - (dev - u4)) | (hold3_out - (holdout - u4)))
    for w in all_recovered:
        chs = [ch for ch, ids in cp4.items() if w in ids]
        tag = "DEV" if w in dev else "HOLD"
        print(f"    {tag} {w}: {', '.join(chs) or '—'}")

    # ── C. 新增 query 的 ExpansionCost ──
    print()
    print("=" * 74)
    print("C. v4a 新增 lexical query 的 ExpansionCost")
    print("    Cost(q) = exclusive_added(q) / DEV_gap_recovered(q)")
    print("    （DEV_gap_recovered 相对 v3 DEV-only baseline 的 19 篇 outside）")
    print("=" * 74)
    qh4 = v4["source_breakdown"]["query_hits"]
    # 找新增 query（v4 有 v3 没有的文本 query）
    v3_queries = set(v3["source_breakdown"]["query_hits"].keys())
    new_queries = [q for q in qh4 if q not in v3_queries
                   and not q.startswith(("BACKWARD", "FORWARD", "REVIEW"))]
    # citation channels 全量（任何 query 的 marginal 都要排除它们）
    citation_all: set[str] = set()
    for ch, ids in cp4.items():
        if ch != "CORE_UMBRELLA" and ch != "SUPPLEMENTAL_ROUTE":
            citation_all |= ids
    print(f"  {'query':<55}{'excl_added':>11}{'DEV_gap':>9}{'HOLD_gap':>10}{'cost(DEV)':>11}")
    for q in sorted(new_queries):
        ids = set(qh4.get(q, []))
        others: set[str] = citation_all | set()
        for q2 in new_queries:
            if q2 != q:
                others |= set(qh4.get(q2, []))
        # 排除同 channel 旧 query（v3 已有的同族 query 不算 marginal 的对照）
        excl = ids - others
        dev_gap = ids & dev3_out
        hold_gap = ids & hold3_out
        cost = (len(excl) / len(dev_gap)) if dev_gap else float("inf")
        cost_s = f"{cost:.0f}" if dev_gap else "∞"
        print(f"  {q[:55]:<55}{len(excl):>11}{len(dev_gap):>9}{len(hold_gap):>10}{cost_s:>11}")
    print("  （excl_added = 该 query 独有新增，相对其他新增 query + citation channels；")
    print("   cost = excl_added / DEV_gap_recovered，HOLDOUT 只观察不计 cost）")


if __name__ == "__main__":
    main()
