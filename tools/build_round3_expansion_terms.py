"""tools/build_round3_expansion_terms.py — v2.1.1 Evidence→Query 转译核心（用户 2026-08-28 定稿）。

问题（Round2 实证）：TC_008 的 central_terms（restorative materials / polymerization
composite）作为 query 语言无区分度且形态失效 → G2R=0/8。
v2.1.1 机制（QGS-blind，全部纪律在此实现）：

  central_terms ≠ expansion_terms
    central_terms  → community interpretation/labeling（Query Generator 禁止读取）
    expansion_terms → query generation（本脚本唯一产出）

  Discriminativeness：
    P(t|C) = (df_C(t)+α)/(|C|+α)      C = community supporting papers
    P(t|B) = (df_B(t)+α)/(|B|+α)      B = 冻结的旧 Candidate DB（depth run 6572 篇
                                      title+abstract；不含 hop2 graph neighbors——
                                      防止新语言自我稀释）
    Disc(t) = log(P(t|C)/P(t|B))，α=1
    门槛：df_C(t) >= 2（防单次怪词）
    ExpansionScore(t) = Disc(t)·log(1+df_C(t))（可选排序）

  verbatim 校验：phrase 必须连续出现于 ≥1 篇支持论文 title/abstract 原文
    （淘汰 token 处理造出的伪短语）

  phrase-family 去冗余：substring containment OR token Jaccard ≥ 0.7 → 一族
    保留 ExpansionScore 最大者

  Expansion Evidence Community ≠ Promoted Community：
    mode A = TC_008 only（最干净验证 central→expansion 修复本身）
    mode B = QGS-blind evidence neighborhoods：任何 community 满足
             paper_count≥3 AND mean_npmi≥0.6 AND retrieval_novelty≥0.5
             即可贡献 expansion vocabulary（CORE 仍不能晋升 Query Family，
             但边界性词汇有价值）——TC_002 若自然满足就进来，不写死 ID

  anchor：registry 已验证 "polymerization shrinkage"（不从社区 mapper 造词）

  queryability：--check-queryability 时每条 expansion 单独 Scopus 查
    standalone_hits == 0 → DROP_UNQUERYABLE（规则提前冻结，不看 QGS）
    standalone_hits > 0  → KEEP

  v2.1.4 anchored queryability（用户 2026-08-28 定稿，Disc+df_dedup 冻结不改）：
    --check-anchored 对 standalone>0 的项再查 "polymerization shrinkage" AND term
    standalone_hits == 0                  → DROP_UNQUERYABLE
    standalone_hits > 0, anchored_hits==0 → REAL_PHRASE_BUT_ANCHOR_INCOMPATIBLE
    anchored_hits > 0                     → ANCHOR_QUERYABLE
    （三态 = Level 2 Query Linguistic Validity；Level 3 Retrieval Utility
      由 tools/pilot_round3_query_utility.py 负责）
    --from-json <file>：跳过重算（公式未变），直接复用已有结果只跑 queryability

输出（用户冻结流水线）：
  community → candidate_terms → verbatim_verified → df_C/df_B/Disc/ExpansionScore
  → phrase_family → representative_expansion_terms → queryability_status
"""
import argparse
import json
import math
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "search_engine", "discovery"))

import term_community as tc_mod

COMMUNITIES_PATH = os.path.join(BASE, "data", "exports", "term_communities_hop2.json")
ENRICHED_PATH = os.path.join(BASE, "data", "cache", "openalex_hop2_enriched.json")
DEPTH_RUN_PATH = os.path.join(BASE, "data", "exports", "query_family_runs_depth.json")
SCOPUS_CACHE = os.path.join(BASE, "data", "cache", "scopus_cache.db")
OUT_PATH = os.path.join(BASE, "data", "exports", "round3_expansion_terms.json")

ALPHA = 1.0
DF_C_MIN = 2
FAMILY_JACCARD = 0.7
ANCHOR = "polymerization shrinkage"
# mode B 的 QGS-blind evidence 门槛（复用 promoter 阈值语义）
EV_MIN_PAPERS = 3
EV_MIN_NPMI = 0.6
EV_MIN_RET_NOV = 0.5
# token 集合（phrase family 用）
TOKEN_RE = re.compile(r"[a-z0-9]+")

# ── v2.1.2 Scientific Phrase Quality Gate（规则，无 LLM；用户 2026-08-28 定）──
# 连续重复 token / 首尾句法功能词 / 数字单位占比 / content tokens / noun-like 检查
REPEAT_RE = re.compile(r"\b(\w+)\s+\1\b", re.I)          # wall wall / cm2 cm2
HEAD_FUNCTION = {"such", "only", "must", "were", "was", "had", "but", "than", "the",
                 "one", "two", "three", "four", "five", "six", "this", "that",
                 "each", "group", "intensity", "between", "using", "investigating",
                 "measurement", "recorded", "obtained", "observed", "showed"}
TAIL_FUNCTION = {"but", "were", "was", "the", "only", "than", "had", "such", "made",
                 "must", "because", "when", "while", "during", "which", "that",
                 "one", "two", "three", "four", "five", "six", "group", "seconds",
                 "minutes", "hours", "days", "cm", "mm", "mpa", "wt"}
NUMERIC_RE = re.compile(r"\d|cm2|mpa|gpa|nm\b|μm|um\b|wt")
NOUN_SUFFIX = re.compile(r"(tion|sion|ment|meter|scope|graph|graphy|ity|ure|ance|"
                         r"ence|ture|ide|mer|ene|ane|ose|ate|ase|phore|tropic|"
                         r"dilat|shrink|strain|stress|curing|polymeri|conversion|"
                         r"contraction|strength|modulus|resin|filler|composite)\b", re.I)


def quality_gate(phrase: str) -> bool:
    """科学短语质量闸（规则）：残片/重复/数字单位/非名词性 → drop。

    unigram（如 dilatometer）允许：必须 noun-like 且非 stopword（v2.1.3 B：
    真正的 unigram 若被 ≥2 篇不同论文使用就应通过——df_dedup 把关）。
    """
    t = phrase.lower()
    toks = t.split()
    if REPEAT_RE.search(t):
        return False                       # wall wall / cm2 cm2
    if len(toks) == 1:
        return toks[0] not in tc_mod.STOPWORDS and bool(NOUN_SUFFIX.search(toks[0]))
    if toks[0] in HEAD_FUNCTION:
        return False                       # only studies investigating / intensity the
    if toks[-1] in TAIL_FUNCTION:
        return False                       # composites were light / exposure duration but
    num_frac = sum(1 for w in toks if NUMERIC_RE.search(w)) / len(toks)
    if num_frac > 0.3:
        return False                       # six seconds / cm2 group
    content = sum(1 for w in toks if w not in tc_mod.STOPWORDS)
    if content < 2:
        return False                       # must made 等
    if not any(NOUN_SUFFIX.search(w) for w in toks):
        return False                       # 至少一个 noun-like token
    return True


def canonicalize_phrase(phrase: str) -> str:
    """canonical key（v2.1.3 用户定稿）：去中间 stopword + 轻量形态归一。

    只用于 morphology dedup / df 聚合 / family grouping / background 匹配。
    **query surface 必须用原 verbatim 形式**（"degree of conversion" 不能删 of
    后拿去搜——否则重造 "polymerization composite" 问题）。
    """
    words = [w for w in phrase.lower().split() if w not in tc_mod.STOPWORDS]
    out = []
    for w in words:
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss") \
                and not w.endswith("us") and not w.endswith("is"):
            w = w[:-1]
        out.append(w)
    return " ".join(out)


def canonicalize_text(text: str) -> str:
    """整篇文本 canonical 化（background 池用）：tokenize → 去 stopword → 保序。"""
    return " ".join(w for w in tc_mod.tokenize(text) if w not in tc_mod.STOPWORDS)


def norm_text(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def token_set(phrase: str) -> set[str]:
    return set(TOKEN_RE.findall(phrase or ""))


def load_communities(path: str) -> dict[str, dict]:
    return {c["community_id"]: c
            for c in json.load(open(path, encoding="utf-8"))["communities"]}


def load_enriched(path: str) -> dict[str, dict]:
    if not os.path.exists(path):
        return {}
    return json.load(open(path, encoding="utf-8"))


def load_background() -> list[str]:
    """B 背景池 = 冻结旧 Candidate DB（depth run 6572 篇 title+abstract）。"""
    records = json.load(open(DEPTH_RUN_PATH, encoding="utf-8"))["records"]
    eids: set[str] = set()
    for recs in records.values():
        for r in recs:
            if r.get("eid"):
                eids.add(r["eid"].strip())
    # eid → doi → scopus_cache 文本
    con = sqlite3.connect(f"file:{SCOPUS_CACHE}?mode=ro&immutable=1", uri=True)
    doi_text: dict[str, str] = {}
    for pid, j in con.execute("SELECT paper_id, normalized_json FROM papers"):
        nj = json.loads(j)
        doi = (nj.get("doi") or "").strip().lower()
        t = (nj.get("title") or "") + " " + (nj.get("abstract") or "")
        if doi and t.strip():
            doi_text[doi] = norm_text(t)
    con.close()
    texts = []
    for recs in records.values():
        for r in recs:
            doi = (r.get("doi") or "").strip().lower()
            if doi and doi in doi_text:
                # 与 community 侧同一套 canonicalization（v2.1.3 用户定稿：
                # 两边必须同 canonicalizer，否则 degree of conversion 与
                # degree conversion 的 df_B 会误判为不同词 → Disc 虚高）
                texts.append(canonicalize_text(doi_text[doi]))
    # 去重
    return list(dict.fromkeys(texts))


def extract_verbatim(papers: list[dict]) -> list[dict]:
    """对支持论文提取 1-3 gram，仅保留 verbatim（连续出现于原文 tokens）。

    unigram 通道（v2.1.3 B）：真正的科学 unigram（dilatometer）若被 ≥2 篇不同
    论文使用应自然浮出——由 quality_gate 的 noun-like 检查把关。
    """
    out = []
    for p in papers:
        text = norm_text(p["title"]) + " " + norm_text(p.get("abstract") or "")
        tokens = tc_mod.tokenize(text)
        grams: set[str] = set()
        for n in (1, 2, 3):
            for i in range(len(tokens) - n + 1):
                gram = tokens[i:i + n]
                if n == 1:
                    if quality_gate(gram[0]):
                        grams.add(gram[0])
                elif tc_mod.keep_ngram(gram):
                    grams.add(" ".join(gram))
        for g in grams:
            out.append({"phrase": g, "wid": p["wid"]})
    return out


def phrase_families(terms: list[dict]) -> list[list[str]]:
    """substring containment OR token Jaccard ≥ 0.7 → family（canonical 层）。"""
    phrases = [t["term"] for t in terms]
    fams: list[list[str]] = []
    for ph in phrases:
        ts = token_set(ph)
        placed = False
        for fam in fams:
            if any(ph in f or f in ph for f in fam):
                fam.append(ph)
                placed = True
                break
            fts = token_set(fam[0])
            if len(ts & fts) / max(len(ts | fts), 1) >= FAMILY_JACCARD:
                fam.append(ph)
                placed = True
                break
        if not placed:
            fams.append([ph])
    return fams


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["A", "B"], default="A",
                    help="A=TC_008 only；B=QGS-blind evidence neighborhoods")
    ap.add_argument("--communities", default=COMMUNITIES_PATH)
    ap.add_argument("--enriched", default=ENRICHED_PATH)
    ap.add_argument("--out", default=OUT_PATH)
    ap.add_argument("--check-queryability", action="store_true",
                    help="对 representative terms 逐条 Scopus standalone 查 "
                         "（规则冻结：hits==0 → DROP_UNQUERYABLE；hits>0 → KEEP；"
                         "不看 QGS——queryability check 是 query-generation 算法的一部分）")
    ap.add_argument("--check-anchored", action="store_true",
                    help="v2.1.4：在 standalone 基础上对 KEEP 再查 "
                         "'\"polymerization shrinkage\" AND term'，三态："
                         "DROP_UNQUERYABLE / REAL_PHRASE_BUT_ANCHOR_INCOMPATIBLE / "
                         "ANCHOR_QUERYABLE（Level 2 Query Linguistic Validity）")
    ap.add_argument("--from-json", default=None,
                    help="跳过重算（Disc+df_dedup 公式未变），从已有 round3 结果文件"
                         "读 representative_terms 只跑 queryability check")
    ap.add_argument("--top", type=int, default=50,
                    help="queryability check 只查 score 前 N 个 representative（默认 50）")
    ap.add_argument("--retry", type=int, default=2,
                    help="QUERYABILITY_UNKNOWN（执行失败/timeout）自动重试次数（默认 2）")
    ap.add_argument("--engine-data-dir", default="data",
                    help="ScopusSearchEngine data_dir（默认 data；只读跑批可指到 "
                         "Temp 副本：scopus_profile + cache/scopus_cache.db，避免写项目目录）")
    args = ap.parse_args()

    if args.from_json:
        # ── v2.1.4：跳过重算，复用已有结果（公式未变，只加 queryability 层）──
        d0 = json.load(open(args.from_json, encoding="utf-8"))
        reps = d0["representative_terms"]
        terms = d0.get("all_verbatim_terms", [])
        comm_meta = d0.get("communities", {})
        n_b = (d0.get("background") or {}).get("n_docs")
        out = dict(d0)
        out["version"] = "round3_expansion_v214"
        out["queryability_status"] = "UNCHECKED"
        out.pop("queryability_summary", None)
        out["v214_note"] = ("--from-json 复用已有 score；Disc+df_dedup 冻结，"
                            "只新增 anchored queryability 三态层")
        print(f"--from-json: {args.from_json}")
        print(f"  reps={len(reps)} | 公式未变，跳过重算，只跑 queryability check")
    else:

    # ── 选 evidence communities（QGS-blind，无 ID 硬编码）──
        if args.mode == "A":
            ev_comm_ids = ["TC_008"]
        else:
            ev_comm_ids = [cid for cid, c in comms.items()
                           if c["paper_count"] >= EV_MIN_PAPERS
                           and c["term_coherence"]["mean_npmi"] >= EV_MIN_NPMI
                           and c.get("retrieval_novelty", 0) >= EV_MIN_RET_NOV
                           and c["role"] != "NOISE_SMALL_CLUSTER"]
        print(f"mode {args.mode}: evidence communities = {ev_comm_ids}")

        # ── 支持论文文本（enriched 补全）──
        all_paper_rows: list[dict] = []
        comm_meta = {}
        for cid in ev_comm_ids:
            c = comms[cid]
            papers = []
            for wid in c.get("supporting_papers", []):
                e = enriched.get(wid, {})
                if not e.get("title"):
                    continue
                papers.append({"wid": wid, "title": e.get("title", ""),
                               "abstract": e.get("abstract") or ""})
            rows = extract_verbatim(papers)
            all_paper_rows.extend({"phrase": r["phrase"], "wid": r["wid"],
                                   "community": cid} for r in rows)
            comm_meta[cid] = {"paper_count": c["paper_count"],
                              "n_with_text": len(papers),
                              "npmi": c["term_coherence"]["mean_npmi"],
                              "retrieval_novelty": c.get("retrieval_novelty"),
                              "role": c["role"]}
            print(f"  {cid} [{c['role']}] papers={len(papers)}（有文本）| "
                  f"verbatim phrases={len(rows)}")

        # ── per-community 统计（v2.1.3 B：证据强度与局部区分度分离）──
        # comm_evidence[cid][canon][surface] = set(wids)
        #   df_in_source = |本 community 内论文|（局部，只用于 Disc_c）
        #   df_dedup     = |∪_c 论文|（跨 community 去重，资格门槛 + score）
        comm_evidence: dict[str, dict[str, dict[str, set]]] = \
            defaultdict(lambda: defaultdict(lambda: defaultdict(set)))
        for r in all_paper_rows:
            if not quality_gate(r["phrase"]):
                continue
            canon = canonicalize_phrase(r["phrase"])
            if not canon:
                continue
            comm_evidence[r["community"]][canon][r["phrase"]].add(r["wid"])
        print("加载背景池（旧 Candidate DB title+abstract，同一套 canonicalization）...")
        bg = load_background()
        print(f"  背景池文档 = {len(bg)}")
        n_b = len(bg)

        # df_B 按 canonical（同一 canonicalizer 下的背景文本子串匹配）
        all_canons = sorted({c for cc in comm_evidence.values() for c in cc})
        df_b: Counter = Counter()
        for ph in all_canons:
            df_b[ph] = sum(1 for t in bg if ph in t)

        # n_c = unique supporting papers in community（BUG FIX：
        # COMMUNITY_DENOMINATOR_ERROR——曾误用 phrase rows，压低 P(t|C)）
        comm_n_papers = {cid: len({r["wid"] for r in all_paper_rows
                                   if r["community"] == cid})
                         for cid in ev_comm_ids}

        # ── 合并：Disc_max × log1p(df_dedup) ──
        term_rows: dict[str, dict] = {}
        for cid, cc in comm_evidence.items():
            n_c = max(comm_n_papers.get(cid, 1), 1)
            for canon, surf_map in cc.items():
                all_wids: set[str] = set()
                surf_count: dict[str, int] = {}
                for s, ws in surf_map.items():
                    all_wids |= ws
                    surf_count[s] = len(ws)
                df_dedup = len(all_wids)
                if df_dedup < DF_C_MIN:      # ALGORITHM CHANGE:
                    continue                 # GLOBAL_DEDUP_SUPPORT_GATE（df_c→df_dedup）
                df_c = len(all_wids)         # 本 community 内论文数（同一 canon 全 surface 并集）
                bdf = df_b[canon]
                p_c = (df_c + ALPHA) / (n_c + ALPHA)
                p_b = (bdf + ALPHA) / (n_b + ALPHA)
                disc_c = math.log(p_c / p_b)
                row = term_rows.setdefault(canon, {
                    "term": canon, "surface_forms": [], "preferred_surface": None,
                    "df_dedup": 0, "background_df": bdf,
                    "source_community": None, "df_in_source": 0,
                    "source_community_size": 0, "support_rate_in_source": 0.0,
                    "disc_max": 0.0, "expansion_score": 0.0,
                    "supporting_paper_ids": []})
                # 跨 community 论文并集（df_dedup 的论文集，supporting_paper_ids 用）
                row.setdefault("_all_wids", set()).update(all_wids)
                # surface_forms 并集（按各自论文数排序）
                seen: dict[str, int] = dict(row["surface_forms"])
                for s, c in surf_count.items():
                    seen[s] = max(seen.get(s, 0), c)
                row["surface_forms"] = sorted(seen.items(), key=lambda kv: -kv[1])
                row["df_dedup"] = len(row["_all_wids"])
                if disc_c > row["disc_max"]:
                    row["disc_max"] = round(disc_c, 4)
                    row["source_community"] = cid
                    row["df_in_source"] = df_c
                    row["source_community_size"] = n_c
                    row["support_rate_in_source"] = round(df_c / n_c, 4)
        # preferred_surface：surface_forms 中支持论文数最大（verbatim 已保证）
        for canon, row in term_rows.items():
            row["surface_forms"] = [s for s, _ in row["surface_forms"]]
            row["preferred_surface"] = row["surface_forms"][0] if row["surface_forms"] else canon
            row["supporting_paper_ids"] = sorted(row.pop("_all_wids"))
            row["expansion_score"] = round(row["disc_max"] * math.log1p(row["df_dedup"]), 4)

        terms = sorted(term_rows.values(), key=lambda t: -t["expansion_score"])
        print(f"\nper-community verbatim 候选（gate + canonical, df_dedup≥{DF_C_MIN}）= "
              f"{len(terms)}")

        # ── phrase family 去冗余（canonical 层）→ 每族代表（ExpansionScore 最大）──
        fams = phrase_families(terms)
        by_phrase = {t["term"]: t for t in terms}
        reps = []
        for fam in fams:
            best = max(fam, key=lambda p: by_phrase[p]["expansion_score"])
            reps.append({"family": sorted(fam), "representative": best,
                         "preferred_surface": by_phrase[best]["preferred_surface"],
                         "score": by_phrase[best]["expansion_score"],
                         "disc": by_phrase[best]["disc_max"],
                         "df_C": by_phrase[best]["df_dedup"],
                         "source_community": by_phrase[best]["source_community"],
                         "support_rate": by_phrase[best]["support_rate_in_source"]})
        reps.sort(key=lambda r: -r["score"])

        print(f"\n{'='*84}")
        print(f"Round3 expansion terms（mode {args.mode}，anchor='{ANCHOR}'，"
              f"per-community Disc）")
        print(f"{'='*84}")
        print(f"{'score':>7} {'disc':>7} {'dfC':>4} {'src':>8} {'support':>7}  "
              f"preferred_surface (canonical)")
        for r in reps[:25]:
            fam_note = "" if len(r["family"]) == 1 else f"  [族: {', '.join(r['family'][:4])}]"
            print(f"{r['score']:>7.2f} {r['disc']:>7.2f} {r['df_C']:>4} "
                  f"{r['source_community']:>8} {r['support_rate']:>7.2f}  "
                  f"{r['preferred_surface']} ({r['representative']}){fam_note}")

        out = {
            "version": "round3_expansion_v213",
            "created_at": "2026-08-28",
            "mode": args.mode,
            "anchor": ANCHOR,
            "formulas": {
                "disc_c": "log((df_c+1)/(|C_c|+1) / (df_B+1)/(|B|+1))  # per-community",
                "score_c": "disc_c * log1p(df_c)",
                "expansion_score": "max_c score_c",
                "alpha": ALPHA, "df_C_min": DF_C_MIN,
                "family_jaccard": FAMILY_JACCARD,
                "canonicalization": "去中间 stopword + 轻量形态归一（仅 canonical key；query 用 verbatim surface）",
                "background": "同一套 canonicalization 的旧 Candidate DB",
            },
            "background": {"n_docs": n_b,
                           "note": "冻结旧 Candidate DB（depth run 6572 篇 title+abstract，canonicalized），不含 hop2 neighbors"},
            "communities": comm_meta,
            "representative_terms": reps,
            "all_verbatim_terms": terms,
            "queryability_status": "UNCHECKED",
            "next": "--check-queryability 逐条 Scopus standalone hit（0 → DROP_UNQUERYABLE）",
        }
    if args.check_queryability or args.check_anchored:
        # ── queryability check（query-generation 算法的一部分；规则冻结、不看 QGS）──
        # v2.1.4 anchored 状态机（用户 2026-08-29 定稿）：
        #   STANDALONE_ZERO           standalone_hits==0（语义筛除：真不可检索）
        #   ANCHOR_INCOMPATIBLE       standalone>0 且 anchored==0（语义筛除）
        #   ANCHOR_QUERYABLE          anchored>0（可通过）
        #   QUERYABILITY_UNKNOWN      执行失败/timeout/导出中断（非语义判断，自动重试）
        # 只有前两种是真正的语义筛除；UNKNOWN 重试后仍失败则保留该状态不判死。
        # （只跑 --check-queryability 时保持 v2.1.3 语义：hits>0 → KEEP）
        import asyncio
        from search_engine.engine import ScopusSearchEngine

        n_check = min(args.top, len(reps))

        async def run_check():
            engine = ScopusSearchEngine(data_dir=args.engine_data_dir)
            await engine.start()
            try:
                for i, r in enumerate(reps[:n_check], 1):
                    term = r["preferred_surface"]   # query 必须用 verbatim surface
                    # Level 2a：standalone（失败自动重试 --retry 次）
                    hits_s = -1
                    for attempt in range(1, args.retry + 1):
                        q_s = f'TITLE-ABS-KEY("{term}")'
                        try:
                            res_s = await engine.search(q_s, limit=1, skip_cache=True)
                            hits_s = len(res_s.papers)
                            break
                        except Exception as e:
                            print(f"    [WARN] {term[:30]}: standalone 失败 "
                                  f"(attempt {attempt}/{args.retry}): {e}")
                            await asyncio.sleep(2)
                    # Level 2b：anchored（仅 standalone>0 才查，省 API）
                    hits_a = None
                    if hits_s > 0 and args.check_anchored:
                        for attempt in range(1, args.retry + 1):
                            q_a = f'TITLE-ABS-KEY("{ANCHOR}" AND "{term}")'
                            try:
                                res_a = await engine.search(q_a, limit=1, skip_cache=True)
                                hits_a = len(res_a.papers)
                                break
                            except Exception as e:
                                print(f"    [WARN] {term[:30]}: anchored 失败 "
                                      f"(attempt {attempt}/{args.retry}): {e}")
                                await asyncio.sleep(2)
                    if hits_s < 0 or hits_a == -1:
                        status = "QUERYABILITY_UNKNOWN"
                    elif hits_s == 0:
                        status = "STANDALONE_ZERO"
                    elif hits_a is None:
                        status = "KEEP"                      # 仅 standalone 模式（v2.1.3 语义）
                    elif hits_a == 0:
                        status = "ANCHOR_INCOMPATIBLE"
                    else:
                        status = "ANCHOR_QUERYABLE"
                    r["queryability"] = {"status": status, "standalone_hits": hits_s}
                    if hits_a is not None:
                        r["queryability"]["anchored_hits"] = hits_a
                    print(f"  [{i:>2}/{n_check}] {status:<26} "
                          f"score={r['score']:>6.2f} {term[:40]}")
                    await asyncio.sleep(1)
            finally:
                await engine.close()

        asyncio.run(run_check())
        from collections import Counter
        st_counter = Counter(r.get("queryability", {}).get("status")
                             for r in reps[:n_check])
        out["queryability_status"] = f"CHECKED top{n_check}"
        out["queryability_summary"] = {"checked": n_check, **dict(st_counter)}
        print(f"\nqueryability: {dict(st_counter)}")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n✓ 已写: {args.out}")


if __name__ == "__main__":
    main()
