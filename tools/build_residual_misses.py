"""tools/build_residual_misses.py — S3 预备：R02 70 个 residual miss 清单（2026-08-29 用户定）。

定义：R02 RELEVANT ∧ Seen_S1=FALSE ∧ Seen_S2=FALSE（S1 与 S2 都漏掉的 Search miss）。
S2 已追回 8 个（见 s2_delta_vs_s1.json r02_recovery），residual = 78 − 8 = 70。

输出：data/exports/terminology/s3_residual_misses.json
  [{wid, doi, title, year, abstract, seen_s1, seen_s2, ...} × 70]

用法：
  python tools/build_residual_misses.py --labels <R02 filled.json> [--out <path>]
"""
import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "tools"))

from build_r02_seen import build_s1_found_sets as _s1_found  # noqa: E402
from build_r02_seen import resolve_seen_s1 as _resolve_seen  # noqa: E402
from build_r02_seen import _norm_doi, _norm_title            # noqa: E402

S2_RAW = os.path.join(BASE, "data", "exports", "terminology", "s2_raw_records.json")
DEFAULT_OUT = os.path.join(BASE, "data", "exports", "terminology",
                           "s3_residual_misses.json")
OPENALEX_CACHE = os.path.join(BASE, "data", "cache", "openalex_cache.json")


def build_s2_found_sets() -> dict:
    """S2 三通道 found sets = S0 ∪ repair1000（s1_found 已含 S0∪repair500；追加 repair1000）。"""
    found = _s1_found()
    raw = json.load(open(S2_RAW, encoding="utf-8"))
    for q, recs in raw.get("records_by_query", {}).items():
        for r in recs:
            if r.get("eid"):
                found["eids"].add(str(r["eid"]).strip())
            if r.get("doi"):
                found["dois"].add(_norm_doi(r["doi"]))
            if r.get("title"):
                found["titles"].add(_norm_title(r["title"]))
    return found


def main():
    ap = argparse.ArgumentParser(description="S3 预备：R02 residual miss 清单")
    ap.add_argument("--labels", required=True, help="R02 filled labels 路径")
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    d = json.load(open(args.labels, encoding="utf-8"))
    labs = d["labels"]
    rel = [l for l in labs if l.get("label") == "RELEVANT"]

    found_s1 = _s1_found()
    found_s2 = build_s2_found_sets()
    v1 = _resolve_seen([l["paper_id"] for l in rel], found_s1)
    v2 = _resolve_seen([l["paper_id"] for l in rel], found_s2)

    residual = []
    n_false_s1 = n_false_s2 = 0
    for l in rel:
        s1 = v1.get(l["paper_id"], {}).get("agent_seen_s1", "UNKNOWN")
        s2 = v2.get(l["paper_id"], {}).get("agent_seen_s1", "UNKNOWN")
        if s1 == "FALSE":
            n_false_s1 += 1
        if s2 == "FALSE":
            n_false_s2 += 1
        if s1 == "FALSE" and s2 == "FALSE":
            residual.append({
                "wid": l["paper_id"], "doi": l.get("doi"), "title": l.get("title"),
                "year": l.get("year"), "abstract": l.get("abstract"),
                "seen_s1": s1, "seen_s2": s2,
            })
    residual.sort(key=lambda x: x["wid"])

    meta = {}
    if os.path.exists(OPENALEX_CACHE):
        cache = json.load(open(OPENALEX_CACHE, encoding="utf-8"))
        for q, resp in cache.items():
            for w in resp.get("results", []):
                wid = (w.get("id") or "").replace("https://openalex.org/", "")
                if wid and wid not in meta:
                    meta[wid] = {"title": w.get("title"),
                                 "year": w.get("publication_year"),
                                 "doi": w.get("doi")}
    # 补充 citation 数据（backward: referenced_works）
    for r in residual:
        m = meta.get(r["wid"], {})
        r["oa_title"] = m.get("title") or r.get("title")
        r["oa_year"] = m.get("year") or r.get("year")

    out = {
        "version": "s3_residual_misses_v1",
        "frozen_at": "2026-08-29",
        "definition": "R02 RELEVANT ∧ Seen_S1=FALSE ∧ Seen_S2=FALSE",
        "count": len(residual),
        "n_relevant_r02": len(rel),
        "n_false_s1": n_false_s1, "n_false_s2": n_false_s2,
        "recovered_by_s2": n_false_s1 - n_false_s2,
        "misses": residual,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"R02 RELEVANT={len(rel)} | Seen_S1=FALSE={n_false_s1} | Seen_S2=FALSE={n_false_s2}"
          f" | residual={len(residual)}（S2 追回 {n_false_s1 - n_false_s2}）")
    print(f"[OK] {args.out}")
    print("\n前 10 篇 residual miss：")
    for r in residual[:10]:
        t = (r["oa_title"] or "")[:70].encode("ascii", "replace").decode("ascii")
        print(f"  {r['wid']} | {t}")


if __name__ == "__main__":
    main()
