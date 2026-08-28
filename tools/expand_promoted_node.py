"""Phase 2.1b Knowledge Expander CLI（用户定 2026-08-26，P2.1 只做 query 生成）。

用法:
    # 只读：生成 + 对历史 registry 去重统计（不写任何东西）
    python tools/expand_promoted_node.py --name "bulk-fill composite formulation" --plan-only

    # 正式：生成并注册进 query registry（硬 invariant：normalized 已存在 → 不注册）
    python tools/expand_promoted_node.py --name "bulk-fill composite formulation"

P2.1 只做 query 生成与去重（retrieval 是 P2.2）。输入只吃**本轮新 APPROVED/PROMOTED
的 node**（从候选池 provenance.promotion 读 relations + causal_chain），不展开旧 ontology。
"""

import argparse
import json
import os
import sys

from search_engine.discovery.query_registry import (
    load_registry, save_registry, register, REGISTRY_PATH,
)
from search_engine.discovery.expander import generate_queries, count_by_family

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

POOL_PATH = "data/exports/phase2_candidates.json"
PROMOTIONS_PATH = "data/exports/ontology_promotions.json"


def _latest_revision_relations(c: dict) -> list[dict]:
    """ontology_promotions.json 中该候选最新 revision 的 relations（新格式）。"""
    if not os.path.exists(PROMOTIONS_PATH):
        return []
    try:
        with open(PROMOTIONS_PATH, encoding="utf-8") as f:
            promos = json.load(f)
    except Exception:
        return []
    for p in promos:
        if p.get("candidate_id") != c.get("candidate_id"):
            continue
        revs = p.get("revisions") or []
        if revs:
            return revs[-1].get("relations", [])
        return p.get("proposed_relations", [])
    return []


def _find_promoted(pool: list[dict], name: str) -> dict | None:
    hits = [c for c in pool
            if name.lower() in c.get("raw_name", "").lower()
            or c.get("candidate_id") == name]
    if len(hits) != 1:
        return None
    return hits[0]


def _promotion_context(c: dict) -> tuple[str | None, int | None]:
    """(promotion_id, promotion_round)：从 provenance.promotion + promotion_history 取。"""
    prov = c.get("provenance") or {}
    promo = prov.get("promotion") or {}
    pid = promo.get("candidate_id") or c.get("candidate_id")
    history = prov.get("promotion_history") or []
    rnd = None
    if history:
        rnd = history[-1].get("version")
    return pid, rnd


def main():
    ap = argparse.ArgumentParser(description="Phase 2.1b Knowledge Expander（P2.1：query 生成）")
    ap.add_argument("--name", required=True, help="本轮新 PROMOTED node（如 bulk-fill composite formulation）")
    ap.add_argument("--plan-only", action="store_true", help="只读：生成+去重统计，不写 registry")
    ap.add_argument("--reset", action="store_true",
                    help="先清空该 node 的旧 registry 记录再注册（query 格式升级/重复注册用）")
    args = ap.parse_args()

    if not os.path.exists(POOL_PATH):
        print(f"✗ 候选池不存在: {POOL_PATH}")
        return
    with open(POOL_PATH, encoding="utf-8") as f:
        pool = json.load(f).get("candidates", [])

    c = _find_promoted(pool, args.name)
    if c is None:
        print(f"✗ 找不到唯一候选: {args.name}（检查 raw_name 或 candidate_id）")
        return
    if c.get("status") != "PROMOTED":
        print(f"⚠ 候选当前 {c.get('status')}（不是 PROMOTED）——Expander 只吃新升格 node，继续")

    prov = c.get("provenance") or {}
    promo = prov.get("promotion") or {}
    # relations 优先取 ontology_promotions.json 最新 revision（GROUNDED+DIRECT 新格式），
    # fallback candidate provenance.promotion.proposed_relations（旧 initial 格式）
    relations = _latest_revision_relations(c)
    if not relations:
        relations = promo.get("proposed_relations", [])
    verification = prov.get("verification") or {}
    pid, rnd = _promotion_context(c)

    queries = generate_queries(c["raw_name"], relations=relations,
                               causal_chain=verification.get("causal_chain"),
                               promotion_id=pid, round_id=rnd)

    registry = load_registry()
    if args.reset:
        # 清空该 node 的旧记录（query 格式升级后旧 normalized 与新不同源，需重建）
        before = len(registry)
        registry = [r for r in registry if r.get("source_node") != c["raw_name"]]
        print(f"· --reset：移除该 node 旧记录 {before - len(registry)} 条")
    if args.plan_only:
        # 对历史 registry 做去重统计（不写）
        added, dups = register(list(registry), queries)
        print("=" * 60)
        print(f"Knowledge Expander plan-only（{c['raw_name']}，真正只读）")
        print("=" * 60)
        print(f"Generated: {len(queries)}   Duplicate: {len(dups)}   New: {len(added)}")
        fam = count_by_family(queries)
        for f in ("NODE", "RELATION", "MECHANISM", "ADJACENT"):
            print(f"  {f:<12}{fam.get(f, 0)}")
        print("\n新 query（normalized 不在历史 registry）:")
        for q in added:
            print(f"  [{q.query_family:<8}] {q.query_text}")
        print(f"\nprovenance: promotion_id={pid}  round={rnd}")
        print("[plan-only] 未写任何东西。正式执行去掉 --plan-only。")
        return

    added, dups = register(registry, queries, path=REGISTRY_PATH)
    print(f"✓ 已注册 {len(added)} 条新 query → {REGISTRY_PATH}（跳过重复 {len(dups)}）")
    for q in added:
        print(f"  [{q.query_family:<8}] {q.query_text}")
    print("下一步 P2.2：执行这些新 query 检索论文（expander 不自己判断新知识）")


if __name__ == "__main__":
    main()
