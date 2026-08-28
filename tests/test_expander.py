"""Phase 2.1b P2.1 测试：query registry + expander。

覆盖（用户定 P2.1 验收）：
    bulk-fill 能生成合理的新 query（4 类：NODE/RELATION/MECHANISM/ADJACENT）
    历史 query 正确去重（normalized 等价视为重复）
    硬 invariant：只注册历史未执行过的新 query；已有记录不被修改
    provenance（promotion_id / promoted_node / round / query_family）齐全
"""

import json

import pytest

from search_engine.discovery.query_registry import (
    QueryRecord, normalize_query, load_registry, save_registry, register,
    has_executed, mark_executed,
)
from search_engine.discovery.expander import generate_queries, count_by_family


# ── normalize ──

def test_normalize_case_punctuation_stopwords():
    assert normalize_query("Bulk-fill, Composite Formulation!") == \
        normalize_query("bulk fill composite formulation")
    assert normalize_query("bulk-fill composite formulation shrinkage stress") == \
        normalize_query("shrinkage stress bulk-fill composite formulation")  # 词序无关
    assert "of" not in normalize_query("degree of conversion")


# ── register：硬 invariant（只注册历史未执行过的新 query）──

def test_register_dedup_by_normalized():
    q1 = QueryRecord(query_text="bulk-fill composite formulation shrinkage stress",
                     normalized_query=normalize_query(
                         "bulk-fill composite formulation shrinkage stress"),
                     source_node="bulk-fill composite formulation")
    q2 = QueryRecord(query_text="shrinkage stress bulk-fill composite formulation",
                     normalized_query=normalize_query(
                         "shrinkage stress bulk-fill composite formulation"),
                     source_node="bulk-fill composite formulation")
    records = []
    added, dups = register(records, [q1, q2])
    assert len(added) == 1                    # q2 normalized 与 q1 等价 → duplicate
    assert len(dups) == 1
    assert len(records) == 1


def test_register_does_not_touch_existing():
    records = [{
        "query_text": "old query", "normalized_query": "old query",
        "source_node": "x", "query_family": "NODE",
        "executed_before": True, "created_at": "",
    }]
    snapshot = json.dumps(records[0], sort_keys=True)
    q = QueryRecord(query_text="new query",
                    normalized_query=normalize_query("new query"))
    added, _ = register(records, [q])
    assert len(added) == 1
    assert json.dumps(records[0], sort_keys=True) == snapshot  # 已有记录原样


def test_register_persists(tmp_path):
    path = tmp_path / "registry.json"
    records = []
    q = QueryRecord(query_text="bulk-fill filler content shrinkage stress",
                    normalized_query=normalize_query(
                        "bulk-fill filler content shrinkage stress"))
    added, _ = register(records, [q], path=str(path))
    assert len(added) == 1
    loaded = load_registry(str(path))
    assert len(loaded) == 1
    assert loaded[0]["query_text"] == "bulk-fill filler content shrinkage stress"


def test_has_executed_and_mark(tmp_path):
    path = tmp_path / "registry.json"
    records = []
    q = QueryRecord(query_text="bulk-fill polymerization kinetics",
                    normalized_query=normalize_query("bulk-fill polymerization kinetics"))
    register(records, [q], path=str(path))
    assert not has_executed(records, "bulk-fill polymerization kinetics")
    assert mark_executed(records, "POLYMERIZATION kinetics bulk-fill", path=str(path))
    assert has_executed(records, "bulk-fill polymerization kinetics")


# ── expander：bulk-fill 生成 4 类 query ──

def _bulkfill_relations():
    return [
        {"source_node": "bulk-fill composite formulation",
         "predicate": "has_design_factor", "target_node": "filler content",
         "evidence_type": "DIRECT", "grounding_status": "GROUNDED"},
        {"source_node": "bulk-fill composite formulation",
         "predicate": "can_reduce", "target_node": "shrinkage stress",
         "evidence_type": "DIRECT", "grounding_status": "GROUNDED"},
        {"source_node": "shrinkage stress",
         "predicate": "contributes_to", "target_node": "interfacial debonding",
         "evidence_type": "DIRECT", "grounding_status": "GROUNDED"},
    ]


def _bulkfill_chain():
    return [
        {"step": "Bulk-fill composite formulations are designed with specific "
                 "composition (e.g., filler content, monomer type)",
         "evidence": "", "paper_id": "W1", "evidence_type": "DIRECT"},
        {"step": "The formulation composition directly influences polymerization "
                 "shrinkage magnitude", "evidence": "", "paper_id": "W2",
         "evidence_type": "DIRECT"},
        {"step": "Polymerization shrinkage generates shrinkage stress at the "
                 "tooth-restoration interface", "evidence": "", "paper_id": "W3",
         "evidence_type": "DIRECT"},
    ]


def test_generate_queries_four_families():
    qs = generate_queries("bulk-fill composite formulation",
                          relations=_bulkfill_relations(),
                          causal_chain=_bulkfill_chain(),
                          promotion_id="c1", round_id=2)
    assert qs, "必须生成 query"
    fams = {q.query_family for q in qs}
    assert fams == {"NODE", "RELATION", "MECHANISM", "ADJACENT"}   # 4 类全覆盖
    # provenance 齐全（用户定：能证明"这篇论文是因为上一轮学到 bulk-fill 才被找到"）
    for q in qs:
        assert q.source_node == "bulk-fill composite formulation"
        assert q.origin_promotion == "c1"
        assert q.origin_round == 2
        assert q.normalized_query


def test_generate_node_queries():
    qs = generate_queries("bulk-fill composite formulation",
                          relations=_bulkfill_relations())
    node_qs = [q for q in qs if q.query_family == "NODE"]
    assert any("shrinkage stress" in q.query_text for q in node_qs)
    assert any("polymerization shrinkage" in q.query_text for q in node_qs)


def test_generate_relation_queries_along_grounded():
    """RELATION：沿 grounded relation target（filler content）生成 node×target×term。"""
    qs = generate_queries("bulk-fill composite formulation",
                          relations=_bulkfill_relations())
    rel_qs = [q for q in qs if q.query_family == "RELATION"]
    assert rel_qs, "必须沿 relation 生成"
    assert any("filler content" in q.query_text for q in rel_qs)
    assert all(q.source_relation.startswith("relation:") for q in rel_qs)


def test_generate_adjacent_queries():
    """ADJACENT：找子方向（stress-relieving monomer / low modulus ...）。"""
    qs = generate_queries("bulk-fill composite formulation")
    adj_qs = [q for q in qs if q.query_family == "ADJACENT"]
    texts = " ".join(q.query_text for q in adj_qs)
    assert "stress-relieving monomer" in texts
    assert "low modulus" in texts
    assert "photoinitiator system" in texts


def test_expander_does_not_judge_knowledge():
    """Expander 只生成 query，不产 candidate/不判机制（分层不混，用户定）。"""
    qs = generate_queries("bulk-fill composite formulation",
                          relations=_bulkfill_relations())
    for q in qs:
        assert q.query_family in ("NODE", "RELATION", "MECHANISM", "ADJACENT")
    # 输出的全部是 query 字符串，没有 candidate/verdict 字段


def test_query_text_openalex_format():
    """query_text 必须是 OpenAlex 兼容格式（'"短语" AND "短语"'）——
    整句会被 search_relevance 当精确短语匹配导致 0 结果（已踩坑）；
    node 短语取前 2 词（全名 5 词短语 AND 目标短语实测 count=0，2 词=92）。"""
    qs = generate_queries("bulk-fill composite formulation")
    for q in qs:
        assert q.query_text.startswith('"'), q.query_text
        assert 'AND "' in q.query_text, q.query_text
    node_q = next(q for q in qs if q.query_family == "NODE")
    assert node_q.query_text == '"bulk-fill composite" AND "shrinkage stress"'
    # source_node 保留全名（provenance 语义完整）
    assert node_q.source_node == "bulk-fill composite formulation"


def test_short_node():
    from search_engine.discovery.expander import _short_node
    assert _short_node("bulk-fill composite formulation") == "bulk-fill composite"
    assert _short_node("dynamic covalent bond exchange") == "dynamic covalent"
    assert _short_node("shrinkage stress") == "shrinkage stress"   # ≤2 词不动


def test_count_by_family():
    qs = generate_queries("bulk-fill composite formulation")
    counts = count_by_family(qs)
    assert sum(counts.values()) == len(qs)
    assert counts.get("NODE", 0) > 0 and counts.get("ADJACENT", 0) > 0


def test_full_pipeline_register_generated(tmp_path):
    """生成 → 注册（去重）→ 再次生成 → 全部重复。"""
    path = tmp_path / "registry.json"
    records = []
    qs1 = generate_queries("bulk-fill composite formulation",
                           relations=_bulkfill_relations(),
                           promotion_id="c1", round_id=2)
    added1, _ = register(records, qs1, path=str(path))
    assert len(added1) == len(qs1)             # 首次全部注册
    # 同一 node 重新展开 → normalized 全部重复（不双写）
    qs2 = generate_queries("bulk-fill composite formulation",
                           relations=_bulkfill_relations(),
                           promotion_id="c1", round_id=2)
    added2, dups2 = register(records, qs2, path=str(path))
    assert len(added2) == 0
    assert len(dups2) == len(qs2)
    assert len(load_registry(str(path))) == len(qs1)
