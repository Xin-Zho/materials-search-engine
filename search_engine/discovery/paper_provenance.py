"""Phase 2.1b Discovery Paper Provenance（用户定 2026-08-26，P2.2）。

**many-to-many**（用户 invariant ②）：同一篇论文可能由 query A / B / C 都找到——
provenance 必须独立存储，不能后来的 query 覆盖前面的。这对以后 RL 很重要：
能知道"哪些 query 实际产生了同一个新 paper"。

独立列表（不塞 paper 一个字段）——paper 一行 + 多条 provenance 追加：
    data/exports/discovery_paper_provenance.json

provenance 链（用户定）：这篇论文是因为上一轮学到了 bulk-fill 才被找到的——
origin=knowledge_expansion + promoted_node + promotion_id + origin_round + query_id。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
import json
import os

PROVENANCE_PATH = "data/exports/discovery_paper_provenance.json"


@dataclass
class DiscoveryPaperProvenance:
    """一条 (paper, query) 来源记录（many-to-many：同 paper 不同 query 各一条）。"""

    paper_id: str
    origin: str = "knowledge_expansion"
    promoted_node: str = ""
    promotion_id: str = ""
    origin_round: int | None = None
    query_id: str = ""
    query_text: str = ""
    query_family: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "DiscoveryPaperProvenance":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def load_provenance(path: str = PROVENANCE_PATH) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else data.get("provenance", [])
    except Exception:
        return []


def save_provenance(records: list[dict], path: str = PROVENANCE_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def add_provenance(records: list[dict], prov: DiscoveryPaperProvenance,
                   path: str | None = None) -> bool:
    """追加一条 (paper, query) 来源（**不覆盖已有**，many-to-many）。

    同 (paper_id, query_id) 组合去重（幂等）；不同 query 对同一 paper 追加多条。
    """
    for r in records:
        if (r.get("paper_id") == prov.paper_id
                and r.get("query_id") == prov.query_id):
            return False
    records.append(prov.to_dict())
    if path is not None:
        save_provenance(records, path)
    return True


def papers_by_query(records: list[dict], query_id: str) -> list[str]:
    return sorted({r["paper_id"] for r in records if r.get("query_id") == query_id})


def queries_for_paper(records: list[dict], paper_id: str) -> list[str]:
    return sorted({r.get("query_id", "") for r in records if r.get("paper_id") == paper_id})


def paper_ids(records: list[dict]) -> set[str]:
    return {r.get("paper_id", "") for r in records if r.get("paper_id")}
