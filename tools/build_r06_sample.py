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
import os
import random
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from search_engine.topic_config import (  # noqa: E402
    DEFAULT_TOPIC, is_legacy_topic, load_topic,
)

UNIVERSE_FILE = os.path.join(BASE, "data/exports/completeness_universes.json")
# P0-2: 以下为 v1.0 legacy 冻结默认（pc001 topic.yaml audit 段已声明同值；非 legacy 必须
# 在 topic.yaml 声明自己的 audit frame——audit 分主题，禁跨主题复用 frame）
UNIVERSE_ID_LEGACY = "pc_001-2026-08-31T162811"
R05_SAMPLE_LEGACY = os.path.join(
    BASE, "data/exports/completeness_labels/pc_001__20260831162811.json")
OUT_DIR_LEGACY = os.path.join(BASE, "data/exports/completeness_labels")
CACHE = os.path.join(BASE, "data/cache/openalex_cache.json")

# ⚠️ P0-2 决策资产：R06 LABEL_SCHEME 是 pc001 主题 rubric 的物化（收缩/固化语境）。
# 换主题须按该主题 rubric.md 重写——审计判定的相关定义是主题决策，非算法。
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
    ap.add_argument("--topic", default=None,
                    help="topic_id（audit frame 从 topic.yaml audit 段读；默认 v1.0 legacy）")
    ap.add_argument("--n", type=int, default=500)
    ap.add_argument("--seed", type=int, default=13,
                    help="抽样 seed（R05 用过 7，R06 独立用 13）")
    args = ap.parse_args()

    # ── P0-2: 主题 audit frame（分主题独立；pc001 legacy → v1.0 冻结值）──
    topic = args.topic or DEFAULT_TOPIC
    cfg = load_topic(topic)
    au = cfg.raw.get("audit") or {}
    universe_id = au.get("frame_universe_id")
    frame_note = f"(topic.yaml audit.frame_universe_id)" if universe_id else ""
    if not universe_id and is_legacy_topic(topic):
        universe_id = UNIVERSE_ID_LEGACY
        frame_note = "(legacy 冻结回退 pc_001-2026-08-31T162811；建议 topic.yaml 声明 audit)"
    if not universe_id:
        raise SystemExit(
            f"[topic] {topic}: topic.yaml 未声明 audit.frame_universe_id——"
            f"audit frame 分主题独立，禁跨主题复用（8 坑 #3/#7）。")
    legacy = is_legacy_topic(topic)
    out_dir = (OUT_DIR_LEGACY if legacy
               else os.path.join(os.path.dirname(cfg.dir), "runs", "audit"))
    os.makedirs(out_dir, exist_ok=True)
    prefix = ("pc_001" if legacy and au.get("output_prefix") in (None, "")
              else au.get("output_prefix") or topic)
    print(f"[topic] {topic} | frame={universe_id} {frame_note} | out={out_dir} | prefix={prefix}")

    us = json.load(open(UNIVERSE_FILE, encoding="utf-8"))
    u = next(x for x in us if x.get("universe_id") == universe_id)
    univ = set(u["paper_ids"])
    # 排除上一轮 audit sample（防 fresh 污染）
    r05_path = au.get("r05_excluded_sample")
    if not r05_path and legacy:
        r05_path = R05_SAMPLE_LEGACY
    if not r05_path:
        print("[WARN] topic.yaml audit.r05_excluded_sample 未声明——跳过上一轮排除（新主题首轮 audit 无前轮）")
        r05 = set()
    else:
        r05p = r05_path if os.path.isabs(r05_path) else os.path.join(BASE, r05_path)
        r05 = {l["paper_id"]
               for l in json.load(open(r05p, encoding="utf-8"))["labels"]}
    pool = sorted(univ - r05)
    if len(r05 & univ):
        print(f"universe {len(univ)} − 前轮 {len(r05 & univ)} → pool {len(pool)}")
    else:
        print(f"universe {len(univ)}（无前轮排除）→ pool {len(pool)}")
    if r05:
        assert r05 <= univ, (f"前轮 sample {len(r05 - univ)} 篇不在 universe——"
                             f"排除集合不合法（frame 是否同源？）")
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
    audit_id = f"{prefix}::{ts}"
    out_path = os.path.join(out_dir, f"{prefix}__{ts}.json")
    payload = {
        "audit_id": audit_id,
        "universe_id": universe_id,
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
