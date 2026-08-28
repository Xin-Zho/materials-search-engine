"""Phase 3 P1.5 — Audit Universe Construction（用户定 2026-08-27）。

核心概念修正（用户：这是 Phase 3 最后一个真正的架构问题）：
> 旧 universe_builder（KB ∪ candidate ∪ staging）= **Agent 已经碰过的论文**
> → 循环：Agent 看过的 → 组成 Universe → 抽样 → 检查 Agent 漏没漏
> → 从没被检索到的论文根本没机会进抽样池 → 数学对但科学结论失效。

所以审计总体必须**先于/独立于 FoundRelevant 定义**：
```
External Audit Universe U*
    ├── Agent 已找到 relevant（F = U* ∩ 已确认 relevant，canonical 去重）
    └── Remaining（U* − F）→ 随机抽样 → missed relevant → Recall_LCB
```

AuditUniverseDefinition 用**宽 umbrella 检索规则**定义总体（高 recall、允许低
precision——宁可多收垃圾，不把可能相关论文排除在总体外）；与 Agent ranking /
prioritizer / relevance score / candidate pool / ontology feedback / RL 完全无关。

独立性原则：Universe Builder 可复用数据库 API，但**绝不**复用 Agent 的
prioritizer/relevance/candidate/ontology/RL 逻辑。

旧 builder 改名 build_agent_seen_pool（debug/trajectory/coverage accounting 用，
禁止进正式 statistical audit——audit.py 有硬拒绝）。
"""

import hashlib
import inspect
import json
import os
import re
from dataclasses import dataclass, field, asdict

from .universe import (
    UniverseSnapshot, freeze_universe, AGENT_SEEN_POOL, EXTERNAL_AUDIT_UNIVERSE,
)

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFINITIONS_DIR = os.path.join(BASE, "data", "audit_universe_definitions")

# ── 协议 invariant ⑧（用户定 2026-08-27）──
AGENT_KNOWLEDGE_MAY_EXPAND_NEVER_CONTRACT = (
    "Agent-derived knowledge may only UNION additional papers into the audit "
    "universe; it may never be used to filter papers out."
)

# channel 类型白名单（用户定）：
#   CORE_UMBRELLA        —— 只描述研究问题本身（Agent 运行前可写）
#   SUPPLEMENTAL_ROUTE   —— Agent 学到的 route 词，只扩大 universe
#   FORWARD/BACKWARD_CITATION / REVIEW_REFERENCE —— 扩网通道
CHANNEL_TYPES = {
    "CORE_UMBRELLA", "SUPPLEMENTAL_ROUTE", "FORWARD_CITATION",
    "BACKWARD_CITATION", "REVIEW_REFERENCE",
}

# 客观过滤白名单（Universe Builder 唯一允许的过滤）：
#   年份 / 文献类型 / 来源数据库 / 明确重复项
# 禁止：相关性模型过滤 / ontology coverage 过滤 / candidate priority 过滤
OBJECTIVE_FILTER_KEYS = {"date_start", "date_end", "document_types", "sources"}


# ── AuditUniverseDefinition：预声明的外部检索规则（可复现、与 Agent 无关）──

@dataclass
class AuditUniverseDefinition:
    """预声明的外部审计总体规则（可复现、与 Agent 无关、只增不减）。

    channels（用户定 2026-08-27 协议 invariant ⑧）：
      CORE_UMBRELLA        —— 只描述研究问题本身（Agent 运行前可写），正式审计根基
      SUPPLEMENTAL_ROUTE   —— Agent 学到的 route 词（ring-opening/thiol-ene/AFCT...）
      FORWARD_CITATION / BACKWARD_CITATION / REVIEW_REFERENCE —— citation/review 扩网
    约束：**Agent knowledge may only UNION additional papers into the audit
    universe; it must never be used to filter papers out.**（只增不减）

    过滤只允许客观条件（年份/文献类型/来源数据库/明确重复项）——绝不允许
    相关性模型过滤 / ontology coverage 过滤 / candidate priority 过滤。
    """
    topic_id: str
    sources: list[str] = field(default_factory=lambda: ["openalex"])
    date_start: int | None = None
    date_end: int | None = None
    language_policy: str = "any"           # any / english_only
    document_types: list[str] = field(default_factory=list)   # 空 = 不限
    channels: dict = field(default_factory=dict)   # {CHANNEL_TYPE: [queries]}——宽规则
    citation_expansion_policy: str = "none"   # none / forward_backward / review_reference
    source_filters: dict = field(default_factory=dict)
    definition_version: str = "1"

    def __post_init__(self):
        # 兼容旧字段：umbrella_queries → CORE_UMBRELLA channel（迁移路径）
        for q in getattr(self, "umbrella_queries", []) or []:
            self.channels.setdefault("CORE_UMBRELLA", []).append(q)
            self.umbrella_queries = []
        # channel 白名单校验（未知类型拒绝——防 Agent 判断混入）
        for ch in self.channels:
            if ch not in CHANNEL_TYPES:
                raise ValueError(
                    f"非法 channel 类型: {ch}（允许: {sorted(CHANNEL_TYPES)}）——"
                    f"Agent 判断不得以任何 channel 名义进入审计总体")

    def fingerprint(self) -> str:
        """定义本身指纹：任何字段变化 → 指纹变 → universe hash 变（可审计）。"""
        payload = json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]

    def all_queries(self) -> list[str]:
        """全部 query（按 channel 展开，保持顺序）。"""
        return [q for ch in sorted(self.channels) for q in self.channels.get(ch, [])]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AuditUniverseDefinition":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def load_definition(topic_id: str, path: str | None = None) -> AuditUniverseDefinition:
    """从 data/audit_universe_definitions/{topic}.json 加载定义。"""
    path = path or os.path.join(DEFINITIONS_DIR, f"{topic_id}.json")
    with open(path, encoding="utf-8") as f:
        return AuditUniverseDefinition.from_dict(json.load(f))


def save_definition(definition: AuditUniverseDefinition,
                    path: str | None = None) -> str:
    path = path or os.path.join(DEFINITIONS_DIR, f"{definition.topic_id}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(definition.to_dict(), f, ensure_ascii=False, indent=2)
    return path


# ── build_audit_universe：定义 → 宽检索 → union → dedup → 基础客观过滤 → 冻结 ──

def _norm_openalex_id(pid: str) -> str:
    """OpenAlex ID 归一化：'openalex:https://openalex.org/W123' / 'W123'
    / 'https://openalex.org/W123' → 'W123'。

    统一规范防止 KB（裸 Wxxx）与检索返回（openalex:https://...）不匹配
    导致 F∩universe 交集失真（已踩坑 2026-08-27）。
    """
    m = re.search(r"(W\d+)", pid or "")
    return m.group(1) if m else (pid or "").strip()


async def fetch_paginated_openalex(backend, query: str, per_page: int = 200,
                                   max_pages: int | None = None) -> list:
    """OpenAlex 全量分页拉取（cursor pagination）——universe builder 专用。

    为什么不能用 search_relevance：它 `per-page=min(limit,200)` 只取一页——
    实测 '"photopolymerization" AND "shrinkage"' 真实命中 831 只取回 200，
    universe 被严重截断（47 篇已确认 relevant 论文因此落在 universe 外）。
    Universe 追求高 recall：**必须拉全 meta.count**，分页直到取完。
    """
    import re
    url = f"{backend.BASE_URL}/works"
    terms = [t.strip().strip('"')
             for t in re.split(r"\s+AND\s+", query or "", flags=re.I) if t.strip()]
    phrases = " ".join(f'"{t}"' for t in terms)
    papers: list = []
    cursor = "*"
    pages = 0
    while True:
        params = {"filter": f"title_and_abstract.search:{phrases}",
                  "per-page": per_page, "cursor": cursor,
                  "sort": "relevance_score:desc"}
        data = await backend._get_json(url, params)
        batch = [backend._work_to_paper(w) for w in data.get("results", [])]
        papers.extend(batch)
        pages += 1
        total = data.get("meta", {}).get("count", len(papers))
        cursor = data.get("meta", {}).get("next_cursor")
        if not cursor or not batch or len(papers) >= total:
            break
        if max_pages and pages >= max_pages:
            break
    return papers[:total] if total > 0 else papers


async def fetch_references_by_id(backend, openalex_id: str,
                                 per_page: int = 200) -> list:
    """BACKWARD_CITATION：种子论文引用的文献（seed → referenced_works）。

    需要 seed 的 referenced_works id 列表（work['referenced_works']，可能是
    完整 URL），提取 W id 后批量取回（OpenAlex filter=openalex_id 最多 50 个/
    请求，分页直到取完）。
    """
    work = await backend._get_json(
        f"{backend.BASE_URL}/works/{openalex_id}", {})
    refs = work.get("referenced_works", [])
    if not refs:
        return []
    ref_ids = [_openalex_id_from_pid(r) for r in refs]
    ref_ids = [r for r in ref_ids if r]
    papers: list = []
    for i in range(0, len(ref_ids), 50):
        chunk = ref_ids[i:i + 50]
        url = f"{backend.BASE_URL}/works"
        params = {"filter": "openalex_id:" + "|".join(chunk),
                  "per-page": min(len(chunk), 200)}
        data = await backend._get_json(url, params)
        papers.extend(backend._work_to_paper(w) for w in data.get("results", []))
    return papers


async def fetch_cited_by(backend, openalex_id: str,
                         per_page: int = 200) -> list:
    """FORWARD_CITATION：引用种子论文的文献（seed → citing works），cursor 分页。"""
    url = f"{backend.BASE_URL}/works"
    papers: list = []
    cursor = "*"
    while True:
        params = {"filter": f"cites:{openalex_id}",
                  "per-page": per_page, "cursor": cursor,
                  "sort": "cited_by_count:desc"}
        data = await backend._get_json(url, params)
        batch = [backend._work_to_paper(w) for w in data.get("results", [])]
        papers.extend(batch)
        total = data.get("meta", {}).get("count", len(papers))
        cursor = data.get("meta", {}).get("next_cursor")
        if not cursor or not batch or len(papers) >= total:
            break
    return papers


# ── citation seed 提取（known relevant / review seeds 的 openalex id）──

def _openalex_id_from_pid(pid: str) -> str | None:
    """从各种 paper id 格式提取 openalex W id（citation 查询用）。"""
    m = re.search(r"(W\d+)", pid or "")
    return m.group(1) if m else None


# ── citation channel 路由（用户定 2026-08-27：CORE 保持路线无关，漏网靠 citation）──

CITATION_CHANNELS = {
    "BACKWARD_CITATION": fetch_references_by_id,   # seed → referenced_works
    "FORWARD_CITATION": fetch_cited_by,            # seed → citing works
}
REVIEW_CHANNEL = "REVIEW_REFERENCE"                # review seed → referenced_works


async def _expand_citation_channel(definition: AuditUniverseDefinition, ch: str,
                                   backend, seeds: list[str],
                                   per_seed_limit: int = 200) -> tuple[set[str], dict]:
    """执行一个 citation 通道：对每个 seed 论文做 1-hop 引用扩展。

    seeds：known relevant 的 openalex W id（BACKWARD/FORWARD），或 review
    seeds（REVIEW_REFERENCE）。返回 (命中 id 集合, 每 seed 计数)。
    """
    fetcher = CITATION_CHANNELS.get(ch)
    if ch == REVIEW_CHANNEL:
        fetcher = fetch_references_by_id
    if fetcher is None:
        return set(), {}
    ids: set[str] = set()
    per_seed: dict[str, int] = {}
    for seed in seeds:
        wid = _openalex_id_from_pid(seed)
        if not wid:
            continue
        try:
            papers = await fetcher(backend, wid, per_page=per_seed_limit)
        except Exception:  # noqa: BLE001 —— 单 seed 失败不整轮失败
            per_seed[seed] = -1
            continue
        seed_ids = {_norm_openalex_id(p.paper_id) for p in papers
                    if getattr(p, "paper_id", "")}
        per_seed[seed] = len(seed_ids)
        ids |= seed_ids
    return ids, per_seed


async def build_audit_universe_async(definition: AuditUniverseDefinition,
                                     search_fn, found_relevant: list[str],
                                     universe_id: str | None = None,
                                     citation_backend=None,
                                     review_seeds: list[str] | None = None,
                                     citation_seeds: list[str] | None = None) -> UniverseSnapshot:
    """async 版 build（分页拉全量）。search_fn 可为同步 mock 或 async backend。

    channel 路由（用户定 2026-08-27）：
      CORE_UMBRELLA / SUPPLEMENTAL_ROUTE —— search_fn（title_and_abstract 宽检索）
      BACKWARD_CITATION / FORWARD_CITATION —— citation_backend 1-hop 引用扩展
        （seed = citation_seeds，默认 known_relevant——补关键词语言体系漏检：
        德语/中文/老论文/用词完全不同的机制论文，不要求与 query 同词）
      REVIEW_REFERENCE —— review seed 的 references（作者已整理历史路线）
    """
    paper_ids: set[str] = set()
    query_hits: dict[str, list[str]] = {}
    query_total_hits: dict[str, int] = {}
    channel_papers: dict[str, set[str]] = {}
    citation_seed_map: dict[str, list[str]] = {}

    citation_seeds = citation_seeds or list(found_relevant)   # 默认 seed = known relevant
    search_channels = {"CORE_UMBRELLA", "SUPPLEMENTAL_ROUTE"}

    for ch in sorted(definition.channels):
        channel_papers[ch] = set()
        queries = definition.channels.get(ch, [])
        # REVIEW_REFERENCE 由 review_seeds 驱动（channels 里 query 列表可为空）——
        # 只有 search/citation 通道的空列表才跳过
        if not queries and ch != REVIEW_CHANNEL:
            continue
        if ch in search_channels:
            for q in queries:
                result = search_fn(q, limit=100000)
                if inspect.iscoroutine(result):
                    result = await result
                papers = result
                ids = [_norm_openalex_id(p.paper_id)
                       for p in papers if getattr(p, "paper_id", "")]
                ids = [i for i in ids if i]
                query_hits[q] = ids
                total = getattr(search_fn, "last_total_hits", None) if not inspect.iscoroutine(
                    search_fn) else None
                query_total_hits[q] = len(ids) if total is None else int(total)
                paper_ids.update(ids)
                channel_papers[ch].update(ids)
        elif ch in CITATION_CHANNELS or ch == REVIEW_CHANNEL:
            seeds = (review_seeds or []) if ch == REVIEW_CHANNEL else citation_seeds
            citation_seed_map[ch] = seeds
            if citation_backend is None:
                continue    # 没接 citation backend → 通道跳过（不崩）
            if ch == REVIEW_CHANNEL and not seeds:
                continue    # REVIEW 无 seeds → 跳过（不崩）
            ids, per_seed = await _expand_citation_channel(
                definition, ch, citation_backend, seeds)
            query_hits[f"{ch}:{len(seeds)}seeds"] = sorted(ids)[:100]
            query_total_hits[f"{ch}:seeds"] = len(ids)
            paper_ids.update(ids)
            channel_papers[ch].update(ids)

    channel_contribution = {ch: len(ids) for ch, ids in channel_papers.items()}
    ids = sorted(paper_ids)
    # F 与 universe 同一 id 规范（_norm_openalex_id）——否则 KB 裸 Wxxx 与
    # openalex:https://... 不匹配，交集失真（已踩坑：F∩universe 从 1 → 20）
    found_norm = sorted({_norm_openalex_id(f) for f in found_relevant if f})
    # 完整 channel 集合必须持久化（用户 2026-08-27 抓的口径缺口）：query_hits
    # 里 citation 通道只存前 100，BACKWARD/FORWARD 全集合离线不可重建——
    # channel 归属 / recovered_previous_gaps / exclusive 分析全靠它。
    channel_papers_sorted = {ch: sorted(v) for ch, v in channel_papers.items()}
    snap = freeze_universe(
        topic_id=definition.topic_id, paper_ids=ids,
        kb_version=f"audit-def:{definition.fingerprint()}",
        search_run_ids=[],
        source_breakdown={"found_relevant": found_norm,
                          "query_hits": query_hits,
                          "query_total_hits": query_total_hits,
                          "channel_contribution": channel_contribution,
                          "channel_papers": channel_papers_sorted,
                          "citation_seed_map": citation_seed_map},
        universe_id=universe_id,
        source_type=EXTERNAL_AUDIT_UNIVERSE,
        definition_version=definition.fingerprint())
    return snap


def build_audit_universe(definition: AuditUniverseDefinition,
                         search_fn, found_relevant: list[str],
                         universe_id: str | None = None,
                         citation_backend=None,
                         review_seeds: list[str] | None = None,
                         citation_seeds: list[str] | None = None) -> UniverseSnapshot:
    """从外部定义构造审计 universe（正式 audit 唯一接受路径）。

    search_fn(query_text, limit) -> list[Paper] 或 coroutine（async backend——
    await 它；真实应使用 fetch_paginated_openalex 拉全量，**不要用单页的
    search_relevance**——831 命中只取 200 会截断 universe，47 篇已确认
    relevant 论文因此落到 universe 外，已踩坑 2026-08-27）。
    **所有 channel 的 query 返回全部 UNION**（invariant ⑧：Agent knowledge
    may expand, never contract——只增不减），dedup 后只做**客观过滤**（日期/
    文献类型，见 OBJECTIVE_FILTER_KEYS——绝无相关性/ontology/candidate 过滤），
    冻结为 EXTERNAL_AUDIT_UNIVERSE snapshot。

    citation_backend：OpenAlex backend（BACKWARD/FORWARD/REVIEW 通道用，
    1-hop 引用扩展——补关键词语言体系漏检）；None = 跳过 citation 通道。

    channel_contribution 写入 source_breakdown：每个 channel 独立贡献的
    unique 论文数（报告用，供审计判断各通道对总体的影响）。

    found_relevant：Agent 已确认 relevant 且落在 U* 内的论文（U* 与 F 的交集由
    snapshot.remaining_pool() 在审计时算——传入 found_relevant 全集即可）。
    """
    import asyncio
    try:
        return asyncio.run(build_audit_universe_async(
            definition, search_fn, found_relevant, universe_id,
            citation_backend=citation_backend, review_seeds=review_seeds,
            citation_seeds=citation_seeds))
    except RuntimeError:
        # 已在事件循环内（CLI asyncio.run 包装时）
        return asyncio.get_event_loop().run_until_complete(
            build_audit_universe_async(
                definition, search_fn, found_relevant, universe_id,
                citation_backend=citation_backend, review_seeds=review_seeds,
                citation_seeds=citation_seeds))


# ── Agent-seen pool（debug 用，禁止进正式审计）──

def build_agent_seen_pool() -> dict:
    """Agent 接触过的论文（KB records + candidate source_papers + staging/provenance）。

    只用于 debugging / trajectory analysis / coverage accounting——
    **不能传给正式 Statistical Audit**（audit.create_audit 对
    source_type=AGENT_SEEN_POOL 硬拒绝，用户 P1.5 定）。
    """
    from search_engine.knowledge_base import KnowledgeBase
    from search_engine.completeness.goldset import normalize_doi

    found_relevant: list[str] = []
    universe: list[str] = []
    seen_found: set[str] = set()

    kb = KnowledgeBase()
    try:
        for rec in kb.get_all():
            pid = getattr(rec, "openalex_id", "") or getattr(rec, "paper_id", "")
            if not pid:
                continue
            universe.append(pid)
            if getattr(rec, "route_mechanism_edges", None):
                doi = normalize_doi(getattr(rec, "doi", "") or "")
                key = doi if doi else pid.lower()
                if key not in seen_found:
                    seen_found.add(key)
                    found_relevant.append(pid)
    finally:
        kb.close()

    pool_path = os.path.join(BASE, "data", "exports", "phase2_candidates.json")
    if os.path.exists(pool_path):
        with open(pool_path, encoding="utf-8") as f:
            pool = json.load(f).get("candidates", [])
        for c in pool:
            for sp in (c.get("source_papers") or []):
                if sp and sp not in universe:
                    universe.append(sp)
            if c.get("status") in ("VALIDATED", "PROMOTED"):
                for sp in (c.get("source_papers") or []):
                    key = sp.lower()
                    if sp and key not in seen_found:
                        seen_found.add(key)
                        found_relevant.append(sp)

    for fname in ("discovery_staging.json", "discovery_paper_provenance.json"):
        p = os.path.join(BASE, "data", "exports", fname)
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if fname == "discovery_staging.json":
            for pp in data.get("papers", []):
                pid = pp.get("paper_id", "")
                if pid and pid not in universe:
                    universe.append(pid)
        else:
            for r in data:
                pid = r.get("paper_id", "")
                if pid and pid not in universe:
                    universe.append(pid)

    return {"paper_ids": universe, "found_relevant": found_relevant,
            "kb_version": "kb-snapshot-v1", "search_run_ids": [],
            "source_type": AGENT_SEEN_POOL}
