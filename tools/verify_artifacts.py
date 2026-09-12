# -*- coding: utf-8 -*-
"""核对每个产物是否"能为自己认证的输入把关"。

为什么需要：2026-09-12 实测过一次脱钩 —— `validation_report_v1.json` 记录的
`candidates_sha256` 指向一份**已不存在的**候选文件（验证完成 32 秒后 discover 又写了一次盘）。
报告说"我基于 X"，就必须有人能验 X 还在不在、还没变。

两条设计原则：

1. **只读**。脱钩本身就是证据，本工具绝不修改任何产物。
2. **不确定就说不确定**。老产物没记输入路径的，报 `UNRESOLVED` / `NO_PROVENANCE`，
   不猜、也不当成通过 —— 把"不知道"和"通过"混在一起，是最容易骗到自己的做法。

用法：
    python tools/verify_artifacts.py             # 扫描 datasets/ 下全部产物
    python tools/verify_artifacts.py --json P    # 另存报告
退出码：出现 MISMATCH / MISSING 时为 1，其余为 0。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATASET = os.path.join(ROOT, "datasets", "photopolymerization_v1")

# 老产物没记 `*_file` 时的兜底映射（**仓库相对路径**）。只在唯一命中时使用；
# 命中多个候选一律 UNRESOLVED —— 猜错了比不猜更糟。
BY_CONVENTION = {
    "candidates_sha256": ["datasets/photopolymerization_v1/emergence_candidates_v2.json"],
    "concepts_sha256": ["datasets/photopolymerization_v1/concepts_full_v2.jsonl",
                        "datasets/photopolymerization_v1/concepts_v1.jsonl"],
    "db_sha256": ["datasets/photopolymerization_v1/paper_meta.db"],
    "spec_sha256": ["datasets/photopolymerization_v1/edge_event_spec.yaml",
                    "datasets/photopolymerization_v1/extraction_spec.yaml"],
    "scope_allowlist_sha256": ["datasets/photopolymerization_v1/scope_allowlist.yaml"],
    "prompt_sha256": ["prompts/p4_1a_concept_extraction_v1.md"],
}

# 同一份文件在不同产物里用了不同键名（历史命名不统一）——显式登记，不靠猜前缀
PATH_KEY_ALIASES = {
    "sample": ["sample_jsonl", "sample_file", "sample_path"],
    "source_db": ["source_db", "source_db_file"],
}

OK, MISMATCH, MISSING, UNRESOLVED, NO_PROVENANCE = (
    "OK", "MISMATCH", "MISSING", "UNRESOLVED", "NO_PROVENANCE")
BAD = (MISMATCH, MISSING)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _rel(p):
    try:
        return os.path.relpath(p)
    except ValueError:
        return p


def _resolve_path(value, base):
    """把产物里记的路径解析成真实文件。

    历史命名不统一，相对路径有时相对于**产物所在目录**（`sample_jsonl: sample_full_v2.jsonl`），
    有时相对于**仓库根**（`db_file: datasets/photopolymerization_v1/paper_meta.db`）。
    规则：按 [产物目录, 仓库根, datasets 目录] 依次尝试，取第一个存在的；
    都不存在时回落到产物目录拼接（好让报告里看到完整的错误路径）。
    """
    if os.path.isabs(value):
        return value
    for b in (base, ROOT, DATASET):
        p = os.path.join(b, value)
        if os.path.exists(p):
            return p
    return os.path.join(base, value)


def resolve_target(key, recorded, base):
    """决定某个 `*_sha256` 字段该拿哪个文件核对。返回 (path|None, how, note)。"""
    stem = key[:-len("_sha256")] if key.endswith("_sha256") else key
    keys = [stem + "_file", stem + "_path", stem] + PATH_KEY_ALIASES.get(stem, [])
    for cand_key in keys:
        v = recorded.get(cand_key)
        if isinstance(v, str) and v:
            return _resolve_path(v, base), "explicit:%s" % cand_key, None

    names = BY_CONVENTION.get(key)
    if not names:
        return None, "none", "无显式路径，且无兜底映射（可能是同名不同指的工具字段）"
    hits = [os.path.join(ROOT, n) for n in names if os.path.exists(os.path.join(ROOT, n))]
    if len(hits) == 1:
        return hits[0], "convention", None
    if not hits:
        return None, "none", "兜底候选都不存在: %s" % ", ".join(names)
    return None, "ambiguous", "兜底候选命中多个: %s" % ", ".join(
        os.path.basename(h) for h in hits)


def audit_artifact(path, base=None):
    """只读核对一份产物。返回 {file, status, checks[]}。"""
    base = base or os.path.dirname(path)
    with open(path, encoding="utf-8") as f:
        try:
            payload = json.load(f)
        except json.JSONDecodeError as e:
            return {"file": _rel(path), "status": UNRESOLVED,
                    "checks": [{"key": "-", "status": UNRESOLVED,
                                "note": "不是合法 JSON: %s" % e}]}
    if not isinstance(payload, dict):
        return {"file": _rel(path), "status": NO_PROVENANCE, "checks": []}

    buckets = []
    for holder_name in ("inputs", "provenance"):
        holder = payload.get(holder_name)
        if isinstance(holder, dict):
            buckets.append((holder_name, holder))
    buckets.append(("(top)", payload))

    checks = []
    seen = set()
    for holder_name, holder in buckets:
        for key, rec in holder.items():
            if not (isinstance(key, str) and key.endswith("_sha256")):
                continue
            if not isinstance(rec, str) or not rec:
                continue
            tag = "%s.%s" % (holder_name, key) if holder_name != "(top)" else key
            if tag in seen:
                continue
            seen.add(tag)
            target, how, note = resolve_target(key, holder, base)
            if target is None:
                checks.append({"key": tag, "recorded": rec[:16], "status": UNRESOLVED,
                               "how": how, "note": note})
                continue
            if not os.path.exists(target):
                checks.append({"key": tag, "recorded": rec[:16], "status": MISSING,
                               "how": how, "file": _rel(target),
                               "note": "产物认证的文件已不存在"})
                continue
            cur = _sha256(target)
            checks.append({"key": tag, "recorded": rec[:16], "current": cur[:16],
                           "how": how, "file": _rel(target),
                           "status": OK if cur == rec else MISMATCH,
                           "note": None if cur == rec else "sha 不符（输入已变或产物过时）"})

    if not checks:
        status = NO_PROVENANCE
    elif any(c["status"] in BAD for c in checks):
        status = MISMATCH if any(c["status"] == MISMATCH for c in checks) else MISSING
    elif any(c["status"] == UNRESOLVED for c in checks):
        status = UNRESOLVED
    else:
        status = OK
    return {"file": _rel(path), "status": status, "checks": checks}


def scan(base=None):
    base = base or DATASET
    out = []
    if not os.path.isdir(base):
        return out
    for name in sorted(os.listdir(base)):
        if name.endswith(".json"):
            out.append(audit_artifact(os.path.join(base, name), base))
    return out


def summarize(results):
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    counts["needs_action"] = sum(1 for r in results if r["status"] in BAD)
    return counts


def print_report(results):
    tag = {OK: "ok ", MISMATCH: "BAD", MISSING: "BAD", UNRESOLVED: "?  ",
           NO_PROVENANCE: "?  "}
    print("产物溯源核对（只读）｜ 共 %d 份" % len(results))
    for r in results:
        print("   [%s] %-46s %s" % (tag.get(r["status"], "?"),
                                    os.path.basename(r["file"]), r["status"]))
        for c in r["checks"]:
            mark = "ok " if c["status"] == OK else (
                "BAD" if c["status"] in BAD else "?  ")
            print("         [%s] %-26s 记录 %-16s 磁盘 %-16s %s"
                  % (mark, c["key"], c.get("recorded", "-"), c.get("current", "-"),
                     c.get("note") or c.get("file", "")))
    s = summarize(results)
    print("\n小结：%s" % json.dumps(s, ensure_ascii=False))
    if s["needs_action"]:
        print("需要处理：脱钩产物**不要直接覆盖** —— 它本身就是证据。"
              "按当前 spec/输入重跑，覆盖前先留档。")
    return 0 if s["needs_action"] == 0 else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=DATASET)
    ap.add_argument("--json", dest="json_out", default=None)
    args = ap.parse_args(argv)
    results = scan(args.base)
    rc = print_report(results)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"results": results, "summary": summarize(results)},
                      f, ensure_ascii=False, indent=1)
        print("[ok] 报告 -> %s" % _rel(args.json_out))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
