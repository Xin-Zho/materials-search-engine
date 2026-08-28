"""tools/build_query_families.py — v2.0 构建首批 Query Families。

从 concept_slots.INITIAL_CONCEPTS 生成 6 类 family：
  FAM-P（PROBLEM_ONLY）       每个 CLEAN P 词一个 family（含词形变体）
  FAM-STRESS（STRESS_SPECIFIC）stress 术语独立 family（含 QGS-learned 历史词）
  FAM-VOLUME（VOLUME_FAMILY） volume/contraction 术语独立 family
  FAM-PM（PROBLEM_MATERIAL）  CLEAN P × CLEAN M
  FAM-PR（PROBLEM_REACTION）  CLEAN P × KB 机制词
  FAM-PC（PROBLEM_CONTEXT）   CLEAN P × C（含 QGS-learned 社区词）

约束（用户 2026-08-28 定稿）：
  - 不手写 QGS missed 单篇 query；QGS-learned 词只以社区/术语级别进入
  - QGS-learned 词自动登记 leakage_ledger_v2.json
  - 每 family budget = K（默认 200，第一版不做动态预算）
  - 生成 query 形式：TITLE-ABS-KEY("term") / TITLE-ABS-KEY("P" AND "X")

用法：
  python tools/build_query_families.py            # 构建并写入 data/query_registry_v2.json
  python tools/build_query_families.py --plan-only  # 只打印不写文件
"""
import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from search_engine.query.concept_slots import INITIAL_CONCEPTS, concepts_by_slot
from search_engine.query.query_family import Family, QueryVariant
from search_engine.query.variant_generator import generate_variants
from search_engine.query.family_registry import (save_registry, save_ledger,
                                                 load_ledger, REGISTRY_PATH)

K_DEFAULT = 200   # 每 family 相同 retrieval budget（第一版 K_f = K）


def _q(term: str) -> str:
    """编译：单短语 query。"""
    return f'TITLE-ABS-KEY("{term}")'


def _q_and(p_term: str, other: str) -> str:
    """编译：P × 其他（AND）。"""
    return f'TITLE-ABS-KEY("{p_term}" AND "{other}")'


def _concept(term: str, slot: str):
    for c in INITIAL_CONCEPTS:
        if c.term == term and c.slot == slot:
            return c
    return None


def build_families() -> list[Family]:
    by = concepts_by_slot()
    clean_p = [c for c in by["problem"] if not c.leakage]        # CLEAN P 词
    stress_p = [c for c in by["problem"] if c.term in
                ("contraction stress", "setting stress", "hardening stress",
                 "cure-induced stress")]                          # QGS-learned stress 词
    volume_p = [c for c in by["problem"] if c.term in
                ("polymerization contraction",)]                   # QGS-learned volume 词
    clean_m = [c for c in by["material"] if not c.leakage]
    clean_r = [c for c in by["reaction"] if not c.leakage]
    ctx_all = by["context"]                                       # 含 QGS-learned 社区词

    families: list[Family] = []

    # ── FAM-P：Problem-only（每个 CLEAN P 词一个 family）──
    for i, c in enumerate(clean_p, 1):
        fam = Family(family_id=f"FAM_P_{i:03d}", family_type="PROBLEM_ONLY",
                     concepts={"problem": [c.term]}, budget=K_DEFAULT,
                     provenance_source="RESEARCH_QUESTION")
        for v in [c.term] + generate_variants(c.term, "CLEAN"):
            fam.variants.append(QueryVariant(v, leakage=False, source="CLEAN"))
        fam.generated_queries = [_q(v.term) for v in fam.variants]
        families.append(fam)

    # ── FAM-STRESS：stress 术语独立 family（CLEAN + QGS-learned）──
    fam = Family(family_id="FAM_STRESS", family_type="STRESS_SPECIFIC",
                 concepts={"problem": [c.term for c in clean_p + stress_p]},
                 budget=K_DEFAULT)
    for c in clean_p + stress_p:
        src = "QGS_V1_LEARNED" if c.leakage else "CLEAN"
        for v in generate_variants(c.term, src, c.leakage):
            fam.variants.append(QueryVariant(v, leakage=c.leakage, source=src,
                                             note="P3-E HISTORICAL_TERMINOLOGY" if c.leakage else ""))
    fam.generated_queries = [_q(v.term) for v in fam.variants]
    families.append(fam)

    # ── FAM-VOLUME：volume/contraction 术语独立 family ──
    fam = Family(family_id="FAM_VOLUME", family_type="VOLUME_FAMILY",
                 concepts={"problem": ["volumetric shrinkage", "polymerization contraction"]},
                 budget=K_DEFAULT)
    for term, leak in (("volumetric shrinkage", False),
                       ("polymerization contraction", True),
                       ("volume change", True), ("dimensional change", True)):
        src = "QGS_V1_LEARNED" if leak else "CLEAN"
        for v in generate_variants(term, src, leak):
            fam.variants.append(QueryVariant(v, leakage=leak, source=src))
    fam.generated_queries = [_q(v.term) for v in fam.variants]
    families.append(fam)

    # ── FAM-PM：Problem × Material（CLEAN × CLEAN）──
    for i, cp in enumerate(clean_p, 1):
        for j, cm in enumerate(clean_m, 1):
            fam = Family(family_id=f"FAM_PM_{i:03d}{j:03d}", family_type="PROBLEM_MATERIAL",
                         concepts={"problem": [cp.term], "material": [cm.term]},
                         budget=K_DEFAULT)
            pv = [cp.term] + generate_variants(cp.term, "CLEAN")   # 始终含主词
            mv = [cm.term] + generate_variants(cm.term, "CLEAN")
            for v in pv:
                for v2 in mv:
                    fam.generated_queries.append(_q_and(v, v2))
            families.append(fam)

    # ── FAM-PR：Problem × Reaction（CLEAN P × KB 机制词）──
    for i, cp in enumerate(clean_p, 1):
        for j, cr in enumerate(clean_r, 1):
            fam = Family(family_id=f"FAM_PR_{i:03d}{j:03d}", family_type="PROBLEM_REACTION",
                         concepts={"problem": [cp.term], "reaction": [cr.term]},
                         budget=K_DEFAULT, provenance_source="KB")
            pv = [cp.term] + generate_variants(cp.term, "CLEAN")
            rv = [cr.term] + generate_variants(cr.term, "CLEAN")
            for v in pv:
                for v2 in rv:
                    fam.generated_queries.append(_q_and(v, v2))
            families.append(fam)

    # ── FAM-PC：Problem × Context（CLEAN P × 全部 C 词，含 QGS-learned 社区）──
    for i, cp in enumerate(clean_p, 1):
        for j, cc in enumerate(ctx_all, 1):
            fam = Family(family_id=f"FAM_PC_{i:03d}{j:03d}", family_type="PROBLEM_CONTEXT",
                         concepts={"problem": [cp.term], "context": [cc.term]},
                         budget=K_DEFAULT,
                         provenance_source="KB" if not cc.leakage else "QGS_V1_LEARNED",
                         derived_from_qgs_v1=cc.leakage)
            pv = [cp.term] + generate_variants(cp.term, "CLEAN")
            cv = [cc.term] + generate_variants(cc.term,
                                               "QGS_V1_LEARNED" if cc.leakage else "CLEAN",
                                               cc.leakage)
            for v in pv:
                for v2 in cv:
                    fam.variants.append(QueryVariant(
                        f"{v} AND {v2}", leakage=cc.leakage,
                        source="QGS_V1_LEARNED" if cc.leakage else "CLEAN"))
                    fam.generated_queries.append(_q_and(v, v2))
            families.append(fam)

    return families


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan-only", action="store_true", help="只打印不写 registry")
    ap.add_argument("--save", default=REGISTRY_PATH)
    args = ap.parse_args()

    families = build_families()
    n_q = sum(len(f.generated_queries) for f in families)
    n_leak = sum(len(f.leakage_variants) for f in families)

    print(f"families: {len(families)}  (types: "
          + ", ".join(f"{t}:{sum(1 for f in families if f.family_type == t)}"
                      for t in sorted({f.family_type for f in families})))
    print(f"generated queries: {n_q}  | QGS-learned variants: {n_leak}")
    print(f"每 family budget: {K_DEFAULT}（第一版 K_f=K，不做动态预算）")
    print()
    for f in families:
        leak = f"[leakage {len(f.leakage_variants)}]" if f.leakage_variants else ""
        print(f"  {f.family_id:<14} {f.family_type:<20} queries={len(f.generated_queries):<3} "
              f"concepts={f.concepts} {leak}")

    # QGS-learned 词登记 leakage ledger（无论 plan-only 与否都打印预览）
    learned = []
    for f in families:
        for v in f.leakage_variants:
            learned.append({"term": v.term, "evidence": v.note or "P3-E failure analysis",
                            "used_in": [f.family_id]})
    # 去重
    seen = set()
    unique_learned = []
    for t in learned:
        if t["term"] not in seen:
            seen.add(t["term"])
            unique_learned.append(t)
    print(f"\nQGS-learned 术语（将登记 leakage ledger，{len(unique_learned)} 个）:")
    for t in unique_learned:
        print(f"  {t['term']:<28} used_in={t['used_in']}")

    if args.plan_only:
        print("\n[plan-only] 未写文件")
        return

    registry = {"registry_version": 2, "architecture": "query_family",
                "created_at": "2026-08-28",
                "note": "v2.0 Query-Family Diversification（单一变量实验 B 侧）",
                "families": [f.to_dict() for f in families]}
    save_registry(registry, args.save)
    from search_engine.query.family_registry import register_leakage_terms
    n = register_leakage_terms(unique_learned)
    print(f"\n✓ registry 已写: {args.save}（{len(families)} families, {n_q} queries）")
    print(f"✓ leakage ledger 登记: {n} 条新（data/leakage_ledger_v2.json）")


if __name__ == "__main__":
    main()
