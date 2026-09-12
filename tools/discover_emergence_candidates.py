"""P4-2 v2：从**知识树演化**中算法化地发现候选方向（LLM 不参与预测）。

架构（用户 2026-09-12 裁定）：

    Historical papers
          |
    Knowledge Extractor          <- P4-1（concept + relation，已完成）
          |
    Knowledge Tree (<=2020)      <- 本工具：节点层 + 边层（typed 关系边 / 共现边）
          |
    Graph Evolution + Statistics <- **纯算法**，无 LLM
          |
    Emerging Direction Candidates
          |
          +--> P4-3 Future Validation (2021-2025)
          +--> LLM Analyst（**只解释**，不预测）

为什么不用 LLM 产生候选：LLM 回答问题「哪些方向有潜力」时会引入**自己的先验**
（训练语料里的热门话题、对领域的既有认知）。那样得到的不是「从这份知识树里
读出来的信号」，而是「模型的印象」，且无法归因、无法复现、无法证伪。
算法化候选的每一步都可以被逐行审计，因此也能被未来的论文证伪。

四类信号（用户定义），本工具把前四者做成候选的**特征向量**而不是互斥类别：

  (1) 节点增长     NODE 候选：节点的按期份额增长 + 加速度 + 首现新近度
  (2) 新边形成     PAIR 特征 `new_edge`：该连接首次出现的年份足够近
  (3) 跨域连接     PAIR 特征 `cross_domain`：两端端点的主题集合互不重叠
  (4) 知识缺口     PAIR_GAP 候选：**尚未共现**但链接预测得分高的概念对
                   （「应该连起来但还没连」= 结构性缺口，可直接被 P4-3 证伪）

── 边层的两种粒度（重要的实现决定）──────────────────────────────────
P4-1 抽出的 typed 关系（source, relation, target）语义清晰，但**极度稀疏**：
7,441 条关系只构成 7,322 个不同三元组，其中出现次数 >=2 的仅 ~102 个，
最大支撑度 4。直接用它做时序信号没有统计质量。

因此边层以**共现对**为基础（两个概念出现在同一篇论文里），typed 关系作为
**语义标注**与**候选质量门**：
  * 共现对 distinct 50,798，support>=2 有 1,323 个，support>=3 有 226 个
  * 其中 3,519 对同时有 typed 关系支撑 -> 标记 edge_grade=TYPED
这不是让步：本工具要检测的是「两个概念之间的连接是否新近形成/增长」，
共现是这一事件的直接观测；typed 关系回答的是「这条连接的语义是什么」。
两者都保留，并在候选记录里分别给出。

── 反泄漏 ────────────────────────────────────────────────────────────
1. 只吃 year <= CUTOFF_YEAR(2020) 的论文，越界即 SystemExit（不是警告）；
2. **不使用任何引用量**（citation_count / fwci / percentile 一律不读源码里也不出现）；
   增长率完全由「按期份额」计算，份额用采样期论文数归一化。
"""
from __future__ import annotations

import argparse
import collections
import datetime
import hashlib
import itertools
import json
import math
import os
import re
import sqlite3
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DATASET_DIR = os.path.join(BASE, "datasets", "photopolymerization_v1")
CONCEPTS_PATH = os.path.join(DATASET_DIR, "concepts_v1.jsonl")
DB_PATH = os.path.join(DATASET_DIR, "paper_meta.db")
OUT_JSON = os.path.join(DATASET_DIR, "emergence_candidates_v2.json")
OUT_CSV = os.path.join(DATASET_DIR, "emergence_candidates_v2.csv")

PREDICTOR_VERSION = "p4_2v2"
CUTOFF_YEAR = 2020
EARLY = (2011, 2015)
LATE = (2016, 2020)

LAPLACE = 0.5                    # 期份额的加性平滑（与 v1 同口径）
NEW_EDGE_SINCE = 2017            # 「新边」的起始年：>= 此年首次出现的连接
NODE_MIN_SUPPORT = 3             # 参与配对的端点至少出现在 3 篇抽取论文里
PAIR_MIN_SUPPORT = 2             # 共现对的支撑度门槛（support=1 是单篇偶发）
GAP_ENDPOINT_MIN_SUPPORT = 8     # 缺口候选的端点要足够成熟（否则缺口无意义）
GAP_MIN_COMMON_NEIGHBORS = 2     # 缺口候选至少要有 2 个共同邻居（链路预测依据）
GAP_MAX_CANDIDATES = 300
PAIR_MAX_CANDIDATES = 600
EVIDENCE_MAX = 6

# 至少一端属这三类（或该对有 typed 关系支撑）才进入候选 —— **构造性依据**，
# 不是"黑名单"：本工具要找的是「研究方向/未解决问题/应用」层面的演化。
# v1 已验证的教训：自由的类型混合会让表征手段（SEM / tensile testing /
# finite element method）因论文数增长而挤满榜单，而它们不是方向。
PAIR_SEMANTIC_TYPES = ("direction", "challenge", "application")
# NODE 候选的预测口径（v1 同样的理由：跨类型不可比）。
# ⚠️ P4-3 实测：challenge 型候选 lift 0.750（同层随机节点 0.491），说明
# 「按规模找问题」找到的是**长期存在的大问题** -> NODE 打分改为知识树位置。
PREDICTION_TYPES = ("direction", "challenge")

# ── NODE：知识树位置打分（用户 2026-09-12 纠正）──────────────────────
# growth 压到 0.05（用户明确「w5 -> 0」）；四个结构信号占 0.95。
NODE_WEIGHTS = {
    "structural_novelty": 0.25,
    "bridge": 0.25,
    "problem": 0.25,
    "combination": 0.20,
    "growth_weak": 0.05,
}
# 分层：规模决定**在哪个组里竞争**，结构决定组内排名（跨层不可比）
NODE_STRATUM_SMALL = "small"
NODE_STRATUM_MID = "mid"
NODE_STRATUM_LARGE = "large"
NODE_SMALL_MAX_DEG = 3
NODE_MID_MAX_DEG = 7
# 结构量的**可测量下限**（不是规模奖励）：孤点/单边节点上参与系数与中介度无定义
NODE_MIN_DEGREE = 2
# 被可测量下限挡掉的计数（构建时填充，写进产物）
EXCLUDED = collections.Counter()

# ── 「表征/测量手段」标记（**可评审规则**，非隐藏黑名单）────────────────
# 为什么需要它：`dynamic mechanical analysis` / `scanning electron microscopy`
# 这类概念的**中介度天生很高** —— 每篇论文都要报告测量，所以它们与谁都共现。
# 这与「枢纽概念」是同一个病理，但**按度数分层挡不住**（DMA 实测 deg=7 落在 mid 层，
# 中介度仍 0.57）。按词面模式识别，规则写进产物供评审；被它排除的候选全部列出。
NODE_METHOD_LIKE_RE = re.compile(
    r"analy[sz]|spectroscop|microscop|calorimet|diffractomet|chromatograph"
    r"|rheomet|ellipsomet|tomograph|\btests?\b|\btesting\b|measurement"
    r"|characteri[sz]ation|\bftir\b|\bnmr\b|\bsem\b|\bdma\b|\bxrd\b",
    re.I)


def is_method_like(name):
    return bool(NODE_METHOD_LIKE_RE.search(name or ""))

# 主榜定义：small/mid 层 **且** 非表征手段。类型不再受限 ——
# v1 限制 `direction|challenge` 的理由是**规模类特征**跨类型不可比；
# 现在特征是结构量且已按层排名，类型不再是混入噪声的通道。
NODE_PREDICTION_STRATA = (NODE_STRATUM_SMALL, NODE_STRATUM_MID)

# ── 结构打分的**阳性对照**（度量方向校验，不是候选）────────────────────
# P4-3 实测：新口径候选在「未来伙伴广度」判据上 lift 1.045（p=0.65）—— NULL。
# 这时有两种完全不同的解释：①特征方向错了 ②数据太薄。
# 判别办法与词面尺子一样：拿一组**已知在 2020 前后打开新连接**的概念，
# 看它们在结构打分里排在哪。若它们也不靠前 -> 操作化本身错了（不是功效问题）。
# ⚠️ 这是**事后构造**的对照（用了 2020 后的知识），只用于校验打分方向，
#    不构成方法有效性的证据，也不参与任何候选筛选。
STRUCTURAL_POSITIVE_CONTROL = (
    "pet-raft polymerization", "photoinduced electron transfer reversible addition",
    "digital light processing", "vat photopolymerization", "4d printing",
    "two-photon polymerization", "front photopolymerization",
    "frontal photopolymerization", "thiol-ene click chemistry",
    "continuous liquid interface production", "bioprinting",
    "machine learning", "low shrinkage", "anisotropic shrinkage",
)

WEIGHTS = {
    "growth": 0.20,
    "new_edge": 0.20,
    # 关联强度的**变化**（ΔPMI）：枢纽概念（photoinitiator / resin / monomer）
    # 与任何概念的新配对都"看起来是新的"，但它们的 PMI 很低（共现并不意外）。
    # 用 ΔPMI 替代"连通度"作为加权特征，是因为连通度**奖励枢纽** ——
    # 实测未替换时 Top-20 被 `photoinitiator + 组织工程` 这类枢纽配对占据。
    "assoc_growth": 0.25,
    "cross_domain": 0.15,
    "gap": 0.20,
}

ST_OK = "OK"
_TOK = re.compile(r"[a-z0-9]{3,}")
_VARIANT_STOP = {"of", "the", "and", "in", "for", "a", "an", "with", "on", "to"}


def _rel(path) -> str:
    """展示用相对路径（⚠️ Windows 上 relpath 跨盘符抛 ValueError，必须兜底）。"""
    try:
        return os.path.relpath(path, BASE).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _cand_id(kind, *parts) -> str:
    raw = "|".join([kind, *[str(p) for p in parts]])
    return kind[:1].lower() + "_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


# ══ 载入与反泄漏 ═════════════════════════════════════════════════════════
def load_inputs(db_path=DB_PATH, concepts_path=CONCEPTS_PATH):
    rows = []
    with open(concepts_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    ok = [r for r in rows if r.get("status") == ST_OK]

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        meta = {r["paper_uid"]: {"year": r["year"], "title": r["title"],
                                 "abstract": r["abstract"],
                                 "primary_topic": r["primary_topic"]}
                for r in con.execute(
                    "SELECT paper_uid, year, title, abstract, primary_topic "
                    "FROM paper_meta")}
    finally:
        con.close()
    return ok, meta


def assert_no_leakage(meta, ok_rows):
    """预测窗口的论文/概念一律不得进入候选发现。越界即硬失败。"""
    years = [meta[r["paper_uid"]]["year"] for r in ok_rows
             if meta.get(r["paper_uid"]) and meta[r["paper_uid"]]["year"]]
    if not years:
        raise SystemExit("[refused] 抽取结果里没有带年份的论文")
    bad = [r["paper_uid"] for r in ok_rows
           if (meta.get(r["paper_uid"]) or {}).get("year", 0) > CUTOFF_YEAR]
    if bad:
        raise SystemExit(
            f"[refused] 发现 {len(bad)} 篇 year > {CUTOFF_YEAR} 的抽取结果："
            f"{bad[:3]} —— 候选发现只允许使用截断年及以前的信息")
    unknown_split = sorted({r["paper_uid"] for r in ok_rows} - set(meta))
    if unknown_split:
        raise SystemExit(f"[refused] 抽取结果里有不在 paper_meta 的论文："
                         f"{unknown_split[:3]}")
    return {"max_year_seen": max(years), "n_papers": len(years),
            "assert_no_future_in_train": max(years) <= CUTOFF_YEAR}


# ══ 知识树 ═══════════════════════════════════════════════════════════════
def build_tree(ok_rows, meta):
    """建时序知识树。

    返回 ``(nodes, typed, pairs, periods)``：
      nodes[name]  = {type, support, years, topics, first_seen}
      typed[(a,b)] = {"relations": Counter, "years": Counter, "papers": [...]}
      pairs[(a,b)] = {"years": Counter, "papers": [...]}   # 共现（无向、去类型）
    """
    nodes = {}
    typed = collections.defaultdict(
        lambda: {"relations": collections.Counter(), "years": collections.Counter(),
                 "papers": []})
    pairs = collections.defaultdict(
        lambda: {"years": collections.Counter(), "papers": []})
    node_papers = collections.defaultdict(list)     # name -> [(year, uid)]
    periods = collections.Counter()

    # 变体归并只用于**诊断**（用户未裁定 canonical map）。这里先按原名建图，
    # 机械变体（词集相同）的簇数会一并报出，供决定是否需要归并资产。
    variant_groups = collections.defaultdict(set)

    for rec in ok_rows:
        uid = rec["paper_uid"]
        m = meta[uid]
        year = m["year"]
        topic = m["primary_topic"]
        if year is None:
            continue
        if EARLY[0] <= year <= EARLY[1]:
            periods["early"] += 1
        elif LATE[0] <= year <= LATE[1]:
            periods["late"] += 1
        names = sorted({c["name"] for c in rec["concepts"]
                        if c.get("name") and c.get("type")})
        type_of = {c["name"]: c["type"] for c in rec["concepts"]
                   if c.get("name") and c.get("type")}
        for n in names:
            nd = nodes.get(n)
            if nd is None:
                nd = nodes[n] = {"type": type_of[n], "support": 0,
                                 "years": collections.Counter(),
                                 "topics": collections.Counter(),
                                 "first_seen": year}
            nd["support"] += 1
            nd["years"][year] += 1
            if topic:
                nd["topics"][topic] += 1
            nd["first_seen"] = min(nd["first_seen"], year)
            node_papers[n].append((year, uid))
            variant_groups[" ".join(sorted(
                t for t in _TOK.findall(n.lower())
                if t not in _VARIANT_STOP))].add(n)

        for a, b in itertools.combinations(names, 2):
            p = pairs[(a, b)]
            p["years"][year] += 1
            if len(p["papers"]) < EVIDENCE_MAX:
                p["papers"].append((year, uid))
        for x in rec.get("relations") or []:
            s, t, r = x.get("source"), x.get("target"), x.get("relation")
            if not s or not t or s == t:
                continue
            key = (s, t) if s <= t else (t, s)
            if key not in pairs:      # 端点未在概念表里（理论上被校验层挡住）
                continue
            te = typed[key]
            te["relations"][r or "other"] += 1
            te["years"][year] += 1
            if len(te["papers"]) < EVIDENCE_MAX:
                te["papers"].append((year, uid))

    multi = {k: v for k, v in variant_groups.items() if len(v) > 1}
    # 共现图邻接表（只收 support>=PAIR_MIN_SUPPORT 的对）——一次建好，
    # 供度中心性/链路预测复用，避免 O(候选 × 边数) 的重复扫描。
    pair_adj = collections.defaultdict(set)
    for (a, b), p in pairs.items():
        if sum(p["years"].values()) >= PAIR_MIN_SUPPORT:
            pair_adj[a].add(b)
            pair_adj[b].add(a)
    diag = {
        "nodes": len(nodes),
        "typed_pairs": len(typed),
        "cooccur_pairs": len(pairs),
        "pairs_support_ge_2": sum(1 for p in pairs.values()
                                  if sum(p["years"].values()) >= 2),
        "variant_clusters_mechanical": len(multi),
        "variant_concepts_involved": sum(len(v) for v in multi.values()),
        "period_papers": dict(periods),
    }
    return nodes, typed, pairs, node_papers, pair_adj, diag


def _node_evidence(name, node_papers, meta, per_side=3):
    """节点候选的证据包：早期与晚期各取若干篇（含原文片段）。

    两侧都取 —— 节点增长本身就是「早期 vs 晚期」的对比，只给晚期证据会让
    解释层无法讨论变化过程。
    """
    if not node_papers or not meta:
        return []
    rows = [(y, u) for y, u in (node_papers.get(name) or []) if y]
    early = sorted([r for r in rows if EARLY[0] <= r[0] <= EARLY[1]])[:per_side]
    late = sorted([r for r in rows if LATE[0] <= r[0] <= LATE[1]],
                  reverse=True)[:per_side]
    out = []
    for y, u in early + late:
        m = meta.get(u) or {}
        out.append({"paper_uid": u, "year": y,
                    "title": (m.get("title") or "")[:220],
                    "period": "early" if y <= EARLY[1] else "late",
                    "snippet": _snippet(m.get("abstract"), name)})
    return out


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _conf(df_pair, df_endpoint):
    """关联规则的**置信度**：``P(b | a)``（带 Laplace 平滑）。

    分子分母都是同量级的计数，Laplace 的偏置会相互抵消 ——
    这正是它比 PMI 更适合「变化量」的原因（见 ``_assoc_growth``）。
    """
    return (df_pair + LAPLACE) / (df_endpoint + 1.0)


def _assoc_growth(de, dl, da_e, da_l, db_e, db_l):
    """关联强度的**变化**（对称平均的 Δconf）。

    定义：``0.5 * [ (P(b|a)_late - P(b|a)_early) + (P(a|b)_late - P(a|b)_early) ]``

    ⚠️ 为什么不用 ΔPMI（我最初的实现，被自己的测试推翻）：
    PMI = log2[ p(a,b) / (p(a)·p(b)) ] 的分母里，**边际概率**在概念罕见时会落到
    Laplace 地板 (0.5/n)。于是「早期两个端点都还没出现」的新兴配对会得到一个
    **虚高的早期 PMI**，ΔPMI 变成大负数 —— 特征系统性地**惩罚新兴连接**，
    与它的文档说明正好相反（实测：特异新配对 ΔPMI=-2.92，而平凡的枢纽配对
    ΔPMI=+0.25）。Δconf 的分子分母都是计数，地板效应同阶，因而相消：
      * 新兴配对     早期 df_pair=0 -> 早期 conf≈0，晚期 conf 高 -> Δ 明显为正
      * 长期共现的平凡配对 两期占比都接近 1 -> Δ≈0
    """
    return 0.5 * ((_conf(dl, da_l) - _conf(de, da_e))
                  + (_conf(dl, db_l) - _conf(de, db_e)))


def _pct_rank(values, higher_is_better=True):
    """百分位排名（0..1）。用 rank 而非原始值：growth / AA 都是长尾分布，
    原始值加权会让单个极端值主导总分。"""
    items = sorted(values.items(), key=lambda kv: kv[1],
                   reverse=higher_is_better)
    n = len(items)
    if n == 1:
        return {items[0][0]: 1.0}
    out, i = {}, 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][1] == items[i][1]:
            j += 1
        pct = 1.0 - (i + j) / 2.0 / (n - 1)
        for k in range(i, j + 1):
            out[items[k][0]] = pct
        i = j + 1
    return out


def _slope(years: collections.Counter):
    """2018-2020 的份额斜率（用 ``docs/年论文数`` 的年比，去掉期规模差异）。"""
    ys = [y for y in range(2018, CUTOFF_YEAR + 1)]
    num = sum(years.get(y, 0) for y in ys)
    if num == 0:
        return 0.0
    xs = list(range(len(ys)))
    vals = [years.get(y, 0) for y in ys]
    mx = sum(xs) / len(xs)
    my = sum(vals) / len(vals)
    den = sum((x - mx) ** 2 for x in xs)
    return 0.0 if den == 0 else sum((x - mx) * (v - my)
                                    for x, v in zip(xs, vals)) / den


# ══ 候选生成 ═════════════════════════════════════════════════════════════
def _endpoint(name, nodes):
    nd = nodes[name]
    return {"name": name, "type": nd["type"], "support": nd["support"],
            "first_seen": nd["first_seen"]}


def build_node_candidates(nodes, typed, pairs, adj, diag, node_papers=None,
                          meta=None):
    """(1) 节点候选：**知识树位置**打分，不是规模打分。

    用户 2026-09-12 的纠正：NODE 不该预测「哪些节点会变大」，而该预测
    「哪些节点处于知识空间中的**高潜力位置**」。

        Potential(node) = f(position in knowledge tree)  !=  f(paper count)

    为什么必须改：按「增长/连通度」排出来的 Top-N 是 polymeri zation shrinkage、
    secondary caries、shrinkage stress —— **长期存在的大问题**。它们大而熟，
    规模类特征必然把它们推上去，而这类问题正是**未来不会突然爆发**的那批。
    P4-3 实测同分层随机节点的命中率反而更高（49% vs 37%），是该缺陷的直接证据。

    ── 规模混淆怎么处理 ──────────────────────────────────────────────
    第一版直接把参与系数/中介度当分数，立刻被枢纽占据（实测
    photopolymerization deg=93 而 P=0.91、med=0.97）—— 度数一大，邻居自然横跨
    多个模块、邻居对自然也大多互不相连。所以不是「不要规模约束」，而是
    **规模决定你在哪个组里竞争，结构决定你在组内的排名**：
      stratum = small(deg 2-3) / mid(4-7) / large(>=8)
    结构特征只在**层内**做百分位排名，跨层不可比；报告主榜取 small+mid
    （用户目标正是「目前还小、但可能改变知识树结构的方向」），large 层单列。

    ── 四个结构信号（用户定义）+ 弱化的增长 ───────────────────────────
      structural_novelty 该节点带来了**新连接**吗（新边占比）+ 自身首现新近度
      bridge             是否连接原本分离的区域（参与系数 P，模块=主场主题）
      problem            是否在解决**已确立的瓶颈**（typed 关系指向 challenge 型
                         概念，按挑战成熟度加权 —— 解决老瓶颈比新问题更值钱）
      combination        是否引入**新组合**（邻居对之间互不相连的比例 = 结构洞）
      growth_weak        仅弱辅助（权重 0.05），用户明确「w5 -> 0」

    support>=2 与 degree>=2 只作**可测量下限**（结构量在孤点上无定义），
    被它挡掉的节点数逐项记录 —— 它不是规模奖励而是测量前提，
    扩量抽取可以直接把它降下来。
    """
    n_e = diag["period_papers"].get("early") or 1
    n_l = diag["period_papers"].get("late") or 1

    # 模块划分：节点的**主场主题**（OpenAlex 自带分类，独立于本图）
    module = {}
    for n, nd in nodes.items():
        module[n] = nd["topics"].most_common(1)[0][0] if nd["topics"] else None

    # typed 关系邻接（带语义）
    tadj = collections.defaultdict(list)
    for (a, b), te in typed.items():
        for r, cnt in te["relations"].most_common():
            tadj[a].append((b, r, cnt))
            tadj[b].append((a, r, cnt))

    rows = []
    for n, nd in nodes.items():
        deg = len(adj.get(n) or ())
        if deg < NODE_MIN_DEGREE:
            EXCLUDED["node_below_min_degree"] += 1
            continue
        if nd["support"] < NODE_MIN_SUPPORT:
            EXCLUDED["node_below_min_support"] += 1
            continue

        nbrs = sorted(adj[n])
        # (1) 结构新颖性：该节点的连接有多「新」
        new_edges = sum(1 for m in nbrs
                        if (_pair_first_seen(pairs, n, m) or 0) >= NEW_EDGE_SINCE)
        # (2) 桥梁性：参与系数（邻居落在多少个不同模块里）
        mod_cnt = collections.Counter(module.get(m) for m in nbrs)
        bridge = 1.0 - sum((v / deg) ** 2 for v in mod_cnt.values())
        # (3) 瓶颈解决能力：typed 关系指向 challenge 型概念，按成熟度加权
        challengers = []
        for m, r, cnt in tadj.get(n, ()):
            if nodes.get(m, {}).get("type") == "challenge":
                challengers.append({"challenge": m, "relation": r, "n": cnt,
                                    "support": nodes[m]["support"],
                                    "first_seen": nodes[m]["first_seen"]})
        problem_raw = sum(math.log1p(c["support"]) * c["n"] for c in challengers)
        # (4) 组合创新：邻居对互不相连的比例（结构洞 / 中介度）
        tot = deg * (deg - 1) / 2
        mediated = sum(1 for i, a in enumerate(nbrs) for b in nbrs[i + 1:]
                       if b not in adj.get(a, ()))
        med_frac = mediated / tot if tot else 0.0

        de = sum(v for y, v in nd["years"].items() if EARLY[0] <= y <= EARLY[1])
        dl = sum(v for y, v in nd["years"].items() if LATE[0] <= y <= LATE[1])
        r_e = (de + LAPLACE) / (n_e + 1)
        r_l = (dl + LAPLACE) / (n_l + 1)

        rows.append({
            "cand_id": _cand_id("NODE", n),
            "kind": "NODE",
            "concept": n,
            "type": nd["type"],
            "support": nd["support"],
            "first_seen": nd["first_seen"],
            "degree": deg,
            "stratum": (NODE_STRATUM_SMALL if deg <= NODE_SMALL_MAX_DEG
                        else NODE_STRATUM_MID if deg <= NODE_MID_MAX_DEG
                        else NODE_STRATUM_LARGE),
            "df_early": de, "df_late": dl,
            "growth": round(r_l / r_e, 4) if r_e else None,
            "new_edge_share": round(new_edges / deg, 4),
            "n_new_edges": new_edges,
            "bridge_raw": round(bridge, 4),
            "n_modules": len([m for m in mod_cnt if m]),
            "problem_raw": round(problem_raw, 4),
            "problem_cohesion": len({c["challenge"] for c in challengers}),
            "challengers": sorted(challengers, key=lambda c: -c["support"])[:6],
            "combination_raw": round(med_frac, 4),
            "n_mediated_pairs": mediated,
            "topics": [t for t, _ in nd["topics"].most_common(4)],
            "neighbors_by_module": _neighbors_by_module(nbrs, module),
            "evidence_papers": _node_evidence(n, node_papers, meta),
            "method_like": is_method_like(n),
            "scores_basis": "knowledge_tree_position(within-stratum pct)",
        })

    by_stratum = collections.defaultdict(list)
    for r in rows:
        by_stratum[r["stratum"]].append(r)
    for group in by_stratum.values():
        rn = _pct_rank({r["cand_id"]: 0.5 * r["new_edge_share"]
                        + 0.5 * (-r["first_seen"]) for r in group})
        rb = _pct_rank({r["cand_id"]: r["bridge_raw"] for r in group})
        rp = _pct_rank({r["cand_id"]: r["problem_raw"]
                        + 0.5 * r["problem_cohesion"] for r in group})
        rc = _pct_rank({r["cand_id"]: r["combination_raw"] for r in group})
        rg = _pct_rank({r["cand_id"]: (r["growth"] or 0.0) for r in group})
        for r in group:
            c = r["cand_id"]
            r["scores"] = {
                "structural_novelty": round(rn[c], 4),
                "bridge": round(rb[c], 4),
                "problem": round(rp[c], 4),
                "combination": round(rc[c], 4),
                "growth_weak": round(rg[c], 4),
            }
            r["emergence_score"] = round(sum(
                NODE_WEIGHTS[k] * r["scores"][k] for k in NODE_WEIGHTS), 6)
            r["rank_in_type"] = 0
        group.sort(key=lambda r: (-r["emergence_score"], r["concept"]))
        for i, r in enumerate(group, 1):
            r["rank_in_type"] = i
    return rows, by_stratum


def _pair_first_seen(pairs, a, b):
    p = pairs.get((a, b)) or pairs.get((b, a))
    return min(p["years"]) if p and p["years"] else None


def _neighbors_by_module(nbrs, module, limit=10):
    out = collections.defaultdict(list)
    for m in nbrs:
        out[module.get(m) or "(unclassified)"].append(m)
    return {k: sorted(v)[:limit] for k, v in
            sorted(out.items(), key=lambda kv: -len(kv[1]))[:5]}


def build_pair_candidates(nodes, typed, pairs, meta, diag, excluded, pair_adj):
    """(2)(3) 新边形成 + 跨域连接 —— 共现对上的时间序列与主题信号。"""
    n_early = diag["period_papers"].get("early") or 1
    n_late = diag["period_papers"].get("late") or 1
    rows = []
    for (a, b), p in pairs.items():
        sup = sum(p["years"].values())
        if sup < PAIR_MIN_SUPPORT:
            excluded["pair_low_support"] += 1
            continue
        na, nb = nodes.get(a), nodes.get(b)
        if not na or not nb:
            excluded["pair_missing_node"] += 1
            continue
        if na["support"] < NODE_MIN_SUPPORT or nb["support"] < NODE_MIN_SUPPORT:
            excluded["pair_low_endpoint_support"] += 1
            continue
        te = typed.get((a, b))
        semantic = (na["type"] in PAIR_SEMANTIC_TYPES
                    or nb["type"] in PAIR_SEMANTIC_TYPES
                    or bool(te))
        if not semantic:
            excluded["pair_no_semantic_anchor"] += 1
            continue

        de = sum(v for y, v in p["years"].items() if EARLY[0] <= y <= EARLY[1])
        dl = sum(v for y, v in p["years"].items() if LATE[0] <= y <= LATE[1])
        r_e = (de + LAPLACE) / (n_early + 1)
        r_l = (dl + LAPLACE) / (n_late + 1)
        da_e = sum(v for y, v in na["years"].items() if EARLY[0] <= y <= EARLY[1])
        da_l = sum(v for y, v in na["years"].items() if LATE[0] <= y <= LATE[1])
        db_e = sum(v for y, v in nb["years"].items() if EARLY[0] <= y <= EARLY[1])
        db_l = sum(v for y, v in nb["years"].items() if LATE[0] <= y <= LATE[1])
        first = min(p["years"])
        # ⚠️ 跨域判据不能用「主题集合是否不相交」：**共现对必然共享那篇论文的主题**，
        # 因此 Jaccard 恒 > 0，该判据不可达（首版实现实测跨域候选为 0）。
        # 正确口径是「两端端点的**主场主题**是否不同」—— 即各自论文里出现最多的主题。
        # 一个只在牙科论文里相遇的配对不是跨域；跨域是「一端的主场在别处」。
        dom_a = na["topics"].most_common(1)[0][0] if na["topics"] else None
        dom_b = nb["topics"].most_common(1)[0][0] if nb["topics"] else None
        topic_contrast = 1.0 - _jaccard(set(na["topics"]), set(nb["topics"]))
        deg_a, deg_b = len(pair_adj.get(a) or ()), len(pair_adj.get(b) or ())

        rels = []
        if te:
            for r, cnt in te["relations"].most_common():
                ys = te["years"]
                rels.append({"relation": r, "n": cnt,
                             "first_seen": min(ys) if ys else None})
        rows.append({
            "cand_id": _cand_id("PAIR", a, b),
            "kind": "PAIR_PRESENT",
            "a": _endpoint(a, nodes),
            "b": _endpoint(b, nodes),
            "support": sup, "df_early": de, "df_late": dl,
            "first_seen": first,
            "rate_early": round(r_e, 6), "rate_late": round(r_l, 6),
            "growth": round(r_l / r_e, 4) if r_e else None,
            "conf_early": round(0.5 * (_conf(de, da_e) + _conf(de, db_e)), 4),
            "conf_late": round(0.5 * (_conf(dl, da_l) + _conf(dl, db_l)), 4),
            "assoc_growth": round(_assoc_growth(de, dl, da_e, da_l,
                                                db_e, db_l), 4),
            "new_edge": first >= NEW_EDGE_SINCE,
            "edge_grade": "TYPED" if te else "CO_OCCUR_ONLY",
            "typed_relations": rels,
            "topics_a": [t for t, _ in na["topics"].most_common(3)],
            "topics_b": [t for t, _ in nb["topics"].most_common(3)],
            "home_topic_a": dom_a, "home_topic_b": dom_b,
            "topic_jaccard": round(1.0 - topic_contrast, 4),
            "topic_contrast": round(topic_contrast, 4),
            "cross_domain": bool(dom_a and dom_b and dom_a != dom_b),
            "degree_a": deg_a, "degree_b": deg_b,
            "connectivity_raw": round(math.sqrt(deg_a * deg_b), 4),
            "evidence_papers": _pair_evidence(meta, p, te, a, b),
        })
    return rows


def _snippet(text, name, width=160):
    """在原文里定位端点词，取其附近片段 —— 证据必须可回溯到原文。"""
    if not text:
        return None
    low = text.lower()
    for tok in sorted(_TOK.findall(name.lower()), key=len, reverse=True):
        i = low.find(tok)
        if i >= 0:
            return text[max(0, i - width // 3): i + width].strip()
    return None


def _pair_evidence(meta, p, te, a, b, limit=EVIDENCE_MAX):
    """候选的证据包：共现论文（含原文片段）+ 抽取到的关系论文。

    每条证据都带 ``snippet``（在摘要里定位端点词后的原文片段），
    这样 LLM Analyst 的解释可以回溯到原文，而不是凭模型印象。
    """
    out = []
    for year, uid in sorted(p["papers"])[:limit]:
        m = meta.get(uid) or {}
        out.append({"paper_uid": uid, "year": year,
                    "title": (m.get("title") or "")[:220],
                    "snippet": _snippet(m.get("abstract"), a)
                               or _snippet(m.get("abstract"), b)})
    if te:
        for year, uid in sorted(te["papers"])[:3]:
            m = meta.get(uid) or {}
            out.append({"paper_uid": uid, "year": year,
                        "title": (m.get("title") or "")[:220],
                        "via": "extracted_relation",
                        "snippet": _snippet(m.get("abstract"), a)
                                   or _snippet(m.get("abstract"), b)})
    return out[:limit + 3]


def build_gap_candidates(nodes, pairs, meta, node_papers, pair_adj):
    """(4) 知识缺口 —— **尚未共现**但链接预测得分高的概念对。

    定义：在共现图上，两端端点各有 >= GAP_ENDPOINT_MIN_SUPPORT 的支撑、
    有 >= GAP_MIN_COMMON_NEIGHBORS 个共同邻居，但**该对本身没有出现过**。
    直觉：两个概念反复与同一批概念一起被讨论，却在文献里从未直接相遇 ——
    这是结构性缺口，也是最容易被未来论文证伪的候选（P4-3 直接看它是否
    在 2021-2025 首次共现）。
    """
    adj = pair_adj
    deg = {n: len(v) for n, v in adj.items()}
    mature = [n for n in nodes if nodes[n]["support"] >= GAP_ENDPOINT_MIN_SUPPORT]
    rows = []
    for a, b in itertools.combinations(sorted(mature), 2):
        if (a, b) in pairs or (b, a) in pairs:
            continue
        common = adj[a] & adj[b]
        if len(common) < GAP_MIN_COMMON_NEIGHBORS:
            continue
        aa = sum(1.0 / math.log(1.0 + deg[c]) for c in common if deg[c] > 1)
        if aa <= 0:
            continue
        na, nb = nodes[a], nodes[b]
        if not (na["type"] in PAIR_SEMANTIC_TYPES
                or nb["type"] in PAIR_SEMANTIC_TYPES):
            continue
        ja = _jaccard(set(na["topics"]), set(nb["topics"]))
        dom_a = na["topics"].most_common(1)[0][0] if na["topics"] else None
        dom_b = nb["topics"].most_common(1)[0][0] if nb["topics"] else None
        rows.append({
            "cand_id": _cand_id("GAP", a, b),
            "kind": "PAIR_GAP",
            "a": _endpoint(a, nodes), "b": _endpoint(b, nodes),
            "support": 0, "first_seen": None,
            "adamic_adar": round(aa, 6),
            "n_common_neighbors": len(common),
            "common_neighbors": sorted(common)[:6],
            "home_topic_a": dom_a, "home_topic_b": dom_b,
            "topic_jaccard": round(ja, 4),
            "topic_contrast": round(1.0 - ja, 4),
            "cross_domain": bool(dom_a and dom_b and dom_a != dom_b),
            "degree_a": len(adj[a]), "degree_b": len(adj[b]),
            "connectivity_raw": round(math.sqrt(len(adj[a]) * len(adj[b])), 4),
            "evidence_papers": [
                {"paper_uid": u, "year": (meta.get(u) or {}).get("year"),
                 "title": ((meta.get(u) or {}).get("title") or "")[:220],
                 "about": n}
                for n in (a, b)
                for _y, u in sorted(node_papers.get(n) or [],
                                    key=lambda t: -(t[0] or 0))[:2]],
        })
    rows.sort(key=lambda r: -r["adamic_adar"])
    return rows[:GAP_MAX_CANDIDATES]


def score_pairs(rows, nodes, hub_min_degree=None):
    """PAIR 候选的加权评分（**同 kind 内**做百分位排名，跨 kind 不可比）。

    ``first_seen is None`` 只出现在缺口候选（TRAIN 期从未共现）——
    它的新近度由 ``gap`` 特征表达，``new_edge`` 记 0，不重复计分。
    ``connectivity`` 只作**诊断**保留，不进权重：它奖励枢纽概念。
    """
    if not rows:
        return rows
    g = {r["cand_id"]: (r.get("growth") or 0.0) for r in rows}
    ne = {}
    for r in rows:
        if r.get("first_seen") is None:
            ne[r["cand_id"]] = 0.0
        elif r["new_edge"]:
            ne[r["cand_id"]] = 1.0
        else:
            ne[r["cand_id"]] = ((r["first_seen"] - NEW_EDGE_SINCE + 1)
                                / (CUTOFF_YEAR - NEW_EDGE_SINCE + 1))
    ag = {r["cand_id"]: r.get("assoc_growth", 0.0) for r in rows}
    # 跨域用**连续**的主题对比度（1 - Jaccard）而不是 0/1 标志：
    # 共现对的主题集合必然有交集，对比度的**大小**才携带信息。
    cd = {r["cand_id"]: r.get("topic_contrast", 0.0) for r in rows}
    gp = {r["cand_id"]: r.get("adamic_adar", 0.0) for r in rows}
    rg, rn, ra, rx, rp = (_pct_rank(g), _pct_rank(ne), _pct_rank(ag),
                          _pct_rank(cd), _pct_rank(gp))
    for r in rows:
        c = r["cand_id"]
        r["scores"] = {
            "growth": round(rg[c], 4),
            "new_edge": round(rn[c], 4),
            "assoc_growth": round(ra[c], 4),
            "cross_domain": round(rx[c], 4),
            "gap": round(rp[c], 4),
        }
        if hub_min_degree:
            r["hub_endpoint"] = bool(r.get("degree_a", 0) >= hub_min_degree
                                     or r.get("degree_b", 0) >= hub_min_degree)
        r["emergence_score"] = round(sum(
            WEIGHTS[k] * r["scores"][k] for k in WEIGHTS), 6)
    rows.sort(key=lambda r: (-r["emergence_score"], r["cand_id"]))
    for i, r in enumerate(rows, 1):
        r["rank_in_kind"] = i
    return rows


def cross_domain_pairs(rows, top_k=40):
    """跨域连接的**专榜**：用户特别点名要看的信号 (3)。

    按综合分排序 —— 综合分里已含主题对比度、新近度与 ΔPMI。
    早期按 support 排会把 1990 年代就存在的跨域配对排在最前（实测如此），
    而这里要看的是**新兴**的跨域连接。
    """
    cd = [r for r in rows if r["cross_domain"]]
    cd.sort(key=lambda r: (-r["emergence_score"], r["cand_id"]))
    return cd[:top_k]


# ══ 输出 ═════════════════════════════════════════════════════════════════
def validation_plan():
    """P4-3 验证协议（写进产物，冻结后不得改判据）。"""
    return {
        "cutoff_year": CUTOFF_YEAR,
        "future_window": [2021, 2025],
        "targets": {
            "PAIR_PRESENT": "该对在 EVAL 期间的新论文里是否比 TRAIN 期更常共同出现",
            "PAIR_GAP": "该对（TRAIN 期从未共现）是否在 EVAL 期间**首次出现**",
            "NODE": ("用户 2026-09-12 纠正：NODE 目标是**结构位置**而非规模，"
                     "故判据是「该概念在 EVAL 期是否获得**新伙伴**（伙伴广度增长）」，"
                     "而不是份额增长。两个判据都报，规模判据仅作参考 —— "
                     "新定义下规模增长**按构造不应是**主判据"),
        },
        "tier1_lexical": {
            "method": "EVAL 论文 title+abstract 里按端点词面（token 前缀匹配）计数",
            "known_limitation": (
                "词面计数与 TRAIN 期的 LLM 概念计数**不同尺度**，因此判据不能用"
                "绝对阈值，必须与随机基线同口径比较（lift / 分位数）"),
        },
        "tier2_concept": {
            "method": "对 EVAL 论文跑同一套 concept+relation 抽取，再用同一算法口径比较",
            "status": "PENDING_USER_APPROVAL",
            "cost_estimate_usd": 8.0,
            "note": "必须在候选冻结之后执行，结果永不回流到候选生成",
        },
        "random_baseline": {
            "design": ("从同一 kind、同一 TRAIN support 分层内随机抽等量候选"
                       "（固定 seed，可复现），用完全相同的 EVAL 度量"),
            "reported": ["hit_rate", "lift", "median_eval_signal", "n"],
            "why": "没有基线的命中率不可解释：一切概念在 2021-2025 都更常出现",
        },
        "no_tuning_after_freeze": True,
    }


def freeze(rows_node, rows_pair, rows_gap, meta_block, power, diag, excluded,
           args, by_stratum=None):
    import yaml
    yml = os.path.join(BASE, "datasets", "photopolymerization_v1",
                       "scope_allowlist.yaml")
    payload = {
        "predictor_version": PREDICTOR_VERSION,
        "frozen_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "paradigm": ("候选由**算法**从知识树演化中产生；LLM 只做解释（P4-2 analyst），"
                     "不参与预测。"),
        "inputs": {
            "concepts_file": _rel(args.concepts),
            "concepts_sha256": _sha256(args.concepts),
            "db_file": _rel(args.db),
            "db_sha256": _sha256(args.db),
            "scope_allowlist_sha256": _sha256(yml) if os.path.exists(yml) else None,
            "tool_file": _rel(__file__),
            "tool_sha256": _sha256(__file__),
        },
        "leakage_guard": meta_block,
        "graph": diag,
        "thresholds": {
            "cutoff_year": CUTOFF_YEAR, "early": list(EARLY), "late": list(LATE),
            "laplace": LAPLACE, "new_edge_since": NEW_EDGE_SINCE,
            "node_min_support": NODE_MIN_SUPPORT,
            "pair_min_support": PAIR_MIN_SUPPORT,
            "gap_endpoint_min_support": GAP_ENDPOINT_MIN_SUPPORT,
            "gap_min_common_neighbors": GAP_MIN_COMMON_NEIGHBORS,
            "pair_semantic_types": list(PAIR_SEMANTIC_TYPES),
            "prediction_types": list(PREDICTION_TYPES),
        },
        "weights": dict(WEIGHTS),
        "node_weights": dict(NODE_WEIGHTS),
        "node_method_like_rule": NODE_METHOD_LIKE_RE.pattern,
        "excluded": dict(excluded),
        "power": power,
        "counts": {
            "node_candidates": len(rows_node),
            "pair_present": len(rows_pair),
            "pair_gap": len(rows_gap),
            "prediction_set_node": len(_node_prediction_rows(rows_node)),
        },
        "node_candidates": rows_node,
        "structural_positive_control": structural_positive_control(
            rows_node, by_stratum),
        "prediction_set_node_rows": _node_prediction_rows(rows_node),
        "pair_candidates": rows_pair,
        "gap_candidates": rows_gap,
        "cross_domain_top": cross_domain_pairs(rows_pair),
        "validation_plan": validation_plan(),
        "note": ("候选发现全程不使用任何引用量指标（源码中不出现 citation_count）；"
                 "增长由按期份额计算，期规模差异已归一化。"
                 "本产物一旦冻结，P4-3 验证不得调参。"),
    }
    return payload


def structural_positive_control(rows_node, by_stratum):
    """已知在新近打开连接的概念，在结构打分里的位置（度量方向校验）。"""
    pct = {}
    for st, group in by_stratum.items():
        n = len(group)
        for r in group:
            pct[r["concept"]] = (round(1.0 - (r["rank_in_type"] - 1) / (n - 1), 3)
                                 if n > 1 else 1.0)
    out = []
    for name in STRUCTURAL_POSITIVE_CONTROL:
        r = next((x for x in rows_node if x["concept"] == name), None)
        if r is None:
            out.append({"concept": name, "in_graph_candidates": False})
            continue
        out.append({
            "concept": name, "in_graph_candidates": True,
            "stratum": r["stratum"], "degree": r["degree"],
            "support": r["support"], "first_seen": r["first_seen"],
            "method_like": r["method_like"],
            "rank_in_stratum": r["rank_in_type"], "stratum_size": len(
                by_stratum.get(r["stratum"], [])),
            "score_percentile_in_stratum": pct.get(name),
            "emergence_score": r["emergence_score"],
            "scores": r["scores"],
            "in_main_board": bool(r["stratum"] in NODE_PREDICTION_STRATA
                                  and not r["method_like"]),
        })
    found = [x for x in out if x["in_graph_candidates"]]
    ps = [x["score_percentile_in_stratum"] for x in found
          if x["score_percentile_in_stratum"] is not None]
    return {
        "note": ("事后构造的**打分方向校验**，不是候选，不构成方法有效性的证据。"
                 "若已知扩张的概念也不靠前，说明结构操作化本身有问题（而非功效不足）"),
        "concepts_total": len(STRUCTURAL_POSITIVE_CONTROL),
        "in_graph_candidates": len(found),
        "median_score_percentile_in_stratum": (round(sorted(ps)[len(ps) // 2], 3)
                                               if ps else None),
        "in_main_board": sum(1 for x in found if x["in_main_board"]),
        "detail": out,
    }


def _node_prediction_rows(rows_node):
    """NODE 主榜：small+mid 层且非表征手段，按层内分数排序（large 层单列）。"""
    main = [r for r in rows_node
            if r["stratum"] in NODE_PREDICTION_STRATA and not r["method_like"]]
    main.sort(key=lambda r: (-r["emergence_score"], r["concept"]))
    return main


def power_report(nodes, pairs, rows_pair, rows_gap, rows_node,
                 hub_min=None):
    supp = collections.Counter(nodes[n]["support"] for n in nodes)
    pair_supp = collections.Counter(sum(p["years"].values())
                                    for p in pairs.values())
    top20 = rows_pair[:20]
    return {
        "node_support_distribution": {f">={k}": sum(v for s, v in supp.items()
                                                    if s >= k)
                                      for k in (1, 2, 3, 5, 10)},
        "pair_support_distribution": {f">={k}": sum(v for s, v in pair_supp.items()
                                                    if s >= k)
                                      for k in (1, 2, 3, 5)},
        "by_type": dict(collections.Counter(
            nodes[n]["type"] for n in nodes)),
        "hub_degree_p90": hub_min,
        "hub_share_top20": (round(sum(1 for r in top20 if r.get("hub_endpoint"))
                                  / len(top20), 3) if top20 else None),
        "hub_share_all_pairs": (round(sum(1 for r in rows_pair
                                          if r.get("hub_endpoint"))
                                      / len(rows_pair), 3) if rows_pair else None),
        "node_strata": dict(collections.Counter(r["stratum"] for r in rows_node)),
        "node_method_like_excluded": sum(1 for r in rows_node if r["method_like"]),
        "node_excluded_by_measurability_floor": dict(EXCLUDED),
        "node_scoring": {"weights": dict(NODE_WEIGHTS),
                         "strata": {"small": f"deg<= {NODE_SMALL_MAX_DEG}",
                                    "mid": f"deg<= {NODE_MID_MAX_DEG}",
                                    "large": "deg> mid"},
                         "within_stratum_ranking": True},
        "candidate_pool": {
            "NODE": len(rows_node),
            "NODE_prediction_set": len(_node_prediction_rows(rows_node)),
            "PAIR_PRESENT": len(rows_pair),
            "PAIR_PRESENT_new_edge": sum(1 for r in rows_pair if r["new_edge"]),
            "PAIR_PRESENT_cross_domain": sum(1 for r in rows_pair
                                             if r["cross_domain"]),
            "PAIR_GAP": len(rows_gap),
        },
        "caveat": ("候选规模的瓶颈是**抽取覆盖面**（993/5545 篇 TRAIN），"
                   "不是算法：support 分布整体偏薄时，候选之间的差异由少量"
                   "论文决定。扩量约 5.5x 后同一门槛下的候选会显著增多。"),
    }


def print_summary(payload, args):
    p = payload
    d = p["graph"]
    print("=" * 78)
    print(f"  P4-2 v2 知识树演化 -> 候选方向（LLM 不参与预测）")
    print(f"  输入: {d['nodes']} 节点 | typed 边 {d['typed_pairs']} | "
          f"共现边 {d['cooccur_pairs']}（support>=2: {d['pairs_support_ge_2']}）")
    print(f"  期规模: {d['period_papers']} | 泄漏断言: "
          f"max_year={p['leakage_guard']['max_year_seen']} "
          f"<= {CUTOFF_YEAR} = {p['leakage_guard']['assert_no_future_in_train']}")
    print("-" * 78)
    c = p["counts"]
    print(f"  候选池: NODE {c['node_candidates']}"
          f"（主榜 {c['prediction_set_node']}）| "
          f"PAIR {c['pair_present']} | GAP {c['pair_gap']}")
    print(f"  被排除: {p['excluded']}")
    print("-" * 78)
    print("  [信号 1] 知识树**位置**候选（不是规模；层内排名，主榜 = small+mid 且非表征手段）")
    print("    标记: 第1位 S/M/L=度数层(deg<=3 / <=7 / >7) | 第2位 .M=是否表征手段(排除出主榜)")
    main = p["prediction_set_node_rows"]
    for r in main[:args.top_k]:
        print(f"    {r['emergence_score']:.3f} #{r['rank_in_type']:<3} "
              f"[{ {'small': 'S', 'mid': 'M', 'large': 'L'}[r['stratum']] }"
              f"{'M' if r['method_like'] else '.'}] "
              f"deg={r['degree']:<3} sup={r['support']:<3} y0={r['first_seen']} "
              f"newE={r['new_edge_share']:.2f} brg={r['bridge_raw']:.2f} "
              f"med={r['combination_raw']:.2f} prob={r['problem_raw']:.1f} "
              f"[{r['type'][:9]:<9}] {r['concept']}")
    lg = [x for x in p["node_candidates"] if x["stratum"] == NODE_STRATUM_LARGE]
    if lg:
        print(f"    ── large 层（deg>7，单独列出、不与小节点竞争）Top-3：")
        for r in lg[:3]:
            print(f"       {r['emergence_score']:.3f} deg={r['degree']:<3} "
                  f"sup={r['support']:<3} {r['concept'][:44]}")
    spc = p.get("structural_positive_control")
    if spc:
        print(f"    ── 结构打分阳性对照（方向校验）: 命中主榜 {spc['in_main_board']}"
              f"/{spc['in_graph_candidates']}（在候选内 "
              f"{spc['in_graph_candidates']}/{spc['concepts_total']}）"
              f" | 层内分数百分位中位数 {spc['median_score_percentile_in_stratum']}")
    print("-" * 78)
    print("  [信号 2/3] 新边形成 + 跨域连接（PAIR 候选，同 kind 内排名）")
    print(f"    标记: N=新边(>={NEW_EDGE_SINCE}) X=跨域 H=含枢纽端点(p90={p['power']['hub_degree_p90']})")
    for r in p["pair_candidates"][:args.top_k]:
        flags = ("N" if r["new_edge"] else "-") + ("X" if r["cross_domain"] else "-") \
            + ("H" if r.get("hub_endpoint") else "-")
        print(f"    {r['emergence_score']:.3f} #{r['rank_in_kind']:<3} "
              f"[{flags}] sup={r['support']:<2} y0={r['first_seen']} "
              f"dConf={r['assoc_growth']:>6.3f} {r['edge_grade'][:8]:<8} "
              f"{r['a']['name'][:24]} + {r['b']['name'][:24]}")
    print("-" * 78)
    print("  [信号 4] 知识缺口（尚未共现，链路预测得分高）")
    for r in p["gap_candidates"][:args.top_k]:
        print(f"    AA={r['adamic_adar']:.3f} cn={r['n_common_neighbors']:<2} "
              f"{r['a']['name'][:28]} + {r['b']['name'][:28]}")
    print("=" * 78)


def _cell(v):
    """CSV 单元： None -> 空串（否则会写出字面量 "None"）。"""
    return "" if v is None else str(v)


def write_csv(path, rows):
    cols = ["kind", "rank", "score", "a", "type_a", "b", "type_b", "support",
            "first_seen", "growth", "assoc_growth_dpmi", "new_edge",
            "cross_domain", "gap", "connectivity_raw", "hub_endpoint",
            "edge_grade", "extra"]
    lines = [",".join(cols)]
    for r in rows:
        if r["kind"] == "NODE":
            # NODE 的列语义：a=概念名，connectivity_raw 位放 degree，
            # extra 放结构特征（用户 2026-09-12 纠正后的新口径）
            lines.append(",".join(_cell(x) for x in (
                r["kind"], r["rank_in_type"], r["emergence_score"], r["concept"],
                r["type"], "", "", r["support"], r["first_seen"], r["growth"],
                "", "", "", "", r["degree"], "", "",
                "stratum=%s;deg=%d;newE=%.2f;bridge=%.2f;med=%.2f;prob=%.1f"
                ";method_like=%s;score_basis=%s" % (
                    r["stratum"], r["degree"], r["new_edge_share"],
                    r["bridge_raw"], r["combination_raw"], r["problem_raw"],
                    r["method_like"], r["scores_basis"]))))
        else:
            sc = r["scores"]
            lines.append(",".join(_cell(x) for x in (
                r["kind"], r["rank_in_kind"], r["emergence_score"],
                r["a"]["name"], r["a"]["type"], r["b"]["name"], r["b"]["type"],
                r["support"], r["first_seen"], r.get("growth"),
                r.get("assoc_growth"), sc["new_edge"], sc["cross_domain"],
                sc["gap"], r.get("connectivity_raw"),
                "1" if r.get("hub_endpoint") else "0",
                r.get("edge_grade") or "PREDICTED",
                "aa=%.3f" % r["adamic_adar"] if "adamic_adar" in r else "")))
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


def main(argv=None):
    global OUT_JSON, OUT_CSV
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="写产物（默认 dry-run）")
    ap.add_argument("--concepts", default=CONCEPTS_PATH)
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--out", default=OUT_JSON)
    ap.add_argument("--csv", default=OUT_CSV)
    ap.add_argument("--top-k", type=int, default=20)
    args = ap.parse_args(argv)
    OUT_JSON, OUT_CSV = args.out, args.csv

    ok_rows, meta = load_inputs(args.db, args.concepts)
    guard = assert_no_leakage(meta, ok_rows)
    nodes, typed, pairs, node_papers, pair_adj, diag = build_tree(ok_rows, meta)
    excluded = collections.Counter()
    degs = sorted(len(v) for v in pair_adj.values())
    # 枢纽标记取 p95：p90 实测过低（阈值=9），几乎所有候选都会被标成含枢纽，
    # 那样这个诊断量就没有区分度了。
    hub_min = degs[int(len(degs) * 0.95)] if degs else None
    rows_node, _by_stratum = build_node_candidates(nodes, typed, pairs, pair_adj,
                                                   diag, node_papers, meta)
    rows_pair = build_pair_candidates(nodes, typed, pairs, meta, diag, excluded,
                                      pair_adj)
    rows_pair = score_pairs(rows_pair[:PAIR_MAX_CANDIDATES], nodes, hub_min)
    rows_gap = score_pairs(build_gap_candidates(nodes, pairs, meta, node_papers,
                                                pair_adj), nodes, hub_min)

    power = power_report(nodes, pairs, rows_pair, rows_gap, rows_node, hub_min)
    payload = freeze(rows_node, rows_pair, rows_gap, guard, power, diag,
                     excluded, args, _by_stratum)
    print_summary(payload, args)

    tb = collections.Counter(r["type"] for r in rows_node)
    print(f"  NODE 候选按类型: {dict(tb)}")
    print(f"  支撑度分布(node): {power['node_support_distribution']}")
    print(f"  支撑度分布(pair): {power['pair_support_distribution']}")
    if not args.apply:
        print("\n  [dry-run] 未写文件。确认后加 --apply")
        return 0
    with open(OUT_JSON, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    allrows = ([dict(r, rank=r["rank_in_type"]) for r in rows_node
                if r["type"] in PREDICTION_TYPES]
               + rows_pair + rows_gap)
    allrows.sort(key=lambda r: (r["kind"], -r["emergence_score"]))
    write_csv(OUT_CSV, allrows)
    print(f"\n[ok] 候选 -> {_rel(OUT_JSON)}\n[ok] 表 -> {_rel(OUT_CSV)}")
    print(f"     冻结指纹 concepts={payload['inputs']['concepts_sha256'][:16]}… "
          f"tool={payload['inputs']['tool_sha256'][:16]}…")
    return 0


if __name__ == "__main__":
    sys.exit(main())
