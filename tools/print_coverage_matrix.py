"""打印 route × mechanism coverage matrix（纯本地，基于 edges + CoverageMatcher）。

Phase 1.8：covered = 存在 supporting edge。统计口径唯一（compute_gap_coverage）：
  DIRECT covered / INHERITED covered / TOTAL covered / OPEN 分开打印，
  不再出现"一个工具报 5、另一个报 8"的口径分裂。
  每个 target 带 type（MECHANISM / ROUTE_PROPERTY / EFFECT）——Phase 1.7 gap
  search 只针对 MECHANISM，后两类展示但不参与 completeness。

用法:
    python tools/print_coverage_matrix.py [--route AFCT]
"""

import argparse
import sys

from search_engine.knowledge_base import KnowledgeBase
from search_engine.route_mechanism_ontology import (
    compute_gap_coverage, get_mechanisms, CORE_ROUTE_MECHANISMS, knowledge_status,
    missing_reason, extraction_subtype,
)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

_TYPE_TAG = {"MECHANISM": "M", "ROUTE_PROPERTY": "P", "EFFECT": "E"}


def main():
    ap = argparse.ArgumentParser(description="打印 route×mechanism coverage matrix（基于 edges）")
    ap.add_argument("--route", default="", help="只看指定 core route")
    args = ap.parse_args()

    kb = KnowledgeBase()
    records = kb.get_all()
    all_edges = []
    for rec in records:
        all_edges.extend(rec.route_mechanism_edges)

    gap_cov = compute_gap_coverage(all_edges)

    stats = {"DIRECT_MODEL": 0, "DIRECT_HUMAN": 0, "DOMAIN_VERIFIED": 0, "INHERITED": 0, "OPEN": 0}
    n_by_type = {"MECHANISM": {"covered": 0, "open": 0},
                 "ROUTE_PROPERTY": {"covered": 0, "open": 0},
                 "EFFECT": {"covered": 0, "open": 0}}
    # 知识状态层统计（用户定：知识完备度只算 confirmed/domain_confirmed，hypothesis 单独记探索）
    ks_stats = {"confirmed": {"covered": 0, "open": 0},
                "domain_confirmed": {"covered": 0, "open": 0},
                "confirmed_missing": {"covered": 0, "open": 0},
                "hypothesis": {"covered": 0, "open": 0},
                "rejected": {"covered": 0, "open": 0},
                "unknown": {"covered": 0, "open": 0}}
    # confirmed_missing 的修复路线细分（用户定 2026-08-25：不能混成一个整体）
    reason_stats = {"SEARCH_GAP": {"covered": 0, "open": 0},
                    "EXTRACTION_GAP": {"covered": 0, "open": 0}}
    # EXTRACTION_GAP 二级细分（MISSING_EDGE / EDGE_TYPE_ERROR，断点位置不同）
    extract_stats = {"MISSING_EDGE": {"covered": 0, "open": 0},
                     "EDGE_TYPE_ERROR": {"covered": 0, "open": 0}}
    # 标注迁移候选（confirmed_missing 但已有 edge → 按证据强度建议升 confirmed / domain_confirmed）
    migrate_to_confirmed: list = []
    migrate_to_domain: list = []

    print("=" * 78)
    print(f"Coverage Matrix  (records={len(records)}, edges={len(all_edges)})")
    print("=" * 78)
    for core in CORE_ROUTE_MECHANISMS:
        if args.route and core != args.route:
            continue
        print(f"\n{core.upper()}")
        for mech in get_mechanisms(core):
            info = gap_cov[(core, mech)]
            status, best, inferred, mtype = (info["status"], info["best"],
                                             info["inferred"], info["type"])
            tag = _TYPE_TAG.get(mtype, "?")
            kst = knowledge_status(core, mech)
            stats[status] += 1
            covered = status != "OPEN"
            n_by_type[mtype]["covered" if covered else "open"] += 1
            ks_stats[kst]["covered" if covered else "open"] += 1
            if kst == "confirmed_missing":
                reason = missing_reason(core, mech)
                reason_stats[reason]["covered" if covered else "open"] += 1
                if reason == "EXTRACTION_GAP":
                    extract_stats[extraction_subtype(core, mech)]["covered" if covered else "open"] += 1
                if covered:
                    # 迁移候选：DIRECT edge → confirmed；DOMAIN_VERIFIED edge → domain_confirmed
                    if status in ("DIRECT_MODEL", "DIRECT_HUMAN"):
                        migrate_to_confirmed.append((core, mech))
                    elif status == "DOMAIN_VERIFIED":
                        migrate_to_domain.append((core, mech))
            # 三态显示：✓ confirmed / ? hypothesis / ○ rejected；未标注默认按证据显示
            if status == "OPEN":
                if kst == "hypothesis":
                    mark = "?"
                elif kst == "rejected":
                    mark = "○"
                elif kst == "confirmed_missing" and missing_reason(core, mech) == "EXTRACTION_GAP":
                    mark = "▲" if extraction_subtype(core, mech) == "MISSING_EDGE" else "⚠"
                else:
                    mark = "✗"   # confirmed_missing/SEARCH_GAP / unknown：真漏检候选
                line = f"  {mark} [{tag}] {mech:<32} [{kst:<16}]"
                if kst == "confirmed_missing":
                    line += f"  ({missing_reason(core, mech)}"
                    if missing_reason(core, mech) == "EXTRACTION_GAP":
                        line += f"/{extraction_subtype(core, mech)}"
                    line += ")"
                if inferred:
                    line += f"  (?) {len(inferred)} 条 inferred 候选: " + \
                            ", ".join(e.paper_id[-14:] for e in inferred[:2])
                print(line)
            else:
                if status == "DIRECT_HUMAN":
                    mark = "✓[DH]"
                elif status == "DOMAIN_VERIFIED":
                    mark = "✓[DV]"
                else:
                    mark = "✓"
                print(f"  {mark} [{tag}] {mech:<32} {status:<13} [{kst}] conf={best.confidence:.2f}  "
                      f"paper={best.paper_id[-18:]}")
                if best.evidence:
                    print(f"      evidence: {best.evidence[:90]}")
                if inferred:
                    print(f"      (?) {len(inferred)} 条 inferred 候选: "
                          + ", ".join(e.paper_id[-14:] for e in inferred[:2]))

    print("\n" + "=" * 78)
    n_strict = stats["DIRECT_MODEL"] + stats["DIRECT_HUMAN"]
    n_domain = n_strict + stats["DOMAIN_VERIFIED"]
    covered = n_domain + stats["INHERITED"]
    print(f"DIRECT_MODEL covered    = {stats['DIRECT_MODEL']}")
    print(f"DIRECT_HUMAN covered    = {stats['DIRECT_HUMAN']}   ← 人工核实（评估 extractor 时单独统计）")
    print(f"DOMAIN_VERIFIED covered = {stats['DOMAIN_VERIFIED']}   ← 领域确认但论文表述弱（不伪装 DIRECT）")
    print(f"INHERITED covered       = {stats['INHERITED']}")
    print(f"C_strict (DM+DH)        = {n_strict}")
    print(f"C_domain (strict+DV)    = {n_domain}")
    print(f"TOTAL covered           = {covered}")
    print(f"OPEN                    = {stats['OPEN']}")
    print(f"(checklist total        = {len(gap_cov)})")

    print("\n知识状态层（用户定：知识库构建 ≠ 完备性验证）:")
    for k in ("confirmed", "domain_confirmed", "confirmed_missing", "hypothesis", "rejected", "unknown"):
        c = ks_stats[k]
        print(f"  {k:<18} covered={c['covered']}  open={c['open']}"
              + ("   ← 领域确认 + DIRECT paper evidence" if k == "confirmed"
                 else ("   ← 领域确认 + DOMAIN_VERIFIED（论文表述弱，不算 strict）" if k == "domain_confirmed" else "")))
    print("  confirmed_missing 细分:")
    for r in ("SEARCH_GAP", "EXTRACTION_GAP"):
        c = reason_stats[r]
        print(f"    {r:<14} covered={c['covered']}  open={c['open']}"
              + ("   ← completeness mode 搜索" if r == "SEARCH_GAP"
                 else "   ← 修 extractor / add_human_edge（不让搜索 agent 浪费时间）"))
    print("  EXTRACTION_GAP 二级细分（断点位置）:")
    for s in ("MISSING_EDGE", "EDGE_TYPE_ERROR"):
        c = extract_stats[s]
        print(f"    {s:<15} covered={c['covered']}  open={c['open']}"
              + ("   ← extractor 漏抽/抽错机制（LLM extraction miss）" if s == "MISSING_EDGE"
                 else "   ← edge 存在但 relation_type 判错（inferred 应 direct）"))
    # 指标1：知识完备度——三个 coverage 数字（用户定 2026-08-25，provenance 比数字重要）：
    #   分母 D = confirmed 全量 + domain_confirmed 全量 + confirmed_missing 仍 open
    #           （领域确认的机制全集，守恒）
    #   Strict = DIRECT_MODEL + DIRECT_HUMAN（论文直接支撑，可审计）
    #   Domain = Strict + DOMAIN_VERIFIED（含领域知识确认）
    #   Total  = Domain + INHERITED（图谱规模展示，不用于 completeness）
    conf_total = ks_stats["confirmed"]["covered"] + ks_stats["confirmed"]["open"]
    dom_conf_total = ks_stats["domain_confirmed"]["covered"] + ks_stats["domain_confirmed"]["open"]
    cm_missing_open = ks_stats["confirmed_missing"]["open"]
    denom = conf_total + dom_conf_total + cm_missing_open
    c_strict = stats["DIRECT_MODEL"] + stats["DIRECT_HUMAN"]
    c_domain = c_strict + stats["DOMAIN_VERIFIED"]
    c_total = c_domain + stats["INHERITED"]
    cov_strict = (c_strict / denom) if denom else 1.0
    cov_domain = (c_domain / denom) if denom else 1.0
    cov_total = (c_total / denom) if denom else 1.0
    n_search = reason_stats["SEARCH_GAP"]["open"]
    n_extract = reason_stats["EXTRACTION_GAP"]["open"]
    print(f"\n指标1 · 知识完备度（分母 D = confirmed {conf_total} + domain_confirmed {dom_conf_total}"
          f" + confirmed_missing_open {cm_missing_open} = {denom}）")
    print(f"  ① Strict Evidence Coverage = DIRECT_MODEL+DIRECT_HUMAN = {c_strict} → {cov_strict:.1%}"
          f"（论文直接支撑——完整性证明口径）")
    print(f"  ② Domain Coverage          = ①+DOMAIN_VERIFIED = {c_domain} → {cov_domain:.1%}"
          f"（含领域知识确认，论文表述弱）")
    print(f"  ③ Total Evidence Graph     = ②+INHERITED = {c_total} → {cov_total:.1%}"
          f"（图谱规模展示，不用于 completeness）")
    print(f"报告句式: 当前知识库严格口径覆盖 {cov_strict:.0%} 的已确认机制"
          f"（含领域确认 {cov_domain:.0%}）；"
          f"剩余缺口中 {n_search} 个检索不足（SEARCH_GAP 进 loop），"
          f"{n_extract} 个结构化抽取不足（EXTRACTION_GAP 修 extractor/人工），"
          f"另有 {ks_stats['hypothesis']['open']} 个科学假说等待探索。")
    # 标注迁移提示：confirmed_missing 已有 edge → 按证据强度升 confirmed / domain_confirmed
    if migrate_to_confirmed:
        print(f"⚠ {len(migrate_to_confirmed)} 个 confirmed_missing 已有 DIRECT edge → 建议升 confirmed: "
              + ", ".join(f"{r}×{m}" for r, m in migrate_to_confirmed))
    if migrate_to_domain:
        print(f"⚠ {len(migrate_to_domain)} 个 confirmed_missing 已有 DOMAIN_VERIFIED edge → 建议升 domain_confirmed: "
              + ", ".join(f"{r}×{m}" for r, m in migrate_to_domain))
    # 指标2：探索覆盖度（hypothesis 中有 inferred 候选/已覆盖的视为探索过）
    hyp_total = ks_stats["hypothesis"]["covered"] + ks_stats["hypothesis"]["open"]
    print(f"指标2 · 探索覆盖度 Exploration = hypothesis 总数 {hyp_total}"
          f"（open {ks_stats['hypothesis']['open']} 个待探索）")

    print("\n按类型:")
    for t in ("MECHANISM", "ROUTE_PROPERTY", "EFFECT"):
        c = n_by_type[t]
        print(f"  {t:<14} covered={c['covered']}  open={c['open']}"
              + ("   ← Phase 1.7 gap search 目标" if t == "MECHANISM" else "   ← 展示，不参与 completeness"))
    print("\n✓ = supporting edge；✗ = SEARCH_GAP 无证据（进 loop）；▲ = MISSING_EDGE 论文在库但无该 edge；"
          "⚠ = EDGE_TYPE_ERROR edge 在但 inferred 应 direct；? = hypothesis；(?) = LLM 推断（不关 gap）")
    kb.close()


if __name__ == "__main__":
    main()
