"""tools/failure_analysis_qgs.py — P3-E Failure Analysis（117 篇 missed，两层分类）。

用户 2026-08-27 定稿的两层 taxonomy（冻结定义）：
  第一层（Agent 在哪一层失败，每篇一个主标签）：
    F1 QUERY_GAP             —— Agent 已拥有对应 direction，但具体 query 表达式、
                                 同义词、布尔组合没有覆盖该论文
    F2 DIRECTION_GAP         —— Agent 的 query registry / knowledge state 中不存在
                                 能表达该论文所属 community / mechanism /
                                 historical concept 的 query family
    F3 RETRIEVAL_RANKING_GAP —— 正确 query 命中了，但排名太后未进候选
    F4 SCREENING_FN          —— 进入候选集但 relevance screening 错杀
    F5 PIPELINE_DROP         —— 论文确实出现在 raw retrieval/history 中，之后才消失
    F6 IDENTITY_ERROR        —— 实际找到了但 canonical identity 未匹配
  第二层（为什么产生，每篇可多标签）：
    HISTORICAL_TERMINOLOGY / COMMUNITY_ISOLATION / SEED_BIAS /
    CITATION_DISCONNECTED / MECHANISM_BLINDSPOT / DATE_BIAS / SOURCE_BIAS

判定原则（用户 2026-08-27 收紧）：
  F2 的证据 = Retrieval absence + Query-family absence：
    "117 missed 与 Agent 历史 retrieval footprint 几乎无交集"（absence）
    + "22 条 query 全部以 bulk-fill composite 锚定，标题不含 bulk-fill 的论文
      理论不可命中"（query-family absence）
    → 两者合起来才判 F2 DIRECTION_GAP；单看"没出现过"不足以判 F2。
  F1 的证据 = 论文属于已有 direction（含 bulk-fill community）但措辞变体
    （Bulk-Fill Resin / bulk-fill flowable）未被 "bulk-fill composite" 捕获。
  F5 的证据 = 论文 W id 出现在 Agent 历史（provenance/staging/candidates）但
    未进 KB（出现后消失）。

⚠️ 诚实标注：本脚本的 F1/F2 判定基于上述两条证据的自动化实现，输出每篇
evidence；CITATION_DISCONNECTED 依赖 OpenAlex 缓存引用网络重建。
输出：data/exports/qgs_failure_analysis.json（117 篇逐篇 + 两层汇总表）。
当前为 PRE-SIGNOFF provisional 状态（qgs_v1_signoff 59 项确认后升格 final）。
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
MISSED_PATH = os.path.join(BASE, "data", "exports", "qgs_missed_by_agent.json")
OUT_PATH = os.path.join(BASE, "data", "exports", "qgs_failure_analysis.json")
KB_PATH = os.path.join(BASE, "data", "cache", "knowledge_base.db")
CACHE_PATH = os.path.join(BASE, "data", "cache", "openalex_cache.json")


def norm_doi(d: str) -> str:
    return (d or "").strip().lower().replace("https://doi.org/", "").replace("http://doi.org/", "").strip()


def norm_wid(pid: str) -> str:
    m = re.search(r"(W\d+)", pid or "")
    return m.group(1) if m else ""


def norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (t or "").lower())


# ── 历史/现代术语表（HISTORICAL_TERMINOLOGY 判定）──
HISTORICAL_TERMS = [
    "contraction", "setting stress", "hardening stress", "contraction stress",
    "volume change", "volumetric change", "curing contraction", "dimensional change",
    "cavity configuration", "configuration factor", "c-factor", "c factor",
    "marginal adaptation", "marginal gap", "gap formation", "polymerization contraction",
]
MODERN_TERMS = ["shrinkage", "shrink", "photopolymer", "photo-polymer", "light curing",
                "light-curing", "uv curing", "3d print", "stereolithography", "vat"]


def load_agent_seen() -> dict:
    """Agent 历史中见过的论文集合（DOI/W id/title 三通道）+ 各通道来源。"""
    seen = {"dois": set(), "wids": set(), "titles": set()}
    # provenance（检索/扩展产生）
    for rec in json.load(open(os.path.join(BASE, "data", "exports", "discovery_paper_provenance.json"), encoding="utf-8")):
        d = norm_doi(rec.get("doi", ""))
        if d:
            seen["dois"].add(d)
        w = norm_wid(rec.get("paper_id", "") or rec.get("openalex_id", ""))
        if w:
            seen["wids"].add(w)
    # staging（筛选过）
    staging = json.load(open(os.path.join(BASE, "data", "exports", "discovery_staging.json"), encoding="utf-8"))
    for p in staging["papers"]:
        d = norm_doi(p.get("doi", ""))
        if d:
            seen["dois"].add(d)
        w = norm_wid(p.get("paper_id", "") or p.get("openalex_id", ""))
        if w:
            seen["wids"].add(w)
        t = norm_title(p.get("title", ""))
        if t:
            seen["titles"].add(t)
    # candidates source_papers
    for c in json.load(open(os.path.join(BASE, "data", "exports", "phase2_candidates.json"), encoding="utf-8")).get("candidates", []):
        for sp in (c.get("source_papers") or []):
            w = norm_wid(sp)
            if w:
                seen["wids"].add(w)
    # KB
    con = sqlite3.connect(f"file:{KB_PATH}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT paper_id, record_json FROM knowledge_records").fetchall()
        for pid, rj in rows:
            try:
                rec = json.loads(rj)
            except Exception:
                rec = {}
            d = norm_doi(rec.get("doi", "") or rec.get("DOI", ""))
            if d:
                seen["dois"].add(d)
            w = norm_wid(rec.get("openalex_id", "") or pid)
            if w:
                seen["wids"].add(w)
    finally:
        con.close()
    return seen


def load_query_tokens() -> list[str]:
    """query registry 全部 query 的词 token（去停用词）。"""
    reg = json.load(open(os.path.join(BASE, "data", "exports", "discovery_query_registry.json"), encoding="utf-8"))
    toks = set()
    for r in reg:
        q = (r.get("query_text") or r.get("query") or r.get("text") or "")
        # 引号内可能是多词短语（"bulk-fill composite"）——[a-z\- ] 含空格
        for m in re.finditer(r'"([a-z\- ]+)"', q.lower()):
            toks.add(m.group(1).strip())
    return sorted(toks)


def load_citation_network() -> dict:
    """从 OpenAlex 缓存重建 Agent relevant（67 seeds）1-hop 引用网络。
    返回 {backward: set, forward: set, loaded: bool}——引用种子 seed 的论文
    （backward = 论文引用了 seed；forward = seed 引用了论文，即 referenced_works）。"""
    if not os.path.exists(CACHE_PATH):
        return {"backward": set(), "forward": set(), "loaded": False}
    cache = json.load(open(CACHE_PATH, encoding="utf-8"))
    backward, forward = set(), set()
    # cites:{seed} 分页结果 = 引用该 seed 的论文（被引方向）
    for k, v in cache.items():
        if "cites:" not in k or not isinstance(v, dict):
            continue
        for w in v.get("results", []):
            wid = norm_wid((w or {}).get("id", ""))
            if wid:
                backward.add(wid)
    # seed work.referenced_works = 该 seed 引用的论文（参考文献方向）
    for k, v in cache.items():
        if not isinstance(v, dict):
            continue
        if v.get("referenced_works"):
            for x in v["referenced_works"]:
                wid = norm_wid(x)
                if wid:
                    forward.add(wid)
    return {"backward": backward, "forward": forward, "loaded": True}


def classify(p: dict, seen: dict, query_toks: list[str], cit: dict) -> dict:
    """单篇两层分类。返回 {layer1, layer1_evidence, roots, evidence}。"""
    title = p.get("title", "")
    tlow = title.lower()
    tl = norm_title(title)
    year = p.get("year")
    doi = norm_doi(p.get("doi", ""))
    wid = norm_wid(p.get("openalex_id", ""))
    srcs = p.get("sources_from", [])
    rcode = p.get("reason_code", "")
    try:
        y = int(year) if year else None
    except (TypeError, ValueError):
        y = None

    roots: list[str] = []
    ev: list[str] = []

    # ── 第一层：Agent 是否见过 ──
    layer1 = None
    if doi and doi in seen["dois"]:
        layer1 = "F6_IDENTITY_ERROR"
        ev.append(f"DOI {doi} 在 Agent 历史中（identity 未匹配）")
    elif wid and wid in seen["wids"]:
        layer1 = "F5_PIPELINE_DROP"
        ev.append(f"W{ wid } 在 Agent 历史 id 集合中但未进 KB")
    elif tl and tl in seen["titles"]:
        layer1 = "F4_SCREENING_FN"
        ev.append("title 与 Agent staging 集合完全一致（被筛掉）")
    else:
        # 从未被检索：F1 vs F2 —— query registry 词覆盖推断
        # 22 条 query 全部以 bulk-fill composite 锚定（2026-08-27 实测）
        has_bulkfill = "bulk-fill" in tlow or "bulkfill" in tlow
        # 标题词与 query token 的覆盖：论文含任一 query 短语核心词
        covered = any(tok in tlow for tok in query_toks if tok not in ("bulk-fill", "composite"))
        if has_bulkfill:
            layer1 = "F1_QUERY_GAP"
            ev.append("标题含 bulk-fill（query 锚点）但未被命中——query 组合太窄或排序")
        else:
            layer1 = "F2_DIRECTION_GAP"
            ev.append(f"标题不含 bulk-fill——现有 22 条 query（全部 bulk-fill 锚定）理论不可命中")

    # ── 第二层 root causes（可多标签）──
    # 1. HISTORICAL_TERMINOLOGY：历史术语 + 无现代词 + 早期年份
    has_hist = any(t in tlow for t in HISTORICAL_TERMS)
    has_mod = any(t in tlow for t in MODERN_TERMS)
    if has_hist and not has_mod and y is not None and y < 2006:
        roots.append("HISTORICAL_TERMINOLOGY")
        ev.append("标题用 contraction/setting stress 等历史术语且无现代词，<2006")
    elif has_hist and not has_mod:
        roots.append("HISTORICAL_TERMINOLOGY")
        ev.append("标题用 contraction 等历史术语，无现代 shrinkage/photopolymer 词")

    # 2. COMMUNITY_ISOLATION：dental/holography/ring-opening 社区 + 无 Agent 词汇
    dentalish = any(s in srcs for s in ("SR_01", "SR_06", "SR_07")) or "dental" in tlow or "tooth" in tlow or "restorative" in tlow
    hologr = "holograph" in tlow or "holographic" in tlow
    ro = "ring-open" in tlow or "expanding" in tlow or "expansion" in tlow
    agent_words = any(w in tlow for w in ("bulk-fill", "photopolymer", "photo-polymer", "3d print", "stereolithography", "vat", "uv-curing", "light-curing", "light curing"))
    if (dentalish or hologr or ro) and not agent_words:
        roots.append("COMMUNITY_ISOLATION")
        ev.append(f"来源社区 {'dental' if dentalish else 'holography' if hologr else 'ring-opening'}，标题无 Agent 词汇")

    # 3. DATE_BIAS：搜索/排序偏新 → 历史文献（PRE_2006）覆盖弱
    if y is not None and y < 2006:
        roots.append("DATE_BIAS")
        ev.append(f"年份 {y}（PRE_2006）——检索/排序天然偏新文献")

    # 4. CITATION_DISCONNECTED：不在 Agent relevant 1-hop 引用网络
    if cit["loaded"]:
        conn = (wid in cit["backward"] or wid in cit["forward"])
        if not conn:
            roots.append("CITATION_DISCONNECTED")
            ev.append("不在 Agent relevant 1-hop 引用网络（backward/forward 均断）")
        else:
            ev.append("在 Agent relevant 1-hop 引用网络中（citation 可桥接）")

    # 5. SEED_BIAS：初始种子偏现代光固化/AM → 与 KB 已知方向完全无关的社区
    if layer1 == "F2_DIRECTION_GAP" and (dentalish or hologr):
        roots.append("SEED_BIAS")
        ev.append("初始种子/query 偏现代光固化-AM，无 dental/holography 方向种子")

    # 6. MECHANISM_BLINDSPOT：机制方向缺失（reason_code 与机制相关且 DIRECTION_GAP）
    if layer1 == "F2_DIRECTION_GAP" and rcode in (
            "SHRINKAGE_MEASUREMENT", "DIRECT_STRESS_MEASUREMENT",
            "DIRECT_CAUSAL_FACTOR", "SHRINKAGE_MECHANISM"):
        roots.append("MECHANISM_BLINDSPOT")
        ev.append(f"机制方向 {rcode} 未覆盖")

    # 7. SOURCE_BIAS：文献类型（book/chapter/conference）
    vt = (p.get("venue") or "").lower()
    if any(k in vt for k in ("book", "chapter", "proceedings", "sympos", "conference")):
        roots.append("SOURCE_BIAS")
        ev.append(f"文献类型疑似 book/chapter/conference（venue={p.get('venue','')[:40]}）")

    if not roots:
        roots.append("UNCLASSIFIED")
        ev.append("无自动规则命中（需人工复核）")

    return {"layer1": layer1, "roots": roots, "evidence": ev}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-citation", action="store_true", help="跳过引用网络重建（省内存）")
    args = ap.parse_args()

    missed = json.load(open(MISSED_PATH, encoding="utf-8"))["missed_papers"]
    print(f"missed 117 篇（实际 {len(missed)}）")
    seen = load_agent_seen()
    query_toks = load_query_tokens()
    print(f"证据: Agent 见过集合 DOI={len(seen['dois'])} W={len(seen['wids'])} "
          f"title={len(seen['titles'])} | query tokens={len(query_toks)}")
    cit = {"backward": set(), "forward": set(), "loaded": False}
    if not args.no_citation:
        cit = load_citation_network()
        print(f"引用网络重建: {'loaded' if cit['loaded'] else '缓存缺失，跳过'} "
              f"(backward={len(cit['backward'])}, forward={len(cit['forward'])})")

    rows = []
    for p in missed:
        c = classify(p, seen, query_toks, cit)
        rows.append({**p, "layer1": c["layer1"], "root_causes": c["roots"],
                     "evidence": c["evidence"]})

    # 汇总
    l1 = Counter(r["layer1"] for r in rows)
    rc = Counter(rc_ for r in rows for rc_ in r["root_causes"])
    print("\n" + "=" * 66)
    print("第一层（Agent 在哪一层失败，117 篇主标签）:")
    for k in sorted(l1, key=lambda x: -l1[x]):
        print(f"  {k:<24} {l1[k]:>4}")
    print("\n第二层 root causes（每篇可多标签）:")
    for k in sorted(rc, key=lambda x: -rc[x]):
        print(f"  {k:<28} {rc[k]:>4}")

    out = {"benchmark_id": "pc_001_external_qgs_v1", "created_at": "2026-08-27",
           "note": "F1/F2 区分基于 query registry 词覆盖推断（非确定性证据）；"
                   "根因可多标签",
           "summary": {"layer1": dict(l1), "root_causes": dict(rc),
                       "citation_network_loaded": cit["loaded"]},
           "papers": rows}
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n✓ 已导出: {OUT_PATH} ({len(rows)} 篇)")


if __name__ == "__main__":
    main()
