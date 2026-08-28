"""Phase 3 P1.5 测试：Audit Universe Construction（用户定 2026-08-27）。

Exit Criteria ⑦（用户新增，Phase 3 冻结前必须修掉的概念级问题）：
> 正式 statistical audit 不允许使用 Agent-seen papers 自己构造 Universe。

- KB/candidate/staging union（AGENT_SEEN_POOL）→ create_audit → **REJECT**
- External UniverseSnapshot（EXTERNAL_AUDIT_UNIVERSE）→ create_audit → **ACCEPT**

外加：AuditUniverseDefinition 指纹稳定/敏感、build_audit_universe 的
union/dedup/filter、宽规则高 recall 低 precision 定位。
"""

import json

import pytest

from search_engine.completeness.universe import (
    freeze_universe, EXTERNAL_AUDIT_UNIVERSE, AGENT_SEEN_POOL, universe_hash,
    load_snapshots, save_snapshot,
)
from search_engine.completeness.universe_builder import (
    AuditUniverseDefinition, load_definition, save_definition,
    build_audit_universe, build_agent_seen_pool, DEFINITIONS_DIR,
)
from search_engine.completeness.audit import (
    create_audit, InvalidAuditUniverse, AWAITING_LABELS,
)


def _paths(tmp_path):
    return dict(audits_path=str(tmp_path / "audits.json"),
                labels_dir=str(tmp_path),
                snapshots_path=str(tmp_path / "universes.json"),
                manifests_path=str(tmp_path / "manifests.json"))


def _paper(pid: str):
    return type("Paper", (), {"paper_id": pid})()


# ── ⑦ 核心：Agent-seen pool 拒绝 / External 接受 ──

def test_agent_seen_pool_rejected(tmp_path):
    """KB/candidate/staging union → create_audit → REJECT（InvalidAuditUniverse）。"""
    p = _paths(tmp_path)
    agent_seen = {
        "paper_ids": ["W1", "W2", "W3", "W4"],
        "found_relevant": ["W1", "W2"],
        "kb_version": "kb-v1", "search_run_ids": [],
        "source_type": AGENT_SEEN_POOL,
    }
    with pytest.raises(InvalidAuditUniverse, match="EXTERNAL_AUDIT_UNIVERSE"):
        create_audit("pc_001", lambda: agent_seen, sample_size=2, **p)


def test_external_universe_accepted(tmp_path):
    """External UniverseSnapshot → create_audit → ACCEPT（AWAITING_LABELS）。"""
    p = _paths(tmp_path)
    ext = {
        "paper_ids": [f"W{i}" for i in range(500)],
        "found_relevant": [f"W{i}" for i in range(50)],
        "kb_version": "audit-def:abc",
        "search_run_ids": [],
        "source_type": EXTERNAL_AUDIT_UNIVERSE,
        "definition_version": "def-fp",
    }
    a = create_audit("pc_001", lambda: ext, sample_size=100, **p)
    assert a.status == AWAITING_LABELS
    assert a.F == 50
    assert a.N_remaining == 450


def test_agent_seen_pool_cannot_self_prove(tmp_path):
    """循环防御语义：Agent 接触过的论文（A B C D）里没有 E——
    用 agent_seen 构造 universe 永远发现不了 E（用户示例）。"""
    p = _paths(tmp_path)
    seen = {"paper_ids": ["A", "B", "C", "D"], "found_relevant": ["A", "B"],
            "kb_version": "v", "search_run_ids": [],
            "source_type": AGENT_SEEN_POOL}
    with pytest.raises(InvalidAuditUniverse):
        create_audit("pc_001", lambda: seen, sample_size=2, **p)
    # E 从未进入 agent_seen → 抽样池里根本没有 E（概念验证：拒绝是正确的防御）


# ── AuditUniverseDefinition 指纹 ──

def test_definition_fingerprint_stable_and_sensitive():
    d1 = AuditUniverseDefinition(
        topic_id="pc_001",
        channels={"CORE_UMBRELLA": ['"photopolymerization" AND "shrinkage"']})
    d2 = AuditUniverseDefinition(
        topic_id="pc_001",
        channels={"CORE_UMBRELLA": ['"photopolymerization" AND "shrinkage"']})
    assert d1.fingerprint() == d2.fingerprint()
    d3 = AuditUniverseDefinition(
        topic_id="pc_001",
        channels={"CORE_UMBRELLA": ['"photopolymerization" AND "shrink"']})
    assert d3.fingerprint() != d1.fingerprint()
    d4 = AuditUniverseDefinition(
        topic_id="pc_001",
        channels={"CORE_UMBRELLA": d1.channels["CORE_UMBRELLA"]},
        date_end=2025)
    assert d4.fingerprint() != d1.fingerprint()


def test_definition_persist_roundtrip(tmp_path):
    d = AuditUniverseDefinition(topic_id="t1",
                                channels={"CORE_UMBRELLA": ["q1"]},
                                date_start=1990, date_end=2026)
    path = save_definition(d, str(tmp_path / "t1.json"))
    d2 = load_definition("t1", path)
    assert d2.fingerprint() == d.fingerprint()
    assert d2.all_queries() == ["q1"]


def test_real_pc001_definition_loaded():
    d = load_definition("pc_001")
    assert d.sources == ["openalex"]
    assert d.date_start == 1990 and d.date_end == 2026
    # 所有 search channel 的 query 都含 AND；citation 通道是 seed 标记
    search_qs = (d.channels.get("CORE_UMBRELLA", [])
                 + d.channels.get("SUPPLEMENTAL_ROUTE", []))
    assert len(search_qs) >= 10
    assert all("AND" in q for q in search_qs)
    # 分层：CORE_UMBRELLA 只描述研究问题（不含机制路线名），SUPPLEMENTAL 才含路线
    core = " ".join(d.channels.get("CORE_UMBRELLA", [])).lower()
    assert "ring-opening" not in core and "thiol-ene" not in core
    assert "shrinkage" in core
    # citation 通道开启（BACKWARD/FORWARD）
    assert d.channels.get("BACKWARD_CITATION") and d.channels.get("FORWARD_CITATION")


# ── ⑧：Agent knowledge may expand, never contract ──

def test_invariant8_constant():
    from search_engine.completeness.universe_builder import (
        AGENT_KNOWLEDGE_MAY_EXPAND_NEVER_CONTRACT,
    )
    assert "UNION" in AGENT_KNOWLEDGE_MAY_EXPAND_NEVER_CONTRACT
    assert "never be used to filter" in AGENT_KNOWLEDGE_MAY_EXPAND_NEVER_CONTRACT


def test_unknown_channel_rejected():
    """未知 channel 类型拒绝——Agent 判断不得以任何 channel 名义混入。"""
    with pytest.raises(ValueError, match="channel"):
        AuditUniverseDefinition(topic_id="t",
                                channels={"AGENT_OPINION": ["q1"]})


def test_supplemental_never_contracts_core():
    """⑧：SUPPLEMENTAL 只 union，不剔除 CORE 论文——加 route 词只会让 universe 变大。"""
    def search(q, limit=500):
        if q == '"core"' or q == 'core-query':
            return [_paper("W1"), _paper("W2")]
        return [_paper("W3"), _paper("W4")]     # supplemental 独有
    core_only = AuditUniverseDefinition(
        topic_id="pc_001",
        channels={"CORE_UMBRELLA": ['"core"']})
    with_supp = AuditUniverseDefinition(
        topic_id="pc_001",
        channels={"CORE_UMBRELLA": ['"core"'],
                  "SUPPLEMENTAL_ROUTE": ['"route-x"']})
    s_core = build_audit_universe(core_only, search, found_relevant=["W1"])
    s_both = build_audit_universe(with_supp, search, found_relevant=["W1"])
    # W1 W2 在 core 里 → 加了 supplemental 后仍在（不收缩）
    assert {"W1", "W2"} <= set(s_both.paper_ids)
    assert s_both.total_count >= s_core.total_count    # 只增不减


def test_core_papers_never_removed_by_supplemental():
    """⑧：无论 supplemental/route 检索返回什么，CORE_UMBRELLA 的论文必然留在 universe。"""
    def search(q, limit=500):
        # 故意：route query 返回与 core 无关的论文（模拟 Agent 学到的新方向 X）
        if '"core"' in q:
            return [_paper("W1"), _paper("W2")]
        return [_paper("X1"), _paper("X2")]     # X 方向（Agent 后发现）
    d = AuditUniverseDefinition(
        topic_id="pc_001",
        channels={"CORE_UMBRELLA": ['"core"'],
                  "SUPPLEMENTAL_ROUTE": ['"route-x"']})
    snap = build_audit_universe(d, search, found_relevant=["W1"])
    # CORE 的 W1/W2 绝不被剔除；X 只追加
    assert {"W1", "W2", "X1", "X2"} <= set(snap.paper_ids)
    # channel 贡献独立统计
    contrib = snap.source_breakdown.get("channel_contribution", {})
    assert contrib.get("CORE_UMBRELLA", 0) == 2
    assert contrib.get("SUPPLEMENTAL_ROUTE", 0) == 2


def test_only_objective_filters_allowed():
    """⑧：Universe Builder 只允许客观过滤（年份/类型/来源/去重），
    禁止相关性/ontology/candidate 过滤——定义 schema 里根本没有这些字段。"""
    from search_engine.completeness.universe_builder import OBJECTIVE_FILTER_KEYS
    assert {"date_start", "date_end", "document_types", "sources"} == OBJECTIVE_FILTER_KEYS
    # schema 层防御：任何非白名单字段不进入 AuditUniverseDefinition
    d = AuditUniverseDefinition.from_dict({
        "topic_id": "t",
        "channels": {"CORE_UMBRELLA": ["q1"]},
        "relevance_threshold": 0.2,          # 恶意字段（应被忽略）
        "ontology_filter": ["route-x"],      # 恶意字段（应被忽略）
    })
    assert "relevance_threshold" not in d.to_dict()
    assert "ontology_filter" not in d.to_dict()


# ── Citation channels（用户定：BACKWARD/FORWARD 1-hop，seed=known relevant）──

class _FakeBackend:
    """模拟 OpenAlex backend 的 citation 方法（带 BASE_URL 契约）。"""
    BASE_URL = "https://api.openalex.org"

    def __init__(self, refs: dict, cited_by: dict):
        self._refs = refs
        self._cited = cited_by

    async def _get_json(self, url, params=None):
        params = params or {}
        filt = params.get("filter", "")
        if filt.startswith("cites:"):
            wid = filt.replace("cites:", "")
            ids = self._cited.get(wid, [])
            return {"meta": {"count": len(ids)},
                    "results": [{"id": f"https://openalex.org/{i}"} for i in ids]}
        if filt.startswith("openalex_id:"):
            # 批量取回：直接返回这些 id 本身（references 批量查询语义）
            wids = [w for w in filt.replace("openalex_id:", "").split("|") if w]
            return {"meta": {"count": len(wids)},
                    "results": [{"id": f"https://openalex.org/{w}"} for w in wids]}
        if "/works/" in url and not filt:
            wid = url.rsplit("/", 1)[-1]
            refs = self._refs.get(wid, [])
            return {"referenced_works": [f"https://openalex.org/{r}" for r in refs]}
        return {"meta": {"count": 0}, "results": []}

    def _work_to_paper(self, w):
        return _paper(w.get("id", "").split("/")[-1])


def test_citation_channels_union_and_never_contract():
    """BACKWARD/FORWARD 只 union 不收缩——CORE 论文永远在，citation 只追加。"""
    from search_engine.completeness.universe_builder import (
        build_audit_universe_async,
    )
    # OpenAlex referenced_works / works id 是 https://openalex.org/W<digits> 格式
    backend = _FakeBackend(
        refs={"W1": ["W20000001", "W20000002"]},   # W1 引用两篇（backward）
        cited_by={"W1": ["W30000001", "W30000002"]})  # 两篇引用 W1（forward）

    async def search(q, limit=100000):
        if "core" in q:
            return [_paper("W1")]
        return [_paper("W1")]

    import asyncio
    d = AuditUniverseDefinition(
        topic_id="pc_001",
        channels={"CORE_UMBRELLA": ['"core"'],
                  "BACKWARD_CITATION": ["seed"],
                  "FORWARD_CITATION": ["seed"]})
    snap = asyncio.run(build_audit_universe_async(
        d, search, found_relevant=["W1"], citation_backend=backend,
        citation_seeds=["W1"]))
    # CORE 的 W1 绝不剔除；backward + forward 追加
    assert "W1" in snap.paper_ids
    assert {"W20000001", "W20000002", "W30000001", "W30000002"} <= set(snap.paper_ids)
    contrib = snap.source_breakdown["channel_contribution"]
    assert contrib["BACKWARD_CITATION"] == 2
    assert contrib["FORWARD_CITATION"] == 2
    assert snap.found_relevant_count() == 1      # W1 在 universe


def test_citation_no_backend_skips_gracefully():
    """没接 citation_backend → citation 通道跳过（不崩），CORE 正常。"""
    from search_engine.completeness.universe_builder import build_audit_universe
    def search(q, limit=100000):
        return [_paper("W1")]
    d = AuditUniverseDefinition(
        topic_id="pc_001",
        channels={"CORE_UMBRELLA": ['"core"'],
                  "BACKWARD_CITATION": ["seed"]})
    snap = build_audit_universe(d, search, found_relevant=["W1"])
    assert "W1" in snap.paper_ids
    assert snap.source_breakdown["channel_contribution"].get("BACKWARD_CITATION", 0) == 0


# ── build_audit_universe：union / dedup / 客观过滤 ──

def test_build_audit_universe_union_dedup():
    """多个 query 的返回 union + dedup（跨 query 重复论文只算一次）。"""
    def search(q, limit=500):
        hits = {
            '"q1"': [_paper("W1"), _paper("W2")],
            '"q2"': [_paper("W2"), _paper("W3")],     # W2 与 q1 重复
        }
        return hits.get(q, [])
    d = AuditUniverseDefinition(topic_id="pc_001",
                                channels={"CORE_UMBRELLA": ['"q1"', '"q2"']})
    snap = build_audit_universe(d, search, found_relevant=["W1"])
    assert snap.source_type == EXTERNAL_AUDIT_UNIVERSE
    assert snap.total_count == 3                     # W1, W2, W3（W2 去重）
    assert snap.definition_version == d.fingerprint()
    assert snap.found_relevant_count() == 1
    assert snap.remaining_pool_size() == 2          # W2, W3


def test_build_audit_universe_hash_includes_definition():
    """定义变化 → universe hash 变化（可审计）。"""
    def search(q, limit=500):
        return [_paper("W1"), _paper("W2")]
    d1 = AuditUniverseDefinition(topic_id="pc_001",
                                 channels={"CORE_UMBRELLA": ['"q1"']})
    d2 = AuditUniverseDefinition(topic_id="pc_001",
                                 channels={"CORE_UMBRELLA": ['"q1"', '"q2"']})
    s1 = build_audit_universe(d1, search, found_relevant=["W1"])
    s2 = build_audit_universe(d2, search, found_relevant=["W1"])
    assert s1.universe_hash != s2.universe_hash


def test_source_type_in_hash():
    """同 ids 但 source_type 不同 → hash 不同（AGENT_SEEN vs EXTERNAL 不混）。"""
    ids = ["W1", "W2", "W3"]
    h_ext = universe_hash(ids, "kb", source_type=EXTERNAL_AUDIT_UNIVERSE)
    h_agent = universe_hash(ids, "kb", source_type=AGENT_SEEN_POOL)
    assert h_ext != h_agent


def test_remaining_pool_accounting():
    """账目硬断言（用户 2026-08-27）：|U| = F + N_remaining，F 是交集口径。"""
    snap = freeze_universe(
        "pc_001", ["A", "B", "C", "D", "E"],
        source_breakdown={"found_relevant": ["A", "B", "X", "Y"]})
    # found_relevant 里 X/Y 不在 universe → 不算 F（交集口径）
    assert snap.found_relevant_count() == 2          # A, B
    assert snap.known_relevant_total() == 4          # A, B, X, Y
    assert snap.found_relevant_outside() == ["X", "Y"]
    assert snap.known_relevant_containment() == 0.5
    acct = snap.check_accounting()
    assert acct == {"total": 5, "F": 2, "N_remaining": 3}


def test_freeze_rejects_unknown_source_type():
    with pytest.raises(ValueError, match="source_type"):
        freeze_universe("t", ["W1"], source_breakdown={"found_relevant": ["W1"]},
                        source_type="HACKED_POOL")


# ── build_agent_seen_pool 改名后仍可用（debug 用途）──

def test_agent_seen_pool_returns_source_type():
    """旧 builder 改名 build_agent_seen_pool：返回 AGENT_SEEN_POOL 标记（debug 用）。"""
    pool = build_agent_seen_pool()
    assert pool["source_type"] == AGENT_SEEN_POOL
    assert isinstance(pool["paper_ids"], list)
    assert isinstance(pool["found_relevant"], list)
