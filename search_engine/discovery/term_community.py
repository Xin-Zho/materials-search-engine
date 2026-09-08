"""search_engine/discovery/term_community.py — v2.1 Term Community v1（用户 2026-08-28 定稿）。

输入：220 篇 bridge>=2 的 citation neighbors（title + 可用 abstract）
流程：
  title + abstract → normalize → 2-3 gram → DF filtering →
  term-document binary matrix → document-level co-occurrence →
  NPMI 边权 → NetworkX Greedy Modularity → Term Communities →
  supporting papers → citation support → Registry Novelty → COMMUNITY_CANDIDATE

设计约束（用户冻结）：
  - 纯 Python 2-3 gram，无 NLP 模型依赖（220 篇不需要 spaCy/embedding）
  - DF 过滤：min_df=2，max_df_ratio≈0.55（太泛的 phrase 视为噪声）
  - CLEAN 归一化：polymerisation→polymerization、hyphen→space
    （禁止加入 QGS 学到的新术语表）
  - 边权用 NPMI（不是 raw cooccurrence，避免高频词把网络粘成一团）；
    建边条件 cooccur_docs>=2 AND NPMI>0；document presence 统计
  - 社区检测：networkx.community.greedy_modularity_communities(weight="weight",
    resolution=1.0)——不用连通分量（高频词会把整个图连成一个 component）；
    label propagation 留作 sensitivity check
  - 职责分离：term_community.py = 发现社区（CANDIDATE 门槛 term_count>=3 AND
    paper_count>=3）；是否 PROMOTE 由 community_promoter.py 决定
  - Novelty 用词面：token Jaccard vs query_registry families（不用 embedding）

输出：data/exports/term_communities_v2.json
用法：
  python search_engine/discovery/term_community.py
"""
import argparse
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE)

BRIDGE_PATH = os.path.join(BASE, "data", "exports", "citation_bridge_v1.json")
ENRICHED_PATH = os.path.join(BASE, "data", "cache", "openalex_neighbors_enriched.json")
REGISTRY_PATH = os.path.join(BASE, "data", "query_registry_v2.json")
OUT_PATH = os.path.join(BASE, "data", "exports", "term_communities_v2.json")

MIN_DF = 2
MAX_DF_RATIO = 0.55
COOCCUR_MIN = 2          # 建边最小共现论文数
NPMI_MIN = 0.0           # 建边最小 NPMI（sensitivity: 0.0/0.1/0.2）
RESOLUTION = 1.0
CAND_TERM_MIN = 3        # COMMUNITY_CANDIDATE 门槛
CAND_PAPER_MIN = 3
NOVELTY_N = 15           # novelty 代表词数量（top-N weighted degree）
BOUNDARY_NOVELTY_MIN = 0.7   # BOUNDARY_TERM_SIGNAL 的 novelty 下限（high）

STOPWORDS = set("""
a an the of and or in on at for to with from by as is are was were be been being
this that these those it its their our your his her we you they them
study studies results result effect effects method methods approach used using
use show shows shown found demonstrate demonstrates demonstrated investigate
investigation investigated examine examined influence influences influenced
analysis analyses analyze analyzed paper papers article journal work data
figure fig table may can could would will should also thus however although
compared comparison compare relative vs versus new novel different various
does did was were than more less most least significantly significant
affected increased decreased higher lower showed found indicates indicated
seen observed reported related concerning regarding between among during
""".split())

CLEAN_MAP = {
    "polymerisation": "polymerization",
    "polymerisations": "polymerizations",
    "polymerised": "polymerized",
    "polymerisation": "polymerization",
    "photopolymerisation": "photopolymerization",
}

TOKEN_RE = re.compile(r"[a-z][a-z0-9]{1,}")


def normalize_text(text: str) -> str:
    t = (text or "").lower()
    t = re.sub(r"-", " ", t)          # hyphen → space（photo-polymerised → photo polymerised）
    t = re.sub(r"[^a-z0-9\s]", " ", t)  # 标点 → space
    for src, dst in CLEAN_MAP.items():
        t = t.replace(src, dst)
    return t


def tokenize(text: str) -> list[str]:
    """清洗 tokenize（不过滤 stopword——位置保留，n-gram 阶段再过滤）。"""
    return [w for w in TOKEN_RE.findall(text)
            if len(w) >= 3 and not w.isdigit()]


def keep_ngram(tokens: list[str]) -> bool:
    """generic linguistic phrase filter（用户 2026-08-28 定）：
    ① 首尾 token 不能是 stopword（中间可含：degree of conversion）
    ② content tokens（非 stopword）≥ 2（does not → 0 → drop）
    """
    if tokens[0] in STOPWORDS or tokens[-1] in STOPWORDS:
        return False
    content = sum(1 for t in tokens if t not in STOPWORDS)
    return content >= 2


def extract_ngrams(tokens: list[str]) -> set[str]:
    """2-gram + 3-gram + generic linguistic filtering（不跨句）。"""
    out = set()
    for n in (2, 3):
        for i in range(len(tokens) - n + 1):
            gram = tokens[i:i + n]
            if keep_ngram(gram):
                out.add(" ".join(gram))
    return out


def token_jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def load_input_papers(bridge_path: str = BRIDGE_PATH,
                      enriched_path: str = ENRICHED_PATH) -> list[dict]:
    """bridge>=2 candidates + enriched 元数据 + bridge class。

    bridge_path/enriched_path 可参数化（v1 = citation_bridge_v1.json + 850
    enriched；hop2 = citation_bridge_hop2.json + 818 enriched）。
    """
    bridge = json.load(open(bridge_path, encoding="utf-8"))
    enriched = {}
    if os.path.exists(enriched_path):
        enriched = json.load(open(enriched_path, encoding="utf-8"))
    papers = []
    for c in bridge["candidates"]:
        if c["bridge_count"] < 2:
            continue
        e = enriched.get(c["wid"], {})
        if not e.get("title"):
            continue
        abstract = e.get("abstract") or ""
        papers.append({
            "wid": c["wid"],
            "title": e["title"],
            "abstract": abstract,
            "text_source": "TITLE_ABSTRACT" if abstract else "TITLE_ONLY",
            "bridge_count": c["bridge_count"],
            "citing_seeds": c.get("citing_seed_ids") or c.get("parent_papers") or [],
            "bridge_class": c.get("class", "NEW_NEIGHBOR"),  # ALREADY_RELEVANT/RETRIEVED/NEW
        })
    return papers


def load_registry_families() -> list[dict]:
    """registry families → {family_id, terms(token 集), phrase 列表}。"""
    reg = json.load(open(REGISTRY_PATH, encoding="utf-8"))
    fams = []
    for f in reg["families"]:
        phrase_tokens: set[str] = set()
        phrases: set[str] = set()
        for q in f.get("generated_queries", []):
            for m in re.findall(r'"([^"]+)"', q):
                phrases.add(m)
                phrase_tokens.update(tokenize(normalize_text(m)))
        for v in (f.get("query_variants") or []):
            if isinstance(v, str):
                phrase_tokens.update(tokenize(normalize_text(v)))
        fams.append({"family_id": f["family_id"], "tokens": phrase_tokens,
                     "phrases": phrases})
    return fams


class TermCommunity:
    def __init__(self, npmi_min: float = NPMI_MIN,
                 bridge_path: str = BRIDGE_PATH,
                 enriched_path: str = ENRICHED_PATH,
                 out_path: str = OUT_PATH):
        self.npmi_min = npmi_min
        self.bridge_path = bridge_path
        self.enriched_path = enriched_path
        self.out_path = out_path
        self.papers: list[dict] = []
        self.phrases: list[str] = []
        self.df: dict[str, int] = {}
        self.matrix: dict[str, set[int]] = {}   # phrase -> doc indices
        self.graph = None
        self.families = load_registry_families()

    # ── STEP 1: phrase extraction + DF 过滤 ──
    def extract(self) -> None:
        doc_phrases: list[set[str]] = []
        for p in self.papers:
            text = normalize_text(p["title"]) + " " + normalize_text(p["abstract"])
            doc_phrases.append(extract_ngrams(tokenize(text)))
        n = len(doc_phrases)
        df = Counter()
        for ph in doc_phrases:
            for g in ph:
                df[g] += 1
        max_df = int(n * MAX_DF_RATIO)
        keep = [g for g, c in df.items() if MIN_DF <= c <= max_df]
        keep.sort(key=lambda g: (-df[g], g))
        self.phrases = keep
        self.df = {g: df[g] for g in keep}
        self.matrix = {g: {i for i, ph in enumerate(doc_phrases) if g in ph}
                       for g in keep}

    # ── STEP 2: NPMI 图 ──
    def build_graph(self):
        import networkx as nx
        n = len(self.papers)
        G = nx.Graph()
        G.add_nodes_from(self.phrases)
        phrases = self.phrases
        mat = self.matrix
        for i in range(len(phrases)):
            gi = phrases[i]
            di = mat[gi]
            for j in range(i + 1, len(phrases)):
                gj = phrases[j]
                co = len(di & mat[gj])
                if co < COOCCUR_MIN:
                    continue
                pij = co / n
                pi = len(di) / n
                pj = len(mat[gj]) / n
                if pij <= 0 or pi <= 0 or pj <= 0:
                    continue
                pmi = math.log(pij / (pi * pj))
                if pmi <= 0:
                    continue
                npmi = pmi / (-math.log(pij))
                if npmi <= self.npmi_min:
                    continue
                G.add_edge(gi, gj, weight=npmi)
        self.graph = G

    # ── STEP 3: Greedy Modularity ──
    def detect(self) -> list[list[str]]:
        import networkx as nx
        if self.graph.number_of_edges() == 0:
            return []
        comms = nx.community.greedy_modularity_communities(
            self.graph, weight="weight", resolution=RESOLUTION)
        return [sorted(c, key=lambda g: (-self.df[g], g)) for c in comms]

    # ── STEP 4: novelty vs registry（token Jaccard）──
    def registry_novelty(self, terms: list[str]) -> dict:
        t_tokens = set()
        for t in terms:
            t_tokens.update(tokenize(normalize_text(t)))
        best_f = None
        best_j = 0.0
        for f in self.families:
            j = token_jaccard(t_tokens, f["tokens"])
            if j > best_j:
                best_j = j
                best_f = f["family_id"]
        return {"score": round(1 - best_j, 4),
                "closest_family": best_f,
                "closest_similarity": round(best_j, 4)}

    # ── 代表词排名：社区内部 weighted degree（NPMI 边权和，非全局 DF）──
    def _inner_weighted_degree(self, community_terms: list[str], term: str) -> float:
        s = 0.0
        for v in community_terms:
            if self.graph.has_edge(term, v):
                s += self.graph[term][v]["weight"]
        return s

    # ── 组装 communities ──
    def assemble(self, comms: list[list[str]]) -> list[dict]:
        out = []
        for ci, terms in enumerate(comms, 1):
            # 代表词：内部 weighted degree desc（1-term 社区无内部边 → 用 df）
            deg = {t: self._inner_weighted_degree(terms, t) for t in terms}
            terms_sorted = sorted(terms, key=lambda t: (-deg[t], -self.df[t], t))
            top_terms = terms_sorted[:NOVELTY_N]
            term_set = set(terms)
            support_docs = set()
            for t in terms:
                support_docs |= self.matrix[t]
            support_papers = [self.papers[i] for i in sorted(support_docs)]
            # 内部边 NPMI 均值
            inner_weights = []
            for u in terms:
                for v in terms:
                    if u < v and self.graph.has_edge(u, v):
                        inner_weights.append(self.graph[u][v]["weight"])
            mean_npmi = sum(inner_weights) / len(inner_weights) if inner_weights else 0.0
            bc = [p["bridge_count"] for p in support_papers]
            n_abs = sum(1 for p in support_papers if p["text_source"] == "TITLE_ABSTRACT")
            nov = self.registry_novelty(top_terms)   # top-N 代表词
            paper_count = len(support_papers)
            # ExistingCoverage（用户定，比 lexical novelty 更贴近目标）：
            # 支撑论文中已被现有检索覆盖（ALREADY_RETRIEVED / ALREADY_RELEVANT）的比例
            n_covered = sum(1 for p in support_papers
                            if p["bridge_class"] in ("ALREADY_RETRIEVED",
                                                     "ALREADY_RELEVANT"))
            existing_coverage = n_covered / paper_count if paper_count else 0.0
            retrieval_novelty = 1 - existing_coverage
            if len(terms) >= CAND_TERM_MIN and paper_count >= CAND_PAPER_MIN:
                status = "COMMUNITY_CANDIDATE"
            elif (len(terms) == 1 and paper_count >= CAND_PAPER_MIN
                  and nov["score"] >= BOUNDARY_NOVELTY_MIN):
                status = "BOUNDARY_TERM_SIGNAL"      # 单 term 但多论文支持 + novelty high
            else:
                status = "BELOW_THRESHOLD"
            # 描述性 role（与 status 分开；用户 2026-08-28 定）：
            # CORE_COMMUNITY = dental 本体（paper 占比 ≥30%），不晋升不拆
            # NOVEL_COMMUNITY_CANDIDATE = 边界且 lexical novelty 高
            # BOUNDARY_COMMUNITY = 边界（≥3 terms & ≥3 papers）
            # BOUNDARY_TERM_SIGNAL = 单 term 信号；NOISE_SMALL_CLUSTER = 其余
            input_n = max(len(self.papers), 1)
            if paper_count / input_n >= 0.30 and len(terms) >= 10:
                role = "CORE_COMMUNITY"
            elif len(terms) >= CAND_TERM_MIN and paper_count >= CAND_PAPER_MIN:
                role = ("NOVEL_COMMUNITY_CANDIDATE" if nov["score"] >= 0.90
                        else "BOUNDARY_COMMUNITY")
            elif status == "BOUNDARY_TERM_SIGNAL":
                role = "BOUNDARY_TERM_SIGNAL"
            else:
                role = "NOISE_SMALL_CLUSTER"
            out.append({
                "community_id": f"TC_{ci:03d}",
                "role": role,
                "status": status,
                "terms": terms_sorted,
                "top_terms_used": top_terms,
                "supporting_papers": [p["wid"] for p in support_papers],
                "paper_count": paper_count,
                "term_count": len(terms),
                "citation_support": {
                    "mean_bridge_count": round(sum(bc) / len(bc), 2) if bc else 0,
                    "max_bridge_count": max(bc) if bc else 0,
                },
                "term_coherence": {"mean_npmi": round(mean_npmi, 4)},
                "novelty": nov,
                "existing_coverage": round(existing_coverage, 3),
                "retrieval_novelty": round(retrieval_novelty, 3),
                "abstract_coverage": round(n_abs / paper_count, 3) if paper_count else 0,
                "qgs_leakage": False,
            })
        out.sort(key=lambda c: (-(c["status"] == "COMMUNITY_CANDIDATE"),
                                -(c["status"] == "BOUNDARY_TERM_SIGNAL"),
                                -c["citation_support"]["max_bridge_count"]))
        return out

    # ── 主流程 ──
    def run(self) -> dict:
        self.papers = load_input_papers(self.bridge_path, self.enriched_path)
        self.extract()
        self.build_graph()
        comms = self.detect()
        communities = self.assemble(comms)
        n_abs = sum(1 for p in self.papers if p["text_source"] == "TITLE_ABSTRACT")
        return {
            "stats": {
                "input_papers": len(self.papers),
                "papers_with_title": len(self.papers),
                "papers_with_abstract": n_abs,
                "candidate_phrases": len(self.phrases),
                "graph_nodes": self.graph.number_of_nodes() if self.graph else 0,
                "graph_edges": self.graph.number_of_edges() if self.graph else 0,
                "communities": len(communities),
                "candidates": sum(1 for c in communities
                                  if c["status"] == "COMMUNITY_CANDIDATE"),
                "boundary_signals": sum(1 for c in communities
                                        if c["status"] == "BOUNDARY_TERM_SIGNAL"),
            },
            "communities": communities,
        }

    # ── sensitivity sweep：NPMI_MIN 0.0/0.1/0.2 ──
    def sweep(self) -> list[dict]:
        rows = []
        for npmi in (0.0, 0.1, 0.2):
            self.npmi_min = npmi
            r = self.run()
            s = r["stats"]
            comms = r["communities"]
            largest = max(comms, key=lambda c: c["term_count"], default=None)
            singletons = sum(1 for c in comms if c["term_count"] == 1)
            mean_coherence = (sum(c["term_coherence"]["mean_npmi"] for c in comms)
                              / len(comms) if comms else 0)
            rows.append({
                "npmi_min": npmi,
                "graph_nodes": s["graph_nodes"],
                "graph_edges": s["graph_edges"],
                "communities": s["communities"],
                "largest_terms": largest["term_count"] if largest else 0,
                "largest_papers": largest["paper_count"] if largest else 0,
                "candidates": s["candidates"],
                "boundary_signals": s["boundary_signals"],
                "singletons": singletons,
                "mean_coherence": round(mean_coherence, 4),
                "largest_ratio": round(
                    (largest["term_count"] / s["graph_nodes"]) if largest and s["graph_nodes"] else 0, 4),
            })
        return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--npmi", type=float, default=NPMI_MIN,
                    help=f"建边最小 NPMI（默认 {NPMI_MIN}）")
    ap.add_argument("--sweep", action="store_true",
                    help="NPMI_MIN = 0.0/0.1/0.2 sensitivity table（其他参数 frozen）")
    ap.add_argument("--bridge", default=BRIDGE_PATH,
                    help="bridge JSON（默认 citation_bridge_v1.json；hop2 传 citation_bridge_hop2.json）")
    ap.add_argument("--enriched", default=ENRICHED_PATH,
                    help="enrichment 缓存（默认 openalex_neighbors_enriched.json）")
    ap.add_argument("--out", default=OUT_PATH,
                    help="输出 JSON（默认 term_communities_v2.json）")
    args = ap.parse_args()

    if args.sweep:
        tc = TermCommunity(bridge_path=args.bridge, enriched_path=args.enriched,
                           out_path=args.out)
        rows = tc.sweep()
        print("=" * 96)
        print("NPMI_MIN sensitivity（max_df_ratio=0.55, min_df=2, cooccur>=2 frozen）")
        print("=" * 96)
        hdr = (f"{'npmi':>5} {'nodes':>6} {'edges':>7} {'comms':>6} "
               f"{'largestT':>8} {'largestP':>8} {'CAND':>5} {'BOUND':>6} "
               f"{'singl':>6} {'meanNPMI':>8} {'LCR':>6}")
        print(hdr)
        for r in rows:
            print(f"{r['npmi_min']:>5.1f} {r['graph_nodes']:>6} {r['graph_edges']:>7} "
                  f"{r['communities']:>6} {r['largest_terms']:>8} {r['largest_papers']:>8} "
                  f"{r['candidates']:>5} {r['boundary_signals']:>6} "
                  f"{r['singletons']:>6} {r['mean_coherence']:>8.4f} {r['largest_ratio']:>6.3f}")
        print("\nLCR = LargestCommunityRatio（最大社区 terms / graph nodes），期望 0.1 后明显下降")
        return

    tc = TermCommunity(npmi_min=args.npmi, bridge_path=args.bridge,
                       enriched_path=args.enriched, out_path=args.out)
    result = tc.run()
    s = result["stats"]
    print("=" * 70)
    print(f"Term Community v1.2（NPMI_MIN={args.npmi}, max_df=0.55, novelty top-{NOVELTY_N}）")
    print(f"  输入: {os.path.basename(args.bridge)} + {os.path.basename(args.enriched)}")
    print("=" * 70)
    print(f"input_papers={s['input_papers']} | abstract={s['papers_with_abstract']} "
          f"({s['papers_with_abstract']/max(s['input_papers'],1)*100:.0f}%)")
    print(f"candidate_phrases={s['candidate_phrases']} | graph_nodes={s['graph_nodes']} "
          f"edges={s['graph_edges']} | communities={s['communities']} "
          f"(CANDIDATE={s['candidates']}, BOUNDARY={s['boundary_signals']})")
    print("\n有效对象（console 只显示 CANDIDATE + BOUNDARY；JSON 保留全部）:")
    for c in result["communities"]:
        if c["status"] == "BELOW_THRESHOLD":
            continue
        tag = "★" if c["status"] == "COMMUNITY_CANDIDATE" else "◇"
        print(f"{tag} {c['community_id']} [{c['status']}] "
              f"terms={c['term_count']} papers={c['paper_count']} "
              f"bridge_mean={c['citation_support']['mean_bridge_count']} "
              f"max={c['citation_support']['max_bridge_count']} "
              f"npmi={c['term_coherence']['mean_npmi']} "
              f"novelty={c['novelty']['score']} "
              f"(closest={c['novelty']['closest_family']} "
              f"sim={c['novelty']['closest_similarity']}) "
              f"abs_cov={c['abstract_coverage']}")
        print(f"    top: {', '.join(c['top_terms_used'][:8])}")
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print(f"\n✓ 已写: {args.out}")


if __name__ == "__main__":
    main()
