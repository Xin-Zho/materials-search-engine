#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""S4 Step 6B/6D：Citation seed-policy benchmark（2026-08-30 用户定）。

目标：比较 seed-selection policy 的跨样本稳健性，而不是 dev recovery 最大化。
假设检验：
    Miss-specific set cover（P0）→ development overfitting
    diverse / representative policy（P2/P3）→ better fresh-audit generalization

Policies:
    P0  = S3 set-cover baseline（16 seeds，来自 s3_final_config）
    P1  = ALL（222 high-conf seeds 1-hop）—— recall/cost 上界
    P2  = Diverse-N（32/64/128）：community × year 分层确定性 round-robin 采样
    P3  = Representative：每 community cluster 选代表 seed（cited_by 最高）

指标（每 policy）：
    seed_count / new_candidates(new_vs_S3) / dev_recovery / recovery_rate
    diversity（seeds 覆盖 community 数、year 桶数）
    robustness（追回 miss 的 mean seed-support、min_support——防单 miss 特判）
    efficiency = recovery / new_candidates

LOCO（leave-one-cluster-out，development-time generalization check）：
    clusters = dental-measurement / holography-recording / 3dp-lithography / other
    对每 holdout C：
      P0'  = 在 train(37−C) misses 上 cost-aware set cover（复刻 S3 逻辑）→ 测 C recovery
      P2/P3 = 全量 policy 对 C 的直接 recovery（不依赖 miss → LOCO-safe）
    比较 P0' vs P2/P3 的 holdout 泛化。

输出：s4_repair_policy_pilot.json（policies 比较 + LOCO 表 + selected seeds + per-miss）
"""

import json
import os
import sys
from collections import defaultdict

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TERM = os.path.join(BASE, "data", "exports", "terminology")
LABELS = os.path.join(BASE, "data", "exports", "completeness_labels",
                      "pc_001__20260830011713_filled.json")
LABELS_DL = os.path.join(os.path.expanduser("~"), "Downloads",
                         "pc_001__20260830011713_filled.json")
RESIDUAL = os.path.join(TERM, "s4_residual_misses.json")
COMM = os.path.join(TERM, "s4_community_discovery.json")
CFG = os.path.join(TERM, "s3_final_config.json")
REACH = os.path.join(TERM, "s4_citation_reachability.json")
OUT = os.path.join(TERM, "s4_repair_policy_pilot.json")

sys.path.insert(0, os.path.join(BASE, "tools"))
from citation_reachability_audit import load_oa  # noqa: E402
from community_discovery import COMMUNITY_DEF  # noqa: E402
from build_r02_seen import _norm_doi, _norm_title  # noqa: E402

YEAR_BUCKETS = [("pre2000", 0, 1999), ("2000s", 2000, 2009), ("2010s", 2010, 2019),
                ("2020s", 2020, 2100)]


def year_bucket(y):
    if not y:
        return "unknown"
    for name, lo, hi in YEAR_BUCKETS:
        if lo <= y <= hi:
            return name
    return "unknown"


def community_of(title: str) -> str:
    t = (title or "").lower()
    for cid, cf in COMMUNITY_DEF.items():
        if any(k in t for k in cf["kw"]):
            return cid
    return "other"


def build_incoming(oa: dict) -> dict:
    """cited_by 索引：ref_wid -> set(citer_wids)。"""
    inc: dict[str, set] = defaultdict(set)
    for wid, m in oa.items():
        for r in m.get("referenced_works", []):
            inc[r].add(wid)
    return inc


def seed_meta(seed: set, oa: dict) -> dict:
    meta = {}
    for w in seed:
        m = oa.get(w, {})
        meta[w] = {
            "wid": w, "title": m.get("title"),
            "year": m.get("publication_year"),
            "refs": len(m.get("referenced_works", [])),
            "cited_by": m.get("cited_by_count") or 0,
            "community": community_of(m.get("title")),
            "year_bucket": year_bucket(m.get("publication_year")),
        }
    return meta


def expand(seed_set: set, oa: dict, inc: dict) -> dict:
    """1-hop 双向展开 → per-seed candidate WID 集。"""
    per_seed: dict[str, set] = {}
    for w in seed_set:
        m = oa.get(w, {})
        cands = set(m.get("referenced_works", []))       # FORWARD：seed 引用
        cands |= inc.get(w, set())                        # BACKWARD：引用 seed
        per_seed[w] = cands
    return per_seed


def load_miss_meta() -> tuple[list, dict]:
    res = json.load(open(RESIDUAL, encoding="utf-8"))
    misses = [m for m in res["misses"] if m["wid"] != "W7110794929"]
    # cluster 标签（dental/holography/3dp 用 community_discovery members，其余 other）
    comm = json.load(open(COMM, encoding="utf-8"))
    cluster_of: dict[str, str] = {}
    for c in comm["communities"]:
        for m in c["members"]:
            cluster_of[m] = c["community_id"]
    miss_meta = {}
    for m in misses:
        wid = m["wid"]
        miss_meta[wid] = {
            "wid": wid,
            "title": m.get("oa_title") or m.get("title"),
            "doi": _norm_doi(m.get("doi")),
            "cluster": cluster_of.get(wid, "other"),
        }
    return misses, miss_meta


def resolve_candidates(cands: set, oa: dict) -> list:
    """candidate WID 集 → (wid, doi, title) 列表。"""
    out = []
    for w in cands:
        m = oa.get(w, {})
        out.append((w, _norm_doi(m.get("doi")), m.get("title")))
    return out


def hit_miss(cands: list, miss: dict) -> bool:
    """候选是否命中 miss（DOI 主判 + title 兜底，与 seen join 同口径）。"""
    if miss["doi"]:
        for _, doi, _t in cands:
            if doi == miss["doi"]:
                return True
    if miss["title"]:
        nt = _norm_title(miss["title"])
        for _, _d, t in cands:
            if t and _norm_title(t) == nt:
                return True
    return False


def policy_stats(seed_list: list, per_seed: dict, oa: dict, found_s3: dict,
                 miss_meta: dict) -> dict:
    """给定 seed 列表，聚合 candidate 池并统计。"""
    cands_by_seed = {w: resolve_candidates(per_seed.get(w, set()), oa) for w in seed_list}
    # 全 candidate 池（去重 by wid）
    pool: dict[str, tuple] = {}
    seed_of: dict[str, set] = defaultdict(set)
    for w, cl in cands_by_seed.items():
        for cwid, doi, t in cl:
            pool.setdefault(cwid, (cwid, doi, t))
            seed_of[cwid].add(w)
    cands = list(pool.values())
    # new_vs_S3：DOI 主判 + title 兜底
    new_pool = []
    for cwid, doi, t in cands:
        if doi and doi in found_s3["dois"]:
            continue
        if not doi and t and _norm_title(t) in found_s3["titles"]:
            continue
        new_pool.append((cwid, doi, t))
    # dev recovery
    recovered = []
    for wid, mm in miss_meta.items():
        if hit_miss(cands, mm):
            recovered.append(wid)
    # robustness：追回 miss 的 seed-support
    supports = [len(seed_of.get(wid, set())) for wid in recovered]
    seed_communities = {oa.get(w, {}).get("title") and community_of(oa[w].get("title"))
                        for w in seed_list if w in oa}
    seed_years = {year_bucket(oa.get(w, {}).get("publication_year")) for w in seed_list}
    rec_clusters = {miss_meta[w]["cluster"] for w in recovered}
    return {
        "seed_count": len(seed_list),
        "candidate_pool": len(cands),
        "new_vs_S3": len(new_pool),
        "dev_recovery": len(recovered),
        "recovered_miss_ids": sorted(recovered),
        "recovery_rate": round(len(recovered) / len(miss_meta), 4),
        "efficiency": round(len(recovered) / len(new_pool), 5) if new_pool else None,
        "diversity": {
            "seed_communities": len(seed_communities),
            "seed_year_buckets": len(seed_years),
            "recovered_miss_clusters": len(rec_clusters),
        },
        "robustness": {
            "mean_seed_support": round(sum(supports) / len(supports), 2)
            if supports else None,
            "min_seed_support": min(supports) if supports else None,
            "note": "追回 miss 的平均 1-hop seed 命中数（低=单 seed 特判风险）",
        },
    }


def diverse_sample(meta: dict, n: int, seed_rng: int = 7) -> list:
    """P2：community × year_bucket 分层，确定性 round-robin（title 排序稳定）。"""
    strata: dict[str, list] = defaultdict(list)
    for w, mm in meta.items():
        strata[(mm["community"], mm["year_bucket"])].append(w)
    for k in strata:
        strata[k].sort()
    picked = []
    keys = sorted(strata.keys())
    i = 0
    while len(picked) < n and any(strata[k] for k in keys):
        k = keys[i % len(keys)]
        if strata[k]:
            picked.append(strata[k].pop(0))
        i += 1
    return picked[:n]


def representative_sample(meta: dict, max_per_cluster: int = 3) -> list:
    """P3：每 community cluster 按 cited_by 降序选代表（diversity+representativeness）。"""
    by_comm: dict[str, list] = defaultdict(list)
    for w, mm in meta.items():
        by_comm[mm["community"]].append(w)
    picked = []
    for c in sorted(by_comm):
        members = sorted(by_comm[c], key=lambda w: -meta[w]["cited_by"])
        picked.extend(members[:max_per_cluster])
    return picked


def set_cover_on(seed_list: list, per_seed: dict, oa: dict, miss_meta: dict,
                 train_wids: set, cost_map: dict) -> list:
    """P0'：在 train misses 上 cost-aware greedy set cover（Score=|ΔRec|/(1+log(1+cost))）。"""
    # miss -> 1-hop seeds（用 reachability 的 hop1_backward/hop1_forward——paths 是 2-hop 含 bridge）
    reach = json.load(open(REACH, encoding="utf-8"))
    miss_seeds = {}
    for p in reach["per_miss"]:
        w = p["miss_wid"]
        if w in train_wids:
            seeds = set(p.get("hop1_backward", [])) | set(p.get("hop1_forward", []))
            miss_seeds[w] = seeds
    # 限制在 policy seed 池内
    sel, covered, n_iter = [], set(), 0
    while n_iter < 200:
        best, best_score, best_delta = None, -1.0, set()
        for w in seed_list:
            if w in sel:
                continue
            delta = {mw for mw, ss in miss_seeds.items()
                     if mw not in covered and w in ss}
            if not delta:
                continue
            score = len(delta) / (1 + (1 + cost_map.get(w, 100)) ** 0.5)
            if score > best_score:
                best, best_score, best_delta = w, score, delta
        if best is None:
            break
        sel.append(best)
        covered |= best_delta
        n_iter += 1
    return sel


def main() -> None:
    out_path = os.path.join(sys.argv[sys.argv.index("--out") + 1]
                            if "--out" in sys.argv else OUT)
    oa = load_oa()
    inc = build_incoming(oa)
    found_s3 = __import__("build_r03_seen", fromlist=["build_s3_found_sets"]).build_s3_found_sets()
    misses, miss_meta = load_miss_meta()
    n_miss = len(misses)
    print(f"oa={len(oa)} | canonical misses={n_miss}")

    # 222 seeds
    from citation_2hop_reachability_audit import build_s3_seeds
    labels_path = LABELS if os.path.exists(LABELS) else LABELS_DL
    seed, n_r03 = build_s3_seeds(labels_path, oa)
    meta = seed_meta(seed, oa)
    print(f"seeds={len(seed)} (R03 seen {n_r03})")

    # P0 seeds（S3 set-cover 16）
    cfg = json.load(open(CFG, encoding="utf-8"))
    p0_seeds = [a["seed_wid"] for a in cfg["citation_actions"]]
    print(f"P0 seeds={len(p0_seeds)}")

    # 全量 expansion（一次，所有 policy 复用）
    per_seed = expand(seed, oa, inc)
    cost_map = {w: len(per_seed.get(w, set())) for w in seed}

    policies = {}
    policies["P0_S3_setcover"] = policy_stats(p0_seeds, per_seed, oa, found_s3, miss_meta)
    policies["P1_ALL"] = policy_stats(sorted(seed), per_seed, oa, found_s3, miss_meta)
    for n in (32, 64, 128):
        d = diverse_sample(meta, n)
        policies[f"P2_Diverse_{n}"] = policy_stats(d, per_seed, oa, found_s3, miss_meta)
    rep = representative_sample(meta)
    policies["P3_Representative"] = policy_stats(rep, per_seed, oa, found_s3, miss_meta)

    # ── LOCO ──
    clusters = sorted({m["cluster"] for m in miss_meta.values()})
    loco = {}
    for hold in clusters:
        hold_wids = {w for w, mm in miss_meta.items() if mm["cluster"] == hold}
        train_wids = {w for w in miss_meta if w not in hold_wids}
        # P0'：train 上 set cover
        p0_sel = set_cover_on(sorted(seed), per_seed, oa, miss_meta, train_wids, cost_map)
        st_p0 = policy_stats(p0_sel, per_seed, oa, found_s3,
                             {w: mm for w, mm in miss_meta.items() if w in hold_wids})
        # P2-64 / P3（不依赖 miss → 直接 holdout 子集统计）
        p2 = [w for w in diverse_sample(meta, 64) if w]
        st_p2 = policy_stats(p2, per_seed, oa, found_s3,
                             {w: mm for w, mm in miss_meta.items() if w in hold_wids})
        st_p3 = policy_stats(rep, per_seed, oa, found_s3,
                             {w: mm for w, mm in miss_meta.items() if w in hold_wids})
        loco[hold] = {
            "holdout_size": len(hold_wids),
            "P0_setcover_on_train": {"seeds": len(p0_sel), "recovery": st_p0["dev_recovery"],
                                     "recovered": st_p0["recovered_miss_ids"]},
            "P2_Diverse64": {"recovery": st_p2["dev_recovery"],
                             "recovered": st_p2["recovered_miss_ids"]},
            "P3_Representative": {"recovery": st_p3["dev_recovery"],
                                  "recovered": st_p3["recovered_miss_ids"]},
        }
        print(f"  LOCO[{hold}]: holdout={len(hold_wids)} "
              f"P0'={st_p0['dev_recovery']} P2-64={st_p2['dev_recovery']} "
              f"P3={st_p3['dev_recovery']}")

    # ── 汇总输出 ──
    summary_rows = []
    for name, st in policies.items():
        summary_rows.append({
            "policy": name,
            "seed_count": st["seed_count"],
            "new_candidates": st["new_vs_S3"],
            "dev_recovery": st["dev_recovery"],
            "recovery_rate": st["recovery_rate"],
            "diversity_communities": st["diversity"]["seed_communities"],
            "diversity_years": st["diversity"]["seed_year_buckets"],
            "robustness_mean_support": st["robustness"]["mean_seed_support"],
            "robustness_min_support": st["robustness"]["min_seed_support"],
            "efficiency": st["efficiency"],
        })
    out = {
        "version": "s4_repair_policy_pilot_v1",
        "frozen_at": "2026-08-30",
        "development_source": "AUDIT_R03",
        "hypothesis": "Miss-specific set cover → development overfitting; "
                      "diverse/representative policy → better fresh-audit generalization",
        "n_seeds_total": len(seed),
        "seed_definition": "KB relevant ∩ S3 seen ∪ R03 RELEVANT ∧ Seen_S3=TRUE",
        "new_vs_S3_basis": "build_s3_found_sets 三通道（eids/dois/titles）",
        "policies": policies,
        "summary": summary_rows,
        "LOCO": loco,
        "loco_conclusion": {
            "note": "P0' 在 train misses 上 set cover（复刻 S3 逻辑）；P2/P3 不依赖 miss（LOCO-safe）。"
                    "比较各 holdout cluster 的 recovery 判断 policy 泛化",
            "P0_overfit_flag": any(l["P0_setcover_on_train"]["recovery"] > 0
                                   and l["P2_Diverse64"]["recovery"] >= 0
                                   for l in loco.values()),
        },
        "interpretation_note": "本文件为 policy 比较（development-time），非 recall 证明；"
                               "S4 最终 policy 冻结后须 fresh R04 独立审计",
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    def safe(s, n=80):
        return (s or "").encode("ascii", "replace").decode("ascii")[:n]

    print("\n" + "=" * 96)
    print("S4 Step 6B/6D citation policy benchmark")
    print("=" * 96)
    print(f"{'policy':<20}{'seeds':>6}{'new':>7}{'rec':>5}{'rate':>8}{'divC':>6}"
          f"{'divY':>6}{'mSup':>7}{'minSup':>7}{'eff':>9}")
    for r in summary_rows:
        eff = f"{r['efficiency']:.5f}" if r["efficiency"] is not None else "—"
        ms = f"{r['robustness_mean_support']}" if r["robustness_mean_support"] is not None else "—"
        mn = f"{r['robustness_min_support']}" if r["robustness_min_support"] is not None else "—"
        print(f"{r['policy']:<20}{r['seed_count']:>6}{r['new_candidates']:>7}"
              f"{r['dev_recovery']:>5}{r['recovery_rate']:>8.1%}{r['diversity_communities']:>6}"
              f"{r['diversity_years']:>6}{ms:>7}{mn:>7}{eff:>9}")
    print(f"\n[OK] written: {out_path}")


if __name__ == "__main__":
    main()
