"""tools/generate_hop2_round2_queries.py — STEP 7：TC_008 → Slot → Query Family（QGS-blind）。

纪律（用户 2026-08-28 冻结）：
  ① TC_008 选择必须是 QGS-blind（已在 QGS-blind 多排序验证：retNov 第 1 / composite
     第 2 / bridge & papers 第 3 → selection_mode=QGS_BLIND）
  ② Slot mapping 只看 community 自身证据（top_terms_used），规则分类器，不读 QGS
  ③ Query 生成不读取 QGS title（脚本只读 term_communities_hop2 + promotions_hop2）
  ④ 生成后冻结 JSON（community evidence → terms → slots → family/query IDs + query 文本）
  ⑤ 之后才允许 Scopus 执行（本脚本不联网）
  ⑥ 最后才允许 QGS regression（本脚本不含任何 QGS 逻辑）

输入：data/exports/community_promotions_hop2.json（TC_008 条目）
输出：data/exports/community_hop2_round2_queries.json（冻结）

slot 映射（纯规则，QGS-blind）：
  P = 聚合/收缩/应力问题词（polymeriz|shrinkage|contraction|stress|conversion|volume）
  M = 材料词（material|composite|resin|filler|monomer|oligomer|polymer）
  R = 机制/过程词（curing|cure|kinetics|photo|activation|initiation|reaction）
  C = 应用/语境词（restoration|dental|cavity|tooth|clinical|filling）
"""
import argparse
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

PROMOTIONS_PATH = os.path.join(BASE, "data", "exports", "community_promotions_hop2.json")
OUT_PATH = os.path.join(BASE, "data", "exports", "community_hop2_round2_queries.json")
K_DEFAULT = 200
COMMUNITY_ID = "TC_008"

P_RE = re.compile(r"polymeriz|shrinkage|contraction|stress|conversion|volume", re.I)
M_RE = re.compile(r"material|composite|resin|filler|monomer|oligomer|polymer", re.I)
R_RE = re.compile(r"curing|cure|kinetics|photo|activat|initiation|reaction", re.I)
C_RE = re.compile(r"restoration|dental|cavity|tooth|clinical|filling", re.I)


def auto_slot_map(terms: list[str]) -> dict:
    """规则分类（QGS-blind）：一个 term 可归多个 slot。"""
    slots = {"P": [], "M": [], "R": [], "C": []}
    for t in terms:
        if P_RE.search(t):
            slots["P"].append(t)
        if M_RE.search(t):
            slots["M"].append(t)
        if R_RE.search(t):
            slots["R"].append(t)
        if C_RE.search(t):
            slots["C"].append(t)
    return slots


def slot_phrase(slot_terms: list[str], role: str) -> list[str]:
    """slot → query 短语（去停用词化后的核心词，用于 AND 组合）。"""
    out = []
    for t in slot_terms:
        core = re.sub(r"\b(composite )?restorative (materials)?\b", "", t)
        core = core.strip()
        out.append(core or t)
    return sorted(set(out))


def q_and(p: str, x: str) -> str:
    return f'TITLE-ABS-KEY("{p}" AND "{x}")'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--promotions", default=PROMOTIONS_PATH)
    ap.add_argument("--out", default=OUT_PATH)
    ap.add_argument("--plan-only", action="store_true")
    args = ap.parse_args()

    promo = json.load(open(args.promotions, encoding="utf-8"))
    tc8 = next(r for r in promo["communities"] if r["community_id"] == COMMUNITY_ID)
    top_terms = tc8["top_terms_used"]

    slots = auto_slot_map(top_terms)
    print("=" * 72)
    print(f"STEP 7 QGS-blind slot mapping（只看 community 自身证据）")
    print("=" * 72)
    print(f"community_id = {COMMUNITY_ID} | role = {tc8['role']} | "
          f"slot_status = {tc8['slot_mapping_status']}")
    print(f"top_terms = {top_terms}")
    print(f"\nproposed_slots（规则分类）:")
    for k in ("P", "M", "R", "C"):
        print(f"  {k} = {slots[k] if slots[k] else '(空)'}")

    # mapping confidence：P 非空 0.4 + M 非空 0.3 + R 非空 0.2 + C 非空 0.1
    conf = (0.4 if slots["P"] else 0) + (0.3 if slots["M"] else 0) \
        + (0.2 if slots["R"] else 0) + (0.1 if slots["C"] else 0)
    evidence = [f"{t} → {[k for k, v in slots.items() if t in v]}" for t in top_terms]
    print(f"\nmapping_confidence = {conf:.2f}（P/M/R/C 全空=0，四槽全有=1.0）")
    print(f"mapping_evidence:")
    for e in evidence:
        print(f"  - {e}")

    if args.plan_only:
        return

    # ── query 生成（QGS-blind，只用 slots）──
    # P 是问题锚；与 M/R/C 组合
    p_terms = slots["P"] or ["polymerization"]   # 兜底：社区若有聚合语境
    fams = []
    queries = []
    qn = 0
    combos = [("M", slots["M"]), ("R", slots["R"]), ("C", slots["C"])]
    for slot_key, slot_terms in combos:
        for x in slot_terms:
            for p in p_terms:
                if x == p or x in p_terms:
                    continue   # 自交/冗余组合（Q05 型）跳过
                qn += 1
                fid = f"V2H2_{COMMUNITY_ID}_{slot_key}{qn:02d}"
                fams.append({"family_id": fid,
                             "community_id": COMMUNITY_ID,
                             "slot": slot_key,
                             "concept": x,
                             "provenance": {
                                 "community_evidence": top_terms,
                                 "slot_mapping": slots,
                                 "mapping_confidence": round(conf, 2),
                                 "selection_mode": "QGS_BLIND",
                                 "leakage": False}})
                queries.append({"query_id": f"{fid}::Q{qn:02d}",
                                "family_id": fid, "community_id": COMMUNITY_ID,
                                "slot": slot_key, "query": q_and(p, x)})

    # 跨 slot 重复 query 去重（如 activated restorative materials 同时归 M/R）
    seen_q: set[str] = set()
    uniq_queries = []
    for q in queries:
        if q["query"] in seen_q:
            continue
        seen_q.add(q["query"])
        uniq_queries.append(q)
    queries = uniq_queries

    out = {
        "version": "hop2_round2_qgs_blind",
        "created_at": "2026-08-28",
        "community": COMMUNITY_ID,
        "selection": {
            "mode": "QGS_BLIND",
            "evidence": "QGS-blind 排序 retNov 第 1（0.9）/ composite 第 2 / bridge&papers 第 3；"
                        "role=BOUNDARY_COMMUNITY, slot=MAPPABLE",
            "leakage": False,
        },
        "slot_mapping": {"slots": slots, "confidence": round(conf, 2),
                         "evidence": evidence},
        "families": fams,
        "queries": queries,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n✓ 已冻结: {args.out}（{len(fams)} families / {len(queries)} queries）")
    print("frozen queries:")
    for q in queries:
        print(f"  {q['query_id']:<42} {q['query']}")


if __name__ == "__main__":
    main()
