"""分析 autonomous loop：query 漏斗 + coverage commit（global rematch）。

读 data/exports/gap_failures_trace.json（run_autonomous_loop.py 产出）。

第一层 · Query-level（每个 gap target 的检索漏斗）：
    F0_NO_RETRIEVAL                    搜索层 0 召回（fallback 链全空）
    F1_NO_TARGET_EVIDENCE              strict(title/abstract) 搜到但 gate 0
    F2_FULLTEXT_EVIDENCE_UNVERIFIED    fulltext(L2/L3) 搜到但 title/abstract gate 0——
                                       检索证据面(fulltext) 与验证证据面(title/abstract) 不一致，
                                       不能凭 fulltext 命中直接关闭 coverage，也不算 F1
    HAS_LEXICAL_TARGET                gate 通过（title/abstract 含 target 词）；真正机制证据在 extractor 后确认

第二层 · Coverage commit（global rematch，原则：query provenance ≠ knowledge destination）：
    论文因 gap A 被搜到，但对全部 open gaps 重匹配，可能关闭 gap B。

    TARGET_GAP_HIT    补了 originating gap
    CROSS_GAP_HIT     没补 originating，但关闭了其他 open gap（搜 A 学会了 B，正面信号）
    NO_COVERAGE_GAIN  没关闭任何 gap

账务 invariant（loop 内强制）：
    newly_closed = union(paper.closed_gaps) & round_start_open
    closed(unique_gaps_closed) == len(newly_closed)
    本地 commit 关闭集合 == coverage matrix 确认关闭集合（matrix_closed）
    不一致 → COVERAGE_COMMIT_INCONSISTENCY，loop 立即停止。

机制 debug：NO_COVERAGE_GAIN 且 gate_pass=True 的论文（如 dual-curing 样本）
    打印完整 trace（target_mech / extracted_mechanisms / evidence / persisted），
    定位 route/gate 都对但 coverage 没进的问题。

用法:
    python tools/analyze_gap_failures.py
    python tools/analyze_gap_failures.py data/exports/gap_failures_trace.json
"""

import argparse
import asyncio
import json
import os
import sys
from collections import Counter

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def query_category(qt) -> str:
    """query 级分类（含 fulltext 证据面不一致的 F2）。"""
    if qt.get("retrieved", 0) == 0:
        return "F0_NO_RETRIEVAL"
    if qt.get("gap_gate", 0) == 0:
        lv = qt.get("level_used")
        if lv is not None and lv >= 2:
            return "F2_FULLTEXT_EVIDENCE_UNVERIFIED"
        return "F1_NO_TARGET_EVIDENCE"
    return "HAS_LEXICAL_TARGET"


def classify_paper(c):
    """commit 级分类：TARGET_GAP_HIT / CROSS_GAP_HIT / NO_COVERAGE_GAIN。"""
    verdict = c.get("verdict") or "NO_COVERAGE_GAIN"
    if verdict == "TARGET_GAP_HIT":
        return "TARGET_GAP_HIT", "补了 originating gap"
    if verdict == "CROSS_GAP_HIT":
        closed = ";".join(f"{x['route']}×{x['mechanism']}" for x in c.get("closed_gaps", []))[:60]
        return "CROSS_GAP_HIT", f"跨 gap 关闭: {closed}"
    crs = ",".join(c.get("canonical_routes") or [])[:40]
    return "NO_COVERAGE_GAIN", f"无 coverage gain (routes={crs or '?'})"


async def main():
    ap = argparse.ArgumentParser(description="Gap closure + coverage commit analysis")
    ap.add_argument("trace", nargs="?", default="data/exports/gap_failures_trace.json")
    args = ap.parse_args()

    if not os.path.exists(args.trace):
        print(f"trace 文件不存在: {args.trace}\n先跑 run_autonomous_loop.py 生成。", file=sys.stderr)
        sys.exit(1)

    result = json.load(open(args.trace, encoding="utf-8"))

    print("=" * 112)
    print("Gap Closure + Coverage Commit (global rematch)")
    print("=" * 112)

    all_queries = []
    all_commits = []
    for rd in result.get("rounds", []):
        round_i = rd["round"]
        print(f"\n--- Round {round_i}: rel={rd['new_relevant_papers']} "
              f"closed={rd.get('new_covered_mechanisms', '?')} "
              f"tgt_papers={rd.get('target_paper_hits', '?')} "
              f"cross_papers={rd.get('cross_paper_hits', '?')} "
              f"disc={rd.get('newly_discovered', '?')} "
              f"init_remain={rd.get('initial_remaining', '?')} "
              f"total_open={rd.get('total_open', '?')} "
              f"q_hit={rd.get('query_gap_hit_rate', 0):.0%} ---")
        if rd.get("commit_consistent") is False:
            print("  ⚠ COVERAGE_COMMIT_INCONSISTENCY: 本地 commit 关闭集合 ≠ coverage matrix 确认集合（loop 已停止）")
        elif not rd.get("coverage_delta_consistent", True):
            print("  ⚠ COVERAGE_DELTA_INCONSISTENCY: new_cov ≠ len(closed_deltas)")

        # coverage delta（本轮真正关闭的 gap + evidence）
        for cd in rd.get("closed_deltas", []):
            print(f"  ✓ CLOSED: {cd['route']} × {cd['mechanism']}  (conf={cd.get('confidence', 0):.2f})"
                  + (f"  evidence: {cd['evidence'][:70]}" if cd.get("evidence") else ""))

        # query 漏斗表
        print(f"{'gap (route × mech)':<32}{'L':<4}{'mode':<7}{'meta':<11}{'retr':<7}{'gate':<7}{'rel':<6}{'hit':<6}  cat")
        print("-" * 112)
        for qt in rd.get("query_traces", []):
            cat = query_category(qt)
            g = qt["originating_gap"]
            gap_str = f"{(g['route'] or '?')[:15]} × {(g['mechanism'] or '-')[:15]}"
            lv = qt.get("level_used")
            lv_str = str(lv) if lv is not None else "∅"
            mode = (qt.get("query_mode") or qt.get("request_mode") or "?")[:6]
            meta = qt.get("meta_count", qt.get("total_hits", 0))
            print(f"{gap_str:<32}{lv_str:<4}{mode:<7}{meta:<11}{qt['retrieved']:<7}"
                  f"{qt['gap_gate']:<7}{qt['relevant']:<6}{qt['hit']:<6}  {cat}")
            all_queries.append((round_i, qt, cat))

        # coverage commit（global rematch：论文关闭了哪些 gap）
        cts = rd.get("commit_traces", [])
        if cts:
            print(f"\n  coverage commit（global rematch — 搜 A 可关 B）:")
            print(f"  {'paper':<22}{'originating':<28}{'canonical_routes':<20}{'closed_gaps':<42}verdict")
            print("  " + "-" * 110)
            for c in cts:
                g = c["originating_gap"]
                gap_str = f"{(g['route'] or '?')[:13]}×{(g['mechanism'] or '-')[:13]}"
                crs = ",".join(c.get("canonical_routes") or [])[:18]
                cg = ";".join(f"{x['route']}×{x['mechanism']}" for x in c.get("closed_gaps", []))[:40]
                print(f"  {c['paper_id'][:20]:<22}{gap_str:<28}{crs:<20}{cg:<42}{c['verdict']}")
            for c in cts:
                cat, reason = classify_paper(c)
                all_commits.append((round_i, cat, reason, c))
            # 机制 debug：有 target evidence 但没关闭 gap 的样本（route/gate 都对，coverage 没进）
            for c in cts:
                if c.get("verdict") == "NO_COVERAGE_GAIN" and c.get("gate_pass"):
                    print(f"\n  [DEBUG] gate_pass 但 NO_COVERAGE_GAIN: {c['paper_id']}")
                    print(f"    originating_mechanism : {c.get('target_mech')}")
                    print(f"    canonical_routes      : {c.get('canonical_routes')}")
                    print(f"    extracted_mechanisms  : {c.get('extracted_mechanisms')}")
                    print(f"    mechanism_evidence    : {c.get('mechanism_evidence')}")
                    print(f"    persisted             : {c.get('persisted')}")

    # ── query 级汇总 ─────────────────────────────────
    print("\n" + "=" * 112)
    print("Query-level 汇总")
    print("=" * 112)
    qcnt = Counter(r[2] for r in all_queries)
    nq = len(all_queries)
    for cat in ["F0_NO_RETRIEVAL", "F1_NO_TARGET_EVIDENCE",
                "F2_FULLTEXT_EVIDENCE_UNVERIFIED", "HAS_EVIDENCE"]:
        n = qcnt.get(cat, 0)
        print(f"  {cat:<30} {n:>3}  ({100 * n / nq:.0f}%)" if nq else f"  {cat}: 0")
    if nq:
        lv_dist = Counter(str(r[1].get("level_used")) for r in all_queries)
        print(f"\n  fallback level 分布: {dict(sorted(lv_dist.items()))}  （∅ = 整链 0 命中）")

    # ── commit 级汇总 ─────────────────────────────────
    print("\n" + "=" * 112)
    print("Coverage commit 汇总（global rematch）")
    print("=" * 112)
    ccnt = Counter(r[1] for r in all_commits)
    ncommit = len(all_commits)
    for cat in ["TARGET_GAP_HIT", "CROSS_GAP_HIT", "NO_COVERAGE_GAIN"]:
        n = ccnt.get(cat, 0)
        pct = 100 * n / ncommit if ncommit else 0
        print(f"  {cat:<20} {n:>3}  ({pct:.0f}%)")
    print(f"\n  总计: {ncommit} 篇候选论文")
    if ncommit:
        print(f"  说明: CROSS_GAP_HIT = 搜 A 学会了 B（跨 gap 知识复用，正面信号）")

    # ── 诊断结论 ─────────────────────────────────────
    print("\n" + "-" * 112)
    print("诊断结论")
    print("-" * 112)
    target_rate = ccnt.get("TARGET_GAP_HIT", 0) / max(ncommit, 1)
    cross_rate = ccnt.get("CROSS_GAP_HIT", 0) / max(ncommit, 1)
    no_gain_rate = ccnt.get("NO_COVERAGE_GAIN", 0) / max(ncommit, 1)
    f1_rate = qcnt.get("F1_NO_TARGET_EVIDENCE", 0) / max(nq, 1)
    f2_rate = qcnt.get("F2_FULLTEXT_EVIDENCE_UNVERIFIED", 0) / max(nq, 1)

    if cross_rate > 0.3:
        print("→ CROSS_GAP_HIT 占比高：搜 A 学会了 B——跨 gap 知识复用在工作。")
        print("   这是知识驱动搜索的正面信号（比纯精准搜索更像自主学习）。")
    elif target_rate > 0.5:
        print("→ TARGET_GAP_HIT 为主：originating gap 精确回流，闭环成立。")
    elif no_gain_rate > 0.5:
        print("→ NO_COVERAGE_GAIN 占比高：论文进来但没关闭任何 gap。")
        print("   看上方 [DEBUG] 块（gate_pass 但没关闭的样本）——extractor / mechanism normalizer / persistence 问题。")
    elif f2_rate > 0.4:
        print("→ F2_FULLTEXT_EVIDENCE_UNVERIFIED 占比高：fulltext(L2/L3) 搜到但 title/abstract 无 evidence。")
        print("   检索证据面(fulltext) 与验证证据面(title/abstract) 不一致——非 query drift 也非 extractor miss。")
        print("   这些论文可作 discovery candidate，但不能凭 fulltext 命中直接关闭 coverage。")
    elif f1_rate > 0.4:
        print("→ F1_NO_TARGET_EVIDENCE 占比高：strict(title/abstract) 搜到但 gate 全不过。")
        print("   说明当前 evidence gate 的字段(title/abstract)里没有 target mechanism——不是 extractor 的问题（gate 在 extractor 之前）。")
    else:
        print("→ 混合情况，逐条看 closed_gaps 与 closed_deltas。")


if __name__ == "__main__":
    asyncio.run(main())
