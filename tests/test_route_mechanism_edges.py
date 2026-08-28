"""Phase 1.8 acceptance test — route—mechanism 证据边模型。

用户验收标准（Regression test for the multi-route paper）：
    该论文应得到 edges：
        AFCT → stress relaxation      ✓
        thiol-ene → delayed gelation  ✓
    不能得到：
        AFCT → delayed gelation       ✗
    hierarchy（thiol-ene is_a step-growth）允许推出：
        step-growth → delayed gelation ✓ (inherited)

核心验证：paper.routes × paper.mechanisms 不再被笛卡尔组合成关系。
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from search_engine.route_mechanism_ontology import (
    CoverageMatcher, build_edge, MECHANISM_CANONICAL,
)
from search_engine.models import RouteMechanismEvidenceEdge


def _sample_edges() -> list[RouteMechanismEvidenceEdge]:
    """用户那篇多路线论文的 edges（extractor 输出 raw → build_edge 归并）。"""
    return [
        build_edge(
            paper_id="openalex:W-sample",
            raw_route="AFCT",
            raw_mechanism="stress relaxation",
            evidence="stress relaxation by addition-fragmentation chain transfer",
            confidence=0.95,
        ),
        build_edge(
            paper_id="openalex:W-sample",
            raw_route="thiol-ene",
            raw_mechanism="delayed gelation",
            evidence="Thiol-ene systems exhibit step-growth mechanism, which delays gelation",
            confidence=0.90,
        ),
    ]


def test_build_edge_canonicalizes():
    """build_edge 本地归并 canonical（不依赖 LLM 一致性）。"""
    e = build_edge("P1", "nanoparticle composite", "stress relief",
                   evidence="...", confidence=0.8)
    assert e.canonical_route == "filler"          # 别名归并
    assert e.canonical_mechanism == "stress relaxation"  # MECHANISM_CANONICAL


def test_canonical_mechanism_mapping():
    assert MECHANISM_CANONICAL["stress relief"] == "stress relaxation"
    assert MECHANISM_CANONICAL["pre-gel stress relaxation"] == "stress relaxation"
    assert MECHANISM_CANONICAL["relax stress"] == "stress relaxation"


def test_acceptance_supports():
    """用户验收：4 条断言。"""
    m = CoverageMatcher()
    edges = _sample_edges()
    afct, thiolene = edges

    # 论文直接支持的（DIRECT_MODEL：extractor 自动产出）
    assert m.edge_supports_gap(afct, "AFCT", "stress relaxation") == "DIRECT_MODEL"
    assert m.edge_supports_gap(thiolene, "thiol-ene", "delayed gelation") == "DIRECT_MODEL"
    # hierarchy 继承（thiol-ene is_a step-growth）
    assert m.edge_supports_gap(thiolene, "step-growth", "delayed gelation") == "INHERITED"
    # 禁止笛卡尔组合：AFCT 的 edge 只有 stress relaxation，不连 delayed gelation
    assert m.edge_supports_gap(afct, "AFCT", "delayed gelation") == "NO_MATCH"
    # 反向也不组合：thiol-ene edge 不连 stress relaxation
    assert m.edge_supports_gap(thiolene, "thiol-ene", "stress relaxation") == "NO_MATCH"


def test_human_verified_edge_returns_direct_human():
    """人工核实的 edge（provenance=human_verified）→ DIRECT_HUMAN，与 model 分开统计。"""
    m = CoverageMatcher()
    e = build_edge("P9", "filler", "reduced polymerizable fraction",
                   "High filler loading reduces the volume fraction of polymerizable resin", 1.0,
                   relation_type="human_verified")
    e.provenance = "manual_audit"
    assert m.edge_supports_gap(e, "filler", "reduced polymerizable fraction") == "DIRECT_HUMAN"
    # 不匹配的 gap 依然 NO_MATCH（human edge 也不能跨 gap）
    assert m.edge_supports_gap(e, "filler", "stress transfer") == "NO_MATCH"


def test_no_cartesian_combination():
    """核心：同一论文有两条 route、两个 mechanism，不会自动组合出 4 条关系。"""
    m = CoverageMatcher()
    edges = _sample_edges()
    # 遍历所有 gap 组合，验证只产生真实支持的 3 个（2 DIRECT + 1 INHERITED）
    combos = []
    for e in edges:
        for r in ("AFCT", "thiol-ene", "step-growth"):
            for mech in ("stress relaxation", "delayed gelation"):
                st = m.edge_supports_gap(e, r, mech)
                if st in ("DIRECT_MODEL", "DIRECT_HUMAN", "INHERITED"):
                    combos.append((e.raw_route, r, mech, st))
    assert len(combos) == 3, f"预期 3 条真实支持，实际 {combos}"
    assert ("AFCT", "AFCT", "stress relaxation", "DIRECT_MODEL") in combos
    assert ("thiol-ene", "thiol-ene", "delayed gelation", "DIRECT_MODEL") in combos
    assert ("thiol-ene", "step-growth", "delayed gelation", "INHERITED") in combos


def test_unbound_mechanism_never_closes_gap():
    """unbound mechanism（route 未知）进 inventory 但永不关闭 gap。"""
    m = CoverageMatcher()
    e = build_edge("P2", "", "accelerated curing",
                   evidence="Higher temperature accelerated curing", confidence=0.6)
    assert e.canonical_route == ""            # unbound
    assert m.edge_supports_gap(e, "AFCT", "accelerated curing") == "NO_MATCH"
    assert m.edge_supports_gap(e, "thiol-ene", "accelerated curing") == "NO_MATCH"


def test_inferred_edge_does_not_close_gap():
    """relation_type=inferred：LLM 推断论文未直说 → 不关闭 gap。"""
    m = CoverageMatcher()
    e = build_edge("P3", "AFCT", "stress relaxation",
                   evidence="inferred from mechanism chain", confidence=0.5,
                   relation_type="inferred")
    assert m.edge_supports_gap(e, "AFCT", "stress relaxation") == "INFERRED"


def test_db_roundtrip():
    """DB 存储/读取 roundtrip（store 同步 edges 表 + record_json）。"""
    from search_engine.knowledge_base import KnowledgeBase
    from search_engine.models import KnowledgeRecord
    import tempfile, os
    tmp = tempfile.mktemp(suffix=".db")
    try:
        kb = KnowledgeBase(db_path=tmp)
        rec = KnowledgeRecord(paper_id="P1", strategy_routes=["AFCT", "thiol-ene"])
        rec.route_mechanism_edges = _sample_edges()
        kb.store(rec)
        loaded = kb.get("P1")
        assert loaded is not None
        assert len(loaded.route_mechanism_edges) == 2
        assert loaded.route_mechanism_edges[0].canonical_route == "AFCT"
        assert loaded.route_mechanism_edges[1].canonical_mechanism == "delayed gelation"
        # edges 表 SQL 查询
        edges_from_table = kb.get_edges(canonical_route="thiol-ene")
        assert len(edges_from_table) == 1
        assert edges_from_table[0].canonical_mechanism == "delayed gelation"
        kb.close()
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def test_analyzer_coverage_from_edges():
    """coverage matrix 基于 edges：covered = 有 supporting edge；旧记录无 edges 不贡献。"""
    from search_engine.knowledge.coverage import MechanismCoverageAnalyzer
    from search_engine.models import KnowledgeRecord

    async def run():
        analyzer = MechanismCoverageAnalyzer(backend=None, normalizer=None)
        # 新记录：有 edges
        rec_new = KnowledgeRecord(paper_id="P1", strategy_routes=["AFCT", "thiol-ene"],
                                  extractor_version="2.0-edges")
        rec_new.route_mechanism_edges = _sample_edges()
        # 旧记录：只有 strategy_routes + physical_mechanisms，无 edges（不迁移）
        rec_old = KnowledgeRecord(paper_id="P2", strategy_routes=["AFCT"],
                                  extractor_version="1.1")
        cov = await analyzer.analyze_route_coverage([rec_new, rec_old])
        mc = cov["mechanism_coverage"]
        # 新记录 edge 支持 → covered
        assert mc["AFCT"]["stress relaxation"]["covered"] is True
        assert mc["thiol-ene"]["delayed gelation"]["covered"] is True
        # hierarchy 继承也 covered（标 INHERITED）：step-growth checklist 的 late gelation
        # 与 thiol-ene edge 的 delayed gelation 是同一概念（MECHANISM_CANONICAL）
        assert mc["step-growth"]["late gelation"]["covered"] is True
        assert mc["step-growth"]["late gelation"]["relation"] == "INHERITED"
        # 禁止笛卡尔组合 → AFCT × delayed gelation 仍 missing
        assert mc["AFCT"]["delayed gelation"]["covered"] is False
        assert "delayed gelation" in cov["missing_mechanisms"]["AFCT"]

    asyncio.run(run())
