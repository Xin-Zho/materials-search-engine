"""Phase 2.1 Round 状态（用户定 schema 2026-08-26）：每轮可重放、可审计。

DiscoveryRound 记录一轮 discovery loop 的完整状态：
    scan 前 KB/ontology 版本 → 扫描/新增/选择 → 验证结果 → promotion 队列
    → 新节点/新关系 → 查询/论文/成本 → scan 后版本 → 停止原因

每轮 append 到 data/exports/discovery_rounds.json——**每轮可重放**
（selected 顺序 + verification 结果引用 + 前后版本 hash 都在）。

kb_version / ontology_version：文件内容 sha256（不是 mtime——内容不变则版本不变）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
import hashlib
import json
import os

ROUNDS_PATH = "data/exports/discovery_rounds.json"

# 默认 KB 文件（controller 可按实际配置覆盖）
DEFAULT_KB_PATH = "data/cache/knowledge_base.db"
# ontology 定义源（CORE_ROUTE_MECHANISMS 等）
DEFAULT_ONTOLOGY_PATH = "search_engine/route_mechanism_ontology.py"


@dataclass
class DiscoveryRound:
    """一轮 discovery loop 的完整状态（用户定 schema）。"""

    round_id: int

    kb_version_before: str
    ontology_version_before: str

    candidates_scanned: int = 0
    new_candidates: int = 0

    selected_candidates: list[str] = field(default_factory=list)

    verification_results: dict = field(default_factory=dict)   # name → verdict
    promotions: list[str] = field(default_factory=list)        # proposal 队列（待人工 approve）

    new_nodes: list[str] = field(default_factory=list)
    new_relations: list[str] = field(default_factory=list)

    # Phase 2.1（用户定）：approval queue 只记 id 引用，不混入 proposal 本体
    proposal_ids_created: list[str] = field(default_factory=list)
    proposal_ids_approved: list[str] = field(default_factory=list)

    # 单候选验证失败不整轮失败（用户 invariant）：记录后继续其他候选
    candidate_errors: list[str] = field(default_factory=list)

    queries_used: int = 0
    papers_retrieved: int = 0
    new_unique_papers: int = 0
    api_cost: float = 0.0

    kb_version_after: str = ""
    ontology_version_after: str = ""

    stop_reason: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "DiscoveryRound":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def file_sha256(path: str) -> str:
    """文件内容 sha256（前 12 位）。文件不存在/不可读 → 空串（表示无基线）。"""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:12]
    except OSError:
        return ""


def kb_version(kb_path: str = DEFAULT_KB_PATH) -> str:
    return file_sha256(kb_path)


def ontology_version(ontology_path: str = DEFAULT_ONTOLOGY_PATH) -> str:
    return file_sha256(ontology_path)


def load_rounds(path: str = ROUNDS_PATH) -> list[DiscoveryRound]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    return [DiscoveryRound.from_dict(r) for r in data]


def save_rounds(rounds: list[DiscoveryRound], path: str = ROUNDS_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([r.to_dict() for r in rounds], f, ensure_ascii=False, indent=2)


def append_round(round_: DiscoveryRound, path: str = ROUNDS_PATH) -> list[DiscoveryRound]:
    """append 一轮（幂等：同 round_id 已存在则替换，防重跑双写）。"""
    rounds = load_rounds(path)
    rounds = [r for r in rounds if r.round_id != round_.round_id]
    rounds.append(round_)
    rounds.sort(key=lambda r: r.round_id)
    save_rounds(rounds, path)
    return rounds


def latest_round(path: str = ROUNDS_PATH) -> DiscoveryRound | None:
    rounds = load_rounds(path)
    return rounds[-1] if rounds else None
