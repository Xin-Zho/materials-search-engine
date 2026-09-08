"""P1-A2: TermBank 归一化读取器。

各主题 termbank.json 的 schema 不统一（历史原因）：
  - photopolymerization_shrinkage（pc001, v1.0 S6 冻结）: 三段式
        terms_verified_exact[] / terms_semantic_candidates[] / terms_rejected[]
        字段含 term/semantic_role/domain/evidence_paper_id/verdict
  - thermochromic_materials（v2 新主题, P1-A）: 平铺式
        terms[]（字段 term/semantic_role/domain/verdict）

本模块把两种 schema 归一为统一 TermEntry 视图，供 S6 bridge 生成器、
S7 term layers 与 QA 链消费——禁止各脚本自己按 topic schema 写特判。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from .topic_config import load_topic, topic_dir  # noqa: F401 (re-export 兼容)


@dataclass
class TermEntry:
    term: str
    role: str                  # lexical | mechanism | observable | context
    domain: str = "general"
    status: str = "CANDIDATE"  # VERIFIED | SEMANTIC_CANDIDATE | CANDIDATE | REJECTED
    evidence_paper_id: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def verified(self) -> bool:
        return self.status == "VERIFIED"


ROLE_ORDER = ("lexical", "mechanism", "observable", "context")


@dataclass
class TermBank:
    topic_id: str
    schema: str                # "pc001_three_tier" | "flat"
    entries: list[TermEntry]
    counts: dict = field(default_factory=dict)

    @property
    def by_role(self) -> dict[str, list[TermEntry]]:
        out = {r: [] for r in ROLE_ORDER}
        for e in self.entries:
            out.setdefault(e.role, []).append(e)
        return out

    @property
    def verified_entries(self) -> list[TermEntry]:
        return [e for e in self.entries if e.verified]

    def term_set(self, roles: tuple[str, ...] | None = None,
                 statuses: tuple[str, ...] | None = None) -> set[str]:
        out = set()
        for e in self.entries:
            if roles and e.role not in roles:
                continue
            if statuses and e.status not in statuses:
                continue
            out.add(e.term)
        return out

    def summary(self) -> dict:
        from collections import Counter
        return {
            "n": len(self.entries),
            "by_role": {r: len(v) for r, v in self.by_role.items()},
            "by_status": dict(Counter(e.status for e in self.entries)),
        }


def _norm_term(t: str) -> str:
    return t.strip().strip('"').rstrip("*")


def load_termbank(topic_id: str) -> TermBank:
    """读 topics/<id>/termbank.json → 归一 TermBank。schema 差异在此收敛。"""
    t = load_topic(topic_id)
    raw = json.load(open(os.path.join(t.dir, "termbank.json"), encoding="utf-8"))

    entries: list[TermEntry] = []
    schema = "flat"
    if "terms_verified_exact" in raw:
        schema = "pc001_three_tier"
        for e in raw.get("terms_verified_exact", []):
            entries.append(TermEntry(
                term=_norm_term(e["term"]),
                role=e.get("semantic_role") or e.get("role") or "lexical",
                domain=e.get("domain", "general"),
                status="VERIFIED",
                evidence_paper_id=e.get("evidence_paper_id"),
                extra={k: v for k, v in e.items()
                       if k not in ("term", "semantic_role", "role", "domain",
                                    "verdict", "evidence_paper_id")},
            ))
        for e in raw.get("terms_semantic_candidates", []):
            entries.append(TermEntry(
                term=_norm_term(e["term"]),
                role=e.get("semantic_role") or e.get("role") or "lexical",
                domain=e.get("domain", "general"),
                status="SEMANTIC_CANDIDATE",
                evidence_paper_id=e.get("evidence_paper_id"),
                extra={k: v for k, v in e.items()
                       if k not in ("term", "semantic_role", "role", "domain",
                                    "verdict", "evidence_paper_id")},
            ))
        for e in raw.get("terms_rejected", []):
            entries.append(TermEntry(
                term=_norm_term(e["term"]),
                role=e.get("semantic_role") or e.get("role") or "lexical",
                domain=e.get("domain", "general"),
                status="REJECTED",
                extra={k: v for k, v in e.items()
                       if k not in ("term", "semantic_role", "role", "domain", "verdict")},
            ))
    elif "terms" in raw and isinstance(raw["terms"], list):
        schema = "flat"
        for e in raw["terms"]:
            verdict = str(e.get("verdict", "CANDIDATE")).upper()
            status = "VERIFIED" if verdict == "VERIFIED_EXACT" else verdict
            entries.append(TermEntry(
                term=_norm_term(e["term"]),
                role=e.get("semantic_role") or e.get("role") or "lexical",
                domain=e.get("domain", "general"),
                status=status,
                extra={k: v for k, v in e.items()
                       if k not in ("term", "semantic_role", "role", "domain", "verdict")},
            ))
    else:
        raise ValueError(f"termbank {topic_id} 无法识别的 schema（无 terms_verified_exact/terms）")

    return TermBank(topic_id=topic_id, schema=schema, entries=entries)
