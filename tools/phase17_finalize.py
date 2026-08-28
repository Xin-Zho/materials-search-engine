"""Phase 1.7 验收冻结 manifest（completeness loop 最后一次验收通过）。

生成 data/exports/phase17_final_baseline.json：
  - initial_true_open_mechanism：4 个 SEARCH_GAP ∩ MECHANISM（loop 输入，冻结）
  - final_gap_status：4 个 gap 的最终状态（true_search_gap / fulltext_validation）
  - loop_acceptance：最后一次验收的关键指标（rel / closed / target / cross）
  - gap_audit：每个 gap 的检索漏斗（retr/gate/rel + 诊断类），来自用户实跑结果

用户定（2026-08-26）：Phase 1.7 通过——剩余缺口被正确分类，无架构 bug
（无 canonical/route/identity/commit bug、无 inferred 污染、无 extraction gap 混入 search）。
completeness loop 冻结，不再优化搜索；4 个 gap 状态固化，Phase 2 不携带"不确定 gap"。

用法:
    python tools/phase17_finalize.py            # 写 manifest + 打印冻结表
    python tools/phase17_finalize.py --dry-run  # 只打印不写
"""

import argparse
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 用户实跑 loop 的验收数据（2026-08-26，不可伪造——从 gap_failures_trace.json 汇总）
LOOP_ACCEPTANCE = {
    "round": 1,
    "new_relevant_papers": 2,
    "unique_gaps_closed": 0,
    "target_gaps_closed": 0,
    "cross_gaps_closed": 0,
    "new_direct_model_edges": 0,
    "initial_true_open_before": 4,
    "initial_true_open_after": 4,
    "stop_reason": "no_gap_reduction",
    "verdict": "PASS —— 剩余缺口全部是科学证据不足，无系统 bug",
}

# 每个 gap 的检索漏斗 + 诊断（用户实跑 loop trace 汇总）
GAP_AUDIT = {
    ("ring-opening", "ring strain relief"): {
        "retrieved": 3, "gate": 0, "relevant": 0,
        "diagnosis": "F1_NO_TARGET_EVIDENCE —— 文献说 ring opening reduces shrinkage，"
                     "但没说 because release of ring strain，证据等级不足",
        "final_status": "true_search_gap",
    },
    ("filler", "stress transfer"): {
        "retrieved": 1, "gate": 0, "relevant": 0,
        "diagnosis": "F1_NO_TARGET_EVIDENCE —— 搜到 1 篇但无 stress transfer evidence；"
                     "debonding filler ≠ stress transfer，stress relaxation ≠ load transfer",
        "final_status": "true_search_gap",
    },
    ("monomer-design", "reduced double bond density"): {
        "retrieved": 2468, "gate": 0, "relevant": 1,
        "diagnosis": "F2_FULLTEXT_EVIDENCE_UNVERIFIED —— abstract 级证据不足，"
                     "需 full text / 更好 extraction / 人工确认；不要继续扩大 query",
        "final_status": "fulltext_validation",
    },
    ("monomer-design", "reduced reactive-group density"): {
        "retrieved": 7820, "gate": 0, "relevant": 1,
        "diagnosis": "F2_FULLTEXT_EVIDENCE_UNVERIFIED —— 同上，大量命中但无直接机制证据",
        "final_status": "fulltext_validation",
    },
}


def main():
    ap = argparse.ArgumentParser(description="Phase 1.7 验收冻结 manifest")
    ap.add_argument("--dry-run", action="store_true", help="只打印不写文件")
    args = ap.parse_args()

    manifest = {
        "phase": "phase17_final_baseline",
        "freeze_date": "2026-08-26",
        "note": "Phase 1.7 completeness 验收通过。剩余 4 个 SEARCH_GAP 无架构 bug，"
                "未关闭原因全部是科学证据不足。loop 冻结，Phase 2 ontology discovery 开始。",
        "loop_acceptance": LOOP_ACCEPTANCE,
        "initial_true_open_mechanism": [
            [r, m] for (r, m) in GAP_AUDIT
        ],
        "final_gap_status": {
            f"{r} × {m}": info["final_status"]
            for (r, m), info in GAP_AUDIT.items()
        },
        "gap_audit": {
            f"{r} × {m}": {
                "retrieved": info["retrieved"],
                "gate": info["gate"],
                "relevant": info["relevant"],
                "diagnosis": info["diagnosis"],
            }
            for (r, m), info in GAP_AUDIT.items()
        },
        "excluded_bugs": [
            "canonical bug", "route mismatch", "identity bug",
            "coverage commit bug", "inferred 污染", "extraction gap 混入 search",
        ],
    }

    print("=" * 78)
    print("Phase 1.7 验收冻结（completeness loop 最后一次验收）")
    print("=" * 78)
    print(f"verdict: {LOOP_ACCEPTANCE['verdict']}")
    print(f"  Round 1: rel={LOOP_ACCEPTANCE['new_relevant_papers']}  "
          f"closed={LOOP_ACCEPTANCE['unique_gaps_closed']}  "
          f"target={LOOP_ACCEPTANCE['target_gaps_closed']}  "
          f"cross={LOOP_ACCEPTANCE['cross_gaps_closed']}  "
          f"new_direct_model_edges={LOOP_ACCEPTANCE['new_direct_model_edges']}")
    print(f"  initial_true_open: {LOOP_ACCEPTANCE['initial_true_open_before']}"
          f" -> {LOOP_ACCEPTANCE['initial_true_open_after']}")
    print(f"  stop_reason: {LOOP_ACCEPTANCE['stop_reason']}")
    print("\n冻结的 4 个 gap 最终状态:")
    for (r, m), info in GAP_AUDIT.items():
        print(f"  [{info['final_status']:<20}] {r} × {m}")
        print(f"        retr={info['retrieved']:<5} gate={info['gate']:<2} rel={info['relevant']:<2}"
              f"  {info['diagnosis']}")
    print("\n排除的 6 类 bug:", ", ".join(manifest["excluded_bugs"]))

    if not args.dry_run:
        os.makedirs("data/exports", exist_ok=True)
        path = "data/exports/phase17_final_baseline.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        print(f"\n已冻结: {path}")
    else:
        print("\n[dry-run] 未写文件。正式运行: python tools/phase17_finalize.py")


if __name__ == "__main__":
    main()
