"""S8 全量抽取 runner（2.0-edges 离线，Scopus abstract 直接喂）。

目标：catalog 中 label=RELEVANT ∧ abstract 的论文（152 篇）→ KnowledgeExtractor
      （search_engine.knowledge_extractor，光固化本体定制 EXTRACT_PROMPT）。

写入策略（安全默认）：
  - 默认只写 data/exports/terminology/s8_extraction_output.json（评审用）
  - --commit 追加写 knowledge_base.db：
      paper_id          = 源记录键（按**值形态**决定命名空间：EID -> scopus:、W -> openalex:）
      canonical_paper_id= 该论文的最优身份（make_paper_uid，DOI > W > EID；
                          无任何有效标识时落 local:<hash>，**绝不**借外部命名空间）
  - 断点续跑：已完成的 key 跳过（读 output 文件）

P0-B1b：本脚本原先是「近失事件」的现场 —— 它写 ``canonical_paper_id = "doi:" + p["doi"]``，
而 ``p["doi"]`` 来自 S8 catalog，那里 EID 曾被兜底塞进 ``doi`` 字段（那 30 条的源头）。
30 条里 4 条 label=RELEVANT，**仅因摘要为空**才没走到这里；否则
``knowledge_records`` 会出现 ``doi:2-s2.0-*``。现在身份一律经唯一出口按值形态识别。

用法（--live 门禁：真实 LLM 抽取须显式确认，默认连 dry-run 也禁止）：
  python tools/run_s8_extraction.py                     # 纯离线：replay/统计，待抽被 gate 拦
  python tools/run_s8_extraction.py --live              # 允许真实 LLM 全量 dry-run → JSON
  python tools/run_s8_extraction.py --live --commit     # 抽取 + 写 DB（replay 已有产物无需 --live）
  python tools/run_s8_extraction.py --live --keys k1,k2 # 指定重跑
"""
import argparse
import asyncio
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from search_engine.topic_config import (  # noqa: E402
    DEFAULT_TOPIC, resolve_input, resolve_output,
)
from search_engine.llm import DeepSeekBackend
from search_engine.models import Paper
from search_engine.knowledge_extractor import KnowledgeExtractor
from search_engine.identity import (  # noqa: E402
    LOCAL_UID_PREFIX, IdentifierClaim, make_paper_uid, make_record_id,
    normalize_identifier, uid_prefix,
)

SOURCE = "tools/run_s8_extraction"

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# P0-2: v1.0 legacy 冻结原址；非 legacy topic 自动路由 topics/<id>/runs/（main 内 global 覆盖）
CATALOG = os.path.join(BASE, "data/exports/terminology/s8_finalkb_catalog.json")
OUT = os.path.join(BASE, "data/exports/terminology/s8_extraction_output.json")


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
    _pid_db, _canonical = paper_identity(p["key"], p.get("doi"))
    entries.append({
        "key": p["key"], "status": "ok", "duration_s": round(dt, 1),
        "paper_id_db": _pid_db,
        "canonical_paper_id": _canonical,
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


def _value_claims(*raws):
    """按**值形态**识别标识（不信任字段名）：返回 IdentifierClaim 列表。

    P0-B1b：本脚本原先直接信任 ``p["doi"]``；而 S8 catalog 的 ``doi`` 字段曾被 EID
    污染（那 30 条的源头）。现在逐个试 DOI / OPENALEX / SCOPUS_EID 的**形态校验** ——
    EID 再也拿不到 ``doi:`` 前缀。
    """
    claims, seen = [], set()
    for raw in raws:
        s = str(raw or "").strip()
        if not s:
            continue
        for t in ("DOI", "OPENALEX", "SCOPUS_EID"):
            n = normalize_identifier(t, s)
            if n and (t, n) not in seen:
                seen.add((t, n))
                claims.append(IdentifierClaim(t, n, s, None, SOURCE))
                break
    return claims


def paper_identity(key, doi):
    """-> ``(knowledge_records.paper_id, canonical_paper_id)``。

    ``paper_id``            源记录键：命名空间由 **key 的值形态**决定（保持既有语义，
                            W1 的 build_entities 仍可反解）
    ``canonical_paper_id``  论文最优身份：DOI > W > EID（``make_paper_uid`` 规则；
                            无任何有效标识时落 ``local:<hash>``，绝不借外部命名空间）
    """
    key_claims = _value_claims(key)
    pid = (make_record_id(uid_prefix(key_claims[0].id_type), key_claims[0].normalized_value)
           if key_claims else make_record_id(LOCAL_UID_PREFIX, str(key or "")[:64]))
    all_claims = _value_claims(doi, key)
    canonical = make_paper_uid(claims=all_claims) if all_claims else ""
    return pid, canonical


def store_entry(kb, key, doi, entry):
    """entry(ok 记录) → KnowledgeRecord → knowledge_base.db。"""
    from search_engine.models import (KnowledgeRecord, Mechanism,
                                      SearchHypothesis,
                                      RouteMechanismEvidenceEdge)
    _pid, _canonical = paper_identity(key, doi)
    rec = KnowledgeRecord(
        paper_id=_pid,
        canonical_paper_id=_canonical,
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
                paper_id=_pid,
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
    ap.add_argument("--topic", default=None,
                    help="topic_id（默认 v1.0 legacy 主题；catalog 输入与 output 随 topic 路由；"
                         "抽取结果始终写全局 knowledge_base.db——v2 主题共享语义）")
    ap.add_argument("--commit", action="store_true",
                    help="抽取成功后写 knowledge_base.db（默认只写 JSON）")
    ap.add_argument("--keys", default="",
                    help="只抽取指定 keys（逗号分隔，重跑用）")
    ap.add_argument("--include-uncertain", action="store_true",
                    help="同时抽取 UNCERTAIN label（默认只抽 RELEVANT）")
    ap.add_argument("--live", action="store_true",
                    help="允许真实 LLM 抽取（默认禁止，防误耗 API 额度）")
    args = ap.parse_args()

    # ── P0-2: 主题命名空间（catalog 输入 = 该主题 finalkb_catalog；output 随 topic 路由）──
    global CATALOG, OUT
    topic = args.topic or DEFAULT_TOPIC
    CATALOG = resolve_input(topic, CATALOG, "finalkb_catalog.json")
    OUT = resolve_output(topic, OUT, "extraction_output.json")
    print(f"[topic] {topic} | catalog={os.path.relpath(CATALOG, BASE)} | "
          f"out={os.path.relpath(OUT, BASE)}")

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
    key = ""

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

    # ── LLM 门禁：S8 抽取是真实 LLM 调用——默认(含 dry-run)禁止，--live 才放行 ──
    # 位置在 commit replay 之后：--commit 纯搬运已有 ok 产物无需 LLM，不受 gate 影响
    if not args.live:
        print(f"[GATE] 待抽 {len(todo)} 篇需真实 LLM——加 --live 确认"
              f"（默认禁止，防误耗 API 额度；replay 已有产物无需 LLM）",
              file=sys.stderr)
        if kb is not None:
            kb.close()
        return 1
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        print("未设 DEEPSEEK_API_KEY（有待抽论文）", file=sys.stderr)
        if kb is not None:
            kb.close()
        return 1

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
