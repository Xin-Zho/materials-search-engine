"""Phase 2.1b Query Registry（用户定 2026-08-26）：历史 query 去重与 provenance。

硬 invariant（用户定）：**只允许执行历史上没有执行过的新 query。**
否则 Expander 很容易重新生成 Phase 0/1 已经搜过的 query（变成普通 query expansion）。

每条记录：
    query_text         —— 原始 query 字符串
    normalized_query   —— 归一化（小写 + 去标点 + 空白折叠 + **词排序**：
                          短语 AND 检索词序无关，"bulk-fill ... shrinkage stress" 与
                          "shrinkage stress bulk-fill ..." 视为同一 query）
    source_node        —— 生成它的 promoted node（如 bulk-fill composite formulation）
    source_relation    —— 沿哪条关系生成（has_design_factor / affects / can_reduce / adjacent）
    origin_round       —— 生成时的 discovery round（None = 人工/工具直跑）
    origin_promotion   —— promotion_id 或 candidate_id（可追溯）
    query_family       —— NODE / RELATION / MECHANISM / ADJACENT
    executed_before    —— 是否已执行检索（P2.2 置 True）
    created_at         —— UTC 时间
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
import json
import os
import re
from datetime import datetime, timezone

REGISTRY_PATH = "data/exports/discovery_query_registry.json"

# 词排序归一化时忽略的停用词（query 去重：只保留关键词）
_STOPWORDS = {"of", "the", "and", "for", "in", "with", "based", "on", "a", "an"}


@dataclass
class QueryRecord:
    query_text: str
    normalized_query: str
    query_id: str = ""               # md5(query_text)[:12]——跨 registry/staging/provenance 唯一 join 键
    source_node: str = ""
    source_relation: str = ""
    origin_round: int | None = None
    origin_promotion: str | None = None
    query_family: str = "NODE"       # NODE / RELATION / MECHANISM / ADJACENT
    executed_before: bool = False
    created_at: str = field(default_factory=lambda:
                            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    def __post_init__(self):
        # query_id 稳定生成：query_text hash（22 条 query 各自唯一；
        # 旧 normalized[:16] 截断会产生碰撞——'bulk composite f/c/d/e' 只 4 个 id，已踩坑 2026-08-27）
        if not self.query_id:
            self.query_id = _query_id(self.query_text)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "QueryRecord":
        q = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        return q


def _query_id(query_text: str) -> str:
    import hashlib
    return hashlib.md5((query_text or "").encode("utf-8")).hexdigest()[:12]


def normalize_query(query: str) -> str:
    """归一化：小写 → 非字母数字全当分隔符（含连字符：'bulk-fill' ≡ 'bulk fill'、
    'stress-relieving' ≡ 'stress relieving'）→ 去停用词 → 词排序。

    词排序：OpenAlex search_relevance 用短语 AND 关键词共现，词序无关——
    "bulk-fill composite formulation shrinkage stress" 与
    "shrinkage stress bulk-fill composite formulation" 是同一检索意图。
    """
    t = re.sub(r"[^a-z0-9]+", " ", query.lower())
    t = re.sub(r"\s+", " ", t).strip()
    tokens = [w for w in t.split(" ") if w and w not in _STOPWORDS]
    return " ".join(sorted(tokens))


def load_registry(path: str = REGISTRY_PATH) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            records = data if isinstance(data, list) else data.get("queries", [])
    except Exception:
        return []
    # 历史兼容：旧记录无 query_id → 补算（md5(query_text)[:12]）
    for r in records:
        if not r.get("query_id"):
            r["query_id"] = _query_id(r.get("query_text", ""))
    return records


def save_registry(records: list[dict], path: str = REGISTRY_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def _normalized_set(records: list[dict]) -> set[str]:
    return {r.get("normalized_query", "") for r in records if r.get("normalized_query")}


def register(records: list[dict], new_records: list[QueryRecord],
             path: str | None = None) -> tuple[list[QueryRecord], list[QueryRecord]]:
    """注册新 query（硬 invariant：normalized 已存在 → 不注册，计为 duplicate）。

    返回 (added_records, duplicate_records)。已有记录**不被修改**（executed_before 保持）。
    """
    existing = _normalized_set(records)
    added: list[QueryRecord] = []
    duplicates: list[QueryRecord] = []
    for q in new_records:
        if q.normalized_query in existing:
            duplicates.append(q)
            continue
        records.append(q.to_dict())
        existing.add(q.normalized_query)
        added.append(q)
    if path is not None and added:
        save_registry(records, path)
    return added, duplicates


def has_executed(records: list[dict], query_text: str) -> bool:
    """该 query（normalize 后）是否已执行过（P2.2 用：只跑未执行的）。"""
    n = normalize_query(query_text)
    for r in records:
        if r.get("normalized_query") == n:
            return bool(r.get("executed_before"))
    return False


def mark_executed(records: list[dict], query_text: str,
                  path: str | None = None) -> bool:
    """标记某 query 已执行（P2.2 检索后调用）。"""
    n = normalize_query(query_text)
    for r in records:
        if r.get("normalized_query") == n:
            r["executed_before"] = True
            if path is not None:
                save_registry(records, path)
            return True
    return False
