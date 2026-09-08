"""tools/citation_reachability_audit.py — S3 Stage A：citation reachability audit（2026-08-29 用户定）。

对 37 篇 CITATION-positive residual miss 做 citation reachability 审计：
确认 citation edge 是否真的从**高置信 S2 seed**出发，而不是从整个 9873 seen 出发。

seed 定义（用户定，防噪音）：
  Seen_S2 = TRUE ∧ relevance = known/high-confidence
    = (KB relevant ∩ S2 seen) ∪ (R02 sample RELEVANT ∧ Seen_S2=TRUE)
  （最低限 title/abstract 含 shrinkage/problem 语义——这里用审计已知 relevant，更强）

输出每篇 residual miss：
  miss_wid / linked_seed_wids / direction(BACKWARD=miss 引用 seed / FORWARD=seed 引用 miss)
  hop=1 / seed_relevance / citation_count

关键指标：
  CitationCoverage_high_conf = # reachable from high-conf seed / 70
  CitationCoverage_all_seen  = # reachable from all 9873 seen / 70   （对比，诊断噪音）

用法：
  python tools/citation_reachability_audit.py --misses <s3_residual_misses.json>
      --r02-labels <R02 filled.json> [--out <path>]
"""
import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "tools"))

from build_r02_seen import _norm_doi  # noqa: E402

DEFAULT_MISSES = os.path.join(BASE, "data", "exports", "terminology",
                              "s3_residual_misses.json")
DEFAULT_OUT = os.path.join(BASE, "data", "exports", "terminology",
                           "s3_citation_audit.json")
S2_RAW = os.path.join(BASE, "data", "exports", "terminology", "s2_raw_records.json")
S2_SEEN = os.path.join(BASE, "data", "exports", "terminology", "s2_seen_set.json")
OA_CACHE = os.path.join(BASE, "data", "cache", "openalex_cache.json")
UNIVERSES = os.path.join(BASE, "data", "exports", "completeness_universes.json")


def load_oa() -> dict:
    """{wid: {doi, title, referenced_works, publication_year, cited_by_count}}。

    2026-08-30 增强：补 publication_year / cited_by_count（S4 6B seed-policy benchmark
    的 diversity 维度需要 year；向后兼容——只加字段不改原键）。
    """
    meta = {}
    if not os.path.exists(OA_CACHE):
        return meta
    cache = json.load(open(OA_CACHE, encoding="utf-8"))
    for q, resp in cache.items():
        for w in resp.get("results", []):
            wid = (w.get("id") or "").replace("https://openalex.org/", "")
            if wid and wid not in meta:
                meta[wid] = {
                    "doi": (w.get("doi") or "").lower().replace("https://doi.org/", ""),
                    "title": w.get("title"),
                    "referenced_works": [r.replace("https://openalex.org/", "")
                                         for r in (w.get("referenced_works") or [])],
                    "publication_year": w.get("publication_year"),
                    "cited_by_count": w.get("cited_by_count"),
                }
    return meta


def seen_dois() -> set[str]:
    """S2 seen DOI（canonical keys 非 EID 部分 + raw records）。"""
    dois = set()
    seen = json.load(open(S2_SEEN, encoding="utf-8"))["keys"]
    for k in seen:
        k = k.strip().lower()
        if not k.startswith("2-s2.0-") and k:
            dois.add(k)
    raw = json.load(open(S2_RAW, encoding="utf-8"))
    for q, recs in raw.get("records_by_query", {}).items():
        for r in recs:
            if r.get("doi"):
                dois.add(str(r["doi"]).strip().lower())
    return dois


def build_high_conf_seeds(labels_path: str, oa: dict) -> tuple[set[str], int]:
    """高置信 seed WID = (KB relevant ∩ S2 seen) ∪ (R02 RELEVANT ∧ Seen_S2=TRUE)。

    用完整 Seen_S2 判定（S2 found sets DOI/title 双通道，与 build_residual_misses 同口径）。
    """
    from build_residual_misses import build_s2_found_sets
    from build_r02_seen import resolve_seen_s1
    found_s2 = build_s2_found_sets()
    labs = json.load(open(labels_path, encoding="utf-8"))["labels"]
    rel = [l for l in labs if l.get("label") == "RELEVANT"]
    verdicts = resolve_seen_s1([l["paper_id"] for l in rel], found_s2)
    seed = {l["paper_id"] for l in rel
            if verdicts.get(l["paper_id"], {}).get("agent_seen_s1") == "TRUE"}
    n_seen = len(seed)
    # KB relevant ∩ S2 seen
    snaps = json.load(open(UNIVERSES, encoding="utf-8"))
    r02_uni = next((u for u in snaps
                    if u["universe_id"] == "pc_001-2026-08-29T130129"), None)
    if r02_uni:
        found = set(r02_uni.get("source_breakdown", {}).get("found_relevant", []))
        kb_verdicts = resolve_seen_s1(list(found), found_s2)
        for wid in found:
            if kb_verdicts.get(wid, {}).get("agent_seen_s1") == "TRUE":
                seed.add(wid)
    return seed, n_seen


def main():
    ap = argparse.ArgumentParser(description="S3 Stage A citation reachability audit")
    ap.add_argument("--misses", default=DEFAULT_MISSES)
    ap.add_argument("--r02-labels", required=True)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    if not os.path.exists(args.misses):
        print(f"[FATAL] 缺 {args.misses}")
        sys.exit(2)
    data = json.load(open(args.misses, encoding="utf-8"))
    misses = data["misses"]
    oa = load_oa()
    seen_d = seen_dois()
    seed, n_seen_sample = build_high_conf_seeds(args.r02_labels, oa)
    print(f"residual = {len(misses)} | oa records = {len(oa)} | seen_dois = {len(seen_d)}")
    print(f"R02 sample RELEVANT seen_S2 = {n_seen_sample} | high-conf seed WID = {len(seed)}")

    # all-seen WID（全 9873，诊断对比）
    all_seen = {wid for wid, m in oa.items() if m["doi"] and m["doi"] in seen_d}

    # forward 索引：seed 引用了谁（只对 high-conf seed 建，省内存）
    seed_forward = {}
    for wid in seed:
        m = oa.get(wid)
        if m:
            seed_forward[wid] = set(m["referenced_works"])

    per_miss = []
    n_hi = n_all = 0
    for r in misses:
        wid = r["wid"]
        m = oa.get(wid, {})
        backward_hi = [w for w in m.get("referenced_works", []) if w in seed]
        forward_hi = [w for w, refs in seed_forward.items() if wid in refs]
        backward_all = [w for w in m.get("referenced_works", []) if w in all_seen]
        reach_hi = bool(backward_hi or forward_hi)
        reach_all = bool(backward_all)
        if reach_hi:
            n_hi += 1
        if reach_all:
            n_all += 1
        per_miss.append({
            "wid": wid,
            "title": (r.get("oa_title") or r.get("title")),
            "citation_positive_high_conf": reach_hi,
            "direction": ("BACKWARD" if backward_hi else "FORWARD" if forward_hi
                          else None),
            "linked_seed_wids": (backward_hi or forward_hi)[:10],
            "linked_seed_count": len(backward_hi or forward_hi),
            "backward_seed_count": len(backward_hi),
            "forward_seed_count": len(forward_hi),
            "hop": 1,
        })

    n = len(misses)
    out = {
        "version": "s3_citation_audit_v1", "frozen_at": "2026-08-29",
        "seed_definition": "Seen_S2=TRUE ∧ relevance=known/high-confidence："
                           "(KB relevant ∩ S2 seen) ∪ (R02 RELEVANT ∧ Seen_S2=TRUE)",
        "n_residual": n,
        "citation_coverage_high_conf": round(n_hi / n, 4) if n else None,
        "citation_coverage_all_seen": round(n_all / n, 4) if n else None,
        "n_high_conf_seeds": len(seed),
        "n_all_seen_wids": len(all_seen),
        "note": "coverage_all_seen 为诊断对比（全 9873 作 seed 的噪音水平）；"
                "S3 repair 只用 high-conf coverage",
        "per_miss": per_miss,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n=== Citation reachability（hop=1）===")
    print(f"  high-conf seed coverage : {n_hi}/{n} = {n_hi / n:.1%}")
    print(f"  all-seen seed coverage  : {n_all}/{n} = {n_all / n:.1%}（诊断）")
    print(f"[OK] {args.out}")


if __name__ == "__main__":
    main()
