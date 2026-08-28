"""Paper identity 审计 + 置信分级重复合并（Phase 1.8 前置）。

问题：同一篇论文可能以多种 paper_id 存在于 KB（历史 ingest 重复）：
    DOI 形式:  10.xxxx/yyyy
    OpenAlex:  openalex:https://openalex.org/W...
同一篇论文同时以多条记录存在 → 污染 paper/edge/closed 计数。

置信分级（用户定）：
    EXACT    DOI ↔ OpenAlex DOI mapping 一致          → 自动 merge
    STRONG   normalized title + year 高度一致         → 可 merge，记录依据
    WEAK     只有标题相似                              → 不自动删除（仅报告）

核心原则：**canonical identity 与 surviving DB row 分离**——
即使保留 OpenAlex v2 那条记录，也要把旧 DOI merge 进保留记录的
canonical_paper_id/doi/openalex_id 字段，否则以后 Scopus→DOI、
OpenAlex→W-ID 还会再次插成两篇。

用法（A 需网络，B 会删记录，先 dry-run 看报告）:
    python tools/audit_paper_identity.py                     # 统计（EXACT/STRONG/WEAK 分级）
    python tools/audit_paper_identity.py --dry-run --merge   # 预览 merge
    python tools/audit_paper_identity.py --merge             # 执行 merge（EXACT + STRONG）
    python tools/audit_paper_identity.py --json              # 输出 identity map
"""

import argparse
import asyncio
import json
import os
import re
import sys

from search_engine.knowledge_base import KnowledgeBase
from search_engine.backends import OpenAlexBackend

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# ── 标题归一化（STRONG/WEAK 判定用）─────────────────────────

_STOP = {"the", "a", "an", "of", "in", "for", "and", "on", "with", "via", "by", "to", "at", "from"}


def _norm_title(t: str) -> str:
    t = (t or "").lower()
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    toks = [w for w in t.split() if w and w not in _STOP]
    return " ".join(toks)


# ── OpenAlex 解析 ──────────────────────────────────────

def _strip_openalex_prefix(pid: str) -> str:
    """'openalex:https://openalex.org/W...' 或 'openalex:W...' → 'W...'"""
    if pid.startswith("openalex:"):
        pid = pid[len("openalex:"):]
    if pid.startswith("https://openalex.org/"):
        pid = pid[len("https://openalex.org/"):]
    return pid


async def _fetch_with_retry(oa, oid: str, retries: int = 2) -> list:
    """_fetch_works_by_ids 带重试（连续几十个请求容易 rate-limit/抖动，导致假 unresolved）。"""
    last = []
    for _ in range(retries + 1):
        try:
            papers = await oa._fetch_works_by_ids([oid])
            if papers:
                return papers
            last = papers
        except Exception:
            pass
        await asyncio.sleep(0.4)
    return last


async def _resolve(oa, rec) -> dict:
    """→ {key, doi, openalex_id, title, year}；key=None = 无法解析。"""
    pid = rec.paper_id
    try:
        if pid.startswith("openalex:"):
            oid = _strip_openalex_prefix(pid)
            papers = await _fetch_with_retry(oa, oid)
            if not papers:
                return {"key": oid, "doi": None, "openalex_id": oid,
                        "title": rec.problem or "", "year": None}
            p = papers[0]
            return {"key": (p.doi or oid), "doi": p.doi, "openalex_id": oid,
                    "title": p.title or "", "year": p.year}
        if pid.startswith("10."):
            p = await oa.get_by_doi(pid)
            if p is None:
                return {"key": pid, "doi": pid, "openalex_id": None,
                        "title": rec.problem or "", "year": None}
            oid = _strip_openalex_prefix(p.paper_id)
            return {"key": pid, "doi": pid, "openalex_id": oid,
                    "title": p.title or "", "year": p.year}
    except Exception as e:
        print(f"  ⚠ 解析失败 {pid}: {type(e).__name__}: {str(e)[:80]}")
    return {"key": None, "doi": None, "openalex_id": None, "title": rec.problem or "", "year": None}


def _is_openalex(pid: str) -> bool:
    return pid.startswith("openalex:")


# ── 分级检测 ──────────────────────────────────────────

def _member(rec, info: dict) -> dict:
    """统一成员结构（EXACT/STRONG/WEAK 共用，merge 阶段依赖这些字段）。"""
    return {
        "paper_id": rec.paper_id,
        "key": info.get("key"),
        "doi": info.get("doi"),
        "openalex_id": info.get("openalex_id"),
        "title": info.get("title", "") or rec.problem or "",
        "year": info.get("year"),
        "version": rec.extractor_version,
        "n_edges": len(rec.route_mechanism_edges),
    }


def _exact_groups(records, resolved) -> tuple[dict, list]:
    """EXACT：按 doi（或 openalex_id）分组的重复。key 相同 = 同论文。"""
    groups: dict[str, list] = {}
    unresolved = []
    for rec in records:
        info = resolved.get(rec.paper_id, {})
        key = info.get("key")
        if key is None:
            unresolved.append(rec)
            continue
        groups.setdefault(key, []).append(_member(rec, info))
    return groups, unresolved


def _strong_pairs(records, resolved, exclude_keys: set) -> tuple[list, list]:
    """STRONG + IDENTITY_CONFLICT 检测（对 EXACT 未覆盖的记录两两比较）。

    返回 (pairs, conflicts)：
      pairs:     [(a, b, basis)] —— STRONG，可 merge（title+year 一致且无冲突标识）
      conflicts: [(a, b, basis)] —— IDENTITY_CONFLICT，**永不自动 merge**

    冲突优先级（用户定）：
      title+year 相同
        ├─ DOI 相同 → EXACT（已在 _exact_groups 处理）
        ├─ 两个非空 DOI 不同 → IDENTITY_CONFLICT（正式论文 vs preprint / .s001 supplementary）
        └─ 无冲突 DOI（一侧缺失）→ STRONG 可 merge

    两个明确存在的不同 DOI 直接覆盖 title similarity——W4401422516(ssrn)
    vs W4404857218(pc.29332)、W7111174545(.s001) vs W4409160795 都不是 dedup 关系。
    """
    cands = []
    for rec in records:
        info = resolved.get(rec.paper_id, {})
        key = info.get("key")
        if key is None or key in exclude_keys:
            continue
        m = _member(rec, info)
        m["norm"] = _norm_title(m["title"])
        cands.append(m)
    pairs, conflicts = [], []
    for i in range(len(cands)):
        for j in range(i + 1, len(cands)):
            a, b = cands[i], cands[j]
            if not a["norm"] or a["norm"] != b["norm"]:
                continue
            # IDENTITY_CONFLICT：两个非空 DOI 存在且不同（含 .s001 supplementary 保护）
            da = (a["doi"] or "").strip().lower()
            db = (b["doi"] or "").strip().lower()
            if da and db and da != db:
                conflicts.append((a, b, f"IDENTITY_CONFLICT: doi 不同 ({a['doi']} vs {b['doi']})"))
                continue
            # STRONG 需要 year 一致（都 None 视为无法确认 → WEAK，不 merge）
            if a["year"] and b["year"] and a["year"] != b["year"]:
                continue
            if a["year"] and b["year"]:
                pairs.append((a, b, f"title+year 一致 ({a['year']})"))
            elif a["year"] or b["year"]:
                pairs.append((a, b, f"title 一致 + year 单侧缺失"))
            # 双侧 year 均 None → 仅标题 → WEAK，不加入
    return pairs, conflicts


def _weak_pairs(records, resolved, exclude_keys: set, strong_seen: set) -> list:
    """WEAK：normalized title 相同但无法确认 year（双侧 None）——只报告不 merge。"""
    cands = []
    for rec in records:
        info = resolved.get(rec.paper_id, {})
        key = info.get("key")
        if key is None or key in exclude_keys or rec.paper_id in strong_seen:
            continue
        m = _member(rec, info)
        m["norm"] = _norm_title(m["title"])
        cands.append(m)
    pairs = []
    for i in range(len(cands)):
        for j in range(i + 1, len(cands)):
            a, b = cands[i], cands[j]
            if a["norm"] and a["norm"] == b["norm"] and not a["year"] and not b["year"]:
                pairs.append((a, b))
    return pairs


def _print_report(records, resolved, exact_groups, strong_pairs, conflict_pairs, weak_pairs, unresolved):
    v1 = sum(1 for r in records if r.extractor_version.startswith("1."))
    v2 = sum(1 for r in records if r.extractor_version.startswith("2."))
    dup_exact = {k: v for k, v in exact_groups.items() if len(v) > 1}
    n_dup_exact = sum(len(v) for v in dup_exact.values())
    print("\n" + "=" * 72)
    print("Paper Identity 审计（EXACT / STRONG / IDENTITY_CONFLICT / WEAK）")
    print("=" * 72)
    print(f"records_total            : {len(records)}")
    print(f"v1_records               : {v1}")
    print(f"v2_records               : {v2}")
    print(f"resolved                 : {sum(1 for r in records if resolved.get(r.paper_id, {}).get('key'))}")
    print(f"unresolved               : {len(unresolved)}")
    print(f"EXACT 重复组             : {len(dup_exact)} 组 / {n_dup_exact} 条   （自动 merge）")
    print(f"STRONG 重复对            : {len(strong_pairs)} 对        （可 merge，记录依据）")
    print(f"IDENTITY_CONFLICT 对     : {len(conflict_pairs)} 对        （永不自动 merge）")
    print(f"WEAK 标题相似            : {len(weak_pairs)} 对        （不自动删除）")
    unique_papers = len(exact_groups) + len(unresolved)
    print(f"unique_papers            : {unique_papers}  (EXACT 合并后)")

    if dup_exact:
        print("\nEXACT 组明细（自动 merge 候选）:")
        for key, members in sorted(dup_exact.items()):
            print(f"  [{str(key)[:42]}]")
            for m in members:
                tag = "  ← canonical 候选" if (_is_openalex(m["paper_id"]) or m["n_edges"] > 0) else ""
                print(f"    - {m['paper_id'][:50]}  v{m['version']} edges={m['n_edges']}{tag}")
    if strong_pairs:
        print("\nSTRONG 对明细（可 merge，记录依据）:")
        for a, b, basis in strong_pairs:
            print(f"  [{basis}]")
            print(f"    - {a['paper_id'][:50]}  year={a['year']}  {a['title'][:50]}")
            print(f"    - {b['paper_id'][:50]}  year={b['year']}  {b['title'][:50]}")
    if conflict_pairs:
        print("\nIDENTITY_CONFLICT 对明细（正式论文/preprint/supplementary，不 merge）:")
        for a, b, basis in conflict_pairs:
            print(f"  [{basis}]")
            print(f"    - {a['paper_id'][:50]}  doi={a.get('doi') or '-'}")
            print(f"    - {b['paper_id'][:50]}  doi={b.get('doi') or '-'}")
    if weak_pairs:
        print("\nWEAK 对明细（仅标题相似，不删除）:")
        for a, b in weak_pairs:
            print(f"  - {a['paper_id'][:50]} ↔ {b['paper_id'][:50]}  ({a['title'][:40]})")
    if unresolved:
        print(f"\n未解析记录（保留原样，不参与 merge）:")
        for r in unresolved:
            print(f"  - {r.paper_id[:55]}  v{r.extractor_version}")


def _merge_identity(kb, canonical_member: dict, merged_infos: list) -> None:
    """把 canonical 自身（缺失时）+ 被删记录的 identity（doi/openalex_id）合并进保留记录。

    原则：canonical identity 与 surviving row 分离——即使保留 openalex 行，
    也要把旧 DOI 写进 canonical_paper_id/doi，防止以后 Scopus→DOI 再插一篇。
    canonical 自己的 openalex_id 优先（老数据缺失时一并回填），
    被删记录的 id 只在 canonical 仍缺失时才填入。
    """
    rec = kb.get(canonical_member["paper_id"])
    if rec is None:
        return
    for info in [canonical_member] + merged_infos:
        if not rec.doi and info.get("doi"):
            rec.doi = info["doi"]
        if not rec.openalex_id and info.get("openalex_id"):
            rec.openalex_id = info["openalex_id"]
    rec.canonical_paper_id = f"doi:{rec.doi}" if rec.doi else rec.paper_id
    kb.store(rec)


async def main():
    ap = argparse.ArgumentParser(description="Paper identity 审计 + 置信分级重复合并")
    ap.add_argument("--merge", action="store_true", help="合并重复记录（EXACT 自动 + STRONG 记录依据）")
    ap.add_argument("--dry-run", action="store_true", help="与 --merge 同用：只预览不实际删除")
    ap.add_argument("--json", action="store_true", help="输出 identity map 到 data/exports/paper_identity.json")
    ap.add_argument("--mailto", default=os.environ.get("OPENALEX_MAILTO", ""), help="OpenAlex polite pool email")
    args = ap.parse_args()

    kb = KnowledgeBase()
    records = kb.get_all()
    if not records:
        print("KB 为空。")
        kb.close()
        return
    print(f"读取 {len(records)} 条记录，正在解析 identity（OpenAlex，带重试）...")
    mailto = args.mailto or None
    resolved: dict = {}
    async with OpenAlexBackend(mailto=mailto) as oa:
        for i, rec in enumerate(records, 1):
            info = await _resolve(oa, rec)
            resolved[rec.paper_id] = info
            print(f"  [{i}/{len(records)}] {rec.paper_id[:48]:<50} → doi={info.get('doi') or '-'}  "
                  f"oid={info.get('openalex_id') or '-'}")

    exact_groups, unresolved = _exact_groups(records, resolved)
    dup_exact = {k: v for k, v in exact_groups.items() if len(v) > 1}
    strong_pairs, conflict_pairs = _strong_pairs(records, resolved, exclude_keys=set(dup_exact.keys()))
    seen = set()
    for a, b, _ in strong_pairs + conflict_pairs:
        seen.add(a["paper_id"])
        seen.add(b["paper_id"])
    weak_pairs = _weak_pairs(records, resolved, exclude_keys=set(dup_exact.keys()), strong_seen=seen)

    _print_report(records, resolved, exact_groups, strong_pairs, conflict_pairs, weak_pairs, unresolved)

    if args.json:
        os.makedirs("data/exports", exist_ok=True)
        path = "data/exports/paper_identity.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "records_total": len(records),
                "resolved": {pid: {"doi": info.get("doi"), "openalex_id": info.get("openalex_id")}
                             for pid, info in resolved.items()},
                "exact_groups": {k: v for k, v in dup_exact.items()},
                "strong_pairs": [[a["paper_id"], b["paper_id"], basis] for a, b, basis in strong_pairs],
                "identity_conflicts": [[a["paper_id"], b["paper_id"], basis] for a, b, basis in conflict_pairs],
                "weak_pairs": [[a["paper_id"], b["paper_id"]] for a, b in weak_pairs],
                "unresolved": [r.paper_id for r in unresolved],
            }, f, ensure_ascii=False, indent=2)
        print(f"\nidentity map 已存: {path}")

    if args.merge:
        all_merges = [(k, v, "EXACT") for k, v in dup_exact.items()]
        for a, b, basis in strong_pairs:
            all_merges.append((f"STRONG:{basis}", [a, b], "STRONG"))
        # IDENTITY_CONFLICT 永不自动 merge（正式论文/preprint/supplementary 不是 dedup 关系）
        if conflict_pairs:
            print(f"\n⚠ {len(conflict_pairs)} 对 IDENTITY_CONFLICT 跳过（不 merge）："
                  f"{', '.join(a['paper_id'].split(':')[-1][:12] + '↔' + b['paper_id'].split(':')[-1][:12] for a, b, _ in conflict_pairs)}")
        if not all_merges:
            print("\n没有可 merge 的重复。")
            kb.close()
            return
        print("\n" + "=" * 72)
        print("Merge 执行" + ("（--dry-run 预览，未实际修改）" if args.dry_run else ""))
        print("=" * 72)
        total_before = len(records)
        deleted = 0
        for key, members, level in all_merges:
            print(f"\n[{level}] {str(key)[:45]}")
            # canonical：有 edges 的 > openalex（重抽可拿完整文本）> 首个
            canonical = next(
                (m for m in members if m["n_edges"] > 0),
                next((m for m in members if _is_openalex(m["paper_id"])), members[0]),
            )
            merged_infos = []
            for m in members:
                if m["paper_id"] == canonical["paper_id"]:
                    print(f"  ✓ 保留  {m['paper_id'][:52]}  v{m['version']} edges={m['n_edges']}")
                else:
                    merged_infos.append(m)
                    if not args.dry_run:
                        kb.delete(m["paper_id"])
                    deleted += 1
                    print(f"  ✗ 删除  {m['paper_id'][:52]}  v{m['version']} edges={m['n_edges']}"
                          f"  (→ canonical)")
            # identity 合并：canonical 自身 + DOI/openalex_id 写进保留记录（不随删行丢失）
            if merged_infos and not args.dry_run:
                _merge_identity(kb, canonical, merged_infos)
                print(f"  ↻ identity 合并 → canonical_paper_id=doi:{canonical.get('doi') or '(无)'} "
                      f"doi={canonical.get('doi') or '(已并)'} openalex_id={canonical.get('openalex_id')}")
        records2 = kb.get_all() if not args.dry_run else records
        print(f"\n合并完成: {total_before} → {len(records2)} 条"
              f" ({'预览' if args.dry_run else '实际'})，删除 {deleted} 条")

    kb.close()


if __name__ == "__main__":
    asyncio.run(main())
