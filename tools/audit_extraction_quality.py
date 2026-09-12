#!/usr/bin/env python
"""P4-1A 抽取质量审计 —— 用户指定的三项验收。

用户 2026-09-12 裁定：**先看知识图质量，不要先看预测**。三项：

  1. **Coverage**       多少论文成功抽取（目标 ~95%）
  2. **Concept diversity** 不是「1000 篇得到 10 个词」
  3. **Temporal sanity** 把 2020 年前的论文抽出「2024 才出现的术语」= 时相污染

第 3 项的实现方式
-----------------
从**全语料**（TRAIN + EVAL 的标题+摘要，即 `paper_meta`）建一个
**词首现年索引**：``token -> 最早出现的年份``。然后对每个抽取出的概念：

    first_attested(concept) = max( 各 token 的首现年 )

一个概念不可能早于它最晚出现的那个词。若 ``first_attested > cutoff(2020)``，
说明这条概念用到了预测窗口之后才出现的词汇 —— 标 ``ANACHRONISM``。

为什么这条检查重要：LLM 知道 2021-2025 发生了什么。它可能在给 2015 年的论文
抽概念时，用**后来才有的术语**去命名当时的想法（例如把旧的"模式识别"写成
"deep learning"）。那等于把未来词汇注入历史特征，是**软性时间泄漏**，
比引用快照泄漏更隐蔽 —— 引用快照至少是个数字，这个是语义层面的。

注意：本检查是**保守的**（max-of-tokens 会被常见词拉高），所以它给的是
**下界**：报出来的比例是「至少这么多概念可疑」，不是精确污染率。

用法::

    python tools/audit_extraction_quality.py
    python tools/audit_extraction_quality.py --coverage-threshold 0.95
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import re
import sqlite3
import statistics
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

AUDIT_VERSION = "p4_1a_audit_v1"
DATASET_DIR = BASE / "datasets" / "photopolymerization_v1"
DEFAULT_DB = DATASET_DIR / "paper_meta.db"
DEFAULT_CONCEPTS = DATASET_DIR / "concepts_v1.jsonl"
DEFAULT_SAMPLE = DATASET_DIR / "sample_v1.jsonl"
DEFAULT_OUT = DATASET_DIR / "extraction_audit.json"

CUTOFF_YEAR = 2020
ST_OK = "OK"
ST_INSUFFICIENT = "INSUFFICIENT_TEXT"

# token 只用**字母**，不把连字符算进去：
#   `thiol-ene` 必须切成 thiol + ene，否则整串永远是 OOV，
#   时相检查会在 `unknown -> continue` 处**静默跳过**它（实测削弱了整条检查）。
# 语料索引与概念侧用**同一个** TOKEN_RE，两边口径必须一致。
TOKEN_RE = re.compile(r"[a-z]{3,}")
# 前缀长度：概念名与原文词形不一致时用于回溯（见 build_term_first_year）
PREFIX_LENS = (5, 6)
STOPWORDS = frozenset("""
the and for with from that this these those using used use its their there where which
into onto over under between during based via due are was were has have had not but
can may may be been being more most less least other another such than then them
new novel high low higher lower multi non pre post self co
""".split())


def _rel(path) -> str:
    """展示用相对路径（跨盘符安全）。"""
    try:
        return os.path.relpath(str(path), str(BASE)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def build_term_first_year(db_path):
    """``token -> 最早年份``，外加**前缀首现年索引**。

    用**全部**论文（含 2021-2025），这样预测窗口之后才出现的词汇才会被看见 ——
    只建 TRAIN 索引的话永远发现不了时相污染。

    为什么需要前缀索引：概念名经过规范化（单数化等），与原文词形常不一致。实测
    ``balloons -> balloon``、``thixotropic -> thixotropy``、
    ``dithioesters -> dithioester``、``Macrocycles -> macrocycle``
    都会被**精确 token 比较**误判成时相异常 —— 首次全量运行报出的 23 条里多数如此。
    前缀索引取「共享前缀的语料词的最早年份」，修掉这个**度量**缺陷
    （它同时印证了模型其实是在照抄论文原文）。

    代价（必须记住）：前缀匹配会让**词族内部的更新换代**漏报
    （``transformer`` 会被 ``transfer``/``transform`` 提前"洗白"）。
    故本工具**同时报两个口径**：``exact``（严、假阳性多）与
    ``prefix``（宽、假阴性多），并要求人工复核剩余项 —— 数量足够小（几十条）。
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT year, title, abstract FROM paper_meta "
            "WHERE year IS NOT NULL AND exclusion_reason IS NULL").fetchall()
    finally:
        con.close()
    first, years = {}, collections.Counter()
    for r in rows:
        y = r["year"]
        years[y] += 1
        text = ((r["title"] or "") + " " + (r["abstract"] or "")).lower()
        for tok in set(TOKEN_RE.findall(text)):
            if tok in STOPWORDS:
                continue
            if tok not in first or y < first[tok]:
                first[tok] = y
    prefix_min = {}
    for tok, y in first.items():
        for L in PREFIX_LENS:
            if len(tok) >= L:
                p = tok[:L]
                if p not in prefix_min or y < prefix_min[p]:
                    prefix_min[p] = y
    return first, years, prefix_min


def concept_first_attested(name, first, prefix_min=None, mode="prefix"):
    """概念首现年。

    ``mode="exact"``  各 token 的**精确**首现年取最大值（严；受词形变体干扰）
    ``mode="prefix"`` 各 token 经**共享前缀**回溯到最早年份，再取最大值（宽）

    返回 ``(year, unknown_tokens)``。``year=None`` 表示有 token 在语料里完全没出现
    （OOV：可能来自语料外知识）。
    """
    toks = [t for t in TOKEN_RE.findall(name.lower()) if t not in STOPWORDS]
    if not toks:
        return None, []

    def year_of(t):
        if mode == "prefix" and prefix_min:
            cands = [prefix_min[t[:L]] for L in PREFIX_LENS
                     if len(t) >= L and t[:L] in prefix_min]
            if cands:
                return min(cands)
        return first.get(t)

    years, unknown = [], []
    for t in toks:
        y = year_of(t)
        if y is None:
            unknown.append(t)
        else:
            years.append(y)
    if unknown:
        return None, unknown
    return (max(years) if years else None), []


def concept_first_attested_legacy_removed():
    """占位：旧版（精确 token）实现已合并进 ``concept_first_attested(mode="exact")``。

    保留此函数只为说明——**不要**再加第二个同名实现：Python 后定义者会静默覆盖
    前者，本文件真的踩过一次（新旧 `concept_first_attested` 并存，
    新签名不生效导致 `unexpected keyword argument 'mode'`）。
    """


def audit(concepts_rows, sample_rows, first, years, *, coverage_threshold,
          prefix_min=None):
    total = len(concepts_rows)
    by_status = collections.Counter(r["status"] for r in concepts_rows)
    ok = [r for r in concepts_rows if r["status"] == ST_OK]
    insufficient = by_status.get(ST_INSUFFICIENT, 0)

    # ── 1. Coverage ────────────────────────────────────────────────────
    # 两个口径都要给：原始覆盖率会被「文本本身不足」的论文拉低（书籍/无摘要），
    # 那是输入问题不是 pipeline 问题。
    numerator = len(ok)
    denom_extractable = total - insufficient
    coverage = {
        "n_total": total,
        "n_ok": len(ok),
        "by_status": dict(by_status),
        "raw_coverage": round(numerator / total, 4) if total else 0.0,
        "extractable_coverage": (round(numerator / denom_extractable, 4)
                                 if denom_extractable else 0.0),
        "insufficient_text": insufficient,
        "threshold": coverage_threshold,
        "pass_raw": (numerator / total >= coverage_threshold) if total else False,
        "pass_extractable": (numerator / denom_extractable >= coverage_threshold)
                            if denom_extractable else False,
        "note": "text 与摘要均不足的论文（书籍/勘误/极老论文）计入 INSUFFICIENT_TEXT",
    }

    # ── 2. Concept diversity ───────────────────────────────────────────
    all_c = [c for r in ok for c in r["concepts"]]
    names = [c["name"] for c in all_c]
    nc = collections.Counter(names)
    n_papers = len(ok)
    per_paper = [r["n_concepts"] for r in ok]
    rels = [x for r in ok for x in r["relations"]]
    rel_types = collections.Counter(x["relation"] for x in rels)
    type_counts = collections.Counter(c["type"] for c in all_c)
    distinct = len(nc)
    # 单一概念占比过高 = 退化成关键词统计
    top_share = (nc.most_common(1)[0][1] / len(names)) if names else 0.0
    # 「词面门退化」信号：一句话概念（纯单词）比例
    one_word = sum(1 for n in names if len(n.split()) == 1)
    diversity = {
        "papers_extracted": n_papers,
        "concepts_total": len(all_c),
        "concepts_distinct": distinct,
        "distinct_ratio": round(distinct / len(all_c), 4) if all_c else 0.0,
        "concepts_per_paper": {
            "mean": round(statistics.mean(per_paper), 2) if per_paper else 0,
            "median": statistics.median(per_paper) if per_paper else 0,
            "min": min(per_paper) if per_paper else 0,
            "max": max(per_paper) if per_paper else 0,
        },
        "relations_total": len(rels),
        "relations_distinct": len({(x["source"], x["relation"], x["target"])
                                   for x in rels}),
        "relation_type_distribution": dict(rel_types),
        "relation_vocabulary_used": f"{len(rel_types)}/8",
        "concept_type_distribution": dict(type_counts),
        "one_word_concept_ratio": round(one_word / len(names), 4) if names else 0.0,
        "top_concept_share": round(top_share, 4),
        "top_concepts": [{"name": n, "n": k, "papers": round(k / n_papers, 3)}
                         for n, k in nc.most_common(15)],
        "verdict": (
            "OK" if (distinct >= 500 and top_share < 0.05 and n_papers
                     and statistics.mean(per_paper) >= 4)
            else "REVIEW"),
        "criteria": "distinct>=500 且 最大单概念占比<5% 且 篇均概念>=4",
    }

    # ── 3. Temporal sanity check ───────────────────────────────────────
    anachronisms, oov = [], collections.Counter()
    checked = 0
    for r in ok:
        for c in r["concepts"]:
            checked += 1
            y_ex, unknown = concept_first_attested(c["name"], first, mode="exact")
            if unknown:
                for t in unknown:
                    oov[t] += 1
                continue
            if y_ex is not None and y_ex > CUTOFF_YEAR:
                y_pf, _ = concept_first_attested(c["name"], first, prefix_min,
                                                 mode="prefix")
                anachronisms.append({
                    "paper_uid": r["paper_uid"], "paper_year": r["year"],
                    "concept": c["name"], "type": c["type"],
                    "first_attested_exact": y_ex,
                    "first_attested_prefix": y_pf,
                    "survives_prefix_check": bool(y_pf and y_pf > CUTOFF_YEAR)})
    # 所有论文都 <=2020，所以任何 first_attested > 2020 的概念都是可疑的
    temporal = {
        "cutoff_year": CUTOFF_YEAR,
        "papers_max_year": max((r["year"] or 0) for r in ok) if ok else None,
        "concepts_checked": checked,
        "anachronism_count": len(anachronisms),
        "anachronism_rate": round(len(anachronisms) / checked, 4) if checked else 0.0,
        "anachronism_count_exact": len(anachronisms),
        "anachronism_count_prefix_refined": sum(
            1 for x in anachronisms if x["survives_prefix_check"]),
        "anachronism_rate_prefix_refined": (
            round(sum(1 for x in anachronisms if x["survives_prefix_check"])
                  / checked, 4) if checked else 0.0),
        "oov_token_types": len(oov),
        "oov_token_top": oov.most_common(10),
        "anachronism_sample": anachronisms[:20],
        "method": ("全语料 token 首现年索引；概念首现年 = 各 token 首现年最大值。"
                   "报**两个口径**：exact（精确 token，严、假阳性多）与 "
                   "prefix（共享前缀回溯 5/6 字符，宽、假阴性多）。"),
        "false_positive_cause": (
            "exact 口径的主要假阳性来源是**词形/单复数差异**：概念名经规范化"
            "（balloons->balloon、thixotropic->thixotropy、dithioesters->dithioester、"
            "Macrocycles->macrocycle），而索引是精确 token。首轮全量运行 23 条中"
            "绝大多数由此产生，且逐条核对后 evidence 均为论文原文引用。"),
        "interpretation": (
            "非零 → 抽取时用到了预测窗口之后才出现的词汇（软性时间泄漏）。"
            "处理方式：①在 prompt 里显式加禁用词提示 ②把污染概念在 P4-2 特征里剔除。"
            "**不得**因为比例低就忽略：它污染的是历史特征本身。"),
    }

    # ── 抽样一致性 ─────────────────────────────────────────────────────
    sample_uids = {r["paper_uid"] for r in sample_rows}
    got_uids = {r["paper_uid"] for r in concepts_rows}
    consistency = {
        "sample_n": len(sample_uids),
        "extracted_n": len(got_uids),
        "missing_from_concepts": len(sample_uids - got_uids),
        "extra_in_concepts": len(got_uids - sample_uids),
        "complete": sample_uids == got_uids,
    }
    return {"coverage": coverage, "diversity": diversity, "temporal": temporal,
            "consistency": consistency,
            "corpus_years": {"min": min(years) if years else None,
                             "max": max(years) if years else None}}


def print_summary(a, coverage_threshold):
    print("═" * 72)
    print(f"  P4-1A 抽取质量审计  [{AUDIT_VERSION}]")
    print("═" * 72)
    c = a["coverage"]
    print(f"  1) Coverage")
    print(f"     抽 {c['n_total']} 篇 | OK {c['n_ok']} | 状态 {c['by_status']}")
    print(f"     原始覆盖率      {c['raw_coverage']:.1%}"
          f"  {'PASS' if c['pass_raw'] else 'BELOW 阈值 %.0f%%' % (coverage_threshold * 100)}")
    print(f"     可抽取覆盖率    {c['extractable_coverage']:.1%}"
          f"（扣除 {c['insufficient_text']} 篇文本不足）"
          f"  {'PASS' if c['pass_extractable'] else 'BELOW'}")
    d = a["diversity"]
    print(f"  2) Concept diversity  -> {d['verdict']}")
    print(f"     概念 {d['concepts_total']} 个 / distinct {d['concepts_distinct']}"
          f"（{d['distinct_ratio']:.1%}）| 篇均 {d['concepts_per_paper']['mean']}"
          f"（中位 {d['concepts_per_paper']['median']}）")
    print(f"     关系 {d['relations_total']} 条 / distinct {d['relations_distinct']}"
          f" | 关系词表用到 {d['relation_vocabulary_used']}")
    print(f"     单概念最大占比 {d['top_concept_share']:.2%} | "
          f"单词概念占比 {d['one_word_concept_ratio']:.1%}")
    print(f"     概念类型 {d['concept_type_distribution']}")
    print(f"     关系类型 {d['relation_type_distribution']}")
    print(f"     Top 概念: " + ", ".join(
        f"{e['name']}({e['n']})" for e in d["top_concepts"][:6]))
    t = a["temporal"]
    n_ref = t.get("anachronism_count_prefix_refined", t["anachronism_count"])
    print(f"  3) Temporal sanity check（cutoff {t['cutoff_year']}）"
          f"  -> {'PASS' if n_ref == 0 else 'REVIEW'}")
    print(f"     检查 {t['concepts_checked']} 个概念")
    print(f"     exact 口径      {t['anachronism_count']}"
          f"（{t['anachronism_rate']:.2%}）—— 严，受词形变体干扰")
    print(f"     prefix 精修口径 {n_ref}"
          f"（{t.get('anachronism_rate_prefix_refined', 0):.2%}）—— 宽")
    print(f"     语料内 max year {t['papers_max_year']} | OOV token 种类 "
          f"{t['oov_token_types']}")
    for s in t["anachronism_sample"][:6]:
        print(f"       ! [exact {s['first_attested_exact']} / prefix "
              f"{s['first_attested_prefix']}] {s['concept']}"
              f"  (paper {s['paper_year']})")
    print(f"     {t['method']}")
    print(f"     假阳性主因: {t.get('false_positive_cause', '')[:110]}")
    cs = a["consistency"]
    print(f"  一致性: sample {cs['sample_n']} vs extracted {cs['extracted_n']} | "
          f"缺 {cs['missing_from_concepts']} 多 {cs['extra_in_concepts']} | "
          f"complete={cs['complete']}")
    print("═" * 72)


def main(argv=None):
    ap = argparse.ArgumentParser(description="P4-1A 抽取质量审计")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--concepts", default=str(DEFAULT_CONCEPTS))
    ap.add_argument("--sample", default=str(DEFAULT_SAMPLE))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--coverage-threshold", type=float, default=0.95)
    args = ap.parse_args(argv)

    rows = [json.loads(l) for l in
            Path(args.concepts).read_text(encoding="utf-8").splitlines() if l.strip()]
    sample = [json.loads(l) for l in
              Path(args.sample).read_text(encoding="utf-8").splitlines() if l.strip()]
    print(f"[audit] concepts={_rel(args.concepts)} ({len(rows)}) | "
          f"sample={_rel(args.sample)} ({len(sample)})")
    first, years, prefix_min = build_term_first_year(args.db)
    print(f"[audit] 首现年索引 {len(first)} 个 token | 语料年份 "
          f"{min(years)}-{max(years)}")
    a = {"audit_version": AUDIT_VERSION,
         "audited_at": datetime.datetime.now().isoformat(timespec="seconds"),
         "sources": {"db": _rel(args.db), "concepts": _rel(args.concepts),
                     "sample": _rel(args.sample)},
         "term_index_size": len(first)}
    a.update(audit(rows, sample, first, years,
                   coverage_threshold=args.coverage_threshold,
                   prefix_min=prefix_min))
    print_summary(a, args.coverage_threshold)
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(a, f, ensure_ascii=False, indent=2)
    print(f"[audit] -> {_rel(args.out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
