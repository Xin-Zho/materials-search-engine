"""tools/reverse_route_attribution.py — v2.0 missed 反向道路分析（用户 2026-08-28 定稿）。

目标：回答"剩下 112 篇（134−22）QGS 为什么没被 v2 找到"，而不是先全局 depth sweep。

每篇 missed paper 的诊断链：
  ① 它属于什么"道路"（problem/material/mechanism/context 词面推断）
  ② 反推"正常应通过什么 query family 找到它"
  ③ v2 有没有这个 family / query？

分类（用户定）：
  A. 道路根本没有  → DIRECTION_GAP（F2）
  B. 有道路但 query 词不对 → QUERY_TERMINOLOGY_GAP（F1；concept 存在但 query 变体缺）
  C. 道路和 query 都有 → EXPORT_DEPTH_CANDIDATE（F3a：query 词面能匹配标题，
     但论文未进 v2.0 run 的 top-100 导出——需 Scopus 查 rank 验证）
  + 已被 v2 找到的 22 篇标 RETRIEVED_OK（不在本表）

⚠️ 反推不得用 TITLE("exact title") 单篇补洞——只做"这一类论文"的系统性归因。
词面匹配是 recall 工具不是判据；每篇输出 evidence（匹配到的 query/concept），
人工可复核。

用法：
  python tools/reverse_route_attribution.py
"""
import argparse
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

BENCH_PATH = os.path.join(BASE, "data", "exports", "pc_001_external_qgs_v1.json")
REGISTRY_PATH = os.path.join(BASE, "data", "query_registry_v2.json")
RUNS_V20_PATH = os.path.join(BASE, "data", "exports", "query_family_runs.json")
OUT_PATH = os.path.join(BASE, "data", "exports", "qgs_reverse_route.json")


def norm_eid(e: str) -> str:
    return (e or "").strip()


def query_phrases(query: str) -> list[str]:
    """提取 query 里双引号短语（小写）。"""
    return [m.lower() for m in re.findall(r'"([^"]+)"', query)]


def load_v20_exported_eids() -> set[str]:
    """v2.0 run（top-100 导出）的 unique EID。"""
    eids = set()
    if os.path.exists(RUNS_V20_PATH):
        runs = json.load(open(RUNS_V20_PATH, encoding="utf-8"))["runs"]
        for r in runs:
            for pid in r["retrieved_ids"]:
                if pid.startswith("2-s2.0"):
                    eids.add(pid)
    return eids


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=OUT_PATH)
    args = ap.parse_args()

    bench = json.load(open(BENCH_PATH, encoding="utf-8"))
    b_scopus = [p for p in bench["papers"] if p.get("scopus_eligibility") == "IN_SCOPUS"]
    registry = json.load(open(REGISTRY_PATH, encoding="utf-8"))
    families = registry["families"]

    # 从 screening manifest 加载 abstract（by idx）——pool 不存 abstract
    # （已查：qgs_candidates_v1.json 740 篇 abstract 全空）。manifest 740 条
    # 中 485 条有 abstract，benchmark 134 篇中 72 篇有。
    MANIFEST = r"C:/Users/Administrator/Downloads/qgs_screening_manifest_completed.json"
    manifest_abstract: dict[int, str] = {}
    if os.path.exists(MANIFEST):
        for p_ in json.load(open(MANIFEST, encoding="utf-8"))["papers"]:
            if p_.get("abstract"):
                manifest_abstract[p_["idx"]] = p_["abstract"]

    # v2 query 短语全集（147 query）+ family concepts
    all_phrases: dict[str, list[str]] = {}     # phrase -> [query 简写]
    family_concepts: dict[str, list[str]] = {}  # family_id -> concepts(全部 slot)
    for f in families:
        fam_phrases = set()
        for q in f["generated_queries"]:
            for ph in query_phrases(q):
                fam_phrases.add(ph)
                all_phrases.setdefault(ph, []).append(f"{f['family_id']}:{q[:50]}")
        family_concepts[f["family_id"]] = sorted(
            {c for slot_terms in f["concepts"].values() for c in slot_terms})
    all_concept_terms = sorted({c for cs in family_concepts.values() for c in cs})

    # v2 已导出 EID（top-100 union，1176）
    v20_eids = load_v20_exported_eids()
    bench_eids = {norm_eid(p.get("scopus_eid")) for p in b_scopus}
    hit_eids = v20_eids & bench_eids

    print(f"B_scopus = {len(b_scopus)} | v2.0 命中 = {len(hit_eids)} | "
          f"missed = {len(b_scopus) - len(hit_eids)}")
    print(f"v2 query 短语数: {len(all_phrases)} | family concepts: {len(all_concept_terms)}")

    rows = []
    for p in b_scopus:
        eid = norm_eid(p.get("scopus_eid"))
        if eid in hit_eids:
            continue   # 22 篇 RETRIEVED_OK 跳过
        title = (p.get("title") or "")
        # 匹配文本 = 标题 + manifest abstract（摘要含短语也算道路存在）
        ab = manifest_abstract.get(p["idx"], "")
        tl = (title + " " + ab).lower()
        has_abstract = bool(ab.strip())
        # ① 词面匹配：标题+摘要含哪些 v2 query 短语
        matched = [(ph, all_phrases[ph][0]) for ph in all_phrases if ph in tl]
        # ② 概念匹配：含哪些 family concept 词（未进 query 的）
        matched_concepts = [c for c in all_concept_terms if c.lower() in tl]
        if matched:
            cls = "EXPORT_DEPTH_CANDIDATE"
            ev = (f"标题{'/摘要' if has_abstract else ''}词面匹配 {len(matched)} 条 query"
                  f"（如 '{matched[0][0]}'→{matched[0][1][:40]}）但未进 top-100 导出——需 Scopus 查 rank")
        elif matched_concepts:
            cls = "QUERY_TERMINOLOGY_GAP"
            ev = f"概念在 v2 family 中（{matched_concepts[:3]}）但无 query 变体覆盖该措辞"
        else:
            cls = "DIRECTION_GAP"
            ev = ("标题+摘要均无任何 v2 query 短语/概念词匹配——道路缺失"
                  + ("" if has_abstract else "（无 abstract 可用，仅按标题）"))
        rows.append({
            "idx": p["idx"], "title": p["title"], "year": p.get("year"),
            "doi": p.get("doi"), "eid": eid,
            "reason_code": p.get("reason_code"),
            "sources_from": p.get("sources_from", []),
            "has_abstract": has_abstract,
            "matched_query_phrases": [m[0] for m in matched[:5]],
            "matched_queries": [m[1] for m in matched[:3]],
            "matched_concepts": matched_concepts[:5],
            "failure_class": cls,
            "evidence": ev,
        })

    from collections import Counter
    dist = Counter(r["failure_class"] for r in rows)
    print("\n112 篇 missed 反向道路归因（词面推断，人工可复核）:")
    for c in ("EXPORT_DEPTH_CANDIDATE", "QUERY_TERMINOLOGY_GAP", "DIRECTION_GAP"):
        n = dist.get(c, 0)
        print(f"  {c:<26} {n:>4}")
    print("\n按 era 的归因分布:")
    for era in ("PRE_2006", "2006_2020", "POST_2020"):
        sub = [r for r in rows if (int(r['year']) <= 2005 if r['year'] and str(r['year']).isdigit() else False) == (era == "PRE_2006")]
        # 简化：用 era 字段重算
    era_of = lambda y: "PRE_2006" if (y and str(y).isdigit() and int(y) <= 2005) else ("2006_2020" if (y and str(y).isdigit() and int(y) <= 2020) else "POST_2020")
    era_dist = {}
    for r in rows:
        e = era_of(r["year"])
        era_dist.setdefault(e, Counter())[r["failure_class"]] += 1
    for e in sorted(era_dist):
        print(f"  {e:<12} {dict(era_dist[e])}")

    # 决策提示（用户定）
    n_dir = dist.get("DIRECTION_GAP", 0)
    n_depth = dist.get("EXPORT_DEPTH_CANDIDATE", 0)
    print("\n决策提示（用户 2026-08-28 定稿）:")
    print(f"  DIRECTION_GAP={n_dir} / EXPORT_DEPTH_CANDIDATE={n_depth}")
    if n_dir >= n_depth:
        print("  → 主导失败是道路缺失：直接进入 v2.1 Cross-Community Discovery，"
              "不必浪费时间拉深搜索")
    else:
        print("  → 主导失败是 depth/rank：值得做 targeted depth 验证"
              "（对 EXPORT_DEPTH_CANDIDATE 查 Scopus rank）")

    out = {"created_at": "2026-08-28",
           "note": "词面推断归因（recall 工具非判据）；EXPORT_DEPTH_CANDIDATE 需 Scopus rank 验证",
           "summary": dict(dist), "era_breakdown": {k: dict(v) for k, v in era_dist.items()},
           "papers": rows}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n✓ 已写: {args.out}（{len(rows)} 篇）")


if __name__ == "__main__":
    main()
