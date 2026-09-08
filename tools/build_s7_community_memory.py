#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/build_s7_community_memory.py — S7.3 execute 结果的文献级 community 构建（2026-09-07）

背景（用户裁决 09-07：2806 new 不做全量 QA → 语义聚类 → 簇级抽样盲评）：
  S7 execute 11 组 Scopus query → new_vs_S6 2806 篇论文。逐篇 LLM QA 太贵且
  community 粒度才能回灌 relation memory（KEEP/FAIL 社区）。本工具：
    rows(7 组 EX + 补跑 EX-10/11) → new_vs_S6 论文 → bge-small-en title+abstract
    语义向量 → KMeans（silhouette 选 k）→ cluster（含 top words / EX 来源 /
    代表标题）→ 每簇 ≤20 抽样 → blind QA corpus（不带 cluster 标注，防确认偏差）

数据缺口处理：
  s7_execute rows 只落盘 title（无 abstract）→ 经 doi 桥接从 Scopus SQLite 缓存
  (data/cache/scopus_cache.db papers 表, paper_id='scopus:'+doi) 恢复 abstract。
  实测恢复率 86.1%（2411/2801 有 doi 行）；无 doi / miss → abstract=None（title-only，
  与 S6 QA 校准 22/70 no-abstract 分层经验一致：missingness 不作 UNCERTAIN 理由）。

纪律：
  - base seen = s6_seen_set.json（S6_SEEN 21782 FROZEN）——只聚 new_vs_S6 论文
    （overlap 211 不重复聚类：已在 S6 检索面内，单独登记）
  - blind：QA corpus 每项仅 {key,title,abstract}，cluster/query/domain 一律不写入
  - 可复现：seed=7（与 R02/R03/R05 审计一致）；embedding 归一化（cosine 语义）
  - 只读：不写 Scopus 缓存、不改 relation memory（那是 P2 的活）

用法：
  .venv\\Scripts\\python.exe tools\\build_s7_community_memory.py \
        [--records data/exports/terminology/s7_execute_query_records.json] \
        [--out data/exports/terminology/s7_community_memory.json] \
        [--corpus data/exports/terminology/s7_community_qa_corpus.json] \
        [--k-range 20,24,28,32,36] [--sample 20] [--seed 7]
"""
import argparse
import datetime
import json
import os
import random
import re
import sqlite3
import sys
from collections import Counter, defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

T = os.path.join(BASE, "data", "exports", "terminology")
DEFAULT_RECORDS = os.path.join(T, "s7_execute_query_records.json")
DEFAULT_OUT = os.path.join(T, "s7_community_memory.json")
DEFAULT_CORPUS = os.path.join(T, "s7_community_qa_corpus.json")
CACHE_DB = os.path.join(BASE, "data", "cache", "scopus_cache.db")
S6_SEEN = os.path.join(T, "s6_seen_set.json")

# title 高频词解释用停用词（材料/文献通用，不去 chemistry 实义词）
STOPS = {
    "the", "a", "an", "of", "for", "and", "or", "in", "on", "to", "with",
    "by", "from", "at", "as", "is", "are", "was", "were", "be", "been",
    "study", "studies", "investigation", "investigations", "using", "based",
    "novel", "new", "effect", "effects", "influence", "role", "toward",
    "into", "during", "after", "between", "over", "under", "via", "their",
    "its", "his", "her", "high", "low", "large", "small", "enhanced",
    "improved", "evaluation", "analysis", "comparison", "properties",
    "property", "performance", "behavior", "behaviour", "materials",
    "material", "application", "applications", "towards", "through",
    "towards", "thermal", "mechanical", "physical", "chemical",
}


def load_json(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def title_words(title):
    """标题 → 小写词 token（用于簇解释 top words）。"""
    t = re.sub(r"[^a-zA-Z0-9\- ]", " ", (title or "").lower())
    return [w for w in t.split() if w not in STOPS and len(w) > 2]


def collect_papers(records_path):
    """records_by_group rows → {key: paper}（跨组去重，记多来源）。
    paper = {key, title, abstract(None 可), source_ex:[gid...], domain,
             layer, doi, has_abstract}"""
    rec = load_json(records_path)
    papers = {}
    for gid, w in rec.get("records_by_group", {}).items():
        for r in w.get("rows", []):
            k = r.get("key")
            if not k:
                continue
            p = papers.setdefault(k, {"key": k, "title": "", "abstract": None,
                                      "doi": None, "source_ex": [],
                                      "domain": w.get("domain"),
                                      "layer": w.get("recall_layer"),
                                      "has_abstract": False})
            if r.get("title") and not p["title"]:
                p["title"] = r["title"]
            p["doi"] = p["doi"] or r.get("doi")
            if gid not in p["source_ex"]:
                p["source_ex"].append(gid)
    return papers


def restore_abstracts(papers):
    """doi 桥接：cache.papers.paper_id == 'scopus:' + doi.lower() → abstract。"""
    doi2key = {}
    for k, p in papers.items():
        d = (p.get("doi") or "").strip().lower()
        if d:
            doi2key.setdefault(d, k)
    if not doi2key:
        return 0
    conn = sqlite3.connect(CACHE_DB)
    got = 0
    # 批量查（chunk 800 防 sqlite 变量上限）
    for i in range(0, len(doi2key), 800):
        chunk = list(doi2key.keys())[i:i + 800]
        marks = ",".join("?" for _ in chunk)
        q = (f"SELECT paper_id, normalized_json FROM papers "
             f"WHERE paper_id IN ({marks})")
        for pid, js in conn.execute(q, ["scopus:" + d for d in chunk]):
            d = pid[len("scopus:"):]
            k = doi2key.get(d)
            if not k:
                continue
            try:
                norm = json.loads(js)
            except Exception:
                continue
            ab = (norm.get("abstract") or "").strip()
            if ab:
                papers[k]["abstract"] = ab
                papers[k]["has_abstract"] = True
                got += 1
    conn.close()
    return got


def embed_texts(papers):
    """bge-small-en-v1.5 向量（title + abstract[:700]），L2 归一化 → ndarray。"""
    from fastembed import TextEmbedding
    model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
    items = sorted(papers.values(), key=lambda p: p["key"])
    texts = []
    for p in items:
        ab = (p.get("abstract") or "").strip()[:700]
        texts.append((p["title"] + (" " + ab if ab else "")).strip() or p["key"])
    vecs = [v for v in model.embed(texts, batch_size=64)]
    # L2 归一化（cosine 语义下 KMeans 用欧氏等价）
    import numpy as np
    X = np.vstack(vecs).astype(np.float32)
    X /= np.linalg.norm(X, axis=1, keepdims=True) + 1e-9
    order = [p["key"] for p in items]
    return X, order


def pick_k(X, k_range):
    """silhouette 选最优 k。"""
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score
    best = None
    for k in k_range:
        km = KMeans(n_clusters=k, n_init=10, random_state=7)
        lab = km.fit_predict(X)
        try:
            s = silhouette_score(X, lab)
        except Exception:
            s = -1.0
        print(f"    k={k:<3} silhouette={s:.4f}")
        if best is None or s > best[1]:
            best = (k, s, km, lab)
    return best


def cluster_and_report(X, order, papers, args):
    k, s, km, lab = pick_k(X, args.k_range)
    print(f"[cluster] k={k} (silhouette={s:.4f})")
    clusters = defaultdict(list)
    for key, cid in zip(order, lab):
        clusters[int(cid)].append(key)
    # 簇解释
    out_clusters = []
    for cid in sorted(clusters):
        keys = clusters[cid]
        src = Counter(p["domain"] for p in
                      (papers[k2] for k2 in keys))
        words = Counter()
        for k2 in keys:
            words.update(title_words(papers[k2]["title"]))
        ex_src = Counter()
        for k2 in keys:
            for g in papers[k2]["source_ex"]:
                ex_src[g] += 1
        exemplars = sorted(keys, key=lambda k2: len(papers[k2].get("abstract") or ""),
                           reverse=True)[:3]
        out_clusters.append({
            "cluster_id": f"C-{cid + 1:03d}",
            "size": len(keys),
            "n_with_abstract": sum(1 for k2 in keys
                                   if papers[k2]["has_abstract"]),
            "domain_src": dict(src.most_common()),
            "ex_sources": dict(ex_src.most_common()),
            "top_words": [w for w, _ in words.most_common(12)],
            "exemplar_titles": [papers[k2]["title"] for k2 in exemplars],
            "papers": sorted(keys),
        })
    return out_clusters


def sample_clusters(clusters, per_cluster, seed):
    """每簇 ≤per_cluster 抽样（seed 固定可复现）。返回 {cluster_id: [keys]}。"""
    rng = random.Random(seed)
    out = {}
    for c in clusters:
        keys = c["papers"]
        n = min(per_cluster, len(keys))
        out[c["cluster_id"]] = rng.sample(keys, n)
    return out


def main():
    ap = argparse.ArgumentParser(description="S7 execute new 论文语义聚类 → 簇级抽样")
    ap.add_argument("--records", default=DEFAULT_RECORDS)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--corpus", default=DEFAULT_CORPUS)
    ap.add_argument("--k-range", default="20,24,28,32,36",
                    help="silhouette 扫描的簇数候选（逗号分隔）")
    ap.add_argument("--sample", type=int, default=20, help="每簇抽样上限")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    args.k_range = [int(x) for x in args.k_range.split(",") if x.strip()]

    s6_keys = set(load_json(S6_SEEN)["keys"])
    papers = collect_papers(args.records)
    # new_vs_S6 过滤
    new_papers = {k: p for k, p in papers.items() if k not in s6_keys}
    overlap = sorted(set(papers) - set(new_papers))
    print("=" * 78)
    print("S7 community discovery（文献级聚类 → 簇级抽样 QA）")
    print("=" * 78)
    print(f"records papers    = {len(papers)}")
    print(f"new_vs_S6         = {len(new_papers)}（参与聚类）")
    print(f"overlap(S6 seen)  = {len(overlap)}（跳过，仅登记）")

    n_abs = restore_abstracts(new_papers)
    print(f"abstract 恢复     = {n_abs}/{len(new_papers)} "
          f"({n_abs / len(new_papers):.1%}，经 doi 桥接 Scopus 缓存)")

    print("[embed] bge-small-en-v1.5 title+abstract ...")
    X, order = embed_texts(new_papers)
    print(f"[embed] X shape = {X.shape}")

    print("[cluster] silhouette 选 k ...")
    clusters = cluster_and_report(X, order, new_papers, args)

    sampling = sample_clusters(clusters, args.sample, args.seed)
    tot_sample = sum(len(v) for v in sampling.values())
    print(f"[sample] 每簇 ≤{args.sample} → 抽样 {tot_sample}/{len(new_papers)}")

    # 写盘 1：community memory（含簇解释，供 analysis/QA 后回灌）
    now = datetime.datetime.now().isoformat(timespec="seconds")
    mem = {
        "schema_version": "1.0",
        "role": "S7_EXECUTE community layer",
        "built_at": now,
        "base_seen": os.path.basename(S6_SEEN),
        "base_seen_size": len(s6_keys),
        "n_papers": len(new_papers),
        "n_with_abstract": n_abs,
        "n_overlap_skipped": len(overlap),
        "embedding": "BAAI/bge-small-en-v1.5 (L2-norm, title+abstract[:700])",
        "cluster_k": len(clusters),
        "sampling": {"per_cluster": args.sample, "total": tot_sample,
                     "seed": args.seed},
        "overlap_keys": overlap,
        "papers": [{"key": p["key"], "title": p["title"],
                    "has_abstract": p["has_abstract"],
                    "source_ex": p["source_ex"], "domain": p["domain"]}
                   for p in new_papers.values()],
        "clusters": clusters,
        "sample_by_cluster": {cid: sorted(v)
                              for cid, v in sampling.items()},
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(mem, f, ensure_ascii=False, indent=1)

    # 写盘 2：blind QA corpus（只含 key/title/abstract，无 cluster 标注）
    corpus_items = []
    for cid, keys in sorted(sampling.items()):
        for k in keys:
            p = new_papers[k]
            corpus_items.append({"key": k,
                                 "title": p["title"],
                                 "abstract": p.get("abstract") or ""})
    corpus = {"role": "S7_EXECUTE community blind QA corpus",
              "built_at": now, "rubric_version": "S6_QA_RUBRIC_V1",
              "n": len(corpus_items),
              "note": "blind：cluster/query/domain 一律不注入（防确认偏差）",
              "papers": corpus_items}
    with open(args.corpus, "w", encoding="utf-8") as f:
        json.dump(corpus, f, ensure_ascii=False, indent=1)

    print(f"\n[OK] community : {args.out}")
    print(f"[OK] qa corpus : {args.corpus}（{len(corpus_items)} 篇，blind）")
    print("\n=== 簇概览（size / domain 来源 / top words）===")
    for c in sorted(clusters, key=lambda x: -x["size"]):
        dom = "/".join(f"{d}:{n}" for d, n in
                       sorted(c["domain_src"].items(),
                              key=lambda x: -x[1])[:3])
        print(f"  {c['cluster_id']} n={c['size']:<4} abs={c['n_with_abstract']:<4} "
              f"| {dom:<28} | {' '.join(c['top_words'][:6])}")
    print(f"\n→ 下一步：run_s7_qa.py --mode run --set {os.path.basename(args.corpus)}"
          f"（blind 三态 QA）→ 簇级 KEEP/FAIL → 回灌 relation memory")


if __name__ == "__main__":
    main()
