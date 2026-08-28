"""tools/screen_qgs.py — B4 人工 relevance screening 工作台 v2（用户定 2026-08-27）。

从 QGS Candidate Pool（qgs_candidates_v1.json，~739 条）生成可编辑 manifest，
人工逐篇决策 RELEVANT / IRRELEVANT / UNRESOLVED + reason code。

关键设计（用户拍板）：
- auto_signal 只在程序内部决定审查顺序（HIGH→MED→LOW），**manifest 与界面均不
  显示**——防 anchoring bias；738/739 篇全部必须审，LOW 不自动跳过。
- reason 用固定 code（下拉选择），note 可选。
- --interactive 逐篇显示（title/year/journal/abstract/DOI/source），每判立即
  保存，随时退出；第二 Reviewer 可独立跑一份 manifest。

用法：
  python tools/screen_qgs.py --generate --api-key KEY   # 补 abstract + 生成 manifest
  python tools/screen_qgs.py --interactive             # 逐篇筛选（可随时退出）
  python tools/screen_qgs.py --apply                   # 回读 → benchmark v1
"""
import argparse
import asyncio
import json
import os
import re
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

POOL_PATH = os.path.join(BASE, "data", "exports", "qgs_candidates_v1.json")
MANIFEST_PATH = os.path.join(BASE, "data", "exports", "qgs_screening_manifest.json")
BENCHMARK_PATH = os.path.join(BASE, "data", "exports", "pc_001_external_qgs_v1.json")

REASON_CODES = {
    "RELEVANT": ["DIRECT_SHRINKAGE_MEASUREMENT", "DIRECT_STRESS_MEASUREMENT",
                 "SHRINKAGE_MECHANISM", "SHRINKAGE_REDUCTION_STRATEGY",
                 "DIRECT_CAUSAL_FACTOR"],
    "IRRELEVANT": ["NO_SHRINKAGE_FOCUS", "BACKGROUND_MENTION_ONLY",
                   "UNRELATED_PHOTOCURING", "MECHANICAL_ONLY", "PROCESS_ONLY",
                   "OUT_OF_SCOPE_SYSTEM"],
    "UNRESOLVED": ["INSUFFICIENT_METADATA", "FULLTEXT_REQUIRED", "IDENTITY_UNCERTAIN"],
}

# 辅助排序词（仅内部用，不写入 manifest / 不显示）
_PROBLEM = ["shrinkage", "contraction", "shrink", "stress", "strain", "volumetric"]
_DOMAIN = ["photo", "polymer", "resin", "composite", "dental", "filler",
           "curing", "cure", "monomer", "adhesive", "restorative", "oligomer",
           "initiator", "polymerization", "photocure", "photopolymer"]


def _signal(title: str) -> int:
    t = (title or "").lower()
    p = sum(1 for w in _PROBLEM if w in t)
    d = sum(1 for w in _DOMAIN if w in t)
    if p >= 1 and d >= 1:
        return 0  # HIGH
    if p >= 1 or d >= 2:
        return 1  # MED
    return 2      # LOW


def norm_wid(pid: str) -> str:
    m = re.search(r"(W\d+)", pid or "")
    return m.group(1) if m else ""


async def fetch_abstracts(cands: list[dict], api_key: str) -> dict:
    """OpenAlex 批量补 abstract（独立临时缓存防污染）。"""
    from search_engine.backends.openalex import OpenAlexBackend
    cache_path = os.path.join(tempfile.gettempdir(), "qgs_openalex_cache.json")
    wid_to_idx = {norm_wid(c.get("openalex_id", "")): i for i, c in enumerate(cands)
                  if norm_wid(c.get("openalex_id", ""))}
    abstracts: dict[int, str] = {}
    async with OpenAlexBackend(api_key=api_key, cache_path=cache_path) as oa:
        wids = list(wid_to_idx)
        for i in range(0, len(wids), 50):
            chunk = wids[i:i + 50]
            data = await oa._get_json(
                f"{oa.BASE_URL}/works",
                {"filter": "openalex_id:" + "|".join(chunk), "per-page": min(len(chunk), 200)})
            for w in data.get("results", []):
                wid = norm_wid(w.get("id", ""))
                ab = w.get("abstract_inverted_index")
                if ab and wid in wid_to_idx:
                    pos = []
                    for word, idxs in ab.items():
                        for ix in idxs:
                            pos.append((ix, word))
                    pos.sort()
                    abstracts[wid_to_idx[wid]] = " ".join(word for _, word in pos)
        cs = oa.credit_summary()
        print(f"abstract 补全: {len(abstracts)}/{len(cands)}（credits {cs['total']}）")
    return abstracts


def build_manifest(cands: list[dict], abstracts: dict) -> list[dict]:
    """内部按信号排序（HIGH→MED→LOW），manifest 不含信号字段。"""
    ordered = sorted(range(len(cands)), key=lambda i: (_signal(cands[i].get("title", "")), i))
    manifest = []
    for rank, i in enumerate(ordered):
        c = cands[i]
        manifest.append({
            "idx": i,
            "rank": rank,  # 审查顺序（内部排序结果，无信号标签）
            "title": c.get("title", ""),
            "year": c.get("year"),
            "venue": c.get("venue", ""),
            "doi": c.get("doi", ""),
            "sources_from": c.get("sources_from", []),
            "identity_status": c.get("identity_status", "RESOLVED"),
            "citation_original": c.get("citation_original", ""),
            "abstract": abstracts.get(i, "")[:1500],
            "decision": "",
            "reason_code": "",
            "note": "",
        })
    return manifest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--generate", action="store_true",
                    help="补 abstract + 生成 screening manifest")
    ap.add_argument("--interactive", action="store_true", help="逐篇筛选")
    ap.add_argument("--apply", action="store_true", help="回读 manifest 生成 benchmark")
    ap.add_argument("--api-key", default=os.environ.get("OPENALEX_API_KEY", ""))
    ap.add_argument("--continue-from", type=int, default=0,
                    help="interactive 从第几个未决策条目继续")
    args = ap.parse_args()

    if args.generate:
        pool = json.load(open(POOL_PATH, encoding="utf-8"))
        cands = pool["candidates"]
        abstracts = asyncio.run(fetch_abstracts(cands, args.api_key)) if args.api_key else {}
        if not args.api_key:
            print("⚠️ 未提供 --api-key，abstract 留空（后续可重跑补全）")
        papers = build_manifest(cands, abstracts)
        manifest = {
            "benchmark_id": "pc_001_external_qgs_v1",
            "criteria_ref": "benchmarks/pc_001_external_benchmark_spec.md 第10节 (B3)",
            "instructions": "decision: RELEVANT/IRRELEVANT/UNRESOLVED；reason_code 用"
                            "列表 code；note 可选。rank 是审查顺序（程序内部排序）。",
            "reason_codes": REASON_CODES,
            "papers": papers,
        }
        with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=1)
        print(f"✓ manifest 已生成: {MANIFEST_PATH}（{len(papers)} 条）")
        print("  提示：python tools/screen_qgs.py --interactive 开始筛选")

    elif args.interactive:
        manifest = json.load(open(MANIFEST_PATH, encoding="utf-8"))
        papers = manifest["papers"]
        codes = manifest["reason_codes"]
        start = max(args.continue_from, 0)
        i = start
        n = len(papers)
        while i < n:
            p = papers[i]
            if p["decision"]:
                i += 1
                continue
            decided = sum(1 for x in papers if x["decision"])
            print("\n" + "=" * 74)
            print(f"[{i + 1} / {n}]  已决策 {decided} 篇")
            print("=" * 74)
            print(f"Title:   {p['title']}")
            print(f"Year:    {p['year']}   Venue: {p['venue']}")
            print(f"DOI:     {p['doi'] or '-'}")
            print(f"Sources: {','.join(p['sources_from'])}"
                  + (f"   [identity: {p['identity_status']}]" if p.get("identity_status") != "RESOLVED" else ""))
            if p.get("citation_original"):
                print(f"原始引用: {p['citation_original']}")
            if p.get("abstract"):
                print(f"Abstract: {p['abstract'][:600]}{'...' if len(p['abstract']) > 600 else ''}")
            print()
            key = input("  [r] RELEVANT  [i] IRRELEVANT  [u] UNRESOLVED  [q] 保存退出  [b] 回退: ").strip().lower()
            if key == "q":
                print("已保存退出")
                break
            if key == "b":
                i = max(0, i - 2)
                continue
            decision = {"r": "RELEVANT", "i": "IRRELEVANT", "u": "UNRESOLVED"}.get(key)
            if not decision:
                print("  无效输入")
                continue
            opts = codes[decision]
            print(f"  reason code ({decision}):")
            for j, c in enumerate(opts, 1):
                print(f"    {j}. {c}")
            rk = input("  选编号（回车=跳过）: ").strip()
            p["decision"] = decision
            p["reason_code"] = opts[int(rk) - 1] if rk.isdigit() and 1 <= int(rk) <= len(opts) else ""
            note = input("  note（可选，回车跳过）: ").strip()
            p["note"] = note
            with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=1)
            i += 1
        print(f"✓ 已保存进度（{sum(1 for x in papers if x['decision'])}/{n} 决策）: {MANIFEST_PATH}")

    elif args.apply:
        manifest = json.load(open(MANIFEST_PATH, encoding="utf-8"))
        papers = manifest["papers"]
        decided = [p for p in papers if p["decision"]]
        if not decided:
            print("⚠️ manifest 还没有任何 decision——先跑 --interactive")
            return
        from collections import Counter
        cnt = Counter(p["decision"] for p in papers)
        relevant = [p for p in papers if p["decision"] == "RELEVANT"]
        out = {
            "benchmark_id": "pc_001_external_qgs_v1",
            "version": 1,
            "created_at": "2026-08-27",
            "criteria_ref": manifest["criteria_ref"],
            "stats": {"total": len(papers), "decided": len(decided),
                      **{k: v for k, v in cnt.items()}},
            "papers": [{"idx": p["idx"], "title": p["title"], "year": p["year"],
                        "venue": p["venue"], "doi": p["doi"],
                        "sources_from": p["sources_from"],
                        "reason_code": p["reason_code"], "note": p["note"]}
                       for p in relevant],
        }
        with open(BENCHMARK_PATH, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print(f"✓ benchmark 已生成: {BENCHMARK_PATH}")
        print(f"  统计: {dict(cnt)}")
        print(f"  B_total(relevant) = {len(relevant)} → 下一步 B5 Scopus eligibility")


if __name__ == "__main__":
    main()
