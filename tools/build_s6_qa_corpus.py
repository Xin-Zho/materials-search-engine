#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/build_s6_qa_corpus.py — S6 pilot QA 语料构建（2026-09-05 用户定：方案 3→1 校准先行）。

从 s6_pilot_query_records（17 actions）构造 S6 新增候选（new_vs_S5）的 canonical 语料：
  canonical new = ∪ rows.key − S5_SEEN（1365，与 pilot aggregate 同口径）
  QA 文本     = title + abstract（abstract 从 scopus_cache.db papers 表按 doi/eid 回填）
  blind 纪律  = QA/校准集每篇带 source 元数据仅供分层抽样与事后归因；
                盲评 prompt（run_s6_qa.py）只注入 title+abstract+rubric，绝不注入 source。

分层校准集（--calibrate，seed=7 与 R02 惯例一致）：
  A 普通(A1-A7)  ~25 | A8 printability ~15 | D(D16/D17) ~15 | B+C ~10 | 边界 ~5
  边界样本 = title 无 shrink 词面但含 consequence 词（deform/warp/distortion/deflection/
    accuracy/gap/leakage/crack…）——校准核心：检验 rubric 不误杀"无 shrink 字面但研究后果"。
  重叠篇归属优先级：A8 > D > B/C > A_other（避免一篇进两层）。

用法：
  python tools/build_s6_qa_corpus.py                     # corpus（canonical new 全量）
  python tools/build_s6_qa_corpus.py --calibrate         # + 分层校准集
输出：
  data/exports/terminology/s6_qa_corpus.json
  data/exports/terminology/s6_qa_calibration_set.json    # --calibrate
"""
import argparse
import json
import os
import random
import re
import sqlite3
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from search_engine.topic_config import DEFAULT_TOPIC, resolve_input, resolve_output  # noqa: E402
from search_engine.identity import scopus_cache_key  # noqa: E402

T = os.path.join(BASE, "data", "exports", "terminology")
# P0-2: 以下为 v1.0 legacy 冻结原址；非 legacy topic 自动路由 topics/<id>/runs/ 通用名
RECORDS = os.path.join(T, "s6_pilot_query_records.json")
S5_SEEN = os.path.join(T, "s5_seen_set.json")
DEFAULT_CORPUS = os.path.join(T, "s6_qa_corpus.json")
DEFAULT_CALIB = os.path.join(T, "s6_qa_calibration_set.json")
CACHE_DB = os.path.join(BASE, "data", "cache", "scopus_cache.db")

CALIB_SEED = 7  # R02 审计惯例 seed=7，可复现

# A8 / D 等分层用 query 白名单
A8_QUERIES = {"S6-A-08"}
D_QUERIES = {"S6-D-16", "S6-D-17"}
A_OTHER_QUERIES = {"S6-A-01", "S6-A-02", "S6-A-03", "S6-A-04",
                   "S6-A-05", "S6-A-06", "S6-A-07"}
BC_QUERIES = {"S6-B-09", "S6-B-10", "S6-C-11", "S6-C-12", "S6-C-13",
              "S6-C-14", "S6-C-15"}

# 边界补充：title 无 shrink 词面、但含 consequence 词（S6 校准核心防误杀检查）
# ⚠️ P0-2 决策资产：A8/D/BC/A_OTHER 白名单 + 词表是 pc001 主题的 S6 校准分层方案
# （query id 来自 pc001 冻结 s6_bridge_queries）。换主题须按其 rubric/query 结构重写，
# 属主题决策而非算法——算法（分层配额/抽样/互斥归属）保持主题无关。
CONSEQUENCE_WORDS = [
    "deform", "warp", "distortion", "distort", "deflection", "curl",
    "dimensional accura", "dimensional error", "marginal gap", "internal gap",
    "leakage", "crack", "void", "fracture", "delamination", "crazing",
    "shrinkage-free" if False else "stress concentrat", "debond", "gapping",
    "contour", "surface profile", "trueness", "fit accuracy",
]
SHRINK_WORDS = ["shrink", "contraction", "volumetric change"]


def has_any_word(text: str, words) -> bool:
    t = (text or "").lower()
    return any(re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", t)
               for w in words)


def load_paper_abstract(con, doi: str, eid: str) -> str:
    """按 paper_id 主键回填 abstract（3 级 fallback）。"""
    try:
        if doi:
            pid = scopus_cache_key(doi)
            row = con.execute("SELECT normalized_json FROM papers WHERE paper_id=?",
                              (pid,)).fetchone()
            if row:
                return json.loads(row[0]).get("abstract") or ""
        if eid:
            pid2 = scopus_cache_key(eid)
            row = con.execute("SELECT normalized_json FROM papers WHERE paper_id=?",
                              (pid2,)).fetchone()
            if row:
                return json.loads(row[0]).get("abstract") or ""
            row = con.execute(
                "SELECT normalized_json FROM papers WHERE paper_id LIKE ? LIMIT 1",
                ("%" + eid,)).fetchone()
            if row:
                return json.loads(row[0]).get("abstract") or ""
    except Exception:
        pass
    return ""


def main():
    ap = argparse.ArgumentParser(description="S6 pilot QA corpus builder")
    ap.add_argument("--topic", default=None,
                    help="topic_id（默认 v1.0 legacy 主题；输入/输出自动路由 topics/<id>/runs/）")
    ap.add_argument("--records", default=None)
    ap.add_argument("--s5-seen", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--calibrate", action="store_true",
                    help="额外输出分层校准集（--calib-out）")
    ap.add_argument("--calib-out", default=None)
    ap.add_argument("--calib-n", type=int, default=0,
                    help="校准集目标规模（默认按分层配额 ~70；可显式覆盖）")
    args = ap.parse_args()

    # ── P0-2: 主题命名空间（输入 = 该主题 pilot 产物；输出随 topic 路由）──
    if not args.topic:
        args.topic = DEFAULT_TOPIC
    if not args.records:
        args.records = resolve_input(args.topic, RECORDS, "pilot_query_records.json")
    if not args.s5_seen:
        args.s5_seen = resolve_input(args.topic, S5_SEEN, "base_seen_set.json")
    if not args.out:
        args.out = resolve_output(args.topic, DEFAULT_CORPUS, "qa_corpus.json")
    if not args.calib_out:
        args.calib_out = resolve_output(args.topic, DEFAULT_CALIB,
                                        "qa_calibration_set.json")

    rec = json.load(open(args.records, encoding="utf-8"))
    rbq = rec["records_by_query"]
    s5_keys = set(json.load(open(args.s5_seen, encoding="utf-8"))["keys"])
    print(f"S5 seen = {len(s5_keys)} | queries = {len(rbq)}")

    # ── canonical new 集合（含 source 元数据）──
    papers = {}  # key -> {key, doi, eid, title, families:set, queries:set, abstract:''}
    for aid, w in rbq.items():
        fam = w["family"]
        for r in w.get("rows", []):
            k = r.get("key")
            if not k or k in s5_keys:
                continue
            p = papers.setdefault(k, {
                "key": k, "doi": r.get("doi") or "", "eid": r.get("eid") or "",
                "title": r.get("title") or "", "families": set(),
                "queries": set(), "abstract": ""})
            p["families"].add(fam)
            p["queries"].add(aid)
    print(f"canonical new candidates = {len(papers)}")

    # ── abstract 回填（只读缓存库）──
    con = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    n_abs = 0
    for p in papers.values():
        p["abstract"] = load_paper_abstract(con, p["doi"], p["eid"])
        if p["abstract"]:
            n_abs += 1
    con.close()
    # set -> list（可 JSON）
    for p in papers.values():
        p["families"] = sorted(p["families"])
        p["queries"] = sorted(p["queries"])
    print(f"abstract 可得率 = {n_abs}/{len(papers)} "
          f"({n_abs / len(papers):.1%})")

    corpus = {
        "version": "s6_qa_corpus_v1",
        "round": "S6_PILOT",
        "status": "DEVELOPMENT",
        "base_seen": "S5_SEEN(20417)",
        "canonical_new_total": len(papers),
        "abstract_present": n_abs,
        "abstract_rate": round(n_abs / len(papers), 4),
        "papers": list(papers.values()),
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(corpus, f, ensure_ascii=False, indent=1)
    print(f"[OK] corpus: {args.out}")

    # ── 分层校准集 ──
    if args.calibrate:
        # 互斥分层归属（优先级 A8 > D > B/C > A_other；边界从 A_other 池补）
        by_layer = {"A8_printability": [], "D": [], "BC": [], "A_other": [],
                    "boundary": []}
        for p in papers.values():
            qs = set(p["queries"])
            if qs & A8_QUERIES:
                by_layer["A8_printability"].append(p)
            elif qs & D_QUERIES:
                by_layer["D"].append(p)
            elif qs & BC_QUERIES:
                by_layer["BC"].append(p)
            elif qs & A_OTHER_QUERIES:
                by_layer["A_other"].append(p)
            # 边界候选：A_other 池中 title 无 shrink 词但含 consequence 词
        boundary_pool = [p for p in by_layer["A_other"]
                         if not has_any_word(p["title"], SHRINK_WORDS)
                         and has_any_word(p["title"], CONSEQUENCE_WORDS)]
        by_layer["boundary_pool_n"] = len(boundary_pool)

        rng = random.Random(CALIB_SEED)
        quotas = {"A8_printability": 15, "D": 15, "BC": 10, "A_other": 25}
        chosen = []
        strata_meta = {}
        for layer, n in quotas.items():
            pool = by_layer[layer]
            k = min(n, len(pool))
            picked = rng.sample(pool, k)
            strata_meta[layer] = {"pool": len(pool), "picked": k}
            for p in picked:
                p["_stratum"] = layer
                chosen.append(p)
        # 边界补充（不占上述配额，从 boundary_pool 再抽，量小）
        b_k = min(5, len(boundary_pool))
        b_picked = rng.sample(boundary_pool, b_k)
        strata_meta["boundary"] = {"pool": len(boundary_pool), "picked": b_k}
        for p in b_picked:
            p["_stratum"] = "boundary"
            chosen.append(p)

        rows = []
        for p in chosen:
            rows.append({
                "key": p["key"], "doi": p.get("doi"), "eid": p.get("eid"),
                "title": p["title"], "abstract": p["abstract"],
                "source_families": p["families"], "source_queries": p["queries"],
                "stratum": p["_stratum"]})
            p.pop("_stratum", None)

        calib = {
            "version": "s6_qa_calibration_set_v1",
            "seed": CALIB_SEED,
            "blind_note": "盲评 prompt 只给 title+abstract+rubric；source_families/"
                          "source_queries/stratum 仅事后归因用，绝不注入盲评",
            "gold_note": "gold 标注请另存 {key: RELEVANT|UNCERTAIN|IRRELEVANT} "
                         "（如 s6_qa_calibration_gold.json）；不回写本文件",
            "strata_meta": strata_meta,
            "n": len(rows),
            "rows": rows,
        }
        with open(args.calib_out, "w", encoding="utf-8") as f:
            json.dump(calib, f, ensure_ascii=False, indent=1)
        print(f"[OK] calibration set: {args.calib_out} (n={len(rows)})")
        for layer, m in strata_meta.items():
            print(f"  {layer:<18} pool={m['pool']:>4} picked={m['picked']}")


if __name__ == "__main__":
    main()
