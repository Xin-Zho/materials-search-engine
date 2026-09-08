"""tools/classify_residual_misses.py — S3: 70 residual miss 证据提取 + 聚类（2026-08-29 用户定）。

70 residual miss（R02 Relevant ∧ Seen_S2=FALSE）四通道证据提取，允许多标签：
  TERM          现有 query vocabulary 未覆盖的表达方式（高辨识词组，MissSupport≥2）
  CITATION      与 S2 seen relevant 存在 citation 路径（backward: miss 引用 seen；
                forward: seen 引用 miss）
  COMMUNITY     属于未覆盖社区/历史语言（共享术语聚类推断）
  INDEX_IDENTITY 数据源收录/元数据/identity resolution 问题

流程：70 misses -> evidence extraction -> clustering（共享术语连通分量）
      -> cluster-level gap diagnosis -> repair 建议
原则（用户定）：先聚类再 repair，MissSupport(t)≥2 或 CitationSupport(t)>0 才进 repair candidate。

输出：data/exports/terminology/s3_miss_diagnosis.json
  {per_miss: [{wid, title, labels, terms, citation, cluster_id, issues}],
   clusters: [{cluster_id, size, members, shared_terms, gap_diagnosis}],
   vocab_stats: {...}}

用法：
  python tools/classify_residual_misses.py --misses <s3_residual_misses.json> [--out <path>]
"""
import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TERM_DIR = os.path.join(BASE, "data", "exports", "terminology")
DEFAULT_MISSES = os.path.join(TERM_DIR, "s3_residual_misses.json")
DEFAULT_OUT = os.path.join(TERM_DIR, "s3_miss_diagnosis.json")
QS_PATH = os.path.join(TERM_DIR, "s1_final_query_set.json")
SEEN2_PATH = os.path.join(TERM_DIR, "s2_seen_set.json")
RAW2_PATH = os.path.join(TERM_DIR, "s2_raw_records.json")
OA_CACHE = os.path.join(BASE, "data", "cache", "openalex_cache.json")

# 主题核心词（不算辨识——它们是领域本身，不是"表达方式变体"）
CORE_TERMS = {
    "polymerization", "photopolymerization", "shrinkage", "shrink", "shrinking",
    "composite", "resin", "resins", "monomer", "monomers", "polymer", "polymers",
    "photopolymer", "curing", "cure", "light", "uv", "dental", "material",
    "materials", "methacrylate", "methacrylates", "acrylate", "acrylates",
}
# 通用学术停用词（含标准停用词，复数由 _norm_w 归一）
STOPWORDS = {
    "effect", "effects", "influence", "study", "studies", "investigation",
    "investigations", "based", "using", "used", "use", "high", "low", "new",
    "novel", "performance", "properties", "property", "behavior", "behaviour",
    "results", "result", "method", "methods", "analysis", "analyses", "application",
    "applications", "preparation", "synthesis", "characterization", "evaluation",
    "comparison", "relationship", "role", "impact", "review", "toward", "towards",
    "via", "with", "from", "into", "during", "after", "before", "between", "among",
    "their", "these", "those", "this", "that", "the", "and", "for", "over",
    "under", "within", "without", "due", "part", "parts", "type", "types",
    "system", "systems", "process", "processing", "produced", "development",
    "determination", "determined", "observed", "showed", "shown", "tested",
    "investigated", "examined", "obtained", "increased", "decreased", "reduced",
    "improved", "significant", "respectively", "however", "therefore",
    # 标准停用词
    "a", "an", "be", "been", "being", "am", "is", "are", "was", "were", "will",
    "would", "could", "should", "can", "may", "might", "must", "shall", "do",
    "does", "did", "done", "has", "have", "had", "having", "not", "no", "nor",
    "only", "also", "than", "then", "there", "here", "where", "when", "why",
    "how", "what", "which", "who", "whom", "whose", "some", "any", "all",
    "both", "each", "few", "many", "most", "more", "other", "others", "same",
    "very", "about", "above", "again", "against", "because", "been", "before",
    "but", "by", "ever", "every", "further", "if", "just", "least", "less",
    "made", "make", "makes", "making", "may", "mean", "means", "might",
    "much", "must", "neither", "next", "nor", "nothing", "of", "off", "once",
    "only", "our", "ours", "out", "own", "per", "quite", "rather", "so",
    "such", "than", "through", "thus", "too", "up", "very", "was", "were",
    "while", "yet", "you", "your", "yours",
}

GRAM = 3  # 最大 n-gram

# n-gram 首尾功能词（截断碎片检测：conversion and / of conversion / kinetics and volume / the optical）
FRAGMENT_EDGE = {
    "and", "or", "of", "to", "the", "a", "an", "in", "on", "for", "with",
    "by", "we", "our", "their", "its", "as", "at", "from", "during",
    "respect", "after", "before", "into", "between", "among", "via",
    "under", "over", "within", "without", "toward", "towards", "through",
    "upon", "about", "than", "then", "also", "only", "not", "both", "each",
}


def _norm_w(x: str) -> str:
    """简单复数归一（studies→study, composites→composite, effects→effect）。"""
    if x.endswith("ies") and len(x) > 4:
        return x[:-3] + "y"
    if x.endswith("s") and len(x) > 3 and not x.endswith("ss"):
        return x[:-1]
    return x


def _is_noise(tok: str) -> bool:
    t = _norm_w(tok)
    return t in CORE_TERMS or t in STOPWORDS or len(t) < 4


def _tokens(text: str) -> list[str]:
    t = (text or "").lower()
    t = re.sub(r"[^a-z0-9\s\-/]+", " ", t)
    return [x for x in re.split(r"\s+", t) if x]


def _grams(text: str) -> list[str]:
    toks = _tokens(text)
    out = []
    for n in range(1, GRAM + 1):
        for i in range(len(toks) - n + 1):
            g = " ".join(toks[i:i + n])
            # 全部 token 都是 noise（核心/停用/太短）→ 丢；
            # 至少一个辨识 token → 保留（允许 shrinkage stress / polymerization kinetics
            # 这类"核心词+辨识词"组合——它们是最重要的表达变体）
            if all(_is_noise(x) for x in g.split()):
                continue
            # n>=2 时首尾 token 不能是功能词（截断碎片：conversion and / of conversion）
            if n >= 2 and (toks[i] in FRAGMENT_EDGE or toks[i + n - 1] in FRAGMENT_EDGE):
                continue
            if n == 1 and len(g) < 6:
                continue  # 单 token 需足够长才算专业词
            if len(g) > 60:
                continue
            out.append(g)
    return out


def load_existing_vocab() -> set[str]:
    """现有 query vocabulary = 19 条 query 引号内词组 + repair term families canonical。"""
    vocab = set()
    qs = json.load(open(QS_PATH, encoding="utf-8"))
    for q in qs["queries"]:
        for m in re.findall(r'"([^"]+)"', q["query_string"]):
            vocab.add(m.lower().strip())
        vocab.add(q["canonical_term"].lower().strip())
    fams = json.load(open(os.path.join(TERM_DIR, "term_families.json"),
                          encoding="utf-8"))["families"]
    for f in fams:
        vocab.add((f.get("canonical_term") or "").lower().strip())
    return {v for v in vocab if v}


def load_oa_meta() -> dict:
    """openalex_cache -> {wid: {doi, title, referenced_works, has_record}}。"""
    meta = {}
    if not os.path.exists(OA_CACHE):
        return meta
    cache = json.load(open(OA_CACHE, encoding="utf-8"))
    for q, resp in cache.items():
        for w in resp.get("results", []):
            wid = (w.get("id") or "").replace("https://openalex.org/", "")
            if wid and wid not in meta:
                doi = (w.get("doi") or "").lower().replace("https://doi.org/", "")
                meta[wid] = {
                    "doi": doi, "title": w.get("title"),
                    "referenced_works": [r.replace("https://openalex.org/", "")
                                         for r in (w.get("referenced_works") or [])],
                    "has_record": True,
                }
    return meta


def build_seen_wids() -> set[str]:
    """S2 seen（canonical keys + raw records DOI）→ OpenAlex WID 集合。

    S2_SEEN_SET keys 大部分是 EID（2-s2.0-），DOI 需从 raw records 补全。
    """
    seen_dois = set()
    seen = json.load(open(SEEN2_PATH, encoding="utf-8"))["keys"]
    for k in seen:
        k = k.strip().lower()
        if not k.startswith("2-s2.0-") and k:
            seen_dois.add(k)
    if os.path.exists(RAW2_PATH):
        raw = json.load(open(RAW2_PATH, encoding="utf-8"))
        for q, recs in raw.get("records_by_query", {}).items():
            for r in recs:
                if r.get("doi"):
                    seen_dois.add(str(r["doi"]).strip().lower())
    oa = load_oa_meta()
    return {wid for wid, m in oa.items()
            if m["doi"] and m["doi"] in seen_dois}


def main():
    ap = argparse.ArgumentParser(description="S3 residual miss 证据提取 + 聚类")
    ap.add_argument("--misses", default=DEFAULT_MISSES)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--min-support", type=int, default=2,
                    help="TERM 候选词跨 miss 最低支持（MissSupport，用户原则 ≥2）")
    args = ap.parse_args()

    if not os.path.exists(args.misses):
        print(f"[FATAL] 缺 {args.misses}——先跑 tools/build_residual_misses.py")
        sys.exit(2)
    data = json.load(open(args.misses, encoding="utf-8"))
    misses = data["misses"]
    print(f"residual miss = {len(misses)}")

    vocab = load_existing_vocab()
    oa = load_oa_meta()
    seen_wids = build_seen_wids()
    print(f"existing vocab = {len(vocab)} | openalex records = {len(oa)}"
          f" | seen_wids = {len(seen_wids)}")

    # ── TERM：候选词（未覆盖 + MissSupport≥2）──
    miss_terms = {}          # wid -> [candidate terms]
    term_miss_support = Counter()
    for r in misses:
        text = " ".join([r.get("oa_title") or r.get("title") or "",
                         r.get("abstract") or ""])
        gs = set(_grams(text))
        cand = [g for g in gs if g not in vocab]
        miss_terms[r["wid"]] = cand
        for g in set(cand):
            term_miss_support[g] += 1
    strong_terms = {g for g, c in term_miss_support.items() if c >= args.min_support}
    print(f"TERM 候选词（MissSupport≥{args.min_support}，未覆盖 vocab）= {len(strong_terms)}")

    # 聚类用"强辨识词"：排除超泛词（出现在 >30% miss 的 1-gram 基本是泛词）
    max_common = max(2, int(len(misses) * 0.3))
    cluster_terms = {g for g, c in term_miss_support.items()
                     if args.min_support <= c <= max_common and g not in vocab}
    print(f"聚类辨识词（support ∈ [{args.min_support}, {max_common}]）= {len(cluster_terms)}")

    # ── CITATION：backward（miss 引用 seen）/ forward（seen 引用 miss）──
    forward_of_miss = defaultdict(set)   # miss wid -> [seen wid 引用者]
    for wid, m in oa.items():
        if wid in seen_wids:
            for r in m["referenced_works"]:
                if r in oa and r in {x["wid"] for x in misses}:
                    forward_of_miss[r].add(wid)

    per_miss = []
    for r in misses:
        wid = r["wid"]
        m = oa.get(wid, {})
        backward = [w for w in m.get("referenced_works", []) if w in seen_wids]
        forward = sorted(forward_of_miss.get(wid, set()))
        issues = []
        if not m.get("has_record"):
            issues.append("NO_OA_RECORD")
        if not (r.get("doi") or "").strip():
            issues.append("NO_DOI")
        if not (r.get("abstract") or "").strip():
            issues.append("NO_ABSTRACT")
        labels = set()
        if cluster_terms & set(miss_terms.get(wid, [])):
            labels.add("TERM")
        if backward or forward:
            labels.add("CITATION")
        if not labels:
            labels.add("COMMUNITY" if not issues else "INDEX_IDENTITY")
        per_miss.append({
            "wid": wid,
            "title": r.get("oa_title") or r.get("title"),
            "terms": sorted(cluster_terms & set(miss_terms.get(wid, []))),
            "citation": {"backward_seen": len(backward), "forward_seen": len(forward),
                         "backward_wids": backward[:10], "forward_wids": forward[:10]},
            "issues": issues,
            "labels": sorted(labels),
        })

    # ── 聚类：共享辨识词连通分量（仅 2+ gram，共享 ≥2 词才连边——1-gram 泛词会造成链式全连通）──
    strong_of = {wid: {g for g in (cluster_terms & set(t)) if " " in g}
                 for wid, t in miss_terms.items()}

    def _edge(a: set, b: set) -> bool:
        return len(a & b) >= 2
    parent = {r["wid"]: r["wid"] for r in misses}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    wids = [r["wid"] for r in misses]
    for i in range(len(wids)):
        for j in range(i + 1, len(wids)):
            if _edge(strong_of[wids[i]], strong_of[wids[j]]):
                union(wids[i], wids[j])

    clusters = defaultdict(list)
    for wid in wids:
        clusters[find(wid)].append(wid)
    cid_map = {root: f"C{idx}" for idx, root in enumerate(sorted(clusters))}
    for p in per_miss:
        p["cluster_id"] = cid_map[find(p["wid"])]

    # cluster 级 gap 诊断
    cluster_out = []
    for root, members in sorted(clusters.items()):
        shared = Counter()
        for wid in members:
            shared.update(strong_of[wid])
        common = [t for t, c in shared.most_common(8) if c >= 2 or len(members) == 1]
        diagnosis = ("共享术语族: " + " / ".join(common[:6])
                     if common else "孤立 miss（无共享术语，需单篇检查）")
        cluster_out.append({
            "cluster_id": cid_map[root], "size": len(members),
            "members": sorted(members), "shared_terms": common,
            "gap_diagnosis": diagnosis,
        })

    labels_cnt = Counter()
    for p in per_miss:
        for l in p["labels"]:
            labels_cnt[l] += 1

    out = {
        "version": "s3_miss_diagnosis_v1",
        "frozen_at": "2026-08-29",
        "definition": "70 residual miss 四通道证据（TERM/CITATION/COMMUNITY/INDEX_IDENTITY，多标签）",
        "n_misses": len(misses),
        "label_counts": dict(labels_cnt),
        "term_candidates": sorted(cluster_terms),
        "per_miss": per_miss,
        "clusters": cluster_out,
        "existing_vocab_size": len(vocab),
        "note": "TERM 候选词须后续按 MissSupport≥2 或 CitationSupport>0 才进 repair candidate",
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print(f"\n=== 标签分布（多标签）===")
    for k, v in labels_cnt.most_common():
        print(f"  {k:<16} {v}")
    print(f"\n=== 聚类 ===")
    for c in sorted(cluster_out, key=lambda x: -x["size"]):
        print(f"  {c['cluster_id']} size={c['size']:>2} | "
              f"{c['gap_diagnosis'][:60]}")
    print(f"\n[OK] {args.out}")


if __name__ == "__main__":
    main()
