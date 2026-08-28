"""tools/generate_community_families.py — v2.1 第一轮 community → Query Family（用户 2026-08-28 定稿）。

ROUND 1：只做 TC_006 + TC_015（TC_017 = HOLDOUT 留第二轮，保持因果干净）。

   TC_006  LED 固化光源社区 → R=photocuring + C=LED/light source（process/context 维度）
   TC_015  filler particle size 社区 → M=filler/particle-size（material 维度）

生成：复用 v2.0 query family 管线（concept_slots / variant_generator / query_family），
每个 community 独立输出（不合并、不污染 v2.0 registry——第一轮实验隔离）。

provenance 链（用户要求）：community_id → terms → slot mapping → family IDs →
query IDs →（检索后）retrieved papers。

用法：
  python tools/generate_community_families.py            # 生成并写 data/exports/community_round1_queries.json
  python tools/generate_community_families.py --plan-only
"""
import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from search_engine.query.concept_slots import Concept
from search_engine.query.query_family import Family, QueryVariant

OUT_PATH = os.path.join(BASE, "data", "exports", "community_round1_queries.json")
K_DEFAULT = 200

# ── 社区定义（来自 term_communities_v2.json + community_promotions_v1.json 审计）──
COMMUNITIES = {
    "TC_006": {
        "name": "LED curing / light source",
        "terms": ["light emitting diode", "curing light", "light curing", "visible light",
                  "curing units", "photo activation"],
        "slot_mapping": {
            "P": ["polymerization shrinkage", "shrinkage stress", "volumetric shrinkage"],
            "M": [],
            "R": ["photocuring"],
            "C": ["light emitting diode", "curing light", "light curing", "visible light",
                  "curing units", "photo activation"],
        },
        "family_types": ["PC", "PR"],   # P×C 为主 + P×R 少量
        "note": "process/context 维度：LED 固化光源作为检索上下文",
    },
    "TC_015": {
        "name": "filler particle size / loading",
        "terms": ["filler particle size", "filler loading", "filler content", "particle size",
                  "filler particle"],
        "slot_mapping": {
            "P": ["polymerization shrinkage", "shrinkage stress", "volumetric shrinkage"],
            "M": ["filler particle size", "filler loading", "filler content", "particle size"],
            "R": [],
            "C": [],
        },
        "family_types": ["PM"],        # P×M
        "note": "material 维度：填料/粒径作为配方参数",
    },
}

P_CLEAN = ["polymerization shrinkage", "shrinkage stress", "volumetric shrinkage"]


def _q(term: str) -> str:
    return f'TITLE-ABS-KEY("{term}")'


def _q_and(p: str, other: str) -> str:
    return f'TITLE-ABS-KEY("{p}" AND "{other}")'


def build_community_families(cid: str, spec: dict) -> list[Family]:
    fams: list[Family] = []
    sm = spec["slot_mapping"]
    if "PC" in spec["family_types"]:
        for c in sm["C"]:
            fid = f"V2C_{cid}_{c.replace(' ', '_')[:24]}"
            fams.append(Family(
                family_id=fid, family_type="COMMUNITY_PC",
                concepts={"problem": sm["P"], "context": [c]},
                variants=[QueryVariant(term=c, source="COMMUNITY")],
                generated_queries=[_q_and(p, c) for p in sm["P"]],
                budget=K_DEFAULT, provenance_source="COMMUNITY_DISCOVERY"))
    if "PR" in spec["family_types"]:
        for r in sm["R"]:
            fid = f"V2C_{cid}_R_{r.replace(' ', '_')[:20]}"
            fams.append(Family(
                family_id=fid, family_type="COMMUNITY_PR",
                concepts={"problem": sm["P"], "reaction": [r]},
                variants=[QueryVariant(term=r, source="COMMUNITY")],
                generated_queries=[_q_and(p, r) for p in sm["P"]],
                budget=K_DEFAULT, provenance_source="COMMUNITY_DISCOVERY"))
    if "PM" in spec["family_types"]:
        for m in sm["M"]:
            fid = f"V2C_{cid}_M_{m.replace(' ', '_')[:24]}"
            fams.append(Family(
                family_id=fid, family_type="COMMUNITY_PM",
                concepts={"problem": sm["P"], "material": [m]},
                variants=[QueryVariant(term=m, source="COMMUNITY")],
                generated_queries=[_q_and(p, m) for p in sm["P"]],
                budget=K_DEFAULT, provenance_source="COMMUNITY_DISCOVERY"))
    return fams


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan-only", action="store_true")
    args = ap.parse_args()

    out = {"version": "round1", "created_at": "2026-08-28",
           "holdout": {"TC_017": "dimethacrylate monomer → 第二轮（HOLDOUT）"},
           "communities": {}}
    for cid, spec in COMMUNITIES.items():
        fams = build_community_families(cid, spec)
        queries = []
        qn = 0
        for f in fams:
            for text in f.generated_queries:
                qn += 1
                queries.append({"query_id": f"{f.family_id}::Q{qn:02d}",
                                "family_id": f.family_id,
                                "query": text})
        entry = {
            "community_id": cid,
            "community_name": spec["name"],
            "terms": spec["terms"],
            "slot_mapping": spec["slot_mapping"],
            "family_types": spec["family_types"],
            "families": [f.family_id for f in fams],
            "queries": queries,
            "n_families": len(fams),
            "n_queries": len(queries),
            "provenance": {"discovered_from": "citation_bridge + term_community",
                           "channels": ["citation", "term_cooccurrence"],
                           "qgs_leakage": False},
        }
        out["communities"][cid] = entry
        print(f"\n{'=' * 60}")
        print(f"{cid} {spec['name']}（{len(fams)} families / {len(queries)} queries）")
        print(f"{'=' * 60}")
        for q in queries:
            print(f"  {q['family_id'][:34]:<36} {q['query']}")

    if args.plan_only:
        return
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n✓ 已写: {OUT_PATH}")


if __name__ == "__main__":
    main()
