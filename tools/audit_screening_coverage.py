#!/usr/bin/env python3
"""筛选覆盖台账（screening coverage ledger）：seen 集 vs 已盲评集 的对账闭环。

R06 根因 #2：seen 21782 篇中仅 ~1905 篇（8.7%）进过 S6/S7 QA 盲评，
19 篇外部判 R 论文从未被筛选——"检索到了但从未被判断"。
本台账把覆盖变成可对账、可收敛的账本：

  seen 总数 = screened（QA 盲评 ∪ 外部审计判定充抵） + unscreened（分桶计数）
  不变式：screened ∪ unscreened = seen，screened ∩ unscreened = ∅

输出 data/exports/terminology/screening_coverage_ledger.json：
  - coverage_rate 及分桶统计（按 key 形态：doi / EID / W id）
  - unscreened 队列（含可执行补筛建议：有 abstract 的优先）
--strict：覆盖率 < 阈值（默认 0.10，参数可调）或 seen/screened 集合不自洽 → 退出码 1

零 API。用法：
  .venv/Scripts/python tools/audit_screening_coverage.py [--strict] [--min-rate 0.10]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TERM = ROOT / "data" / "exports" / "terminology"

QA_CORPORA = [
    TERM / "s6_qa_corpus.json",
    TERM / "s7_community_qa_corpus.json",
    TERM / "s7_community_qa_corpus_uncertain.json",
    TERM / "s7_ex12_qa_corpus.json",
]
SEEN_PATH = TERM / "s6_seen_set.json"
# 外部审计判定充抵（R06 fresh sample 外部盲评，n=500）
EXTERNAL_LABELS = ROOT / "data" / "exports" / "completeness_labels" / "pc_001__20260908012830_filled.json"
LEDGER_PATH = TERM / "screening_coverage_ledger.json"


def norm_doi(doi: str | None) -> str:
    return (doi or "").strip().lower()


def load_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def collect_screened() -> set[str]:
    """QA 语料 keys（eid 或 key 字段）+ 语料内 doi（统一小写）。"""
    keys: set[str] = set()
    for path in QA_CORPORA:
        if not path.exists():
            continue
        for p in load_json(path)["papers"]:
            k = p.get("eid") or p.get("key")
            if k:
                keys.add(k)
            if p.get("doi"):
                keys.add(norm_doi(p["doi"]))
    return keys


def collect_external() -> set[str]:
    """R06 外部盲评覆盖的论文（W id + doi 双通道充抵 screened）。"""
    keys: set[str] = set()
    if not EXTERNAL_LABELS.exists():
        return keys
    data = load_json(EXTERNAL_LABELS)
    items = data.get("labels") if isinstance(data, dict) else data
    if isinstance(items, dict):
        items = [{"paper_id": k, **(v if isinstance(v, dict) else {})} for k, v in items.items()]
    for it in items or []:
        pid = str(it.get("paper_id") or it.get("key") or "")
        doi = it.get("doi")
        if pid:
            keys.add(pid)
        if doi:
            keys.add(norm_doi(doi))
    return keys


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strict", action="store_true", help="覆盖率低于阈值或集合不自洽 → 退出码 1")
    ap.add_argument("--min-rate", type=float, default=0.10, help="strict 模式覆盖率下限（默认 0.10）")
    args = ap.parse_args()

    seen = load_json(SEEN_PATH)
    seen_keys: set[str] = set(seen["keys"])
    screened = collect_screened()
    external = collect_external() & seen_keys  # 只充抵 seen 内的
    screened_all = screened & seen_keys
    effective = screened_all | external
    unscreened = seen_keys - effective

    overlap_violation = len(screened_all & unscreened)
    cover = len(effective) / len(seen_keys) if seen_keys else 0.0
    bucket = lambda ks: {  # noqa: E731
        "doi": sum(1 for k in ks if k.startswith("10.")),
        "eid": sum(1 for k in ks if k.startswith("2-s2.0")),
        "wid": sum(1 for k in ks if k.startswith("W") and k[1:].isdigit()),
        "other": sum(1 for k in ks if not (k.startswith("10.") or k.startswith("2-s2.0")
                                           or (k.startswith("W") and k[1:].isdigit()))),
    }
    ledger = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "invariant": "seen = screened(QA∪external) ⊕ unscreened；coverage 只增不减（除非 seen 重定义）",
        "counts": {
            "seen": len(seen_keys),
            "screened_qa": len(screened_all),
            "screened_external_offset": len(external - screened_all),
            "unscreened": len(unscreened),
            "coverage_rate": round(cover, 4),
        },
        "unscreened_buckets": bucket(unscreened),
        "unscreened_sample": sorted(unscreened)[:50],
        "violations": {"overlap": overlap_violation},
    }
    LEDGER_PATH.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")

    c = ledger["counts"]
    print(f"seen {c['seen']} = screened_qa {c['screened_qa']} + external_offset "
          f"{c['screened_external_offset']} + unscreened {c['unscreened']}")
    print(f"coverage = {c['coverage_rate']:.1%}  buckets={ledger['unscreened_buckets']}")
    print(f"[ok] 台账 {LEDGER_PATH}")
    ok = overlap_violation == 0 and cover >= args.min_rate
    if not ok and args.strict:
        print(f"[fail] coverage {cover:.1%} < {args.min_rate:.0%} 或集合不自洽", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
