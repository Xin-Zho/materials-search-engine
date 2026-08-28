"""Candidate Scanner：从 KB 高召回发现 ontology 外候选（不负责判断）。

高召回来源（用户定 2026-08-26）：
  - 所有 raw_route（无法归并到 core route 的）
  - 所有 raw_mechanism（canonical 后不在 checklist 的）
统计：paper_count / edge_count / route association / mechanism association。

原则：宁可多发现，不要负责判断。
"""

from __future__ import annotations

from collections import defaultdict

from ..route_mechanism_ontology import (
    assign_route, canonicalize_mechanism, get_mechanisms, CORE_ROUTE_MECHANISMS,
)
from .candidate import RawCandidate

_CHECKLIST_MECHS = {m for r in CORE_ROUTE_MECHANISMS for m in get_mechanisms(r)}


def scan_kb(kb) -> list[RawCandidate]:
    """扫描 KB 全部 edges → RawCandidate 列表（高召回，未过滤）。"""
    raw: dict[tuple[str, str], RawCandidate] = {}
    for rec in kb.get_all():
        for e in rec.route_mechanism_edges:
            route = (e.canonical_route or e.raw_route or "").strip()
            # 1) raw_route 候选：无法归并到 core route
            rr = (e.raw_route or "").strip()
            if rr and assign_route([rr]) is None:
                key = ("route", rr)
                if key not in raw:
                    raw[key] = RawCandidate(raw_name=rr, kind="route")
                raw[key].paper_ids.add(e.paper_id)
                raw[key].edge_count += 1
                if route:
                    raw[key].route_assoc.add(route)
                if len(raw[key].evidence_samples) < 3 and e.evidence:
                    raw[key].evidence_samples.append(
                        {"paper": e.paper_id, "evidence": e.evidence[:120]})
            # 2) raw_mechanism 候选：canonical 后不在 checklist
            rm = (e.raw_mechanism or "").strip()
            if rm:
                canon = canonicalize_mechanism(rm) or rm
                if canon not in _CHECKLIST_MECHS and rm not in _CHECKLIST_MECHS:
                    key = ("mechanism", rm)
                    if key not in raw:
                        raw[key] = RawCandidate(raw_name=rm, kind="mechanism")
                    raw[key].paper_ids.add(e.paper_id)
                    raw[key].edge_count += 1
                    if route:
                        raw[key].route_assoc.add(route)
                    if e.canonical_mechanism:
                        raw[key].mechanism_assoc.add(e.canonical_mechanism)
                    if len(raw[key].evidence_samples) < 3 and e.evidence:
                        raw[key].evidence_samples.append(
                            {"paper": e.paper_id, "evidence": e.evidence[:120]})
    return list(raw.values())
