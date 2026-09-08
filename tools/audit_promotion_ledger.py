#!/usr/bin/env python3
"""晋升对账不变式（Roadmap v2 #1 护栏）：每一条 R/U QA 判定必须可对账。

不变式（三方恒等）：
  R/U 判定总数 = KB 收录数 + 待补队列数
  待补队列每条必须带原因码（promotion_pending_queue.json）：
    no_abstract          QA 判 R 但无 abstract 未抽取
    u_deferred           UNCERTAIN 缓收（挂起待晋升规则裁决）
    identity_unresolved  判 R 有摘要却无法与 KB 主键 reconcile（异常，优先复核）
  newly_queued = 本次审计新发现"无着落"的判定数 —— 这就是漏水报警：
  任何新 R/U 判定只要没进 KB 就会被排队并计数，--strict 下非零退出。

零 API：只用冻结 QA labels/corpus + data/cache/knowledge_base.db + 队列文件。
用法：
  .venv/Scripts/python tools/audit_promotion_ledger.py            # 对账并刷新队列
  .venv/Scripts/python tools/audit_promotion_ledger.py --strict   # newly_queued>0 即退出码 1
输出：
  终端对账表；data/exports/terminology/promotion_pending_queue.json（处置台账）
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TERM = ROOT / "data" / "exports" / "terminology"
KB_DB = ROOT / "data" / "cache" / "knowledge_base.db"
QUEUE_PATH = TERM / "promotion_pending_queue.json"
# 处置闭环：disposition 引擎已裁决的动作（r06_disposition_log 最后一次 apply run）
DISPOSITION_LOG = TERM / "r06_disposition_log.json"

QA_SOURCES = [
    ("S6", TERM / "s6_qa_labels.json"),
    ("S7", TERM / "s7_community_qa_labels.json"),
    ("S7U", TERM / "s7_community_qa_labels_uncertain.json"),
    ("S7EX12", TERM / "s7_ex12_qa_labels.json"),
]
CORPORA = [
    TERM / "s6_qa_corpus.json",
    TERM / "s7_community_qa_corpus.json",
    TERM / "s7_community_qa_corpus_uncertain.json",
]
RANK = {"RELEVANT": 2, "UNCERTAIN": 1}


def norm_doi(doi: str | None) -> str:
    return (doi or "").strip().lower()


def load_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def collect_judged() -> dict[str, dict]:
    """{eid: {label, doi, has_abstract, stages}}，只留 R/U，跨轮次取更优标签。"""
    meta_by_eid: dict[str, dict] = {}
    for path in CORPORA:
        if not path.exists():
            continue
        for p in load_json(path)["papers"]:
            eid = p.get("eid") or p.get("key")
            if eid:
                meta_by_eid[eid] = {
                    "doi": norm_doi(p.get("doi")),
                    "has_abstract": bool((p.get("abstract") or "").strip()),
                }
    out: dict[str, dict] = {}
    for stage, path in QA_SOURCES:
        if not path.exists():
            continue
        for eid, label in load_json(path)["labels"].items():
            if label not in RANK:
                continue
            entry = out.setdefault(
                eid,
                {"eid": eid, "label": None, "stages": {}, **meta_by_eid.get(eid, {})},
            )
            if entry["label"] is None or RANK[label] > RANK[entry["label"]]:
                entry["label"] = label
            entry["stages"][stage] = label
    return out


def kb_identity_index() -> dict[str, set[str]]:
    """KB papers 三通道身份索引（norm_doi / openalex_id / scopus_eid）→ paper_id 集。"""
    con = sqlite3.connect(f"file:{KB_DB}?mode=ro", uri=True)
    idx: dict[str, set[str]] = {}
    for paper_id, doi, oa, eid in con.execute(
        "select paper_id, doi, openalex_id, scopus_eid from papers"
    ):
        for k in {norm_doi(doi), (oa or "").strip(), (eid or "").strip()} - {""}:
            idx.setdefault(k, set()).add(paper_id)
    con.close()
    return idx


def reason_code(rec: dict) -> str:
    if rec["label"] == "UNCERTAIN":
        return "u_deferred"
    if not rec.get("has_abstract", False):
        return "no_abstract"
    return "identity_unresolved"


def load_dispositioned() -> set[str]:
    """已由 disposition 引擎 apply 收录的 keys（最后一轮 applied run 的 promote 动作）。"""
    if not DISPOSITION_LOG.exists():
        return set()
    log = load_json(DISPOSITION_LOG)
    applied = [e for e in log.get("entries", []) if e.get("applied")]
    if not applied:
        return set()
    return {p["key"] for p in applied[-1]["proposals"] if str(p.get("action", "")).startswith("promote")}


def load_disposition_state() -> dict[str, dict]:
    """最后一次 disposition run（含 dry-run）对每个 key 的裁决，供队列条目同步处置理由。"""
    if not DISPOSITION_LOG.exists():
        return {}
    log = load_json(DISPOSITION_LOG)
    entries = log.get("entries", [])
    if not entries:
        return {}
    return {p["key"]: p for p in entries[-1].get("proposals", [])}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strict", action="store_true", help="newly_queued>0 时退出码 1（CI 护栏）")
    args = ap.parse_args()

    judged = collect_judged()
    kb_idx = kb_identity_index()
    dispositioned = load_dispositioned()
    disp_state = load_disposition_state()
    queue: dict = load_json(QUEUE_PATH) if QUEUE_PATH.exists() else {"entries": {}}
    entries: dict = queue.setdefault("entries", {})

    n_kb = n_queued = newly_queued = n_dispositioned = 0
    newly: list[str] = []
    for eid, rec in sorted(judged.items()):
        hits = kb_idx.get(eid, set()) | (kb_idx.get(rec.get("doi"), set()) if rec.get("doi") else set())
        if hits:
            n_kb += 1
            entries.pop(eid, None)  # 已收录 → 清出队列
            continue
        if eid in dispositioned:
            n_dispositioned += 1  # 已由 disposition 引擎裁决（KB 写入在 apply 批次内）
            entries.pop(eid, None)
            continue
        q = entries.get(eid)
        if q and q.get("reason_code"):
            q["label"] = rec["label"]  # 同步最新标签，处置状态保留
            d = disp_state.get(eid)
            if d:
                q["disposition"] = {"rule": d.get("rule"), "action": d.get("action"),
                                    "reason": d.get("reason")}
            n_queued += 1
            continue
        now = datetime.now().isoformat(timespec="seconds")
        prev = entries.get(eid, {})
        entries[eid] = {
            "label": rec["label"],
            "doi": rec.get("doi"),
            "stages": rec["stages"],
            "reason_code": reason_code(rec),
            "queued_at": now,
            "note": "auto-queued by audit_promotion_ledger；原因码/处置待复核",
        }
        if prev:
            entries[eid]["queued_at"] = prev.get("queued_at", now)
        n_queued += 1
        newly_queued += 1
        newly.append(eid)

    # 队列里已不在 R/U 判定集的陈旧条目（如改判 I）→ 移除，留审计痕迹在 git
    stale = [eid for eid in entries if eid not in judged]
    for eid in stale:
        entries.pop(eid)

    queue["updated_at"] = datetime.now().isoformat(timespec="seconds")
    queue["invariant"] = "R/U 判定 = KB 收录 + 待补队列（每条带原因码）；newly_queued=0"
    queue["counts"] = {
        "judged_RU": len(judged),
        "in_kb": n_kb,
        "dispositioned": n_dispositioned,
        "queued": n_queued,
        "newly_queued": newly_queued,
    }
    TERM.mkdir(parents=True, exist_ok=True)
    with open(QUEUE_PATH, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=2)

    from collections import Counter
    codes = Counter(q["reason_code"] for q in entries.values())
    print(f"R/U 判定（S6+S7 全轮次合并去重）: {len(judged)}")
    print(f"  KB 已收录           : {n_kb}")
    print(f"  待补队列            : {n_queued}  {dict(codes)}")
    print(f"  本次新发现无着落    : {newly_queued} {newly[:8]}")
    print(f"[ok] 队列 {QUEUE_PATH}")
    if newly_queued and args.strict:
        print("[fail] 存在未处置的 R/U 判定（先复核队列或修复晋升管线）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
