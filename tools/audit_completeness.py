"""Phase 3 — tools/audit_completeness.py（用户定 2026-08-27）。

CLI 三个动作（审计工作流两个时刻分离）：
  ① --create           冻结 universe → 抽样 → 导出待审样本 → AWAITING_LABELS
  ② --audit-id --labels 加载独立 Auditor 标签 → COMPLETED + 统计 + STOP
  ③ --audit-id --replay 重放（全量可复现）

⚠️ labels 必须来自独立审计流程（Human review，或未来独立模型+盲审+人工校验），
绝不能由 Search Agent 自己判——否则等于让被审计的人给自己漏检打分。
"""

import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)


def _external_universe_builder(topic: str = "pc_001"):
    """外部审计总体（用户 P1.5 定：正式 audit 唯一接受的 universe 来源）。

    从 data/audit_universe_definitions/{topic}.json 的宽 umbrella 规则构造——
    与 Agent ranking/prioritizer/candidate/ontology 无关（高 recall 低 precision）。
    found_relevant：Agent 已确认 relevant 且落在 U* 内的论文（KB 有 edges 的
    论文，doi 去重；VALIDATED/PROMOTED 候选的 source_papers）。
    """
    from search_engine.completeness.universe_builder import (
        load_definition, build_agent_seen_pool, EXTERNAL_AUDIT_UNIVERSE,
    )

    # F：Agent 已确认 relevant（canonical 去重）
    seen = build_agent_seen_pool()
    found_relevant = seen["found_relevant"]

    # U*：外部定义 → 宽检索（真实构建走 tools/build_audit_universe.py；
    # 这里直接冻结已构建的 snapshot——CLI --create 前必须先 build）
    from search_engine.completeness.universe import load_snapshots
    snaps = load_snapshots()
    ext = next((s for s in snaps
                if s.get("topic_id") == topic
                and s.get("source_type") == "EXTERNAL_AUDIT_UNIVERSE"), None)
    if ext is None:
        raise SystemExit(
            f"✗ 未找到 topic={topic} 的 EXTERNAL_AUDIT_UNIVERSE snapshot\n"
            f"  先构建: python tools/build_audit_universe.py --topic {topic}\n"
            f"  （正式审计不接受 Agent-seen pool 自证没漏）")
    return {"paper_ids": ext["paper_ids"],
            "found_relevant": found_relevant,
            "kb_version": ext.get("kb_version", ""),
            "search_run_ids": [],
            "source_type": EXTERNAL_AUDIT_UNIVERSE,
            "definition_version": ext.get("definition_version", "")}


def main():
    ap = argparse.ArgumentParser(description="Phase 3 Completeness Audit")
    ap.add_argument("--topic", default="pc_001")
    ap.add_argument("--create", action="store_true", help="创建审计（冻结+抽样+导出样本）")
    ap.add_argument("--sample-size", type=int, default=500)
    ap.add_argument("--confidence", type=float, default=0.95)
    ap.add_argument("--target-recall", type=float, default=0.95)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--audit-id", default="")
    ap.add_argument("--labels", default="", help="独立 Auditor 标签文件路径")
    ap.add_argument("--replay", action="store_true", help="重放指定 audit_id")
    args = ap.parse_args()

    from search_engine.completeness.audit import (
        create_audit, load_labels, replay, find_audit,
    )
    from search_engine.completeness.report import build_report

    if args.create:
        audit = create_audit(args.topic,
                             lambda: _external_universe_builder(args.topic),
                             sample_size=args.sample_size,
                             confidence_level=args.confidence,
                             target_recall=args.target_recall, seed=args.seed)
        label_path = os.path.join(BASE, "data", "exports",
                                  "completeness_labels", f"{audit.audit_id}.json")
        print("=" * 60)
        print("Phase 3 Audit Created")
        print("=" * 60)
        print(f"audit_id:      {audit.audit_id}")
        print(f"universe_hash: {audit.universe_hash}")
        print(f"Found relevant F:           {audit.F}")
        print(f"Remaining pool N_remaining: {audit.N_remaining}")
        print(f"sample: {audit.sample_size} papers")
        print(f"status: {audit.status}")
        print()
        print(f"待审样本已导出: {label_path}")
        print("请由独立 Auditor（Human review / 独立模型+盲审+人工校验）逐篇标注")
        print("RELEVANT / IRRELEVANT，然后:")
        print(f"  python tools/audit_completeness.py --audit-id {audit.audit_id} "
              f"--labels {label_path}")
        return

    if not args.audit_id:
        ap.error("需要 --audit-id（或 --create 创建新审计）")

    if args.replay:
        audit = replay(args.audit_id)
        if audit is None:
            print(f"✗ 未找到 audit {args.audit_id}")
            return
        print(build_report(audit, _load_diagnostics(audit)))
        print()
        print("[replay] 与首次运行逐位一致（含数学重算校验）")
        return

    if args.labels:
        audit = find_audit(args.audit_id)
        if audit is None:
            print(f"✗ 未找到 audit {args.audit_id}")
            return
        if not os.path.exists(args.labels):
            print(f"✗ 标签文件不存在: {args.labels}")
            return
        with open(args.labels, encoding="utf-8") as f:
            labels_data = json.load(f)
        audit = load_labels(audit, labels_data)
        print(build_report(audit, _load_diagnostics(audit)))
        if audit.status != "COMPLETED":
            print()
            print("label 未完整——不输出正式 Recall_LCB；宁可不出数，"
                  "也不要默认当 irrelevant")
        return

    ap.error("动作未指定：--create / --audit-id --labels / --audit-id --replay")


def _load_diagnostics(audit) -> dict:
    """诊断数据（A 区展示，无停止权）——从现有数据构建。

    第一版：goldset 用真实 15 篇 + known_dois（KB records 的 doi 集合）；
    saturation 用 query registry 的检索统计；capture 标记 INVALID_ASSUMPTION
    （只有 query-family 通道，无独立通道）。
    """
    diag = {}

    # Gold set
    try:
        from search_engine.completeness.goldset import (
            load_gold_set, goldset_report, normalize_doi,
        )
        from search_engine.knowledge_base import KnowledgeBase
        kb = KnowledgeBase()
        known_dois = set()
        known_by_title = {}
        try:
            for rec in kb.get_all():
                doi = normalize_doi(getattr(rec, "doi", "") or "")
                if doi:
                    known_dois.add(doi)
                pid = getattr(rec, "openalex_id", "") or getattr(rec, "paper_id", "")
                title = getattr(rec, "source_text", "") or ""
                if pid and title:
                    known_by_title[title] = pid
        finally:
            kb.close()
        diag["goldset"] = goldset_report(load_gold_set(), known_dois,
                                         known_by_title=known_by_title)
    except Exception as e:  # noqa: BLE001
        diag["goldset"] = {"status": "INSUFFICIENT_DATA", "reason": str(e)}

    # Saturation：query registry 执行统计（round = 每 query 一轮，P1 简化）
    try:
        from search_engine.completeness.saturation import saturation_report
        reg_path = os.path.join(BASE, "data", "exports",
                                "discovery_query_registry.json")
        if os.path.exists(reg_path):
            with open(reg_path, encoding="utf-8") as f:
                reg = json.load(f)
            rounds = [{"round_id": i + 1,
                       "new_unique_papers": r.get("new_unique_count", 0) or 0,
                       "new_relevant_papers": None}
                      for i, r in enumerate(reg) if r.get("status") == "SUCCEEDED"]
            diag["saturation"] = saturation_report(rounds).to_dict()
        else:
            diag["saturation"] = {"status": "INSUFFICIENT_DATA"}
    except Exception as e:  # noqa: BLE001
        diag["saturation"] = {"status": "INSUFFICIENT_DATA", "reason": str(e)}

    # Capture：当前只有 query-family 通道 → 正确行为 = 拒绝硬算
    try:
        from search_engine.completeness.capture import chao_diagnostic
        diag["capture"] = chao_diagnostic(
            [["query-family"]], source_types=["NODE", "RELATION",
                                              "MECHANISM", "ADJACENT"])
    except Exception as e:  # noqa: BLE001
        diag["capture"] = {"status": "NOT_ENOUGH_INDEPENDENT_CHANNELS",
                           "reason": str(e)}

    return diag


if __name__ == "__main__":
    main()
