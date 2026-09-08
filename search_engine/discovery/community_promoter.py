"""search_engine/discovery/community_promoter.py — v2.1 Community Promoter v1（用户 2026-08-28 定稿）。

职责（与 term_community.py 分离）：
  term_community.py = 发现社区（role/status/证据）
  community_promoter.py = 判断哪些社区值得变成新 Query Family

输入：
  data/exports/term_communities_v2.json（role/status/novelty/citation_support/
  term_coherence/existing_coverage/retrieval_novelty）

第一版判定（固定规则，不学权重；用户定）：
  paper_count >= 3
  AND term_count >= 3
  AND mean_bridge >= MEAN_BRIDGE_MIN
  AND mean_npmi  >= COHERENCE_MIN
  AND retrieval_novelty >= RETRIEVAL_NOVELTY_MIN   ← 关键新指标
  AND role != CORE_COMMUNITY                        ← dental 本体不晋升

⚠️ 用户提醒：lexical novelty 不能单独决定晋升（TC_001 novelty 0.93 也会过线）。
必须联合 role + citation/term structure + RetrievalNovelty。

RetrievalNovelty = 1 - ExistingCoverage（支撑论文中被现有检索覆盖的比例）——
比词面 Jaccard 更贴近"现有搜索道路是否覆盖好"这一目标。

输出：data/exports/community_promotions_v1.json
用法：
  python search_engine/discovery/community_promoter.py
"""
import argparse
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE)

COMMUNITIES_PATH = os.path.join(BASE, "data", "exports", "term_communities_v2.json")
OUT_PATH = os.path.join(BASE, "data", "exports", "community_promotions_v1.json")

MEAN_BRIDGE_MIN = 2.5          # 平均桥强度
COHERENCE_MIN = 0.6            # 内部 NPMI 均值
RETRIEVAL_NOVELTY_MIN = 0.5    # 现有检索覆盖 <50%
LEXICAL_NOVELTY_REF = 0.7      # 词面 novelty 只作参考展示（不作门槛）

# SLOT_SCHEMA_GAP 检测：方法/测量类词（用户 2026-08-28 定）——
# ATR/infrared spectroscopy/spectral characterization 等无法自然落入 P/M/R/C，
# 标 suggested_missing_slot="method"，不硬塞 Context；待多个高质社区暴露后再扩 schema
METHOD_WORDS = re.compile(
    r"spectroscop|spectra|reflectance|microscop|dilatomet|measurement|measure"
    r"|analysis|characterization|characteris|test|assessment|evaluation|monitoring"
    r"|scanning|tomograph|calorimet|rheolog|chromatograph|spectrometr|diffract"
    r"|indentation|hardness|tensile|compressive|modulus test|elastic modulus test",
    re.I)


def slot_mapping(c: dict) -> dict:
    """P/M/R/C slot mapping audit（用户 2026-08-28 定）。

    MAPPABLE      → 语义能自然落进现有 P/M/R/C
    SLOT_SCHEMA_GAP → 核心 terms 是方法/测量（Method 维度），现有 schema 表达不干净
    """
    terms = " ".join(c.get("top_terms_used", []) or [])
    if METHOD_WORDS.search(terms):
        return {"slot_mapping_status": "SLOT_SCHEMA_GAP",
                "suggested_missing_slot": "method"}
    return {"slot_mapping_status": "MAPPABLE", "suggested_missing_slot": None}


def decide(c: dict) -> dict:
    """单社区判定，返回 (verdict, reasons)。"""
    reasons = []
    passed = True
    if c["role"] == "CORE_COMMUNITY":
        return "NOT_PROMOTED", ["CORE_COMMUNITY（dental 本体，不晋升不拆）"]
    if c["status"] != "COMMUNITY_CANDIDATE":
        return "NOT_PROMOTED", [f"status={c['status']}（未达 CANDIDATE 门槛）"]
    checks = [
        ("paper_count>=3", c["paper_count"] >= 3),
        ("term_count>=3", c["term_count"] >= 3),
        (f"mean_bridge>={MEAN_BRIDGE_MIN}",
         c["citation_support"]["mean_bridge_count"] >= MEAN_BRIDGE_MIN),
        (f"mean_npmi>={COHERENCE_MIN}",
         c["term_coherence"]["mean_npmi"] >= COHERENCE_MIN),
        (f"retrieval_novelty>={RETRIEVAL_NOVELTY_MIN}",
         c["retrieval_novelty"] >= RETRIEVAL_NOVELTY_MIN),
    ]
    for name, ok in checks:
        if not ok:
            passed = False
            reasons.append(name)
    return ("PROMOTION_CANDIDATE" if passed else "NOT_PROMOTED", reasons)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--communities", default=COMMUNITIES_PATH)
    ap.add_argument("--show-all", action="store_true", help="打印全部（默认只看非 NOISE）")
    ap.add_argument("--out", default=OUT_PATH,
                    help="输出 JSON（默认 community_promotions_v1.json）")
    args = ap.parse_args()

    data = json.load(open(args.communities, encoding="utf-8"))
    comms = data["communities"]

    results = []
    for c in comms:
        verdict, reasons = decide(c)
        slot = slot_mapping(c)
        entry = {
            "community_id": c["community_id"],
            "role": c["role"],
            "verdict": verdict,
            "reasons": reasons,
            "slot_mapping_status": slot["slot_mapping_status"],
            "suggested_missing_slot": slot["suggested_missing_slot"],
            "paper_count": c["paper_count"],
            "term_count": c["term_count"],
            "citation_support": c["citation_support"],
            "term_coherence": c["term_coherence"],
            "novelty": c["novelty"],
            "existing_coverage": c.get("existing_coverage"),
            "retrieval_novelty": c.get("retrieval_novelty"),
            "top_terms_used": c.get("top_terms_used", [])[:8],
            "supporting_papers": c.get("supporting_papers", []),
        }
        results.append(entry)

    cands = [r for r in results if r["verdict"] == "PROMOTION_CANDIDATE"]
    mappable = [r for r in cands if r["slot_mapping_status"] == "MAPPABLE"]
    gaps = [r for r in cands if r["slot_mapping_status"] == "SLOT_SCHEMA_GAP"]
    print("=" * 78)
    print(f"Community Promoter v1.1（rules: bridge>={MEAN_BRIDGE_MIN}, "
          f"npmi>={COHERENCE_MIN}, retNovelty>={RETRIEVAL_NOVELTY_MIN}）")
    print("=" * 78)
    print(f"communities={len(comms)} | PROMOTION_CANDIDATE={len(cands)} "
          f"（MAPPABLE={len(mappable)}, SLOT_SCHEMA_GAP={len(gaps)}）")
    print("\n★ PROMOTION CANDIDATES（含 slot audit）:")
    for r in cands:
        tag = "✅" if r["slot_mapping_status"] == "MAPPABLE" else "⚠️"
        print(f"  {tag} {r['community_id']} [{r['role']}] papers={r['paper_count']} "
              f"bridge={r['citation_support']['mean_bridge_count']} "
              f"npmi={r['term_coherence']['mean_npmi']} "
              f"retNovelty={r['retrieval_novelty']} lexNovelty={r['novelty']['score']} "
              f"slot={r['slot_mapping_status']}"
              f"{'(缺:' + r['suggested_missing_slot'] + ')' if r['suggested_missing_slot'] else ''}")
        print(f"      top: {', '.join(r['top_terms_used'][:6])}")
    if gaps:
        print(f"\n⚠️ SLOT_SCHEMA_GAP（{len(gaps)} 个——Method 维度无法落 P/M/R/C，"
              f"暂缓进 query generation）:")
        for r in gaps:
            print(f"  {r['community_id']} papers={r['paper_count']} "
                  f"retNov={r['retrieval_novelty']} "
                  f"suggested_missing_slot={r['suggested_missing_slot']}")
    if args.show_all:
        print("\n全部判定:")
        for r in results:
            if r["role"] == "NOISE_SMALL_CLUSTER":
                continue
            tag = "★" if r["verdict"] == "PROMOTION_CANDIDATE" else " "
            print(f"  {tag} {r['community_id']} [{r['role']}] {r['verdict']:<20} "
                  f"papers={r['paper_count']} retNov={r['retrieval_novelty']} "
                  f"({' '.join(r['reasons']) if r['reasons'] else 'OK'})")

    out = {
        "version": "v1_rules",
        "created_at": "2026-08-28",
        "thresholds": {
            "mean_bridge_min": MEAN_BRIDGE_MIN,
            "coherence_min": COHERENCE_MIN,
            "retrieval_novelty_min": RETRIEVAL_NOVELTY_MIN,
            "lexical_novelty_ref": LEXICAL_NOVELTY_REF,
            "note": "lexical novelty 只作参考不作门槛（用户：TC_001 0.93 也会误过线）",
        },
        "summary": {"total": len(results),
                    "promotion_candidates": len(cands)},
        "communities": results,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n✓ 已写: {args.out}")


if __name__ == "__main__":
    main()
