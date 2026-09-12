"""P4-3：用 2021-2025 的文献独立判定 P4-2 候选的真伪（含随机基线）。

判定的对象是**算法发现的候选**（`emergence_candidates_v2.json`，已冻结），
不是 LLM 的说法 —— LLM 只提供解释层产物，不参与判定。

两条独立通道：

  Tier 1（本工具，无需 LLM）：
      把候选的端点当作**词面**，在 EVAL(2021-2025) 与 TRAIN(<=2020) 两段论文的
      标题+摘要里分别计数，比较**同一个度量**在两段上的相对变化。
      关键设计：两侧用同一把尺子（都是词面计数），因此不存在
      「TRAIN 用 LLM 计数 vs EVAL 用词面计数」的尺度错配；
      TRAIN 侧在**完整 in-scope TRAIN 视图**上测量（不是 993 篇抽样），
      以保证统计质量。

  Tier 2（未执行，待批准）：对 EVAL 论文跑同一套 concept+relation 抽取（约 $8），
      再用完全相同的算法口径比较。它必须在候选冻结之后执行，结果永不回流。

随机基线（必需，否则命中率不可解释）：
  一切概念在 2021-2025 都更常出现（文献总量增长、主题变热）。
  故按**同 kind、同支撑度分层**抽等量随机候选，用完全相同的度量比较：
    PAIR_PRESENT  从共现图里同 support 的随机对中抽
    PAIR_GAP      从「成熟节点间未共现、且共同邻居数相同」的随机对中抽
    NODE          从同 type、同 support 的随机节点中抽

判据：
  PAIR_PRESENT   EVAL 词面计数 > 0 且 EVAL 期率 > TRAIN 期率
  PAIR_GAP       EVAL 词面计数 > 0（= 缺口被填补）—— 这是最干净的一类检验，
                 因为 TRAIN 期该对共现为 0 是**构造保证**的
  NODE           EVAL 期率 > TRAIN 期率

用法：
    python tools/validate_emergence_p4_3.py                # dry-run
    python tools/validate_emergence_p4_3.py --apply        # 写报告
"""
from __future__ import annotations

import argparse
import bisect
import collections
import datetime
import hashlib
import importlib.util
import json
import math
import os
import random
import re
import sqlite3
import sys
from pathlib import Path

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tools"))

DATASET_DIR = Path(BASE) / "datasets" / "photopolymerization_v1"
CANDIDATES_PATH = DATASET_DIR / "emergence_candidates_v2.json"
DB_PATH = DATASET_DIR / "paper_meta.db"
OUT_PATH = DATASET_DIR / "validation_report_v1.json"
OUT_CSV = DATASET_DIR / "validation_report_v1.csv"

VERIFIER_VERSION = "p4_3_v1"
# ── 阳性对照（**度量灵敏度检验**，不是候选）──────────────────────────────
# 这些概念是本领域公认在 2021-2025 扩张的方向（增材制造、4D 打印、
# 数字光处理、组织工程…）。它们的作用是回答一个问题：
#   「零结果到底是候选不够好，还是这把词面尺子根本没有灵敏度？」
# 若阳性对照的命中率明显高于基线 -> 尺子有灵敏度 -> 候选的零结果才有解释力。
# ⚠️ 这是**事后（post hoc）**构造的对照，只用于校验度量，**不构成**任何
#    关于候选方法有效性的证据。
POSITIVE_CONTROL = (
    "additive manufacturing", "3d printing", "4d printing",
    "digital light processing", "vat photopolymerization",
    "tissue engineering", "bioprinting", "hydrogel",
    "soft robotics", "continuous liquid interface production",
    "biocompatibility", "wear resistance", "stimuli-responsive polymer",
)
CUTOFF_YEAR = 2020
FUTURE = (2021, 2025)
SEED = 13
TOKEN_MIN_LEN = 5            # 词面匹配只认真实长度 >=5 的 token（避免 a/of/in 之类）
TOKEN_STOP = frozenset(
    "based using study effect effects their there these those which where with "
    "from into than that this have been more most other others such between "
    "during different various several results result analysis used using".split())


def _rel(path) -> str:
    try:
        return os.path.relpath(str(path), BASE).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_discoverer():
    spec = importlib.util.spec_from_file_location(
        "discover_emergence_candidates",
        os.path.join(BASE, "tools", "discover_emergence_candidates.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ══ 词面度量 ═════════════════════════════════════════════════════════════
_WORD = re.compile(r"[a-z0-9]+")


def significant_tokens(name):
    """端点概念的「显著 token」：长度 >= TOKEN_MIN_LEN 且非通用词。

    若一个 token 都不满足（如 `DLP`、`3D`），退回全部长度 >=2 的 token ——
    宁可放宽也不能把端点变成空集（空集会让匹配恒假，静默污染指标）。
    """
    toks = [t for t in _WORD.findall((name or "").lower())
            if t not in TOKEN_STOP]
    sig = [t for t in toks if len(t) >= TOKEN_MIN_LEN]
    if sig:
        return sig
    return [t for t in toks if len(t) >= 2] or toks


class LexIndex:
    """词面索引（倒排）。

    为什么不用「逐篇扫描 + 逐 token 比较」：候选 + 基线共约 180 个端点组合，
    每个都要在 11k 篇文本上做 token 匹配 —— 朴素实现是 ~10^9 次字符串比较，
    实测会跑不动。倒排表把每次匹配变成集合运算。

    token 匹配规则（互为子串的情形用前缀桶 + 前缀枚举覆盖）：
      * 完全相同
      * 文本 token 以概念 token 开头（`shrink` -> `shrinkage`）—— 长度 >=5 才启用
      * 概念 token 以文本 token 开头（文本里写的是简称）
      * 共享 >=6 字符前缀（`polymer` / `polymers` / `polymerization`）
    """

    def __init__(self, rows, train_period, eval_period):
        self.inv = collections.defaultdict(set)
        self.docs = []
        for i, (uid, year, text) in enumerate(rows):
            toks = frozenset(_WORD.findall((text or "").lower()))
            self.docs.append((uid, year, toks))
            for t in toks:
                self.inv[t].add(i)
        self.sorted_vocab = sorted(self.inv)
        self.pfx6 = collections.defaultdict(set)
        self.sfx6 = collections.defaultdict(set)
        for t in self.inv:
            if len(t) >= 6:
                self.pfx6[t[:6]].add(t)
            if len(t) >= 8:
                # 词尾桶：处理「概念 token 被包含在更长复合词里」的情形，
                # 例如 polymerization ⊂ photopolymerization ——
                # 这种包含关系既不在前缀桶里，也不互为前缀，靠词尾桶 + 包含校验兜住。
                self.sfx6[t[-6:]].add(t)
        lo, hi = train_period
        self.train = {i for i, (_u, y, _t) in enumerate(self.docs)
                      if y is not None and lo <= y <= hi}
        lo, hi = eval_period
        self.eval = {i for i, (_u, y, _t) in enumerate(self.docs)
                     if y is not None and lo <= y <= hi}
        self._cache = {}

    def matching_tokens(self, tok):
        got = self._cache.get(tok)
        if got is not None:
            return got
        res = set()
        if tok in self.inv:
            res.add(tok)
        if len(tok) >= 5:                       # 文本 token 以它开头
            i = bisect.bisect_left(self.sorted_vocab, tok)
            j = bisect.bisect_left(self.sorted_vocab, tok + "\uffff")
            res.update(self.sorted_vocab[i:j])
        for k in range(5, len(tok)):            # 文本 token 是它的前缀
            p = tok[:k]
            if p in self.inv:
                res.add(p)
        if len(tok) >= 6:
            res.update(self.pfx6.get(tok[:6], ()))
        if len(tok) >= 8:
            for w in self.sfx6.get(tok[-6:], ()):
                if tok in w:
                    res.add(w)
        self._cache[tok] = res
        return res

    def _docset(self, name):
        """端点命中的论文集合。

        ⚠️ 端点**内部**的显著 token 也是**合取**（全部命中），不是"任一命中"。
        首版用了并集，结果 `polymerization shrinkage` 单独就能匹配 68% 的 TRAIN
        论文（靠 `polymerization` 一个词），度量几乎失去区分度。
        合取之后「polymerization shrinkage」要求文本同时出现这两个词 ——
        这才是这个概念的意思。
        """
        toks = significant_tokens(name)
        if not toks:
            return None
        d = None
        for t in toks:
            s = set()
            for w in self.matching_tokens(t):
                s |= self.inv[w]
            d = s if d is None else (d & s)
            if not d:
                return set()
        return d

    def count(self, names, period):
        """同时命中**全部**端点名的论文数（合取 —— 精度优先）。"""
        sets = [self._docset(n) for n in names]
        if any(s is None for s in sets):
            return None
        d = sets[0]
        for s in sets[1:]:
            d &= s
            if not d:
                return 0
        return len(d & self._period_set(period))

    def _period_set(self, period):
        return self.train if period[1] <= CUTOFF_YEAR else self.eval

    def n_docs(self, period):
        """该期在语料里的论文数（份额归一化的分母）。"""
        return len(self.train) if period[1] <= CUTOFF_YEAR else len(self.eval)


def _verdict(lift, sig):
    """把 lift + 显著性翻译成一句可复核的判定，避免读者各自解读。"""
    if lift is None:
        return "NO_BASELINE"
    p = sig.get("p_two_sided")
    if p is None:
        return "INCONCLUSIVE"
    if p < 0.05 and lift > 1.0:
        return "SIGNAL（优于同分层随机基线，p<0.05）"
    if p < 0.05 and lift < 1.0:
        return "ANTI_SIGNAL（显著劣于基线）"
    return "NULL（与基线无可区分差异）"


def load_text_rows(db_path):
    """载入 in-scope 且未被排除的论文文本（与视图同口径）。"""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT paper_uid, year, title, abstract FROM paper_meta "
            "WHERE in_scope = 1 AND exclusion_reason IS NULL "
            "AND split IN ('TRAIN','EVAL')").fetchall()
    finally:
        con.close()
    return [(r[0], r[1], f"{r[2] or ''} {r[3] or ''}") for r in rows]


# ══ 度量与判据 ═══════════════════════════════════════════════════════════
def measure(idx, names, train_period, eval_period):
    """同时算共现计数与**词面 PMI**。

    为什么把 PMI 提为主判据：只看共现计数或份额时，语料整体漂移会淹没信号
    —— 首版实测真实候选与随机基线的命中率都是 50%，毫无区分度。
    PMI 已经除掉「两个概念各自有多常见」，比较的是**关联强度本身**的变化，
    这与候选端 ΔPMI 特征同构，因此判据与特征测的是同一件事。
    """
    res = {}
    for tag, period in (("train", train_period), ("eval", eval_period)):
        joint = idx.count(names, period)
        if joint is None:
            return {"measurable": False}
        marg = []
        for n in names:
            c = idx.count([n], period)
            if c is None:
                return {"measurable": False}
            marg.append(c)
        n_doc = idx.n_docs(period)
        p_j = (joint + 0.5) / (n_doc + 1)
        denom = 1.0
        for c in marg:
            denom *= (c + 0.5) / (n_doc + 1)
        pmi = math.log2(p_j / denom) if denom > 0 else 0.0
        res[tag] = {"lex": joint, "rate": round(p_j, 8), "pmi": round(pmi, 4)}
    tr, ev = res["train"], res["eval"]
    return {
        "measurable": True,
        "train_lex": tr["lex"], "eval_lex": ev["lex"],
        "train_rate": tr["rate"], "eval_rate": ev["rate"],
        "rate_ratio": round(ev["rate"] / tr["rate"], 4) if tr["rate"] else None,
        "train_pmi": tr["pmi"], "eval_pmi": ev["pmi"],
        "lex_dpmi": round(ev["pmi"] - tr["pmi"], 4),
    }


def is_hit(kind, m):
    """判据分两类。

    PAIR_*:  **关联强度是否增强**（词面 ΔPMI > 0）且 EVAL 侧确有共现。
            对 PAIR_GAP 也用同一条 —— 原因见 design.known_limitations：
            「TRAIN 期从未共现」是**抽取样本(993 篇)上的事实**，不是语料事实，
            实测词面在 TRAIN 期就能找到上百次共现，故缺口语义的检验需 Tier 2。

    NODE :  ΔPMI 对**单个概念**恒等于 0（p/p，数学上无定义），
            因此节点类只能用份额增长（期率比 > 1）。首版误用 PMI 判据时
            节点命中率恒为 0 —— 那是度量假象，不是实验结论。
    """
    if not m.get("measurable"):
        return None
    if kind == "NODE":
        return m["eval_lex"] > 0 and (m["rate_ratio"] or 0) > 1.0
    return m["eval_lex"] > 0 and m["lex_dpmi"] > 0


def wilson(hits, n, z=1.96):
    if not n:
        return (None, None)
    p = hits / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (round(max(0.0, c - h), 3), round(min(1.0, c + h), 3))


def two_proportion_z(h1, n1, h2, n2):
    """两组命中率的双比例 z 检验（两点分布近似，双尾）。

    为什么必须报它：n=20 时两组差 10 个百分点完全可能是噪声，
    单看 CI 重叠与否容易误判。n=424 vs 372 时同样 13 个百分点的差距
    z≈3.9 -> p<1e-4，是可以下结论的。
    """
    if not n1 or not n2:
        return {"z": None, "p_two_sided": None}
    p1, p2 = h1 / n1, h2 / n2
    se = math.sqrt(p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2)
    if se == 0:
        return {"z": None, "p_two_sided": None}
    z = (p1 - p2) / se
    return {"z": round(z, 3), "p_two_sided": round(math.erfc(abs(z) / math.sqrt(2)), 6)}


def summarize_group(rows):
    valid = [r for r in rows if r["hit"] is not None]
    hits = sum(1 for r in valid if r["hit"])
    ratios = [r["measure"]["rate_ratio"] for r in valid
              if r["measure"].get("rate_ratio")]
    dpmis = [r["measure"]["lex_dpmi"] for r in valid
             if r["measure"].get("measurable")]
    rates = [r["measure"]["eval_rate"] for r in valid if r["measure"].get("measurable")]
    return {
        "n": len(valid), "n_immmeasurable": len(rows) - len(valid),
        "hits": hits,
        "hit_rate": round(hits / len(valid), 4) if valid else None,
        "hit_rate_wilson95": wilson(hits, len(valid)),
        "median_rate_ratio": (round(sorted(ratios)[len(ratios) // 2], 3)
                              if ratios else None),
        "median_eval_rate": (round(sorted(rates)[len(rates) // 2], 6)
                             if rates else None),
        "median_lex_dpmi": (round(sorted(dpmis)[len(dpmis) // 2], 4)
                            if dpmis else None),
    }


# ══ 随机基线 ═════════════════════════════════════════════════════════════
def baseline_present(cands, all_pairs_support, rng):
    """PAIR_PRESENT 基线：从**同 support** 的共现对里随机抽（排除已选中的）。"""
    used = {frozenset((c["a"]["name"], c["b"]["name"])) for c in cands}
    by_sup = collections.defaultdict(list)
    for (a, b), s in all_pairs_support.items():
        by_sup[s].append((a, b))
    out = []
    for c in cands:
        s = c["support"]
        pool = [p for p in by_sup.get(s, [])
                if frozenset(p) not in used]
        if not pool:
            continue
        a, b = rng.choice(pool)
        used.add(frozenset((a, b)))
        out.append({"a": a, "b": b, "support": s, "matched_to": c["cand_id"]})
    return out


def baseline_node(cands, nodes, rng):
    """NODE 基线：同 type、同 support 的随机节点。

    ⚠️ 严格同 support 会在候选数很大时**匹配池耗尽**（实测 top-k=500 取走全部
    86 个真实节点后，基线拿到 0 个），于是基线 NULL —— 那时看起来像"没有基线"
    而不是"没有信号"。故逐级放宽：同 support -> support±1 -> 同 type 任意 support，
    并记录实际放宽幅度。
    """
    by_type = collections.defaultdict(list)
    for n, nd in nodes.items():
        by_type[nd["type"]].append((n, nd["support"]))
    used = {c["concept"] for c in cands}
    out = []
    for c in cands:
        cands_pool = [(n, sp) for n, sp in by_type.get(c["type"], [])
                      if n not in used]
        if not cands_pool:
            continue
        exact = [x for x in cands_pool if x[1] == c["support"]]
        near = [x for x in cands_pool if abs(x[1] - c["support"]) <= 1]
        pick = rng.choice(exact or near or cands_pool)
        used.add(pick[0])
        out.append({"concept": pick[0], "type": c["type"],
                    "support": pick[1], "matched_to": c["cand_id"],
                    "match_tolerance": ("exact" if pick[1] == c["support"]
                                        else "near" if abs(pick[1]
                                                           - c["support"]) <= 1
                                        else "type_only")})
    return out


def baseline_gap(cands, nodes, adj, rng):
    """PAIR_GAP 基线：成熟节点间**未共现**且共同邻居数相同的随机对。"""
    mature = sorted(n for n in nodes
                    if nodes[n]["support"] >= 8)
    used = {frozenset((c["a"]["name"], c["b"]["name"])) for c in cands}
    out = []
    for c in cands:
        target_cn = c["n_common_neighbors"]
        got = None
        for _ in range(4000):
            a, b = rng.sample(mature, 2)
            k = frozenset((a, b))
            if k in used or a in adj and b in adj[a]:
                continue
            if len(adj[a] & adj[b]) != target_cn:
                continue
            got = (a, b)
            break
        if got:
            used.add(frozenset(got))
            out.append({"a": got[0], "b": got[1], "support": 0,
                        "n_common_neighbors": target_cn,
                        "matched_to": c["cand_id"]})
    return out


# ══ 主流程 ═══════════════════════════════════════════════════════════════
def run(args):
    d = _load_discoverer()
    ok, meta = d.load_inputs(str(DB_PATH))
    nodes, typed, pairs, node_papers, adj, diag = d.build_tree(ok, meta)
    all_pairs_support = {k: sum(v["years"].values()) for k, v in pairs.items()}

    cand = json.loads(Path(args.candidates).read_text(encoding="utf-8"))
    top_k = args.top_k
    real_pair = [r for r in cand["pair_candidates"]
                 if r["support"] >= args.min_pair_support][:top_k]
    real_gap = cand["gap_candidates"][:top_k]
    real_node = [r for r in cand["node_candidates"]
                 if r["type"] in cand["thresholds"]["prediction_types"]][:top_k]

    train_period = (d.EARLY[0], d.CUTOFF_YEAR)
    eval_period = FUTURE
    idx = LexIndex(load_text_rows(str(DB_PATH)), train_period, eval_period)
    print(f"[data] 论文 {len(idx.docs)} 篇 | TRAIN({train_period[0]}-"
          f"{train_period[1]}) {idx.n_docs(train_period)} 篇 | "
          f"EVAL({eval_period[0]}-{eval_period[1]}) {idx.n_docs(eval_period)} 篇")

    rng = random.Random(args.seed)
    b_present = baseline_present(real_pair, all_pairs_support, rng)
    b_node = baseline_node(real_node, nodes, rng)
    b_gap = baseline_gap(real_gap, nodes, adj, rng)
    print(f"[baseline] PAIR {len(b_present)} | NODE {len(b_node)} | "
          f"GAP {len(b_gap)}（seed={args.seed}）")

    def eval_pair(items, tag):
        rows = []
        for c in items:
            names = [c["a"] if isinstance(c["a"], str) else c["a"]["name"],
                     c["b"] if isinstance(c["b"], str) else c["b"]["name"]]
            m = measure(idx, names, train_period, eval_period)
            rows.append({"id": c.get("cand_id") or c.get("matched_to"),
                         "pair": names, "support": c.get("support"),
                         "measure": m, "hit": is_hit(tag, m)})
        return rows

    def eval_node(items):
        rows = []
        for c in items:
            m = measure(idx, [c["concept"]], train_period, eval_period)
            rows.append({"id": c.get("cand_id") or c.get("matched_to"),
                         "concept": c["concept"], "type": c["type"],
                         "support": c.get("support"),
                         "measure": m, "hit": is_hit("NODE", m)})
        return rows

    res = {
        "PAIR_PRESENT": {
            "real": eval_pair(real_pair, "PAIR_PRESENT"),
            "baseline": eval_pair(b_present, "PAIR_PRESENT"),
        },
        "PAIR_GAP": {
            "real": eval_pair(real_gap, "PAIR_GAP"),
            "baseline": eval_pair(b_gap, "PAIR_GAP"),
        },
        "NODE": {
            "real": eval_node(real_node),
            "baseline": eval_node(b_node),
        },
    }

    groups = {}
    for k, v in res.items():
        s_real = summarize_group(v["real"])
        s_base = summarize_group(v["baseline"])
        lift = None
        if s_real["hit_rate"] is not None and s_base["hit_rate"]:
            lift = round(s_real["hit_rate"] / s_base["hit_rate"], 3)
        sig = two_proportion_z(s_real["hits"], s_real["n"],
                               s_base["hits"], s_base["n"])
        groups[k] = {"real": s_real, "baseline": s_base, "lift": lift,
                     "significance": sig,
                     "verdict": _verdict(lift, sig)}

    _pc = positive_control(idx, train_period, eval_period, nodes, args)
    const = {
        "verifier_version": VERIFIER_VERSION,
        "finished_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "inputs": {
            "candidates_file": _rel(args.candidates),
            "candidates_sha256": _sha256(args.candidates),
            "db_sha256": _sha256(str(DB_PATH)),
            "predictor_version": cand.get("predictor_version"),
            "frozen_at": cand.get("frozen_at"),
            "tool_sha256": _sha256(Path(__file__)),
        },
        "design": {
            "cutoff_year": CUTOFF_YEAR, "future_window": list(FUTURE),
            "train_period": list(train_period),
            "train_papers": idx.n_docs(train_period),
            "eval_papers": idx.n_docs(eval_period),
            "tier1_lexical": {
                "rule": ("论文标题+摘要中：每个端点概念的显著 token（len>=5 且非"
                         "通用词）**全部**命中，且所有端点都命中（整体合取，精度优先）"),
                "token_match": ("相等 / 文本 token 以概念 token 开头（>=5）/ "
                                "概念 token 以文本 token 开头 / 共享 >=6 字符前缀 / "
                                "概念 token 被更长的复合词包含（>=8，如 "
                                "polymerization ⊂ photopolymerization）"),
                "why_same_ruler": ("TRAIN 与 EVAL 都用词面计数，避免"
                                   "「TRAIN 用 LLM 计数 vs EVAL 用词面计数」的尺度错配"),
            },
            "criteria": {
                "PAIR_PRESENT": "eval_lex > 0 且 词面 ΔPMI > 0（关联强度增强）",
                "PAIR_GAP": "同 PAIR_PRESENT（缺口语义 Tier 1 无法检验，见 limitations）",
                "NODE": ("eval_lex > 0 且 期率比 > 1（单概念 ΔPMI 数学上恒为 0，"
                         "不能用）"),
            },
            "random_baseline": {
                "seed": args.seed,
                "design": ("同 kind 同支撑度分层抽样（PAIR 同 support；GAP 同共同邻居数；"
                           "NODE 同 type 同 support），用完全相同的度量"),
            },
            "known_limitations": [
                "词面匹配存在**同形异义**风险（例如 `shrinkage` 在陶瓷烧结语境里含义不同），"
                "会抬高 EVAL 计数；缓解手段是合取匹配（要求全部端点 token 命中）与随机基线对照",
                "**Tier 1 无法检验 PAIR_GAP 的「缺口被填补」**：「TRAIN 期从未共现」是"
                "抽取样本（993 篇）上的事实，不是语料事实 —— 实测词面在 TRAIN 期就能"
                "找到上百次共现。故 GAP 类别在这里只能用与其它类别相同的判据"
                "（关联强度是否增强），缺口语义的检验需要 Tier 2",
                "Tier 1 无法识别「同一概念的不同说法」（变体稀释），因此是**下界偏向**的度量",
                "样本量小（每组 10-30），单个组的差异不可过度解读；组间比较看 lift 与置信区间重叠情况",
                "EVAL 侧从未抽取概念 —— Tier 1 只做词面，概念级判定属 Tier 2（待批准）",
            ],
        },
        "groups": groups,
        "positive_control": _pc,
        "sensitivity": sweep_sensitivity(cand, idx, train_period, eval_period,
                                         nodes, all_pairs_support, adj, args),
        "per_candidate": res,
        "tier2_status": {
            "status": "PENDING_USER_APPROVAL",
            "what": "对 EVAL 论文跑同一套 concept+relation 抽取，再用同一算法口径比较",
            "cost_estimate_usd": 8.0,
            "papers": idx.n_docs(eval_period),
            "must_run_after": "候选冻结（本报告使用的 candidates_sha256）",
        },
        "verdict_note": ("命中率必须与同分层随机基线比较才有意义："
                         "一切概念在 2021-2025 都会更常出现。"
                         "显著性用双比例 z 检验（双尾）；n 只有几十时不要读单组数值。"),
        "instrument_validation": {
            "positive_control_hits": f"{_pc['summary']['hits']}/{_pc['summary']['n']}",
            "positive_control_hit_rate": _pc["summary"]["hit_rate"],
            "positive_control_median_rate_ratio": _pc["summary"]["median_rate_ratio"],
            "how_to_read": ("阳性对照若命中率低，说明**尺子没有灵敏度**，"
                            "此时候选的零结果不可解释为「候选无效」。"
                            "本轮对照命中率见上两行。"),
            "caveat": _pc["note"],
        },
    }
    return const


def positive_control(idx, train_period, eval_period, nodes, args):
    """阳性对照：已知扩张概念在**同一把尺子**下的命中率。

    如果它们也命中不了，说明度量没有灵敏度，零结果不可解释为「候选无效」。
    """
    rows = []
    for name in POSITIVE_CONTROL:
        m = measure(idx, [name], train_period, eval_period)
        rows.append({"concept": name, "measure": m,
                     "hit": is_hit("NODE", m),
                     "in_graph": name in nodes})
    s = summarize_group(rows)
    return {"note": ("事后构造的**度量灵敏度检验**，不是候选，不构成方法有效性的证据"),
            "concepts": list(POSITIVE_CONTROL), "summary": s, "detail": rows}


def sweep_sensitivity(cand, idx, train_period, eval_period, nodes,
                      all_pairs_support, adj, args):
    """支撑度敏感性：PAIR 候选按 support 分层后各自的 lift。

    用途：区分两种失败模式 ——
      * 若 lift 随 support 单调上升 -> 「功效不足」（样本变厚就有信号）
      * 若各层都在 1.0 附近             -> 「该特征与未来无关」
    这是本轮唯一能把「方法无效」与「数据不够」分开的证据。
    """
    out = {}
    # NODE 按 type 分层：区分「direction 型候选」与「challenge 型候选」。
    # 动机：challenge 类概念大多是**长期存在的问题**（shrinkage、secondary caries），
    # 它们被选进榜是因为在 993 篇抽样里"出现次数多"，而不是因为正在兴起。
    nreal = [r for r in cand["node_candidates"]
             if r["type"] in cand["thresholds"]["prediction_types"]]
    nbase = baseline_node(nreal, nodes, random.Random(args.seed))
    for t in ("direction", "challenge"):
        rr = [{"cand_id": r["cand_id"], "concept": r["concept"],
               "type": r["type"], "support": r["support"],
               "measure": measure(idx, [r["concept"]], train_period, eval_period)}
              for r in nreal if r["type"] == t]
        bb = [{"cand_id": r["matched_to"], "concept": r["concept"],
               "type": r["type"], "support": r["support"],
               "measure": measure(idx, [r["concept"]], train_period, eval_period)}
              for r in nbase if r["type"] == t]
        for x in rr + bb:
            x["hit"] = is_hit("NODE", x["measure"])
        if rr:
            sr, sb = summarize_group(rr), summarize_group(bb)
            out[f"NODE:{t}"] = {
                "real": sr, "baseline": sb,
                "lift": (round(sr["hit_rate"] / sb["hit_rate"], 3)
                         if sr["hit_rate"] is not None and sb["hit_rate"] else None)}
    for ms in (2, 3, 4, 5):
        reals = [r for r in cand["pair_candidates"] if r["support"] >= ms]
        if len(reals) < 5:
            continue
        rng = random.Random(args.seed)
        base = baseline_present(reals, all_pairs_support, rng)
        rr = [{"cand_id": c["cand_id"], "a": c["a"]["name"], "b": c["b"]["name"],
               "support": c["support"],
               "measure": measure(idx, [c["a"]["name"], c["b"]["name"]],
                                  train_period, eval_period)}
              for c in reals]
        bb = [{"cand_id": c["matched_to"], "a": c["a"], "b": c["b"],
               "support": c["support"],
               "measure": measure(idx, [c["a"], c["b"]], train_period,
                                  eval_period)}
              for c in base]
        for x in rr:
            x["hit"] = is_hit("PAIR_PRESENT", x["measure"])
        for x in bb:
            x["hit"] = is_hit("PAIR_PRESENT", x["measure"])
        sr, sb = summarize_group(rr), summarize_group(bb)
        out[f"support>={ms}"] = {
            "real": sr, "baseline": sb,
            "lift": (round(sr["hit_rate"] / sb["hit_rate"], 3)
                     if sr["hit_rate"] is not None and sb["hit_rate"] else None),
        }
    return out


def print_report(rep, top_k):
    print("=" * 78)
    print(f"  P4-3 Tier-1 验证（词面，无 LLM）| 候选 Top-{top_k}")
    dd = rep["design"]
    print(f"  TRAIN {dd['train_papers']} 篇 | EVAL {dd['eval_papers']} 篇 | "
          f"判据见 design.criteria")
    print("-" * 78)
    pc = rep.get("positive_control")
    if pc:
        s = pc["summary"]
        print(f"  ── 阳性对照（度量灵敏度检验）: 命中 {s['hits']}/{s['n']} "
              f"= {s['hit_rate']} CI{s['hit_rate_wilson95']} "
              f"| 中位期率比 {s['median_rate_ratio']}")
        print(f"     注: {pc['note']}")
    if rep.get("sensitivity"):
        print("  ── 分层敏感性（NODE 按 type / PAIR 按 support）")
        for k, v in rep["sensitivity"].items():
            r, b = v["real"], v["baseline"]
            print(f"    {k:<12} 真实 {r['hits']}/{r['n']}={r['hit_rate']} "
                  f"| 基线 {b['hits']}/{b['n']}={b['hit_rate']} | lift {v['lift']}")
    print("-" * 78)
    for k, g in rep["groups"].items():
        r, b = g["real"], g["baseline"]
        print(f"  {k}")
        print(f"    真实候选: 命中 {r['hits']}/{r['n']} = {r['hit_rate']} "
              f"CI{r['hit_rate_wilson95']} | 中位期率比 {r['median_rate_ratio']}")
        print(f"    随机基线: 命中 {b['hits']}/{b['n']} = {b['hit_rate']} "
              f"CI{b['hit_rate_wilson95']} | 中位期率比 {b['median_rate_ratio']}")
        print(f"    lift = {g['lift']} | z={g['significance']['z']} "
              f"p={g['significance']['p_two_sided']}")
        print(f"    判定: {g['verdict']}  (不可测 {r['n_immmeasurable']})")
    print("=" * 78)
    print("  逐候选（真实侧）：")
    for k, v in rep["per_candidate"].items():
        for x in v["real"]:
            nm = x.get("pair") or [x.get("concept")]
            m = x["measure"]
            if not m.get("measurable"):
                print(f"    [{k:<12}] —— 不可测: {nm}")
                continue
            flag = "HIT " if x["hit"] else "miss"
            print(f"    [{k:<12}] {flag} train={m['train_lex']:<4} "
                  f"eval={m['eval_lex']:<4} ratio={m['rate_ratio']:<7} "
                  f"{' + '.join(nm)[:56]}")
    print("=" * 78)


def write_csv(path, rep):
    cols = ["group", "side", "id", "label", "support", "train_lex", "eval_lex",
            "rate_ratio", "hit"]
    lines = [",".join(cols)]
    for k, v in rep["per_candidate"].items():
        for side in ("real", "baseline"):
            for x in v[side]:
                m = x["measure"]
                label = " + ".join(x["pair"]) if x.get("pair") else x.get("concept", "")
                lines.append(",".join(str(z) for z in (
                    k, side, x.get("id") or "", label.replace(",", ";"),
                    x.get("support"), m.get("train_lex"), m.get("eval_lex"),
                    m.get("rate_ratio"), x.get("hit"))))
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="写报告（默认 dry-run）")
    ap.add_argument("--candidates", default=str(CANDIDATES_PATH))
    ap.add_argument("--out", default=str(OUT_PATH))
    ap.add_argument("--csv", default=str(OUT_CSV))
    ap.add_argument("--top-k", type=int, default=20,
                    help="每类取前 K 个候选做验证")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--min-pair-support", type=int, default=2,
                    help="只验证支撑度 >= 此值的 PAIR 候选（功效敏感性分析用）")
    args = ap.parse_args(argv)

    rep = run(args)
    print_report(rep, args.top_k)

    # 冻结完整性：报告的候选指纹必须与冻结产物一致
    cur = _sha256(args.candidates)
    if rep["inputs"]["candidates_sha256"] != cur:
        raise SystemExit("[refused] 候选文件在验证过程中被改动")
    if not args.apply:
        print("\n[dry-run] 未写报告。加 --apply 落盘")
        return 0
    Path(args.out).write_text(json.dumps(rep, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    write_csv(args.csv, rep)
    print(f"\n[ok] 报告 -> {_rel(args.out)}\n[ok] 表 -> {_rel(args.csv)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
