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


def _external_universe_builder(topic: str = "pc_001", universe_id: str = ""):
    """外部审计总体（用户 P1.5 定：正式 audit 唯一接受的 universe 来源）。

    从 data/audit_universe_definitions/{topic}.json 的宽 umbrella 规则构造——
    与 Agent ranking/prioritizer/candidate/ontology 无关（高 recall 低 precision）。
    found_relevant：Agent 已确认 relevant 且落在 U* 内的论文（KB 有 edges 的
    论文，doi 去重；VALIDATED/PROMOTED 候选的 source_papers）。

    universe_id（2026-08-30 R04 补丁）：显式指定 EXTERNAL_AUDIT_UNIVERSE snapshot。
    R04 frame 升级到宽定义（bae4cd5a5dc6, n=5890）时传入，避免取到窄 frame（241a7a93def7）
    的最新 snapshot（抽样框枯竭 47 篇）。
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
    if universe_id:
        ext = next((s for s in snaps if s.get("universe_id") == universe_id), None)
        if ext is None:
            raise SystemExit(f"✗ 未找到 universe_id={universe_id} snapshot\n"
                             f"  可用: {[s.get('universe_id') for s in snaps][-5:]}")
    else:
        ext = next((s for s in snaps
                    if s.get("topic_id") == topic
                    and s.get("source_type") == "EXTERNAL_AUDIT_UNIVERSE"), None)
    if ext is None:
        raise SystemExit(
            f"✗ 未找到 topic={topic} 的 EXTERNAL_AUDIT_UNIVERSE snapshot\n"
            f"  先构建: python tools/build_audit_universe.py --topic {topic}\n"
            f"  （正式审计不接受 Agent-seen pool 自证没漏）")
    print(f"[universe] {ext.get('universe_id')} | n={len(ext.get('paper_ids', []))} | "
          f"def={ext.get('definition_version', '')[:12]}")
    return {"paper_ids": ext["paper_ids"],
            "found_relevant": found_relevant,
            "kb_version": ext.get("kb_version", ""),
            "search_run_ids": [],
            "source_type": EXTERNAL_AUDIT_UNIVERSE,
            "definition_version": ext.get("definition_version", "")}


def _build_label_metadata(paper_ids: list[str]) -> dict:
    """从 openalex_cache 构建 {wid: {title, year, doi, abstract}}（Auditor 模板用）。

    覆盖验证：pc_001 universe 1572/1572 全覆盖（2026-08-29）。
    """
    cache_path = os.path.join(BASE, "data", "cache", "openalex_cache.json")
    if not os.path.exists(cache_path):
        return {}
    cache = json.load(open(cache_path, encoding="utf-8"))

    def rebuild(inv):
        if not inv:
            return None
        pos = {}
        for w, idxs in inv.items():
            for i in idxs:
                pos[i] = w
        return " ".join(pos[i] for i in sorted(pos))

    ids = set(paper_ids)
    meta = {}
    for q, resp in cache.items():
        for w in resp.get("results", []):
            wid = (w.get("id") or "").replace("https://openalex.org/", "")
            if wid in ids and wid not in meta:
                meta[wid] = {"title": w.get("title"),
                             "year": w.get("publication_year"),
                             "doi": (w.get("doi") or "").replace("https://doi.org/", ""),
                             "abstract": rebuild(w.get("abstract_inverted_index"))}
    return meta


def _build_agent_seen(sample_ids: list[str]):
    """返回 (predicate, coverage_info) —— v3.0 三态 agent_seen。

    predicate: wid -> "TRUE" | "FALSE" | "UNKNOWN"
      TRUE    = DOI（主判）/ 归一化 title（兜底）命中 Search A 检索结果
      FALSE   = DOI 存在但未命中 → 高置信 miss
      UNKNOWN = 无 DOI 且 title 未命中 → identity repair（不算 miss）
    """
    from search_engine.audit.agent_seen import resolve_agent_seen
    resolved = resolve_agent_seen(sample_ids)
    n = {k: sum(1 for v in resolved.values() if v["agent_seen"] == k)
         for k in ("TRUE", "FALSE", "UNKNOWN")}
    coverage = {
        "TRUE": n["TRUE"], "FALSE": n["FALSE"], "UNKNOWN": n["UNKNOWN"],
        "note": "agent_seen 三态（2026-08-29 用户定稿）：TRUE=已见 / FALSE=高置信 miss / "
                "UNKNOWN=identity 未解析走 repair",
    }

    def _seen(pid: str):
        return resolved.get(pid, {}).get("agent_seen", "UNKNOWN")

    return _seen, coverage


def main():
    ap = argparse.ArgumentParser(description="Phase 3 Completeness Audit")
    ap.add_argument("--topic", default="pc_001")
    ap.add_argument("--create", action="store_true", help="创建审计（冻结+抽样+导出样本）")
    ap.add_argument("--exclude-audit", default="",
                    help="从抽样池排除这些 audit_id 的 sampled_paper_ids（逗号分隔可多个，"
                         "如 R03 排除 R01,R02 整个历史 sample："
                         "pc_001::20260829043656,pc_001::20260829130129）"
                         "——参与过历史 search 开发的样本不得进 test set")
    ap.add_argument("--exclude-papers", default="",
                    help="显式排除 paper_ids JSON 列表文件路径（与 --exclude-audit 二选一）")
    ap.add_argument("--universe-id", default="",
                    help="显式指定 EXTERNAL_AUDIT_UNIVERSE snapshot（R04 frame 升级用："
                         "宽 frame bae4cd5a5dc6 传 pc_001-2026-08-30T011615；默认取第一个匹配）")
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
        # R02 硬约束：R02_sample ∩ R01_sample = ∅（参与过 S1 开发的论文不得进 test set）
        exclude = set()
        if args.exclude_audit:
            from search_engine.completeness.audit import load_audits
            all_audits = load_audits()
            for aid in [x.strip() for x in args.exclude_audit.split(",") if x.strip()]:
                au = [a for a in all_audits if a.get("audit_id") == aid]
                if not au:
                    print(f"[FATAL] --exclude-audit 未找到: {aid}")
                    sys.exit(2)
                exclude |= set(au[0].get("sampled_paper_ids", []))
                print(f"[exclude] audit {aid}：+{len(au[0].get('sampled_paper_ids', []))} 篇"
                      f"（累计 {len(exclude)}）")
        elif args.exclude_papers:
            import json as _json
            exclude = set(_json.load(open(args.exclude_papers, encoding="utf-8")))
            print(f"[exclude] 排除 {len(exclude)} 篇（显式列表）")
        audit = create_audit(args.topic,
                             lambda: _external_universe_builder(args.topic, args.universe_id),
                             sample_size=args.sample_size,
                             confidence_level=args.confidence,
                             target_recall=args.target_recall, seed=args.seed,
                             exclude_papers=exclude)
        # 重导模板（带 title/year/doi/abstract 元数据，帮助独立 Auditor 判断）
        from search_engine.completeness.audit import export_labels_template
        meta = _build_label_metadata(audit.sampled_paper_ids)
        label_path = export_labels_template(audit, metadata=meta)
        print("=" * 60)
        print("Phase 3 Audit Created")
        print("=" * 60)
        print(f"audit_id:      {audit.audit_id}")
        print(f"universe_hash: {audit.universe_hash}")
        print(f"Found relevant F:           {audit.F}")
        print(f"Remaining pool N_remaining: {audit.N_remaining}")
        print(f"sample: {audit.sample_size} papers（seed={audit.seed}，SRS 不放回）")
        print(f"metadata coverage: {sum(1 for m in meta.values() if m.get('title'))}"
              f"/{audit.sample_size} title 可用")
        print(f"status: {audit.status}")
        print()
        print(f"待审样本已导出: {label_path}")
        print("请由独立 Auditor（Human review / 独立模型+盲审+人工校验）逐篇标注")
        print("三态 RELEVANT / UNCERTAIN / IRRELEVANT（UNCERTAIN 单独报告/adjudication，")
        print("不混入 negative；统计 m 只计 RELEVANT），然后:")
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
        # v3.0 miss 口径（2026-08-29 用户定）：m = RELEVANT ∧ agent_seen=false。
        # agent_seen = wid 的 DOI ∈ Search A 检索结果（depth run ∪ Round1 ∪ Round3）。
        # 样本来自 remaining pool（只排除 KB-confirmed 25 篇），检索过但未确认入库的
        # 论文必须从 m 扣减——否则 m 虚高、Recall_LCB 被低估。
        agent_seen, _cov = _build_agent_seen(audit.sampled_paper_ids)
        audit = load_labels(audit, labels_data, agent_seen=agent_seen)
        print(build_report(audit, _load_diagnostics(audit)))
        if audit.status != "COMPLETED":
            print()
            print("label 未完整——不输出正式 Recall_LCB；宁可不出数，"
                  "也不要默认当 irrelevant")
        else:
            print()
            print(f"m 口径（v3.0 三态）：样本 RELEVANT {audit.n_relevant} 篇："
                  f"agent_seen=TRUE（已在检索结果）{audit.n_agent_seen} / "
                  f"FALSE（高置信 miss）{audit.m} / "
                  f"UNKNOWN（identity 未解析，走 repair）{audit.n_agent_unknown}")
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
