#!/usr/bin/env python
"""P4-0 Step 1：构建时间隔离基准数据集的**统一元数据表**。

用户 2026-09-12 裁定：
  Q1 语料池   = ``openalex_cache``（P4-0 首版主池）
  Q2 时间切分 = ≤2020 TRAIN / 2021-2025 EVAL
  Q3 统一表   = 必做（本工具）
  Q4 抽取规模 = 1000-2000 篇分层采样（P4-1，不在本步）

产物（``datasets/photopolymerization_v1/``）::

    paper_meta.db     统一元数据表 + dataset_meta
    train_papers.jsonl
    eval_papers.jsonl
    benchmark.yaml    冻结的实验协议（含本步实测计数）
    build_report.json 验收报告
    extraction_cache/ P4-1 抽取缓存目录（本步只建空目录）

设计要点
--------
1. **反泄漏是这个数据集的第一属性**，不是后置检查。
   OpenAlex 的 ``cited_by_count`` 是 **2026 快照**，它包含 2021-2025 的引用 ——
   拿它当 TRAIN 特征就是把答案喂给模型。故：
     ``citation_count``        快照值，**仅供 EVAL 侧/参考**，禁止做 TRAIN 特征
     ``citations_asof_cutoff`` 截断到 2020 的引用数，TRAIN 侧唯一可用口径
   并保留原始 ``counts_by_year`` 直方图，使 P4-2 可以任选 cutoff 重算。
2. **年份缺失一律 NULL，禁止插值**。时间隔离数据集里一个假年份比缺一个样本危险得多。
3. **身份复用 KB 的 identity 规则**（同一 `make_paper_uid`，只改优先级参数），
   不新写归一化 —— 并同时落 ``preferred_identifier`` 与 ``kb_paper_uid`` 作显式桥，
   避免出现第二个身份命名空间。
4. **重复实体只标记不合并**（KB 铁律）。三档证据分别计数：同 uid / 同 DOI / 同题同年。
5. 主库 ``knowledge_base.db`` 与 ``scopus_cache.db`` 全程**只读**。

用法::

    python tools/build_p4_0_dataset.py              # dry-run（默认，零写入）
    python tools/build_p4_0_dataset.py --apply      # 实际落盘
"""
from __future__ import annotations

import argparse
import collections
import datetime
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from search_engine.identity import (  # noqa: E402
    IdentifierClaim,
    extract_from_paper_uid,
    make_paper_uid,
    normalize_identifier,
    normalize_title,
    scopus_cache_key_value,
    uid_prefix,
)
from search_engine.paper_writer import uid_type_matches_value  # noqa: E402

BUILD_VERSION = "p4_0_v2"
DATASET_VERSION = "photopolymerization_v1"

OPENALEX_CACHE = BASE / "data" / "cache" / "openalex_cache.json"
SCOPUS_DB = BASE / "data" / "cache" / "scopus_cache.db"
KB_DB = BASE / "data" / "cache" / "knowledge_base.db"
OUT_DIR = BASE / "datasets" / DATASET_VERSION

# ── 时间切分（用户裁定 Q2，冻结）─────────────────────────────────────────
TRAIN_MAX_YEAR = 2020
EVAL_MIN_YEAR = 2021
EVAL_MAX_YEAR = 2025

YEAR_MIN_PLAUSIBLE = 1900
YEAR_MAX_PLAUSIBLE = 2100

# ── 身份优先级（用户裁定：P4 语料 OpenAlex 优先）──────────────────────────
P4_PRIORITY = {"OPENALEX": 0, "DOI": 1, "SCOPUS_EID": 2,
               "PUBMED": 3, "ARXIV": 4, "ISBN": 5, "URL": 6}

# ── 排除规则（保守；每条都在报告里计数，不做静默丢弃）────────────────────
NON_RESEARCH_TYPES = frozenset({
    "paratext", "peer-review", "editorial", "erratum",
    "retraction", "book-review", "reference-entry",
})

SPLIT_TRAIN = "TRAIN"
SPLIT_EVAL = "EVAL"
SPLIT_OUT_OF_RANGE = "EXCLUDED_OUT_OF_RANGE"
SPLIT_NO_YEAR = None

EXCL_RETRACTED = "EXCLUDED_RETRACTED"
EXCL_PARATEXT = "EXCLUDED_PARATEXT"
EXCL_TYPE = "EXCLUDED_NON_RESEARCH_TYPE"

NOT_SET = "NOT_SET"

# ══ 范围门（scope gate）══════════════════════════════════════════════════
# 为什么必须有它 —— 这是本步最重要的发现，不是洁癖：
#   语料池由 `filter=title_and_abstract.search:"..."` / `q="title/abstract
#   has (...)"` 这类**松散文本匹配**构造，且 `sort=cited_by_count:desc`。
#   松散匹配 + 按引用降序 = 必然返回**全库引用最高的论文**。实测每一页都把
#   「R: A Language and Environment for Statistical Computing」(35.3 万引)、
#   「Generalized Gradient Approximation Made Simple」(21.5 万引)、
#   「Deep Residual Learning for Image Recognition」(22.7 万引) 一起捞了进来。
#   后果：分层采样的 Layer 1（高影响基础论文）会被这些与光固化无关的
#   全球巨引论文占满，concept graph 会长在 R 语言和 PCR 方法学上。
#
# 门的设计原则：高召回（宁滥勿缺），**不静默丢弃**，逐行留证据。
#   实测（14,318 篇池）：
#     STRONG                 6,882 (48.1%)  KB 召回 77.8%  巨引残留 0/10
#     STRONG|ADJ            11,436 (79.9%)  KB 召回 98.4%  巨引残留 0/10
#     STRONG|ADJ|CHANNEL    12,653 (88.4%)  KB 召回 98.4%  巨引残留 0/10
#     STRONG|CHANNEL        10,256 (71.6%)  KB 召回 84.1%  巨引残留 0/10
#   取 STRONG|ADJ —— 召回 98.4%（126 篇 KB∩池中漏 2 篇，见 scope.kb_recall），
#   且 10 篇全球巨引论文全部出局。CHANNEL 只作为**记录项**（可回填的扩张旋钮），
#   因为它会把「引用了种子论文但与光固化无关」的论文一并放进来。
STRICT_RE = re.compile(r"""
 photo-?\s?polymer | photocure | photo-?\s?curab | photocrosslink | photoinitiat
| photosensiti | photolithograph | photoresist | photoacid
| light-?\s?fueled | \buv-?\s?cur | light-?\s?cur | ultraviolet\s+cur
| thiol-?\s?ene | vinylcyclopropane | vinyl\s+sulfonate
| ring-?\s?opening\s+polymeri | \bROMP\b | ring\s+strain\s+relief
| polymeri[sz]ation\s+shrinkage | shrinkage\s+stress | shrink\s+stress
| degree\s+of\s+conversion | double-?\s?bond\s+conversion | monomer\s+elution
| dental\s+(?:composite|restorat|adhesive|resin)
| composite\s+resin | resin\s+composite | resin\s+cement | bulk-?\s?fill
| stereolithograph | vat\s+photopolymer | two-?\s?photon\s+polymeri | \bDLP\b | \bSLA\b
| 光聚合 | 光固化 | 光致聚合物 | 紫外固化 | 光刻 | 光敏树脂 | 光引发剂
""", re.I | re.X)

# ⚠️ 上面必须用 ``\s+`` 而不是裸空格：``re.X``（verbose）模式下**模式里的空白会被忽略**，
# 写 ``polymerization shrinkage`` 实际编译成 ``polymerizationshrinkage``，永远匹配不上。
# 这个 bug 曾让整个强特征层形同虚设（多词短语全部失效），只剩单字词兜底。
# 见 tests/test_p4_0_dataset.py::test_multiword_patterns_actually_match

# 广义材料/高分子词族：**单独不足以定范围**，必须 >= ADJACENT_MIN_FAMILIES 个不同词族同时命中。
# 词族命中现在**只记录、不决定** in_scope —— P4-1A pilot 证明词面门在每一层都会漏
# （epoxy / thiol / conversion / polymer 在别的材料学科同样高频）。
ADJACENT_TERMS = (
    ("polymer", re.compile(r"\bpolymeri[sz]|\bpolymer", re.I)),
    ("monomer", re.compile(r"\bmonomer|macromonomer", re.I)),
    ("acrylate", re.compile(r"acrylat|methacrylat", re.I)),
    ("oligomer", re.compile(r"\boligomer", re.I)),
    ("resin", re.compile(r"\bresin", re.I)),
    ("curing", re.compile(r"\bcur(?:e|es|ed|ing|able)\b", re.I)),
    ("conversion", re.compile(r"\bconversions?\b", re.I)),
    ("crosslinking", re.compile(r"cross-?link", re.I)),
    ("gelation", re.compile(r"gelation|gel\s+point", re.I)),
    ("epoxy", re.compile(r"\bepoxy|epoxid", re.I)),
    ("thiol", re.compile(r"\bthiol", re.I)),
    ("composite", re.compile(r"\bcomposites?\b", re.I)),
    ("dental", re.compile(r"\bdental\b", re.I)),
    ("photochemistry", re.compile(
        r"photochemic|photoreactive|photosensiti|photoinduced|photostable", re.I)),
    ("hydrogel", re.compile(r"hydrogel", re.I)),
    ("thermoset", re.compile(r"thermoset|thermosetting", re.I)),
    ("mechanics", re.compile(r"mechanical\s+propert|tensile\s+strength|elastic\s+modulus",
                             re.I)),
    ("filler", re.compile(r"\bfiller", re.I)),
    ("zh", re.compile(r"光聚合|光固化|光敏|紫外|固化|聚合|收缩|单体|树脂|交联"
                      r"|复合材料|牙科|增材制造", re.I)),
)
ADJACENT_MIN_FAMILIES = 2

# ── 范围档 ────────────────────────────────────────────────────────────
SCOPE_TOPIC = "TOPIC_ALLOW"        # primary_topic 在白名单内 -> in_scope（主信号）
SCOPE_TEXT = "CORE_TEXT"           # 严格词面命中 -> in_scope（召回救援）
SCOPE_ADJACENT_ONLY = "ADJACENT_ONLY"   # 仅广义词族（>=2）-> **不**在范围内（只记录）
SCOPE_CHANNEL_ONLY = "CHANNEL_ONLY"     # 无词面证据但有定向通道 -> **不**在范围内
SCOPE_OFF_TOPIC = "OFF_TOPIC"           # 无任何证据
IN_SCOPE_TIERS = frozenset({SCOPE_TOPIC, SCOPE_TEXT})
SCOPE_TIER_ORDER = (SCOPE_TOPIC, SCOPE_TEXT, SCOPE_ADJACENT_ONLY,
                    SCOPE_CHANNEL_ONLY, SCOPE_OFF_TOPIC)

# 定向通道：查询本身带主题约束（引文扩展 / 标题检索 / 单篇查询）
TARGETED_CHANNELS = frozenset({"title_search", "cites_expansion", "single_work"})

SCOPE_ALLOWLIST = BASE / "datasets" / DATASET_VERSION / "scope_allowlist.yaml"


def load_scope_allowlist(path=SCOPE_ALLOWLIST):
    """读**已冻结**的主题白名单（committed 资产，改动需重跑 build + 采样）。

    返回 ``(topic_set, meta)``。文件缺失时返回空集 —— 那会让范围门退化成
    纯词面门（精度会掉），所以调用方会把这件事显式报出来。
    """
    if not Path(path).exists():
        return set(), {"loaded": False, "path": _rel(path), "n": 0}
    import yaml
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    topics = {e["topic"] for e in (doc.get("include") or []) if e.get("topic")}
    pending = {e["topic"] for e in (doc.get("pending_review") or []) if e.get("topic")}
    return topics, {"loaded": True, "path": _rel(path),
                    "version": doc.get("version"), "n": len(topics),
                    "pending_review": sorted(pending)}


def classify_channel(url, payload):
    """把缓存响应 URL 归类为来源通道（决定该响应是否受主题约束）。"""
    u = str(url).lower()
    if "title.search:" in u:
        return "title_search"          # filter=title.search:... -> 主题约束强
    if "cites:" in u:
        return "cites_expansion"       # filter=cites:W...     -> 种子引文，主题相关
    if payload.get("results"):
        return "keyword_search"        # title_and_abstract.search / q= -> 松散匹配
    return "single_work"


def scope_of(title_abstract_text, channels, primary_topic=None, allowlist=None):
    """范围判定：返回 ``(tier, evidence)``。

    判定顺序（**主题优先**）：
      1. ``primary_topic`` ∈ 白名单          -> ``TOPIC_ALLOW``（主信号，精度）
      2. 严格词面命中（STRICT_RE）            -> ``CORE_TEXT``（召回救援）
      3. 仅 >=2 个广义词族                   -> ``ADJACENT_ONLY``（只记录）
      4. 有定向通道但无词面证据               -> ``CHANNEL_ONLY``（只记录）
      5. 其余                                -> ``OFF_TOPIC``

    为什么不是「词面单独定范围」：P4-1A pilot 实测，纯词面门抽出的 30 篇里
    ~17 篇离题（DNA 甲基化 / 石墨烯 / 有机光伏 / 热重分析 / T 细胞免疫 /
    剂量换算 / 蛋白序列比对），逐个收紧词表后仍漏 8 篇 —— 因为
    ``epoxy``/``thiol``/``conversion``/``polymer`` 在别的材料学科同样高频。
    """
    text = title_abstract_text or ""
    allowlist = allowlist or set()
    if primary_topic and primary_topic in allowlist:
        return SCOPE_TOPIC, [f"topic:{primary_topic}"]
    strong = sorted({m.group(0).lower() for m in STRICT_RE.finditer(text)})
    if strong:
        return SCOPE_TEXT, strong[:5]
    fams = sorted({name for name, rx in ADJACENT_TERMS if rx.search(text)})
    if len(fams) >= ADJACENT_MIN_FAMILIES:
        return SCOPE_ADJACENT_ONLY, fams[:6]
    if channels & TARGETED_CHANNELS:
        return SCOPE_CHANNEL_ONLY, sorted(channels)
    return SCOPE_OFF_TOPIC, fams[:3]



def _rel(path) -> str:
    """展示用相对路径（跨盘符安全，见 P0-B2.2 的教训）。"""
    try:
        return os.path.relpath(str(path), str(BASE)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _sha256(path, limit=None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _doi(raw):
    """裸 DOI 归一化（走 identity 的唯一口径，但容错 URL 包壳）。"""
    if raw is None:
        return None
    s = str(raw).strip()
    low = s.lower()
    for p in ("https://doi.org/", "http://doi.org/", "http://dx.doi.org/",
              "https://dx.doi.org/", "doi:"):
        if low.startswith(p):
            s = s[len(p):]
            break
    return normalize_identifier("DOI", s)


def _wid(raw):
    if raw is None:
        return None
    return normalize_identifier("OPENALEX", str(raw))


def rebuild_abstract(inverted):
    """OpenAlex 的 ``abstract_inverted_index`` -> 可读摘要。

    形态：``{token: [pos, ...]}``。按位置还原即可（不做任何改写/截断）。
    """
    if not inverted:
        return None
    pos = {}
    for token, idxs in inverted.items():
        for i in idxs:
            pos[i] = token
    if not pos:
        return None
    return " ".join(pos[i] for i in sorted(pos))


def load_openalex_pool(path):
    """展开缓存响应为 ``{openalex_url: work}``，并记录每个 work 的**来源通道**。

    缓存里混了两种响应形态：search 响应（``results``）与单篇查询（顶层即 work）。
    两者都取，按 openalex work id 去重（单篇查询实测全部落在 search 结果内，
    去重后不会凭空增加语料 —— 这一点在报告里记录）。

    来源通道以 ``__channels``（set）挂在 work 上，供范围门使用：
    通道是**可复现的构造证据** —— 它记录了这篇论文是被哪类查询捞回来的。
    """
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)

    by_id = {}
    chan_stat = collections.Counter()
    n_search = n_single = n_expanded = 0
    for url, payload in raw.items():
        if not isinstance(payload, dict):
            continue
        chan = classify_channel(url, payload)
        chan_stat[chan] += 1
        if payload.get("results"):
            n_search += 1
            for w in payload["results"]:
                n_expanded += 1
                wid = w.get("id")
                if not wid:
                    continue
                if wid not in by_id:
                    w["__channels"] = set()
                    w["__origin"] = chan
                    by_id[wid] = w
                by_id[wid]["__channels"].add(chan)
        elif payload.get("id"):
            n_single += 1
            wid = payload["id"]
            if wid not in by_id:
                payload["__channels"] = set()
                payload["__origin"] = chan
                by_id[wid] = payload
            by_id[wid]["__channels"].add(chan)
    return by_id, {"responses": len(raw), "search_responses": n_search,
                   "single_work_responses": n_single,
                   "works_expanded": n_expanded, "works_deduped": len(by_id),
                   "channel_responses": dict(chan_stat)}


def load_scopus_abstract_index(path):
    """``DOI -> (abstract, scopus_url)``。只读；用于摘要回填，不引入新语料。"""
    idx = {}
    if not Path(path).exists():
        return idx
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for pid, rj in con.execute("SELECT paper_id, normalized_json FROM papers"):
            try:
                d = json.loads(rj)
            except Exception:
                continue
            # 缓存键的反解走唯一出口（P0-B1b 收口）：不做 startswith("scopus:") 手剥
            doi = _doi(d.get("doi")) or _doi(scopus_cache_key_value(pid))
            if not doi:
                continue
            ab = (d.get("abstract") or "").strip()
            url = (d.get("scopus_url") or "").strip()
            if ab or url:
                idx[doi] = (ab or None, url or None)
    finally:
        con.close()
    return idx


def load_kb_bridge(path):
    """KB 交叉引用桥。

    返回 ``(by_key, eid_by_uid)``：
      by_key : ``(id_type, normalized_value) -> kb_paper_uid``
      eid_by_uid : ``kb_paper_uid -> scopus_eid``
    KB 全程只读。
    """
    by_key, eid_by_uid = {}, {}
    if not Path(path).exists():
        return by_key, eid_by_uid
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for t, v, u in con.execute(
                "SELECT id_type, normalized_value, paper_uid FROM paper_identifiers"):
            by_key.setdefault((t, v), u)
            if t == "SCOPUS_EID":
                eid_by_uid.setdefault(u, v)
    finally:
        con.close()
    return by_key, eid_by_uid


def _split_of(year):
    if not isinstance(year, int) or not (YEAR_MIN_PLAUSIBLE < year < YEAR_MAX_PLAUSIBLE):
        return SPLIT_NO_YEAR
    if year <= TRAIN_MAX_YEAR:
        return SPLIT_TRAIN
    if EVAL_MIN_YEAR <= year <= EVAL_MAX_YEAR:
        return SPLIT_EVAL
    return SPLIT_OUT_OF_RANGE


def build_records(by_id, scopus_idx, kb_by_key, kb_eid_by_uid, allowlist=None):
    """把 OpenAlex work 转成 paper_meta 行（纯计算，零写入）。"""
    records = []
    stats = collections.Counter()

    for wid_url, w in by_id.items():
        wid = _wid(wid_url)
        doi = _doi(w.get("doi"))
        year_raw = w.get("publication_year")
        year = year_raw if isinstance(year_raw, int) and \
            YEAR_MIN_PLAUSIBLE < year_raw < YEAR_MAX_PLAUSIBLE else None

        # ── 摘要：OpenAlex 优先，scopus_cache 回填 ──────────────────────
        abstract = rebuild_abstract(w.get("abstract_inverted_index"))
        abstract_source = "openalex" if abstract else None
        scopus_url = None
        if doi and doi in scopus_idx:
            sc_ab, scopus_url = scopus_idx[doi]
            if not abstract and sc_ab:
                abstract = sc_ab
                abstract_source = "scopus_cache"
            stats["scopus_cache_hit"] += 1

        # ── 身份（唯一出口；P4 优先级 = OPENALEX 优先）──────────────────
        claims = []
        for id_type, val in (("OPENALEX", wid), ("DOI", doi)):
            if val:
                claims.append(IdentifierClaim(id_type, val, val, None,
                                              "p4_0.openalex_cache"))
        paper_uid = make_paper_uid(claims=claims,
                                   title=w.get("title") or w.get("display_name"),
                                   year=year, priority=P4_PRIORITY)
        # 同一优先级下的「最优引用标识」（KB 口径 DOI 优先）—— 用户裁定的
        # {entity_id, preferred_identifier} 两个概念，这里落成两列。
        preferred = make_paper_uid(claims=claims, title=w.get("title") or
                                   w.get("display_name"), year=year)

        # ── KB 交叉桥（按 DOI 再 OPENALEX，取 EID）──────────────────────
        kb_uid = None
        for t, v in (("DOI", doi), ("OPENALEX", wid)):
            if v and (t, v) in kb_by_key:
                kb_uid = kb_by_key[(t, v)]
                break
        scopus_eid = kb_eid_by_uid.get(kb_uid) if kb_uid else None
        if kb_uid:
            stats["kb_bridge_hit"] += 1

        # ── 作者 / 期刊 ────────────────────────────────────────────────
        auths = []
        for a in (w.get("authorships") or []):
            name = ((a.get("author") or {}).get("display_name") or "").strip()
            if name:
                auths.append(name)
        src = ((w.get("primary_location") or {}).get("source") or {})
        venue = (src.get("display_name") or "").strip() or None

        # ── 引用：快照 vs 反泄漏口径 ───────────────────────────────────
        cby = [x for x in (w.get("counts_by_year") or [])
               if isinstance(x.get("year"), int)]
        win_start = min((x["year"] for x in cby), default=None)
        win_end = max((x["year"] for x in cby), default=None)
        asof = None
        if cby:
            asof = sum(x.get("cited_by_count") or 0
                       for x in cby if x["year"] <= TRAIN_MAX_YEAR)

        topics = [t.get("display_name") for t in (w.get("topics") or [])
                  if t.get("display_name")]
        kws = [k.get("display_name") for k in (w.get("keywords") or [])
               if k.get("display_name")]

        # ── 排除规则 ───────────────────────────────────────────────────
        reason = None
        if w.get("is_retracted"):
            reason = EXCL_RETRACTED
        elif w.get("is_paratext") or (w.get("type") in NON_RESEARCH_TYPES):
            reason = EXCL_PARATEXT if w.get("is_paratext") else EXCL_TYPE

        # ── 范围判定（主题优先；用回填后的摘要，召回更高）───────────────
        channels = set(w.get("__channels") or ())
        primary_topic = ((w.get("primary_topic") or {}).get("display_name"))
        scope_tier, scope_evidence = scope_of(
            ((w.get("title") or w.get("display_name") or "") + " "
             + (abstract or "")), channels,
            primary_topic=primary_topic, allowlist=allowlist)

        records.append({
            "paper_uid": paper_uid,
            "title": (w.get("title") or w.get("display_name") or "").strip() or None,
            "abstract": abstract,
            "year": year,
            "doi": doi,
            "openalex_id": wid,
            "scopus_eid": scopus_eid,
            "source": "openalex_cache",
            "citation_count": w.get("cited_by_count"),
            "authors": auths,
            "venue": venue,
            "split": _split_of(year),
            # ── 扩展字段 ───────────────────────────────────────────────
            "preferred_identifier": preferred,
            "kb_paper_uid": kb_uid,
            "citations_asof_cutoff": asof,
            "counts_by_year_json": json.dumps(cby, ensure_ascii=False) if cby else None,
            "counts_window_start": win_start,
            "counts_window_end": win_end,
            "primary_topic": primary_topic,
            "topics_json": json.dumps(topics, ensure_ascii=False) if topics else None,
            "keywords_json": json.dumps(kws, ensure_ascii=False) if kws else None,
            "n_authors": len(auths),
            "type": w.get("type"),
            "language": w.get("language"),
            "is_retracted": 1 if w.get("is_retracted") else 0,
            "is_paratext": 1 if w.get("is_paratext") else 0,
            "referenced_works_count": w.get("referenced_works_count"),
            "fwci": w.get("fwci"),
            "abstract_source": abstract_source,
            "scopus_url": scopus_url,
            "duplicate_group_id": None,
            "exclusion_reason": reason,
            # ── 范围证据 ───────────────────────────────────────────────
            "scope_tier": scope_tier,
            "in_scope": 1 if scope_tier in IN_SCOPE_TIERS else 0,
            "scope_evidence": json.dumps(scope_evidence, ensure_ascii=False),
            "provenance_channel": ",".join(sorted(channels)),
            "source_json": json.dumps({
                "origin": w.get("__origin"),
                "channels": sorted(channels),
                "openalex": wid_url,
                "ids": w.get("ids") or {},
                "cache": _rel(OPENALEX_CACHE),
            }, ensure_ascii=False),
        })
    return records


def mark_duplicates(records):
    """三档证据标记重复实体候选；**只标记不合并**。

    档 1 同 uid（构造保证为 0）｜档 2 同 DOI 多 uid｜档 3 同题同年多 uid。
    强证据优先：同一行若同时命中多档，``duplicate_group_id`` 取最强档。
    """
    by_doi = collections.defaultdict(list)
    by_title = collections.defaultdict(list)
    by_uid = collections.defaultdict(list)
    for r in records:
        by_uid[r["paper_uid"]].append(r)
        if r["doi"]:
            by_doi[r["doi"]].append(r)
        t = normalize_title(r["title"])
        if t:
            by_title[(t, r["year"])].append(r)

    dup_uid = [g for g in by_uid.values() if len(g) > 1]
    dup_doi = {k: v for k, v in by_doi.items() if len(v) > 1}
    dup_title = {k: v for k, v in by_title.items() if len(v) > 1}

    for k, grp in dup_doi.items():
        gid = "DOI_COLLISION:" + hashlib.sha256(k.encode()).hexdigest()[:12]
        for r in grp:
            r["duplicate_group_id"] = gid
    for (t, y), grp in dup_title.items():
        gid = "TITLE_YEAR_COLLISION:" + hashlib.sha256(
            f"{t}|{y}".encode()).hexdigest()[:12]
        for r in grp:
            if not r["duplicate_group_id"]:
                r["duplicate_group_id"] = gid

    return {
        "same_uid": {"groups": len(dup_uid),
                     "rows": sum(len(g) for g in dup_uid)},
        "same_doi_multi_uid": {"groups": len(dup_doi),
                               "rows": sum(len(v) for v in dup_doi.values())},
        "same_title_year_multi_uid": {"groups": len(dup_title),
                                      "rows": sum(len(v) for v in dup_title.values())},
        "policy": ("**只标记不合并**（KB 铁律）。同 DOI 是强证据但可能是"
                   "版本/镜像；同题同年大量为 preprint、book chapter、"
                   "以及标题极短的普通论文，**不得**作为合并依据"),
    }


DDL = (
    """CREATE TABLE paper_meta (
    paper_uid              TEXT PRIMARY KEY,
    title                  TEXT,
    abstract               TEXT,
    year                   INTEGER,
    doi                    TEXT,
    openalex_id            TEXT,
    scopus_eid             TEXT,
    source                 TEXT NOT NULL,
    citation_count         INTEGER,
    authors                TEXT,
    venue                  TEXT,
    split                  TEXT,
    preferred_identifier   TEXT,
    kb_paper_uid           TEXT,
    citations_asof_cutoff  INTEGER,
    counts_by_year_json    TEXT,
    counts_window_start    INTEGER,
    counts_window_end      INTEGER,
    primary_topic          TEXT,
    topics_json            TEXT,
    keywords_json          TEXT,
    n_authors              INTEGER,
    type                   TEXT,
    language               TEXT,
    is_retracted           INTEGER NOT NULL DEFAULT 0,
    is_paratext            INTEGER NOT NULL DEFAULT 0,
    referenced_works_count INTEGER,
    fwci                   REAL,
    abstract_source        TEXT,
    scopus_url             TEXT,
    duplicate_group_id     TEXT,
    exclusion_reason       TEXT,
    scope_tier             TEXT,
    in_scope               INTEGER NOT NULL DEFAULT 0,
    scope_evidence         TEXT,
    provenance_channel     TEXT,
    source_json            TEXT
)""",
    "CREATE INDEX idx_pm_split ON paper_meta(split)",
    "CREATE INDEX idx_pm_year ON paper_meta(year)",
    "CREATE INDEX idx_pm_topic ON paper_meta(primary_topic)",
    "CREATE INDEX idx_pm_doi ON paper_meta(doi)",
    "CREATE INDEX idx_pm_scope ON paper_meta(in_scope, split)",
    """CREATE VIEW v_photopolymerization AS
    SELECT * FROM paper_meta
    WHERE in_scope = 1 AND exclusion_reason IS NULL AND split IN ('TRAIN','EVAL')""",
    """CREATE TABLE dataset_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
)""",
)

COLUMNS = ("paper_uid", "title", "abstract", "year", "doi", "openalex_id",
           "scopus_eid", "source", "citation_count", "authors", "venue", "split",
           "preferred_identifier", "kb_paper_uid", "citations_asof_cutoff",
           "counts_by_year_json", "counts_window_start", "counts_window_end",
           "primary_topic", "topics_json", "keywords_json", "n_authors", "type",
           "language", "is_retracted", "is_paratext", "referenced_works_count",
           "fwci", "abstract_source", "scopus_url", "duplicate_group_id",
           "exclusion_reason", "scope_tier", "in_scope", "scope_evidence",
           "provenance_channel", "source_json")

JSONL_FIELDS = ("paper_uid", "title", "abstract", "year", "doi", "openalex_id",
                "scopus_eid", "venue", "authors", "n_authors", "primary_topic",
                "citation_count", "citations_asof_cutoff",
                "referenced_works_count", "type", "fwci",
                "duplicate_group_id", "abstract_source",
                "in_scope", "scope_tier")


def write_db(records, db_path, meta):
    if Path(db_path).exists():
        Path(db_path).unlink()
    con = sqlite3.connect(db_path)
    try:
        for stmt in DDL:
            con.execute(stmt)
        placeholders = ",".join("?" * len(COLUMNS))
        con.executemany(
            f"INSERT INTO paper_meta ({','.join(COLUMNS)}) VALUES ({placeholders})",
            [tuple(json.dumps(r[c], ensure_ascii=False)
                   if c == "authors" else r[c] for c in COLUMNS)
             for r in records])
        con.executemany("INSERT INTO dataset_meta (key, value) VALUES (?,?)",
                        [(k, json.dumps(v, ensure_ascii=False) if not isinstance(v, str)
                          else v) for k, v in meta.items()])
        con.commit()
    finally:
        con.close()


def write_jsonl(records, path, split):
    rows = [r for r in records if r["split"] == split and not r["exclusion_reason"]]
    rows.sort(key=lambda r: (-(r.get("citations_asof_cutoff") or 0),
                             -(r.get("citation_count") or 0), r["paper_uid"]))
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps({k: r[k] for k in JSONL_FIELDS},
                               ensure_ascii=False) + "\n")
    return len(rows)


def acceptance(records, dup, pool_meta, kb_keys=(), allowlist_meta=None):
    """用户要求的六项验收 + 反泄漏完整性 + 范围门审计。"""
    total = len(records)
    inc = [r for r in records if not r["exclusion_reason"]]
    train = [r for r in inc if r["split"] == SPLIT_TRAIN]
    ev = [r for r in inc if r["split"] == SPLIT_EVAL]
    out = [r for r in records if r["split"] == SPLIT_OUT_OF_RANGE]
    noyear = [r for r in records if r["split"] is None]

    def cov(fn):
        n = sum(1 for r in records if fn(r))
        return n, round(n / total, 4) if total else 0.0

    # ── 范围门：分层计数 + 对 KB 已知相关论文的召回校验 ──────────────────
    tiers = collections.Counter(r["scope_tier"] for r in records)
    in_scope = [r for r in records if r["in_scope"]]
    kb_keys = set(kb_keys)
    kb_in_pool = [r for r in records
                  if ("DOI", r["doi"]) in kb_keys
                  or ("OPENALEX", r["openalex_id"]) in kb_keys]
    kb_missed = [r for r in kb_in_pool if not r["in_scope"]]
    n_kb = len(kb_in_pool)
    allowlist_meta = allowlist_meta or {}

    def _recall(pred):
        if not n_kb:
            return None
        return round(sum(1 for r in kb_in_pool if pred(r)) / n_kb, 4)

    topic_only = {SCOPE_TOPIC}
    adopted = {SCOPE_TOPIC, SCOPE_TEXT}
    plus_adj = adopted | {SCOPE_ADJACENT_ONLY}
    scope_block = {
        "tiers": {k: tiers[k] for k in SCOPE_TIER_ORDER},
        "allowlist": allowlist_meta,
        "variant_comparison": {
            "topic_only（主题白名单单独）": {
                "in_scope": sum(1 for r in records if r["scope_tier"] in topic_only),
                "kb_recall": _recall(lambda r: r["scope_tier"] in topic_only),
            },
            "topic_or_strict_text（采用）": {
                "in_scope": sum(1 for r in records if r["scope_tier"] in adopted),
                "kb_recall": _recall(lambda r: r["scope_tier"] in adopted),
                "note": "严格词面门做召回救援：主题被 OpenAlex 误分类但文本明确是光固化的论文",
            },
            "再加 ADJACENT_ONLY": {
                "in_scope": sum(1 for r in records if r["scope_tier"] in plus_adj),
                "kb_recall": _recall(lambda r: r["scope_tier"] in plus_adj),
                "note": "P4-1A pilot 证明这一档会放入石墨烯/光伏/免疫学等离题论文 -> 不采用",
            },
        },
        "in_scope": len(in_scope),
        "in_scope_pct": round(len(in_scope) / total, 4) if total else 0.0,
        "in_scope_train": sum(1 for r in in_scope if r["split"] == SPLIT_TRAIN),
        "in_scope_eval": sum(1 for r in in_scope if r["split"] == SPLIT_EVAL),
        # 视图才是 P4-1 的操作口径 —— 与 in_scope_train/eval 的差异要显式给出，
        # 否则「6,636 vs 6,604」看起来像算术错误：差异来自正交的第二维
        # （exclusion_reason：撤稿 / paratext / 非研究类型）。
        "view_counts": {
            "view": "v_photopolymerization",
            "definition": "in_scope=1 AND exclusion_reason IS NULL AND split IN ('TRAIN','EVAL')",
            "total": sum(1 for r in in_scope
                         if not r["exclusion_reason"]
                         and r["split"] in (SPLIT_TRAIN, SPLIT_EVAL)),
            "train": sum(1 for r in in_scope
                         if not r["exclusion_reason"] and r["split"] == SPLIT_TRAIN),
            "eval": sum(1 for r in in_scope
                        if not r["exclusion_reason"] and r["split"] == SPLIT_EVAL),
        },
        "kb_recall": {
            "kb_intersect_pool": len(kb_in_pool),
            "missed": len(kb_missed),
            "recall": (round(1 - len(kb_missed) / len(kb_in_pool), 4)
                       if kb_in_pool else None),
            "method": ("以 KB paper_identifiers 里已收录的论文为已知相关集，"
                       "取其在池中的交集，检查范围门是否漏掉"),
            "missed_sample": [{"paper_uid": r["paper_uid"],
                               "title": (r["title"] or "")[:80],
                               "primary_topic": r["primary_topic"]}
                              for r in kb_missed[:10]],
        },
        "top_cited_in_scope": [
            {"paper_uid": r["paper_uid"], "cited": r["citation_count"],
             "title": (r["title"] or "")[:70], "tier": r["scope_tier"]}
            for r in sorted(in_scope, key=lambda x: -(x["citation_count"] or 0))[:8]],
        "why": ("语料池以松散文本匹配 + 按引用降序构造，必然混入全库巨引论文；"
                "不加范围门则 Layer 1 高影响层会被 R 语言/PCR/ResNet 一类"
                "与光固化无关的论文占满"),
    }

    return {
        "total_papers": total,
        "train_count": len(train),
        "eval_count": len(ev),
        "missing_year_count": len(noyear),
        "duplicate_entity_count": {
            "same_uid": dup["same_uid"]["rows"],
            "same_doi_multi_uid": dup["same_doi_multi_uid"]["rows"],
            "same_title_year_multi_uid": dup["same_title_year_multi_uid"]["rows"],
            "rows_with_group_id": sum(1 for r in records if r["duplicate_group_id"]),
        },
        "scope": scope_block,
        "identifier_coverage": {
            "openalex_id": cov(lambda r: bool(r["openalex_id"]))[0],
            "doi": cov(lambda r: bool(r["doi"]))[0],
            "scopus_eid": cov(lambda r: bool(r["scopus_eid"]))[0],
            "by_pct": {"openalex_id": cov(lambda r: bool(r["openalex_id"]))[1],
                       "doi": cov(lambda r: bool(r["doi"]))[1],
                       "scopus_eid": cov(lambda r: bool(r["scopus_eid"]))[1]},
            "note": ("scopus_eid 只能来自 KB paper_identifiers（缓存里 92,952 条 "
                     "scopus_url 全为数字形态，**不含** 2-s2.0- EID）—— 低覆盖率是"
                     "数据源事实，不是构建缺陷"),
        },
        "abstract_coverage": {
            "with_abstract": cov(lambda r: bool(r["abstract"]))[0],
            "pct": cov(lambda r: bool(r["abstract"]))[1],
            "from_openalex": sum(1 for r in records if r["abstract_source"] == "openalex"),
            "from_scopus_cache": sum(1 for r in records if r["abstract_source"] == "scopus_cache"),
        },
        "excluded": {
            "out_of_range_gt_2025": len(out),
            "no_year": len(noyear),
            "retracted": sum(1 for r in records if r["exclusion_reason"] == EXCL_RETRACTED),
            "paratext": sum(1 for r in records if r["exclusion_reason"] == EXCL_PARATEXT),
            "non_research_type": sum(1 for r in records if r["exclusion_reason"] == EXCL_TYPE),
            "total_included": len(inc),
            "total_excluded": total - len(inc),
        },
        "reconciliation": {
            # 两类口径**有交叠**，必须显式对账，否则看起来像算术错误：
            #   split 是「时间归属」（值域 TRAIN/EVAL/OUT_OF_RANGE/NULL）
            #   exclusion_reason 是「是否剔出语料」（正交的另一维）
            # 一篇 >2025 的撤稿论文同时属于两个桶。
            "identity": "total = included + excluded；"
                        "included = train + eval + out_of_range + no_year（限 included 内）",
            "total": total,
            "included": len(inc),
            "excluded": total - len(inc),
            "included_breakdown": {
                "train": sum(1 for r in inc if r["split"] == SPLIT_TRAIN),
                "eval": sum(1 for r in inc if r["split"] == SPLIT_EVAL),
                "out_of_range_gt_2025": sum(1 for r in inc
                                            if r["split"] == SPLIT_OUT_OF_RANGE),
                "no_year": sum(1 for r in inc if r["split"] is None),
            },
            "overlap_note": ("out_of_range_gt_2025 / no_year 计的是**全部**行，"
                             "其中可能有同时被 exclusion_reason 剔除的（如撤稿且 >2025）"),
        },
        "uid_invariants": {
            "prefix_and_value_form_match": all(
                uid_type_matches_value(r["paper_uid"])[0] for r in records),
            "all_uids_unique": len({r["paper_uid"] for r in records}) == total,
            "preferred_matches_uid_when_no_doi": all(
                r["preferred_identifier"] == r["paper_uid"]
                for r in records if not r["doi"]),
            "prefix_distribution": dict(collections.Counter(
                r["paper_uid"].split(":", 1)[0] for r in records)),
        },
        "leakage_integrity": {
            "train_rows_with_citations_asof": sum(
                1 for r in train if r["citations_asof_cutoff"] is not None),
            "train_citation_count_is_snapshot": True,
            "note": ("``citation_count`` 为 2026 快照、含未来引用，**禁止**做 TRAIN "
                     "特征；TRAIN 侧只允许 ``citations_asof_cutoff``。"
                     "该值受 OpenAlex counts_by_year 窗口（实测约 2012-2026）左截断"),
            "eval_papers_never_extracted": True,
        },
        "split_integrity": {
            "train_max_year": max((r["year"] for r in train), default=None),
            "eval_min_year": min((r["year"] for r in ev), default=None),
            "overlap": 0,
            "assert_no_future_in_train": all(
                r["year"] <= TRAIN_MAX_YEAR for r in train),
        },
        "pool": pool_meta,
        "distinct_primary_topic": len({r["primary_topic"] for r in inc
                                       if r["primary_topic"]}),
    }


def build_benchmark_yaml(acc, dup, meta, source_hashes):
    """冻结实验协议。本步只**定义**协议，不执行任何抽取/预测。"""
    return {
        "dataset": DATASET_VERSION,
        "build_version": BUILD_VERSION,
        "built_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "role": "Temporal Scientific Forecasting Benchmark (P4-0)",
        "statement": (
            "本数据集的目的不是「建立完整光固化数据库」，而是验证一个闭环："
            "历史文献结构 -> 知识表示 -> 方向预测 -> 未来验证。"
            "因此它的第一属性是**时间隔离**，不是覆盖度。"),
        "corpus": {
            "source": _rel(OPENALEX_CACHE),
            "source_sha256": source_hashes.get("openalex_cache"),
            "records_expanded": acc["pool"]["works_expanded"],
            "records_deduped": acc["pool"]["works_deduped"],
            "dedup_key": "OpenAlex work id",
            "record_source_label": "openalex_cache",
            "note": ("主池只此一处。scopus_cache.db 仅用于**摘要回填**，"
                     "不引入新语料；knowledge_base.db 仅用于身份桥与 EID 补齐，全程只读"),
        },
        "split": {
            "rule": "publication_year",
            "train": {"max_year": TRAIN_MAX_YEAR, "n": acc["train_count"]},
            "eval": {"min_year": EVAL_MIN_YEAR, "max_year": EVAL_MAX_YEAR,
                     "n": acc["eval_count"]},
            "excluded_out_of_range": {"n": acc["excluded"]["out_of_range_gt_2025"],
                                      "reason": "year > 2025；既不可训练也不可作 label"},
            "no_year": {"n": acc["excluded"]["no_year"],
                        "policy": "split=NULL，**禁止插值**；保留在 paper_meta 供审计"},
            "year_missing_policy": "NULL（时间隔离数据集里假年份比缺样本危险）",
            "total_records": acc["total_papers"],
            "included_records": acc["excluded"]["total_included"],
        },
        "leakage_policy": {
            "hard_rules": [
                "TRAIN 侧的一切特征只能来自 publication_year <= 2020 时可获得的信息",
                "cited_by_count 是 2026 快照，包含 2021-2025 的引用 —— **禁止**作为 TRAIN 特征、选样依据或任何排序键",
                "TRAIN 侧引用强度只允许使用 citations_asof_cutoff（= counts_by_year 中 year<=2020 之和）",
                "EVAL 侧语料不得参与任何 concept 抽取、graph 构建、指标计算或超参选择",
                "验证阶段只读 EVAL 侧；任何跨 split 的字符串/标识匹配都必须在 TRAIN 快照上做",
            ],
            "forbidden_train_features": ["citation_count",
                                         "citation_normalized_percentile",
                                         "counts_by_year_json（除 year<=2020 部分）",
                                         "fwci"],
            "allowed_train_features": ["title", "abstract", "year", "venue",
                                       "primary_topic", "topics_json",
                                       "keywords_json", "n_authors",
                                       "referenced_works_count",
                                       "citations_asof_cutoff"],
            "known_censoring": [{
                "field": "citations_asof_cutoff",
                "issue": ("OpenAlex counts_by_year 只返回约 15 年窗口"
                          "（本池实测 2012-2026），早于窗口起点的引用不可见"),
                "evidence": ("1995 年某篇 cited_by_count=406，而 counts_by_year "
                             "求和仅 203"),
                "consequence": "旧论文的 asof 引用被**左截断**，跨年代比较会低估早期论文",
                "mitigation": ("counts_window_start / counts_window_end 随行保存；"
                               "P4-2 若做引用速率类指标，必须同年份窗口比较，"
                               "或改用 TRAIN 内部相对排序"),
            }],
            "self_check": "build_report.json -> acceptance.leakage_integrity / split_integrity",
        },
        "scope": {
            "status": "APPLIED（构建时逐行判定，证据落库可审计）",
            "problem": (
                "语料池由 `filter=title_and_abstract.search:\"...\"` 与 "
                "`q=\"title/abstract has (...)\"` 这类**松散文本匹配**构造，"
                "并全部 `sort=cited_by_count:desc`。松散匹配 + 按引用降序 "
                "必然返回全库引用最高的论文：实测每一页都混入 "
                "R 语言(35.3 万引)、GGA(21.5 万引)、ResNet(22.7 万引)、"
                "Folin 酚试剂蛋白定量(31.9 万引) 等与光固化无关的论文"),
            "consequence": (
                "P4-1 的分层采样 Layer 1（高影响基础论文）会被这些论文占满，"
                "concept graph 会长在 R 语言与 PCR 方法学上"),
            "gate": {
                "tier_TOPIC_ALLOW": ("primary_topic ∈ scope_allowlist.yaml -> in_scope"
                                     "（**主信号**，语义级，精度来源）"),
                "tier_CORE_TEXT": ("STRICT_RE 严格词面命中 -> in_scope"
                                   "（召回救援：主题被 OpenAlex 误分类但文本明确）"),
                "tier_ADJACENT_ONLY": ("仅 >=2 个广义词族 -> **不**在范围内"
                                       "（只记录；pilot 证明这一档含石墨烯/光伏/免疫学）"),
                "tier_CHANNEL_ONLY": ("无词面证据但来自定向通道"
                                      "（title_search / cites_expansion / single_work）"
                                      "-> **不**在范围内，仅记录，可作扩张旋钮"),
                "tier_OFF_TOPIC": "无任何证据",
                "in_scope_definition": "tier ∈ {CORE, ADJACENT}",
                "validation": ("变体对照在同一次构建内实测（见 "
                               "result.variant_comparison）：STRONG 单独更严、"
                               "召回更低；STRONG|ADJACENT 召回达标；"
                               "再加 CHANNEL 召回不升却多纳入 1 千余篇，故不采用。"
                               "三者的「全球巨引残留」均为 0/10"),
                "threshold": "召回优先、宁滥勿缺（与本项目检索侧口径一致）",
            },
            "result": {
                "tiers": acc["scope"]["tiers"],
                "variant_comparison": acc["scope"]["variant_comparison"],
                "in_scope": acc["scope"]["in_scope"],
                "in_scope_pct": acc["scope"]["in_scope_pct"],
                "in_scope_train": acc["scope"]["in_scope_train"],
                "in_scope_eval": acc["scope"]["in_scope_eval"],
                "view_counts": acc["scope"]["view_counts"],
            },
            "kb_recall_check": acc["scope"]["kb_recall"],
            "usage": ("P4-1 采样必须从 `v_photopolymerization` 视图取"
                      "（= in_scope AND 未排除 AND split∈{TRAIN,EVAL}）；"
                      "CHANNEL_ONLY 的论文是**已记录未启用**的扩张池，"
                      "启用前需先评估其主题精度"),
            "not_silently_dropped": ("全部 14,318 行保留在 paper_meta，"
                                     "in_scope / scope_tier / scope_evidence "
                                     "逐行落库，范围判定可复核、可回滚"),
        },
        "identifier_policy": {
            "paper_uid": {
                "priority": ["OPENALEX", "DOI", "SCOPUS_EID"],
                "reason": "语料以 OpenAlex 为记录源（用户 2026-09-12 裁定）",
                "prefix_by_type": {t: uid_prefix(t)
                                   for t in ("OPENALEX", "DOI", "SCOPUS_EID")},
                "generator": "search_engine.identity.make_paper_uid（唯一出口，priority 参数化）",
                "invariant": "前缀与值形态必须一致；uid 生成后**永不变**",
            },
            "preferred_identifier": {
                "priority": ["DOI", "OPENALEX", "SCOPUS_EID"],
                "reason": ("KB 口径。用户裁定 entity_id 与 preferred_identifier 是"
                           "两个概念：前者像 git commit hash（不变），后者像 "
                           "branch pointer（可变）。resolver 返回 {entity_id, "
                           "preferred_identifier} 即可，不改写 entity_id"),
            },
            "kb_paper_uid": "跨库桥（NULL = 不在 KB）；防止出现第二个身份命名空间",
            "shared_code": "search_engine/identity.py —— 本数据集**不新写**归一化/前缀拼接",
        },
        "duplicate_entity_policy": {
            "policy": "只标记不合并（KB 铁律）；duplicate_group_id 由强到弱取档",
            "tiers": ["same_uid（构造保证 0）", "same_doi_multi_uid",
                      "same_title_year_multi_uid"],
            "counts": dup,
            "warning": ("同题同年共 "
                        f"{dup['same_title_year_multi_uid']['rows']} 行，"
                        "其中大量是 preprint、book chapter 或标题极短的普通论文 —— "
                        "**不得**作为合并依据，也不得在采样时静默去重"),
        },
        "exclusion_policy": {
            "retracted": acc["excluded"]["retracted"],
            "paratext": acc["excluded"]["paratext"],
            "non_research_type": acc["excluded"]["non_research_type"],
            "non_research_type_values": sorted(NON_RESEARCH_TYPES),
            "policy": "保留在 paper_meta（exclusion_reason 可审计），但不进 train/eval jsonl",
        },
        "extraction_protocol": {
            "status": "DEFINED_NOT_EXECUTED（P4-1）",
            "never_extract": ["EVAL 侧（它只用于对答案）"],
            "candidate_pool": ("TRAIN 侧 **且 in_scope=1**（视图 v_photopolymerization）"
                               " —— 直接对全池采样会把全球巨引论文排到 Layer 1"),
            "schema": {"material": [], "mechanism": [], "method": [],
                       "application": [], "problem": [], "keywords": []},
            "target_size": {"min": 1000, "max": 2000},
            "sampling": {
                "strategy": "分层（禁止纯随机 —— 热门方向会占满样本）",
                "layer1_high_impact": {"share": 0.20,
                                       "purpose": "捕获 foundational concepts",
                                       "rank_by": "citations_asof_cutoff（非快照）"},
                "layer2_fast_growth": {"share": 0.40,
                                       "purpose": "发现 emerging topics",
                                       "rank_by": "TRAIN 内部引用增速（限同年窗口）"},
                "layer3_long_tail": {"share": 0.40,
                                     "purpose": "避免知识树只看热点"},
                "strata_key": "primary_topic（742 个）",
            },
            "first_version_scope": ("不做 Search->Learn->Search 的复杂 Agent；"
                                    "只做 Paper -> extractor -> graph -> trend -> prediction"),
            "cache_dir": "extraction_cache/",
        },
        "prediction_protocol": {
            "status": "DEFINED_NOT_EXECUTED（P4-2）",
            "no_ml_v1": True,
            "metrics": {
                "growth": "N2020 / N2015",
                "acceleration": "slope(2018-2020)",
                "novelty": "first_seen",
                "connectivity": "degree centrality on concept graph",
                "cross_domain": "跨主题共现（如 photopolymerization + biomedicine + 3D printing）",
            },
            "score": "Emergence Score = 上述指标的排序组合",
        },
        "validation_protocol": {
            "status": "DEFINED_NOT_EXECUTED（P4-3）",
            "eval_window": [EVAL_MIN_YEAR, EVAL_MAX_YEAR],
            "top_k": 20,
            "ground_truth": "EVAL 侧 concept 的真实增长方向",
            "venues": ["Nature", "Science", "Advanced Materials",
                       "Advanced Functional Materials", "Nature Communications",
                       "Nature Materials", "Science Advances"],
        },
        "paused_by_decision": [
            "Active learning", "Recall audit", "全量 citation expansion",
            "完善 knowledge graph ontology", "agent workflow",
        ],
        "known_limitations": [
            f"摘要覆盖 {acc['abstract_coverage']['pct']:.1%}"
            "（OpenAlex 自带的 inverted index 为 "
            f"{acc['abstract_coverage']['from_openalex']}，"
            f"scopus_cache 回填 {acc['abstract_coverage']['from_scopus_cache']}）"
            " —— 无摘要的论文在 P4-1 只能靠标题抽取",
            f"scopus_eid 覆盖仅 {acc['identifier_coverage']['scopus_eid']}"
            f"（{acc['identifier_coverage']['by_pct']['scopus_eid']:.1%}）",
            "counts_by_year 左截断（见 leakage_policy.known_censoring）",
            "语料来自既有检索缓存，不是对 OpenAlex 全库的系统抽样 —— "
            "方向分布受当年检索策略影响（dental materials 2492 / "
            "photopolymerization 2081 / additive manufacturing 1451）",
            "同题同年 623 行未去重（策略见 duplicate_entity_policy）",
            "范围门用**主题白名单**（datasets/photopolymerization_v1/"
            "scope_allowlist.yaml，committed 可评审）而非纯词面：pilot 实测纯词面门"
            "抽出的 30 篇里 ~17 篇离题（DNA 甲基化/石墨烯/有机光伏/热重分析/"
            "T 细胞免疫/剂量换算/蛋白序列比对），逐个收紧词表后仍漏 8 篇",
            f"范围外仍有 {acc['scope']['tiers'][SCOPE_OFF_TOPIC] + acc['scope']['tiers'][SCOPE_CHANNEL_ONLY] + acc['scope']['tiers'][SCOPE_ADJACENT_ONLY]}"
            " 篇留在 paper_meta（逐行 tier/evidence 可审计，不静默丢弃）",
            "pending_review 的 9 个主题（复合材料/水凝胶/硅氧烷/通用高分子/液晶/"
            "光致变色…）**暂不纳入**，纳入与否会明显改变语料分布 —— 待用户裁定",
        ],
    }


def print_summary(acc, dup):
    print("═" * 68)
    print(f"  P4-0 Step 1  统一元数据表  [{BUILD_VERSION}]")
    print("═" * 68)
    print(f"  Total papers            {acc['total_papers']}")
    print(f"  Train count (<=2020)    {acc['train_count']}")
    print(f"  Eval  count (2021-2025) {acc['eval_count']}")
    print(f"  Missing year count      {acc['missing_year_count']}")
    print(f"  Duplicate entity count  same_uid={dup['same_uid']['rows']} "
          f"same_doi={dup['same_doi_multi_uid']['rows']} "
          f"same_title_year={dup['same_title_year_multi_uid']['rows']}")
    ic = acc["identifier_coverage"]
    print(f"  Identifier coverage     openalex_id={ic['openalex_id']} "
          f"doi={ic['doi']} scopus_eid={ic['scopus_eid']}")
    print(f"  Abstract coverage       {acc['abstract_coverage']['with_abstract']} "
          f"({acc['abstract_coverage']['pct']:.1%})")
    print("─" * 68)
    ex = acc["excluded"]
    print(f"  排除: >2025={ex['out_of_range_gt_2025']} no_year={ex['no_year']} "
          f"retracted={ex['retracted']} paratext={ex['paratext']} "
          f"type={ex['non_research_type']}  → 纳入 {ex['total_included']}")
    si = acc["split_integrity"]
    print(f"  split 完整性: train<= {si['train_max_year']} | "
          f"eval>= {si['eval_min_year']} | 未来泄漏={si['overlap']} "
          f"| assert={si['assert_no_future_in_train']}")
    sc = acc["scope"]
    t = sc["tiers"]
    print("─" * 68)
    print(f"  范围门: IN_SCOPE {sc['in_scope']} ({sc['in_scope_pct']:.1%})  "
          f"TOPIC_ALLOW={t[SCOPE_TOPIC]} CORE_TEXT={t[SCOPE_TEXT]}")
    print(f"          ADJACENT_ONLY={t[SCOPE_ADJACENT_ONLY]} "
          f"CHANNEL_ONLY={t[SCOPE_CHANNEL_ONLY]} OFF_TOPIC={t[SCOPE_OFF_TOPIC]}")
    for name, v in sc["variant_comparison"].items():
        print(f"    · {name}: {v['in_scope']} 篇, KB 召回 "
              f"{(v['kb_recall'] * 100):.1f}%" if v["kb_recall"] is not None
              else f"    · {name}: {v['in_scope']} 篇")
    kr = sc["kb_recall"]
    print(f"    └ KB∩池 {kr['kb_intersect_pool']} 篇，漏 {kr['missed']}，"
          f"召回 {kr['recall']:.1%}" if kr["recall"] is not None else "    └ KB 无交集")
    print(f"    └ in_scope 内 TRAIN {sc['in_scope_train']} / "
          f"EVAL {sc['in_scope_eval']}")
    print(f"  引用最高的 in_scope 论文（Layer 1 会看到这些）:")
    for r in sc["top_cited_in_scope"][:4]:
        print(f"      {r['cited']:>7}  {r['title'][:52]}")
    print("═" * 68)


def main(argv=None):
    ap = argparse.ArgumentParser(description="P4-0 Step 1：构建时间隔离基准数据集")
    ap.add_argument("--out", default=str(OUT_DIR), help="输出目录")
    ap.add_argument("--openalex-cache", default=str(OPENALEX_CACHE))
    ap.add_argument("--scopus-db", default=str(SCOPUS_DB))
    ap.add_argument("--kb-db", default=str(KB_DB))
    ap.add_argument("--scope-allowlist", default=str(SCOPE_ALLOWLIST))
    ap.add_argument("--apply", action="store_true", help="实际落盘（默认 dry-run）")
    args = ap.parse_args(argv)

    out = Path(args.out)
    print(f"[p4.0] {BUILD_VERSION}  out={_rel(out)}  "
          f"mode={'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"[p4.0] 主池 {_rel(args.openalex_cache)}（只读）")

    by_id, pool_meta = load_openalex_pool(args.openalex_cache)
    scopus_idx = load_scopus_abstract_index(args.scopus_db)
    kb_by_key, kb_eid_by_uid = load_kb_bridge(args.kb_db)
    allowlist, allowlist_meta = load_scope_allowlist(args.scope_allowlist)
    print(f"[p4.0] 范围白名单 {allowlist_meta.get('version')} "
          f"n={allowlist_meta.get('n')} "
          f"{'' if allowlist_meta.get('loaded') else '[WARN] 未加载(退化为纯词面门)'}")
    print(f"[p4.0] 展开 {pool_meta['works_expanded']} -> 去重 "
          f"{pool_meta['works_deduped']} work | scopus 摘要索引 {len(scopus_idx)} "
          f"| KB 桥 {len(kb_by_key)} 键")

    records = build_records(by_id, scopus_idx, kb_by_key, kb_eid_by_uid,
                            allowlist=allowlist)
    dup = mark_duplicates(records)
    acc = acceptance(records, dup, pool_meta, kb_keys=set(kb_by_key),
                     allowlist_meta=allowlist_meta)

    src_hash = _sha256(args.openalex_cache)
    meta = {
        "dataset": DATASET_VERSION, "build_version": BUILD_VERSION,
        "built_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "train_max_year": TRAIN_MAX_YEAR,
        "eval_year_range": f"{EVAL_MIN_YEAR}-{EVAL_MAX_YEAR}",
        "openalex_cache_sha256": src_hash,
        "records": acc["total_papers"],
        "included": acc["excluded"]["total_included"],
        "builder": "tools/build_p4_0_dataset.py",
    }
    bench = build_benchmark_yaml(acc, dup, meta,
                                 {"openalex_cache": src_hash})

    print_summary(acc, dup)

    if not args.apply:
        print("\n[DRY-RUN] 零写入。加 --apply 落盘。")
        print(json.dumps(acc["identifier_coverage"]["by_pct"], ensure_ascii=False))
        return 0

    out.mkdir(parents=True, exist_ok=True)
    (out / "extraction_cache").mkdir(exist_ok=True)
    (out / "extraction_cache" / ".gitkeep").touch()

    write_db(records, out / "paper_meta.db", meta)
    n_train = write_jsonl(records, out / "train_papers.jsonl", SPLIT_TRAIN)
    n_eval = write_jsonl(records, out / "eval_papers.jsonl", SPLIT_EVAL)

    report = {"acceptance": acc, "duplicates": dup, "meta": meta,
              "jsonl": {"train": n_train, "eval": n_eval}}
    with open(out / "build_report.json", "w", encoding="utf-8",
              newline="\n") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    with open(out / "benchmark.yaml", "w", encoding="utf-8",
              newline="\n") as f:
        import yaml
        yaml.safe_dump(bench, f, allow_unicode=True, sort_keys=False,
                       default_flow_style=False, width=100)

    print(f"\n[applied] paper_meta.db  train_papers.jsonl({n_train})  "
          f"eval_papers.jsonl({n_eval})  benchmark.yaml  build_report.json")
    print(f"[applied] -> {_rel(out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
