"""人工 review Candidate Pool：更新 DiscoveryCandidate 状态（状态机校验）+ 添加 seed。

状态机（用户定 2026-08-26）：
    RAW → TYPED → {ALIAS, IRRELEVANT, EXISTING_KNOWLEDGE, CANDIDATE}
                CANDIDATE → VERIFYING → {REJECTED, ADJACENT, NEED_MORE_EVIDENCE,
                                         SEARCH_INCONCLUSIVE, VALIDATED} → PROMOTED

自动流程不能 reopen（ADJACENT/REJECTED 是终态，防 ADJACENT↔VERIFYING 无意义循环）；
**人工审计可以 reopen**（--direct = MANUAL_REOPEN，需 --reason 非空）：
    ADJACENT / REJECTED → VERIFYING（旧错误 verdict 的人工重开，review_log 记 MANUAL_REOPEN）
    CANDIDATE → REJECTED / ADJACENT（人工快速判定，记 MANUAL_OVERRIDE）

候选来源（Phase 2 不能只依赖 scanner）：
    scanner         —— 从 KB edges 直接扫出
    human_seed      —— 人工推导的 seed（如 dynamic covalent bond exchange ← self-healing/DCB）
    hypothesis_seed —— 从 hypothesis 空间推导的 seed

用法:
    python tools/review_candidates.py --list [--status CANDIDATE]
    python tools/review_candidates.py --name "incremental curing" --set-status VERIFYING --reason "验证中"
    # 人工重开终态（旧错误 verdict，如 ADJACENT → VERIFYING）:
    python tools/review_candidates.py --name "incremental curing" --set-status VERIFYING --direct \
        --reason "verifier 修复：seed evidence 进语料，重跑"
    # 人工快速否决 CANDIDATE:
    python tools/review_candidates.py --name "xxx" --set-status REJECTED --direct --reason "..."
    # 添加 seed candidate（human_seed / hypothesis_seed）:
    python tools/review_candidates.py --add "dynamic covalent bond exchange" --type MECHANISM \
        --source human_seed --rel MEDIUM --reason "从 self-healing / DCB evidence 推导出的邻接机制"
"""

import argparse
import hashlib
import json
import os
import sys

from search_engine.discovery import (
    STATUS_FLOW, STATUS_LABELS, DiscoveryCandidate, CANDIDATE_TYPES, CANDIDATE_SOURCES,
    InvalidTransition,
)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

POOL_PATH = "data/exports/phase2_candidates.json"


def _load() -> dict:
    with open(POOL_PATH, encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict):
    with open(POOL_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _find(cands: list[dict], name: str) -> list[dict]:
    n = name.lower()
    return [c for c in cands if n in c["raw_name"].lower() or c["candidate_id"] == name]


def _add_candidate(data: dict, args) -> None:
    cid = hashlib.md5(args.add.encode("utf-8")).hexdigest()[:12]
    for c in data.get("candidates", []):
        if c["candidate_id"] == cid:
            print(f"✗ 已存在同 id 候选: {c['raw_name']}")
            return
    cand = DiscoveryCandidate(
        candidate_id=cid,
        raw_name=args.add,
        candidate_type=args.type,
        source=args.source,
        independent_paper_count=0,
        domain_relevance=args.rel,
        status="CANDIDATE",  # seed 直接是候选（TYPED 完成）
        provenance={"seed_reason": args.reason or "", "by": "human_seed"},
    )
    data.setdefault("candidates", []).append(cand.to_dict())
    _save(data)
    print(f"✓ 添加 seed: {args.add}  [{args.type}, source={args.source}, {args.rel}]")
    print(f"  下一步: python tools/review_candidates.py --name \"{args.add}\" "
          f"--set-status VERIFYING --reason \"第一轮验证\"")


def main():
    ap = argparse.ArgumentParser(description="Phase 2.0 Candidate Pool 人工 review + seed 添加")
    ap.add_argument("--list", action="store_true", help="列出候选")
    ap.add_argument("--status", default="", help="按状态过滤列出")
    ap.add_argument("--name", default="", help="定位候选（raw_name 子串或 candidate_id）")
    ap.add_argument("--set-status", default="", choices=list(STATUS_FLOW.keys()),
                    help="更新到的状态")
    ap.add_argument("--direct", action="store_true",
                    help="人工审计 override（需 --reason 非空）：MANUAL_REOPEN（ADJACENT/REJECTED "
                         "→ VERIFYING，旧错误 verdict 重开）或 MANUAL_OVERRIDE（CANDIDATE 快速判定）")
    ap.add_argument("--reason", default="", help="review 记录 / seed 理由（--direct 时必须非空）")
    ap.add_argument("--add", default="", help="添加 seed candidate（human_seed / hypothesis_seed）")
    ap.add_argument("--type", default="UNKNOWN", choices=list(CANDIDATE_TYPES),
                    help="seed 的 candidate_type（默认 UNKNOWN）")
    ap.add_argument("--source", default="human_seed", choices=list(CANDIDATE_SOURCES),
                    help="seed 来源（默认 human_seed）")
    ap.add_argument("--rel", default="UNKNOWN", choices=["HIGH", "MEDIUM", "LOW", "UNKNOWN"],
                    help="seed 的 domain_relevance（默认 UNKNOWN）")
    args = ap.parse_args()

    if args.add:
        if not os.path.exists(POOL_PATH):
            print(f"✗ 未找到候选池: {POOL_PATH}（先跑 python tools/discover_candidates.py 或 "
                  f"在项目根目录执行）")
            return
        data = _load()
        _add_candidate(data, args)
        return

    if not os.path.exists(POOL_PATH):
        print(f"✗ 未找到候选池: {POOL_PATH}（先跑 python tools/discover_candidates.py）")
        return
    data = _load()
    cands = data.get("candidates", [])

    if args.list or (not args.name and not args.set_status):
        rows = cands
        if args.status:
            rows = [c for c in rows if c["status"] == args.status]
        print(f"Candidate Pool（共 {len(cands)} 条，显示 {len(rows)} 条）")
        for c in sorted(rows, key=lambda x: -x["independent_paper_count"])[:40]:
            src = c.get("source", "scanner")
            print(f"  [{c['status']:<18}] {c['candidate_type']:<20} {c['raw_name'][:40]}"
                  f"  ({c['independent_paper_count']} 篇, {c['domain_relevance']}, {src})")
        return

    if not args.name:
        print("需要 --name 定位候选")
        return

    hits = _find(cands, args.name)
    if not hits:
        print(f"✗ 找不到候选: {args.name}")
        return
    if len(hits) > 1:
        print("匹配到多个候选，请更精确:")
        for c in hits:
            print(f"  {c['candidate_id']}  {c['raw_name']}  [{c['status']}]")
        return

    c = hits[0]
    if not args.set_status:
        print(f"当前: {c['raw_name']}  status={c['status']}  type={c['candidate_type']}"
              f"  source={c.get('source', 'scanner')}")
        print(f"  允许迁移: {STATUS_FLOW.get(c['status'], [])}")
        return

    cur, nxt = c["status"], args.set_status
    was_terminal_reopen = (cur in ("ADJACENT", "REJECTED") and nxt == "VERIFYING")
    was_revalidation = (cur == "VALIDATED" and nxt == "VERIFYING")
    try:
        # --direct = 人工审计 override（MANUAL_REOPEN / REVALIDATION / MANUAL_OVERRIDE），需 reason 非空
        candidate_obj = DiscoveryCandidate(**{k: v for k, v in c.items()
                                              if k in DiscoveryCandidate.__dataclass_fields__})
        candidate_obj.transition(nxt, manual_override=args.direct, reason=args.reason)
        c["status"] = nxt
    except InvalidTransition as e:
        print(f"✗ {e}")
        return

    # review_log 记录动作语义（用户定：人工重开不能伪装成算法自判）
    if args.direct:
        action = ("REVALIDATION" if was_revalidation
                  else ("MANUAL_REOPEN" if was_terminal_reopen else "MANUAL_OVERRIDE"))
    else:
        action = "TRANSITION"
    c.setdefault("review_log", []).append({
        "from": cur, "to": nxt, "action": action,
        "reason": args.reason or STATUS_LABELS.get(nxt, ""), "by": "human_review",
    })
    _save(data)
    print(f"✓ {c['raw_name']}: {cur} → {nxt}  [{action}]")
    if args.reason:
        print(f"  reason: {args.reason}")


if __name__ == "__main__":
    main()

