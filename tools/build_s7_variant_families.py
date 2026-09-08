"""build_s7_variant_families.py — 从 S7 v2 QA 盲评 reason 提炼变体族词典（词典通道主干）。

背景（2026-09-07 QA v2 calibration）：
  v2 novelty_gate 按 B 端词源层判 verdict（FRESH_NORMALIZED/DISCOVERY_ONLY/...），
  但 QA 盲评显示 FRESH_NORMALIZED 19 条里 10 条 QA=LOW——全部死于同一模式：
  B 端词面是 S6 已扫 observable 的**变体/同族**（warpage~structural deformation、
  marginal gap~marginal leakage、crack~enamel crack propagation...），字面匹配抓不到。

设计（用户 Q1 裁决：词典 + embedding 双通道）：
  ① 词典通道：本文件输出 s7_variant_families.json —— 冻结同族表。
     canonical = S6 已扫 observable / outcome 参照词
     variants  = QA reason 明示或领域同族的候选 B 词面（v3 gate 命中→novelty_risk）
  ② embedding 通道：v3 gate 中对词典未覆盖的 B 做 BGE/相似度判定（本文件不含）。

用法：
  python tools/build_s7_variant_families.py            # 生成 + 回测
  python tools/build_s7_variant_families.py --dry-only # 仅打印词典不写文件

产物：
  data/exports/terminology/s7_variant_families.json
  (诊断打印: 词典标记 vs QA novelty 的吻合统计——development data 只诊断不调参)
"""
import argparse
import json
import os
from collections import Counter, defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T = os.path.join(BASE, "data", "exports", "terminology")
OUT = os.path.join(T, "s7_variant_families.json")


# ── 冻结词典（人工归纳，来源 = QA reason 原文标注）────────────────────────────
# canonical 尽量用 S6 A 类 / C 类 outcome 参照词原文；
# QA reason 原句放进 evidence，保证可追溯。
VARIANT_FAMILIES = [
    {
        "family_id": "F01_structural_deformation",
        "canonical": "structural deformation",          # S6 A 类已扫
        "variants": [
            "warpage", "warping", "deformation", "distortion",
            "dimensional change", "geometric inaccuracy",
        ],
        "domain": None,     # structural deformation 变体为通用语言层（不限 domain）
                            # RC24 deformation@dental 亦 LOW；deformation 变体 dental/3dp 通用
        "evidence": [
            "RC04: Warpage in 3DP is a structural deformation variant already covered",
            "RC10: warpage are both already in S6's explored 3DP observable space",
            "RC24: deformation ... A: softening effect, structural deformation",
            "RC35: warpage is a variant of structural deformation already explored",
        ],
        "note": "QA 对 warpage 有 HIGH 例外(RC26 delayed gel point→warpage)：A 端具体机制可救。"
                "故词典只作 risk 标记不作硬拒。",
    },
    {
        "family_id": "F02_marginal_gap_leakage",
        "canonical": "marginal leakage",                # S6 A 类已扫
        "variants": ["marginal gap", "marginal seal", "microleakage", "gap formation"],
        "domain": ["dental"],
        "evidence": [
            "RC06: marginal gap are already in S6's explored dental observable space",
            "RC14: marginal gap directly within S6's explored dental shrinkage space",
            "RC25: marginal gap is in A (S6 explored)",
            "RC33: marginal gap direct duplicate of explored dental cluster",
        ],
        "note": "dental 全 LOW，无一例外；marginal gap 词面不在 S6 字面集合，纯变体。",
    },
    {
        "family_id": "F03_crack_enamel",
        "canonical": "enamel crack propagation",        # S6 A 类已扫（dental）
        "variants": ["crack", "cracking", "micro-crack", "crack propagation",
                     "enamel crack"],
        "domain": ["dental"],
        "evidence": [
            "RC15: Crack propagation leading to marginal gap ... already covered",
            "RC19: Internal gaps leading to cracks is a direct dental consequence",
        ],
        "note": "crack 在 coatings RC13/23 是 MEDIUM（跨 domain 复活）——词典 domain 限定 dental。",
    },
    {
        "family_id": "F04_shrink_outcome",
        "canonical": "low shrinkage stress",            # S6 C 类 outcome（非 A 类！）
        "variants": ["near-net-zero shrinkage", "reduced shrinkage", "film shrinkage",
                     "low-shrinkage", "low shrinkage", "net-zero shrinkage",
                     "zero shrinkage", "minimal shrinkage"],
        "domain": ["dental", "composites", "coatings", "3dp"],
        "evidence": [
            "RC02: Near-net-zero shrinkage is a direct synonym of low-shrinkage outcome "
            "already used in S6's anchored outcomes",
            "RC05: Film shrinkage is explicitly listed as an anchored outcome",
            "RC29/RC37: near-net-zero shrinkage ... anchored outcomes (C/B)",
            "RC11/RC14: shrinkage word family × dental observable",
        ],
        "note": "S6 C 类 mechanism_to_outcome_shrink 已把 shrink outcome 当 B 扫过。"
                "B 端命中此族→LOW（RC02/05/29/37 全 LOW）；但 A 端命中不降级"
                "（RC09 near-net-zero shrinkage→dimensional accuracy MEDIUM：A 端可用）。",
    },
    {
        "family_id": "F05_internal_gap_void",
        "canonical": "internal gap",                    # S6 A 类已扫
        "variants": ["internal void", "porosity", "pore", "gas entrapment",
                     "void formation"],
        "domain": ["dental", "3dp", "composites"],
        "evidence": [
            "RC11: Low-shrinkage composites and internal gap formation already covered",
            "RC19: Internal gaps leading to cracks is a direct dental consequence",
        ],
        "note": "void formation 本身即 S6 A 类词。",
    },
    {
        "family_id": "F06_printability",
        "canonical": "printability",                    # S6 A 类已扫（3dp）
        "variants": ["print fidelity", "printing accuracy", "build accuracy",
                     "layer adhesion"],
        "domain": ["3dp", "sla"],
        "evidence": [
            "RC10: Printability and warpage both already in S6's explored 3DP space",
            "RC22: cross-domain drift (D) already showed low relevance",
        ],
        "note": "",
    },
]

# domain 饱和参照（QA 判 LOW 的 domain 语境）：S6 A 类在该 domain 的 observable 密度
# dental 4/10 由 A 类直接 + 临床后果链；3dp 次之；coatings/composites/optics 近空白
DOMAIN_SATURATION = {
    "dental": 0.74,      # QA LOW 率（40 条实测，development diagnostic）
    "3dp": 0.43,
    "coatings": 0.17,
    "composites": 1.0,   # 样本少(n=1)仅 diagnostic
}


def build():
    return {
        "version": "1.0",
        "status": "FROZEN_DEVELOPMENT_DIAGNOSTIC",
        "source": "s7_relation_qa_v2.json QA reason 提炼 + 领域归纳（2026-09-07）",
        "usage": "v3 novelty gate 词典通道：B 端命中 variants→novelty_risk.variant_family，"
                 "verdict 不变（QA 终审）；不硬拒，仅供排序/降档参照",
        "canonical_source": "S6 trajectory A 类 observable + C 类 shrink outcome",
        "domain_saturation": DOMAIN_SATURATION,
        "families": VARIANT_FAMILIES,
    }


def variant_lookup():
    """term → family_id 映射（含 canonical 自身）"""
    m = {}
    for fam in VARIANT_FAMILIES:
        for v in [fam["canonical"]] + fam["variants"]:
            m[v.lower()] = fam["family_id"]
    return m


def backtest():
    """用 40 条 QA 盲评对照词典命中率（development data 诊断，不调参）。"""
    cands = json.load(open(os.path.join(T, "s7_relation_candidates_v2.json"),
                           encoding="utf-8"))["candidates"]
    qa = json.load(open(os.path.join(T, "s7_relation_qa_v2.json"),
                        encoding="utf-8"))["labels"]
    cm = {x["candidate_id"]: x for x in cands}
    vmap = variant_lookup()
    rows = []
    for cid, l in qa.items():
        x = cm[cid]
        B = x["concept_B"].lower().rstrip("*")
        fam = vmap.get(B)
        fam_doms = (next(f["domain"] for f in VARIANT_FAMILIES
                         if f["family_id"] == fam) if fam else None)
        dom_ok = fam and (fam_doms is None or x["domain"] in fam_doms)
        rows.append((cid, x["novelty_gate"]["verdict"], B, l["novelty"],
                     l["search_action"], fam if dom_ok else None))
    print("=== 词典命中 vs QA novelty（40 条，development diagnostic）===")
    print(f"{'cid':5s} {'gate':22s} {'B端':34s} {'QA':6s} {'act':4s} family")
    for cid, g, B, nv, act, fam in sorted(rows):
        print(f"{cid:5s} {g:22s} {B:34s} {nv:6s} {act:4s} {fam or ''}")
    hit = [r for r in rows if r[5]]
    miss = [r for r in rows if not r[5]]
    print(f"\n词典命中 {len(hit)} 条:")
    print("  命中且 QA=LOW :", sum(1 for r in hit if r[3] == "LOW"))
    print("  命中但 QA!=LOW:", sum(1 for r in hit if r[3] != "LOW"))
    print(f"未命中 {len(miss)} 条（交给 embedding 通道 / QA）:")
    for r in miss:
        if r[3] == "LOW":
            print("   [未命中但QA=LOW]", r[1], r[2])
    # gate=FRESH 且 QA=LOW 的 10 条里词典能标几条？
    fresh_low = [r for r in rows if r[1] == "FRESH_NORMALIZED" and r[3] == "LOW"]
    fresh_low_hit = [r for r in fresh_low if r[5]]
    print(f"\n核心病灶（FRESH_NORMALIZED 且 QA=LOW）{len(fresh_low)} 条，"
          f"词典命中 {len(fresh_low_hit)} 条 = "
          f"{len(fresh_low_hit)/max(len(fresh_low),1):.0%}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-only", action="store_true", help="只打印不写文件")
    args = ap.parse_args()
    data = build()
    print(json.dumps(data, ensure_ascii=False, indent=1)[:2000])
    print("\n=== 回测 ===")
    backtest()
    if not args.dry_only:
        json.dump(data, open(OUT, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print(f"\n[OK] 写入 {OUT}")


if __name__ == "__main__":
    main()
