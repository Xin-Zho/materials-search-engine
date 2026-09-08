"""S8 全量抽取 runner（2.0-edges 离线，Scopus abstract 直接喂）。

目标：catalog 中 label=RELEVANT ∧ abstract 的论文（152 篇）→ KnowledgeExtractor
      （search_engine.knowledge_extractor，光固化本体定制 EXTRACT_PROMPT）。

写入策略（安全默认）：
  - 默认只写 data/exports/terminology/s8_extraction_output.json（评审用）
  - --commit 追加写 knowledge_base.db：
      paper_id = 'scopus:'+EID（如 scopus:2-s2.0-xxx，与 openalex: 体系一致、无 doi 也唯一）
      canonical_paper_id = 'doi:10.xxx'（有 doi 时）
  - 断点续跑：已完成的 key 跳过（读 output 文件）

用法：
  python tools/run_s8_extraction.py                # 全量 dry-run → JSON
  python tools/run_s8_extraction.py --batch 20     # 每批暂停？否——批次仅用于打印
  python tools/run_s8_extraction.py --commit       # dry-run 后补写 DB
  python tools/run_s8_extraction.py --keys k1,k2   # 指定重跑
"""
import argparse
import asyncio
import json
import os
import sys
import time

from search_engine.llm import DeepSeekBackend
from search_engine.models import Paper
from search_engine.knowledge_extractor import KnowledgeExtractor

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

CATALOG = "data/exports/terminology/s8_finalkb_catalog.json"
OUT = "data/exports/terminology/s8_extraction_output.json"


def load_done():
    """已完成 keys（断点续跑）。"""
    if not os.path.exists(OUT):
        return set()
    try:
        d = json.load(open(OUT, encoding="utf-8"))
        return {p["key"] for p in d.get("papers", [])
                if p.get("status") == "ok"}
    except Exception:
        return set()


def save_results(entries, status_counts):
    out = {
        "role": "S8 2.0-edges 抽取产物（dry-run 评审；--commit 写 knowledge_base.db）",
        "extractor_version": "2.0-edges",
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "status_counts": status_counts,
        "papers": entries,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)


async def extract_one(ex, p, entries, status_counts):
    t0 = time.time()
    paper = Paper(paper_id=p["key"], title=p["title"],
                  year=None, abstract=p["abstract"], doi=p["doi"])
    try:
        rec = await ex.extract(paper)
    except Exception as e:
        rec = None
        err = f"{type(e).__name__}: {e}"
    dt = time.time() - t0
    if rec is None:
        entries.append({"key": p["key"], "status": "fail",
                        "duration_s": round(dt, 1),
                        "error": err if "err" in dir() else "extract None"})
        status_counts["fail"] += 1
        return False
    entries.append({
        "key": p["key"], "status": "ok", "duration_s": round(dt, 1),
        "paper_id_db": "scopus:" + p["key"],
        "canonical_paper_id": ("doi:" + p["doi"]) if p["doi"] else "",
        "problem": rec.problem,
        "strategy_routes": rec.strategy_routes,
        "materials": rec.materials,
        "mechanisms": [{"cause": m.cause, "mechanism": m.mechanism,
                        "effect": m.effect, "canonical": m.canonical,
                        "evidence": m.evidence, "confidence": m.confidence}
                       for m in rec.physical_mechanisms],
        "edges": [{"route": e.raw_route, "canonical_route": e.canonical_route,
                   "mechanism": e.raw_mechanism,
                   "canonical_mechanism": e.canonical_mechanism,
                   "evidence": e.evidence, "confidence": e.confidence,
                   "relation_type": e.relation_type}
                  for e in rec.route_mechanism_edges],
        "hypotheses": [{"hypothesis": h.hypothesis, "rationale": h.rationale,
                        "queries": h.queries}
                       for h in rec.search_hypotheses],
        "characterization_methods": rec.characterization_methods,
        "concepts": rec.concepts,
    })
    status_counts["ok"] += 1
    return True


def store_entry(kb, key, doi, entry):
    """entry(ok 记录) → KnowledgeRecord → knowledge_base.db。"""
    from search_engine.models import (KnowledgeRecord, Mechanism,
                                      SearchHypothesis,
                                      RouteMechanismEvidenceEdge)
    rec = KnowledgeRecord(
        paper_id="scopus:" + key,
        canonical_paper_id=("doi:" + doi) if doi else "",
        doi=doi or "", openalex_id="",
        problem=entry.get("problem") or "",
        strategy_routes=entry.get("strategy_routes") or [],
        materials=entry.get("materials") or [],
        physical_mechanisms=[Mechanism(**{k: m.get(k, "") for k in
                                         ("cause", "mechanism", "effect",
                                          "canonical", "evidence",
                                          "confidence")})
                             for m in entry.get("mechanisms") or []],
        route_mechanism_edges=[
            RouteMechanismEvidenceEdge(
                paper_id="scopus:" + key,
                raw_route=e.get("route") or "",
                canonical_route=e.get("canonical_route") or "",
                raw_mechanism=e.get("mechanism") or "",
                canonical_mechanism=e.get("canonical_mechanism") or "",
                evidence=e.get("evidence") or "",
                confidence=e.get("confidence") or 0.0,
                relation_type=e.get("relation_type") or "direct")
            for e in entry.get("edges") or []],
        search_hypotheses=[SearchHypothesis(**h)
                           for h in entry.get("hypotheses") or []],
        characterization_methods=entry.get("characterization_methods") or [],
        concepts=entry.get("concepts") or [],
        extractor_version="2.0-edges",
        extraction_status="ok",
    )
    kb.store(rec)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true",
                    help="抽取成功后写 knowledge_base.db（默认只写 JSON）")
    ap.add_argument("--keys", default="",
                    help="只抽取指定 keys（逗号分隔，重跑用）")
    ap.add_argument("--include-uncertain", action="store_true",
                    help="同时抽取 UNCERTAIN label（默认只抽 RELEVANT）")
    args = ap.parse_args()

    catalog = json.load(open(CATALOG, encoding="utf-8"))
    want = [p for p in catalog["papers"]
            if p["label"] == "RELEVANT" and p["abstract"]]
    if args.include_uncertain:
        want += [p for p in catalog["papers"]
                 if p["label"] == "UNCERTAIN" and p["abstract"]]
    if args.keys:
        ks = set(k.strip() for k in args.keys.split(",") if k.strip())
        want = [p for p in want if p["key"] in ks]
    print(f"目标: {len(want)} 篇（R + abstract）")

    done = load_done()
    todo = [p for p in want if p["key"] not in done]
    print(f"已完成 {len(done)} / 待抽 {len(todo)}")

    # 需抽取时才要 LLM key（commit replay 已有产物不需要）
    if todo:
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not key:
            print("未设 DEEPSEEK_API_KEY（有待抽论文）", file=sys.stderr)
            return 1

    # 保留已有 entries（断点续跑）：output 是累积产物。
    # 只删除本次待抽（todo，含 --keys 重跑）key 的旧记录，其余 ok 原样保留。
    entries = []
    if os.path.exists(OUT):
        try:
            entries = json.load(open(OUT, encoding="utf-8")).get("papers", [])
            todo_keys = {p["key"] for p in todo}
            entries = [e for e in entries if e["key"] not in todo_keys]
        except Exception:
            entries = []
    status_counts = {"ok": sum(1 for e in entries if e["status"] == "ok"),
                     "fail": sum(1 for e in entries if e["status"] == "fail")}

    kb = None
    if args.commit:
        from search_engine.knowledge_base import KnowledgeBase
        kb = KnowledgeBase()
        # replay 已有 ok 产物（dry-run 后 commit 不重抽 LLM；todo 空也可纯 replay）
        ok_by_key = {e["key"]: e for e in entries if e["status"] == "ok"}
        replay = [p for p in want if p["key"] in ok_by_key]
        for p in replay:
            store_entry(kb, p["key"], p["doi"], ok_by_key[p["key"]])
        print(f"commit: replay {len(replay)} 篇已有产物直接入库"
              f"（{len(todo)} 篇待抽取）")

    if not todo:
        print("无待抽论文。")
        if kb is not None:
            kb.close()
        save_results(entries, status_counts)
        return 0

    llm = DeepSeekBackend(api_key=key)
    ex = KnowledgeExtractor(llm, extractor_version="2.0-edges")

    t_all = time.time()
    try:
        for i, p in enumerate(todo, 1):
            rec_ok = await extract_one(ex, p, entries, status_counts)
            # commit 模式重建 KnowledgeRecord 入库
            if rec_ok and args.commit and kb is not None:
                store_entry(kb, p["key"], p["doi"], entries[-1])
            if i % 10 == 0 or i == len(todo):
                save_results(entries, status_counts)
                el = time.time() - t_all
                print(f"  [{i}/{len(todo)}] ok={status_counts['ok']} "
                      f"fail={status_counts['fail']} ({el:.0f}s)")
    finally:
        if kb is not None:
            kb.close()
        save_results(entries, status_counts)

    el = time.time() - t_all
    print(f"\n完成: ok={status_counts['ok']} fail={status_counts['fail']} "
          f"({el:.0f}s ≈ {el/60:.1f}min)")
    print(f"产物: {OUT}")
    if not args.commit:
        print("dry-run 完成——评审后 --commit 写 knowledge_base.db")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
