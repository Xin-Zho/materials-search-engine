#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/miss_query_reachability.py — Miss Query Reachability Test（2026-09-01 用户拍板）。

问题：S5 depth 实验 INVALID（Scopus export API 忽略 offset + key 级判定混 alias）。
换一个更直接、零联网的检查：对 R05 的 43 篇 miss，判定每篇是否满足任何一条
S5 Boolean query（论文级语义匹配，不看排名）：

    ∃ q ∈ Q_S5: paper ⊨ q ?

分类：
  MATCHES_QUERY_BUT_NOT_RETRIEVED —— 满足某条 query 却不在 candidate DB
      → 问题在 retrieval backend / 收录环节
  MATCHES_NO_QUERY                 —— 不满足任何 query
      → 问题在 query expressivity / coverage（36 条 query 没指向它）

判定近似（保守方向，宁可多判 MATCHES）：
  - 文本 = title + abstract（labels 38/43 有 abstract；缺的用 openalex_cache
    abstract_inverted_index 重建）
  - TITLE-ABS-KEY 词项用子串匹配（比 Scopus token 匹配宽松，只可能多命中）
  - 通配符（"cure shrink*" / "3d print*"）按前缀匹配
  - 顶层 AND 分 clause、clause 内 OR 分组（括号感知分割）
  - 注：Scopus 的 TITLE-ABS-KEY 含 keywords 字段，我们只有 title/abstract——
    若 miss 仅 keywords 命中会误判 MATCHES_NO_QUERY（保守方向：偏 query-side 结论）

输入（全部复用现有产物，不重跑检索）：
  --misses  data/exports/terminology/r05_misses.json
  --labels  R05 filled labels（取 abstract）
  --actions data/exports/terminology/s5_final_actions.json（36 条冻结 query）

输出：
  r05_miss_query_reachability.json
    per-miss：verdict（MATCHES_*）+ matched action_ids（或 []）
    分布：MATCHES_QUERY_BUT_NOT_RETRIEVED n / MATCHES_NO_QUERY n
    结论分支：
      MATCHES_QUERY_BUT_NOT_RETRIEVED 大 → retrieval backend 问题
      MATCHES_NO_QUERY 大             → query expressivity/coverage 问题（支持 Query Generator/RL）
"""
import argparse
import datetime
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tools"))

T = os.path.join(BASE, "data", "exports", "terminology")
DEFAULT_MISSES = os.path.join(T, "r05_misses.json")
DEFAULT_ACTIONS = os.path.join(T, "s5_final_actions.json")
DEFAULT_OUT = os.path.join(T, "r05_miss_query_reachability.json")
OA_CACHE = os.path.join(BASE, "data", "cache", "openalex_cache.json")

# ── Boolean query 解析（Scopus TITLE-ABS-KEY 子集）────────────────────────

def _split_top_level(s: str, sep: str) -> list[str]:
    """按 sep（'AND'/'OR' 多字符 token）在顶层（括号外）分割。
    词边界：sep 前后非字母（防 'land' 里的 and 误切）。
    """
    parts, depth, cur = [], 0, []
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch == "(":
            depth += 1
            cur.append(ch)
            i += 1
            continue
        if ch == ")":
            depth -= 1
            cur.append(ch)
            i += 1
            continue
        if depth == 0 and s.startswith(sep, i):
            before_ok = (i == 0 or not s[i - 1].isalpha())
            after_ok = (i + len(sep) >= n or not s[i + len(sep)].isalpha())
            if before_ok and after_ok:
                parts.append("".join(cur).strip())
                cur = []
                i += len(sep)
                continue
        cur.append(ch)
        i += 1
    if cur:
        parts.append("".join(cur).strip())
    return parts


def _strip_outer_parens(s: str) -> str:
    """剥掉完全包裹整个字符串的最外层括号（clause 级 OR 组）。"""
    s = s.strip()
    while s.startswith("(") and s.endswith(")"):
        depth, ok = 0, True
        for i, ch in enumerate(s):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i != len(s) - 1:
                    ok = False
                    break
        if ok and depth == 0:
            s = s[1:-1].strip()
        else:
            break
    return s


def parse_query(q: str) -> list[list[str]] | None:
    """解析 TITLE-ABS-KEY(EXPR) → clauses（AND 顶层），每 clause = OR 词项列表。
    返回 None 表示解析失败（不参与判定）。
    词项清理：去引号、去尾部通配符、小写；空项丢弃。
    """
    m = re.search(r"TITLE-ABS-KEY\((.*)\)$", q.strip(), flags=re.S)
    if not m:
        return None
    inner = m.group(1)
    clauses = []
    for part in _split_top_level(inner, "AND"):
        terms = []
        for t in _split_top_level(_strip_outer_parens(part), "OR"):
            t = t.strip().strip('"').strip("'").strip()
            if not t:
                continue
            terms.append(t.rstrip("*").lower())
        if terms:
            clauses.append(terms)
    return clauses or None


def clause_hit(clause: list[str], text: str) -> bool:
    """clause（OR 词项列表）命中 = 任一词项子串命中文本。
    带通配符的已在解析时 rstrip('*')——按前缀子串匹配即可（子串比前缀宽，
    保守多判 MATCHES）。
    """
    tl = text.lower()
    return any(t in tl for t in clause)


def query_hit(clauses: list[list[str]] | None, text: str) -> bool:
    """query 命中 = 所有 clause 命中（AND 语义）。None（解析失败）→ 不命中。"""
    if not clauses:
        return False
    return all(clause_hit(c, text) for c in clauses)


# ── 文本获取 ───────────────────────────────────────────────

def build_text_map(labels_path: str, misses: list[dict]) -> dict:
    """paper_id -> (title, abstract)。labels 优先；缺 abstract 用 openalex 重建。"""
    text_map = {}
    try:
        labs = json.load(open(labels_path, encoding="utf-8"))["labels"]
        for l in labs:
            text_map[l["paper_id"]] = (l.get("title") or "", l.get("abstract") or "")
    except Exception:
        pass
    # 缺 abstract 的 miss 从 openalex cache 补（abstract_inverted_index 重建）
    need = [m["paper_id"] for m in misses if m["paper_id"] not in text_map
            or not text_map[m["paper_id"]][1]]
    if need:
        try:
            cache = json.load(open(OA_CACHE, encoding="utf-8"))
            want = set(need)
            for q, resp in cache.items():
                for w in resp.get("results", []):
                    wid = (w.get("id") or "").replace("https://openalex.org/", "")
                    if wid not in want:
                        continue
                    inv = w.get("abstract_inverted_index") or {}
                    if inv:
                        pos = {}
                        for word, idxs in inv.items():
                            for i in idxs:
                                pos[i] = word
                        ab = " ".join(pos[i] for i in sorted(pos)) if pos else ""
                    else:
                        ab = ""
                    t, a = text_map.get(wid, ("", ""))
                    if not a and ab:
                        text_map[wid] = (t or w.get("title") or "", ab)
        except Exception as e:
            print(f"[WARN] openalex abstract 补缺失败: {e}")
    return text_map


def main():
    ap = argparse.ArgumentParser(description="Miss Query Reachability Test")
    ap.add_argument("--misses", default=DEFAULT_MISSES)
    ap.add_argument("--labels",
                    default=r"C:\Users\Administrator\Downloads\pc_001__20260831162811_filled.json")
    ap.add_argument("--actions", default=DEFAULT_ACTIONS)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--sample", type=int, default=0,
                    help=">0 时只随机抽 N 篇（默认 0 = 全部 43）")
    args = ap.parse_args()

    miss = json.load(open(args.misses, encoding="utf-8"))["misses"]
    acts = json.load(open(args.actions, encoding="utf-8"))["actions"]
    if args.sample:
        import random
        rng = random.Random(20260901)
        miss = rng.sample(miss, min(args.sample, len(miss)))

    text_map = build_text_map(args.labels, miss)

    # 预解析 36 条 query（失败打印警告）
    parsed = []
    for a in acts:
        clauses = parse_query(a["query_string"])
        if clauses is None:
            print(f"[WARN] {a['action_id']} 解析失败（跳过）：{a['query_string'][:60]}")
        parsed.append({"action_id": a["action_id"], "bridge_type": a.get("bridge_type"),
                       "clauses": clauses})

    results = []
    for m in miss:
        pid = m["paper_id"]
        title, abstract = text_map.get(pid, ("", ""))
        text = f"{title} {abstract}"
        matched = []
        for p in parsed:
            if p["clauses"] and query_hit(p["clauses"], text):
                matched.append(p["action_id"])
        verdict = ("MATCHES_QUERY_BUT_NOT_RETRIEVED" if matched
                   else "MATCHES_NO_QUERY")
        results.append({
            "paper_id": pid, "title": title, "year": m.get("year"),
            "doi": m.get("doi"), "verdict": verdict,
            "matched_actions": matched,
            "text_available": bool(title) or bool(abstract),
        })

    from collections import Counter
    vc = Counter(r["verdict"] for r in results)
    n_q = vc.get("MATCHES_QUERY_BUT_NOT_RETRIEVED", 0)
    n_no = vc.get("MATCHES_NO_QUERY", 0)
    total = len(results)

    if n_q > n_no * 2:
        branch = ("RETRIEVAL_BACKEND：多数 miss 满足 S5 query 却没进 candidate DB"
                  "——问题在收录/去重/导出环节，不是 query")
    elif n_no > n_q * 2:
        branch = ("QUERY_EXPRESSIVITY：多数 miss 不满足任何 S5 query——36 条 query "
                  "没指向它；支持继续 Query Generator / learned search policy（未来 RL）")
    else:
        branch = ("MIXED：两类相当——需结合 miss 明细判断（可查满足的 action 是否 "
                  "覆盖薄弱）")

    out = {
        "version": "r05_miss_query_reachability_v1",
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "method": "∃q∈Q_S5: paper⊨q（title+abstract 子串匹配；OR 组内任一命中、"
                  "AND 组全命中；通配符前缀；非排名判定）",
        "inputs": {"misses": os.path.basename(args.misses), "n_misses": total,
                   "actions": os.path.basename(args.actions), "n_actions": len(acts)},
        "scope_note": "TITLE-ABS-KEY 含 keywords，我们只有 title/abstract——"
                      "仅 keywords 命中的 miss 会被判 MATCHES_NO_QUERY（保守方向）",
        "summary": {
            "MATCHES_QUERY_BUT_NOT_RETRIEVED": n_q,
            "MATCHES_NO_QUERY": n_no,
            "n_total": total,
            "query_parse_fail": sum(1 for p in parsed if p["clauses"] is None),
            "branch": branch,
        },
        "results": sorted(results, key=lambda r: (r["verdict"], r["year"] or 0)),
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print("=" * 72)
    print(f"Miss Query Reachability Test（{total} miss × {len(acts)} S5 queries）")
    print("=" * 72)
    print(f"  MATCHES_QUERY_BUT_NOT_RETRIEVED = {n_q}/{total}")
    print(f"  MATCHES_NO_QUERY                 = {n_no}/{total}")
    print(f"  query 解析失败                    = {sum(1 for p in parsed if p['clauses'] is None)}")
    print(f"\n  分支: {branch}")
    print("\n=== 明细（MATCHES 在前）===")
    for r in out["results"]:
        tag = "Q✓" if r["verdict"] == "MATCHES_QUERY_BUT_NOT_RETRIEVED" else "  "
        print(f"  [{tag}] {r['title'][:58]}")
        if r["matched_actions"]:
            print(f"        → {','.join(r['matched_actions'])}")
    print(f"\n[OK] {args.out}")


if __name__ == "__main__":
    main()
