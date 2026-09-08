"""R06 fresh audit sample 构建（2026-09-08，用户拍板：复用 R05 universe + 外部盲评）。

方法：
  universe  = pc_001-2026-08-31T162811（5890 WID，OpenAlex FRAME_V2 wide）
  排除      = R05 sample 500（pc_001__20260831162811.json labels）→ fresh pool 5390
  sample    = random.sample(pool, n)（seed 独立于 R05 用过的 7）
  语料      = openalex_cache.json 反查（100% 命中：title/doi/year/abstract inverted→text）

输出：data/exports/completeness_labels/pc_001__<ts>.json（R05 payload 同构，
  labels 列表含 paper_id/title/year/doi/abstract，判定留空待独立 Auditor 填）。
"""
import argparse
import datetime
import json
import random
import re

UNIVERSE_FILE = "data/exports/completeness_universes.json"
UNIVERSE_ID = "pc_001-2026-08-31T162811"
R05_SAMPLE = "data/exports/completeness_labels/pc_001__20260831162811.json"
CACHE = "data/cache/openalex_cache.json"
OUT_DIR = "data/exports/completeness_labels"

LABEL_SCHEME = {
    "REL": "Title/abstract substantively measures, models, evaluates, reduces, or "
           "treats polymerization/curing shrinkage, shrinkage stress, or cure-induced "
           "deformation as a material/process outcome or mechanism.",
    "IRR": "Available title/abstract does not show polymerization/curing shrinkage, "
           "shrinkage stress, or cure-induced deformation as a substantive study "
           "objective, mechanism, or measured outcome; any mention is incidental/background.",
    "NONCURE": "Reported shrinkage/deformation is thermal, sintering, pyrolysis, drying, "
               "biological, swelling/deswelling, or another non-polymerization/non-curing "
               "process rather than target polymerization/curing shrinkage.",
    "UNC": "Insufficient title/abstract evidence for reliable blind adjudication; "
           "curing/resin dimensional behavior is plausible, but target "
           "polymerization/curing shrinkage relevance cannot be established from the "
           "available record.",
}


def rebuild_abstract(aii):
    """abstract_inverted_index → 原文。"""
    if not aii:
        return ""
    pos = []
    for word, idxs in aii.items():
        for i in idxs:
            pos.append((i, word))
    pos.sort()
    return " ".join(w for _, w in pos)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=500)
    ap.add_argument("--seed", type=int, default=13,
                    help="抽样 seed（R05 用过 7，R06 独立用 13）")
    args = ap.parse_args()

    us = json.load(open(UNIVERSE_FILE, encoding="utf-8"))
    u = next(x for x in us if x.get("universe_id") == UNIVERSE_ID)
    univ = set(u["paper_ids"])
    r05 = {l["paper_id"]
           for l in json.load(open(R05_SAMPLE, encoding="utf-8"))["labels"]}
    pool = sorted(univ - r05)
    assert len(r05 & univ) == 500
    print(f"universe {len(univ)} − R05 {len(r05 & univ)} → pool {len(pool)}")

    random.seed(args.seed)
    sample = random.sample(pool, min(args.n, len(pool)))
    print(f"sample n={len(sample)} (seed={args.seed})")

    # 语料反查（openalex_cache）
    cache = json.load(open(CACHE, encoding="utf-8"))
    meta = {}
    for url, resp in cache.items():
        for w in (resp.get("results") or []):
            wid = (w.get("id") or "").replace("https://openalex.org/", "")
            if wid and wid not in meta:
                doi = (w.get("doi") or "").replace("https://doi.org/", "")
                meta[wid] = {
                    "title": w.get("display_name") or w.get("title") or "",
                    "year": w.get("publication_year"),
                    "doi": doi,
                    "abstract": rebuild_abstract(w.get("abstract_inverted_index")),
                }
    miss = [w for w in sample if w not in meta]
    if miss:
        print(f"⚠ cache miss: {len(miss)} — {miss[:5]}")
    labels = []
    n_abs = 0
    for wid in sample:
        m = meta.get(wid, {"title": "", "year": None, "doi": "", "abstract": ""})
        if m["abstract"]:
            n_abs += 1
        labels.append({
            "paper_id": wid, "title": m["title"], "year": m["year"],
            "doi": m["doi"], "abstract": m["abstract"],
            # label 留空——独立 Auditor 盲评后填（RELEVANT/UNCERTAIN/IRRELEVANT）
        })

    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    audit_id = f"pc_001::{ts}"
    out_path = f"{OUT_DIR}/pc_001__{ts}.json"
    payload = {
        "audit_id": audit_id,
        "universe_id": UNIVERSE_ID,
        "label_scheme": LABEL_SCHEME,
        "labels": labels,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"[ok] {out_path}")
    print(f"  audit_id={audit_id} | n={len(labels)} | with_abstract={n_abs}"
          f"({n_abs/len(labels):.0%})")
    print("\n盲评入口: python tools/label_completeness_sample.py --labels "
          f"{out_path}")


if __name__ == "__main__":
    main()
