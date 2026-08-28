"""审计修复测试：repair_staging_join 离线重建 query_id 关联（用户定 2026-08-27）。

覆盖：registry 无 query_id → 补算（md5(query_text)[:12]）；
provenance 碰撞 id（normalized[:16]）→ 重算；staging.query_ids 空 → 从
provenance 重建（many-to-many）；relevance_status 重置 STAGED。
"""

import json

import pytest

from search_engine.discovery.query_registry import _query_id
from tools.repair_staging_join import repair_staging_join


def _mk(tmp_path, registry, provenance, staging):
    rp = tmp_path / "registry.json"
    pp = tmp_path / "provenance.json"
    sp = tmp_path / "staging.json"
    rp.write_text(json.dumps(registry), encoding="utf-8")
    pp.write_text(json.dumps(provenance), encoding="utf-8")
    sp.write_text(json.dumps({"papers": staging}), encoding="utf-8")
    return str(rp), str(pp), str(sp)


def test_repair_registry_gets_query_id(tmp_path):
    reg = [{"query_text": '"bulk-fill composite" AND "shrinkage stress"',
            "normalized_query": "bulk composite fill shrinkage stress",
            "source_node": "bulk-fill"}]
    rp, pp, sp = _mk(tmp_path, reg, [], [])
    stats = repair_staging_join(rp, pp, sp)
    fixed = json.loads(tmp_path.joinpath("registry.json").read_text(encoding="utf-8"))
    assert fixed[0]["query_id"] == _query_id(fixed[0]["query_text"])
    assert stats["registry_unique_query_ids"] == 1


def test_repair_provenance_query_id_recomputed(tmp_path):
    """碰撞 id（normalized[:16] 截断）→ 重算为 md5(query_text)[:12]，22 条不再碰撞成 4 个。"""
    prov = [{"paper_id": "W1", "query_text": '"bulk-fill composite" AND "shrinkage stress"',
             "query_id": "bulk composite f", "query_family": "NODE"},
            {"paper_id": "W2", "query_text": '"bulk-fill composite" AND "shrinkage strain"',
             "query_id": "bulk composite f", "query_family": "NODE"}]  # 旧碰撞 id
    rp, pp, sp = _mk(tmp_path, [], prov, [])
    repair_staging_join(rp, pp, sp)
    fixed = json.loads(tmp_path.joinpath("provenance.json").read_text(encoding="utf-8"))
    qids = {p["query_id"] for p in fixed}
    assert len(qids) == 2                                   # 不再碰撞
    assert fixed[0]["query_id"] == _query_id(fixed[0]["query_text"])


def test_repair_staging_query_ids_rebuilt_and_restaged(tmp_path):
    """staging.query_ids 从 provenance 重建（many-to-many）+ relevance 重置 STAGED。"""
    prov = [
        {"paper_id": "W1", "query_id": "q1", "query_text": "t1", "query_family": "NODE"},
        {"paper_id": "W1", "query_id": "q2", "query_text": "t2", "query_family": "ADJACENT"},
    ]
    staging = [{"paper_id": "W1", "title": "t", "abstract": "a",
                "relevance_status": "UNCERTAIN", "query_ids": [""]}]  # 空关联 + 已 screen
    rp, pp, sp = _mk(tmp_path, [], prov, staging)
    stats = repair_staging_join(rp, pp, sp)
    fixed = json.loads(tmp_path.joinpath("staging.json").read_text(encoding="utf-8"))["papers"]
    # query_id 重算为 md5(query_text)[:12]（q1/q2 是占位值）
    assert fixed[0]["query_ids"] == sorted({_query_id("t1"), _query_id("t2")})
    assert fixed[0]["relevance_status"] == "STAGED"         # 重新 screening
    assert stats["staging_restaged"] == 1


def test_repair_no_crash_empty_files(tmp_path):
    rp, pp, sp = _mk(tmp_path, [], [], [])
    stats = repair_staging_join(rp, pp, sp)
    assert stats["registry_queries"] == 0
    assert stats["staging_papers"] == 0
