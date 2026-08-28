"""Phase 3 Statistical Completeness Audit — Universe 冻结快照（用户定 2026-08-27）。

核心 invariant（用户定）：**一次 audit 必须针对一个冻结 universe**。
如果今天随机抽 500 篇 → 明天 Phase 2 又新增 2000 篇 → 继续用原来的统计量，
数学已经不成立（剩余池变了，超几何分布参数失效）。所以 UniverseSnapshot 在
抽样前一次性冻结，universe_hash 保证同参重放逐位可复现。

Universe = 搜索结束后系统的全部候选文献池（found relevant F ∪ remaining pool U）：
- found relevant F   —— 搜索系统判定 relevant 的论文（KB + candidate pool source_papers）
- remaining pool U   —— 其余文献（检索返回过但未判 relevant + 未被搜索系统触及的）
"""

import hashlib
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

COMPLETENESS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "exports")
SNAPSHOT_PATH = os.path.join(COMPLETENESS_DIR, "completeness_universes.json")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# source_type（用户定 2026-08-27 P1.5）：
#   EXTERNAL_AUDIT_UNIVERSE —— 由 AuditUniverseDefinition 的外部宽检索规则构造
#                             （正式 statistical audit 唯一接受）
#   AGENT_SEEN_POOL        —— Search Agent 接触过的论文（KB∪candidate∪staging），
#                             只用于 debug/trajectory/coverage accounting，禁止进正式 audit
EXTERNAL_AUDIT_UNIVERSE = "EXTERNAL_AUDIT_UNIVERSE"
AGENT_SEEN_POOL = "AGENT_SEEN_POOL"
VALID_SOURCE_TYPES = {EXTERNAL_AUDIT_UNIVERSE, AGENT_SEEN_POOL}


def universe_hash(paper_ids: list[str], kb_version: str, schema: str = "v1",
                  source_type: str = EXTERNAL_AUDIT_UNIVERSE,
                  definition_version: str = "") -> str:
    """universe 内容指纹：sorted(paper_ids) + kb_version + schema + source_type
    + definition_version。

    同参冻结 → 相同 hash（可复现）；任何一篇论文增减/版本/source_type 变化 → hash 变化。
    source_type 进 hash：AGENT_SEEN_POOL 与 EXTERNAL 同名同 id 也是不同 universe。
    """
    payload = ("|".join(sorted(set(paper_ids)))
               + f"||{kb_version}||{schema}||{source_type}||{definition_version}")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass
class UniverseSnapshot:
    """一次审计的冻结 universe。创建后不可变（appender 外无修改路径）。"""
    topic_id: str
    universe_id: str
    created_at: str = field(default_factory=_now)

    # 核心：候选文献池（全部论文 id，含 found relevant）
    paper_ids: list[str] = field(default_factory=list)
    total_count: int = 0

    # 来源类型（用户定 P1.5）：正式审计只接受 EXTERNAL_AUDIT_UNIVERSE
    source_type: str = EXTERNAL_AUDIT_UNIVERSE
    definition_version: str = ""     # AuditUniverseDefinition 指纹

    # 环境指纹（可复现/审计）
    kb_version: str = ""
    search_run_ids: list[str] = field(default_factory=list)

    # 来源拆分：{"found_relevant": [ids], "remaining_pool": [ids], ...}
    source_breakdown: dict = field(default_factory=dict)

    universe_hash: str = ""

    def __post_init__(self):
        self.paper_ids = sorted(set(self.paper_ids))
        self.total_count = len(self.paper_ids)
        if not self.universe_hash:
            self.universe_hash = universe_hash(
                self.paper_ids, self.kb_version, source_type=self.source_type,
                definition_version=self.definition_version)

    def found_relevant_count(self) -> int:
        """F：**universe 内**已确认 relevant 的论文数（|U ∩ FoundRelevant|）。

        用户定 2026-08-27（账目口径）：F 必须是交集口径——found_relevant 里
        不在 universe 的论文不算 F（它们暴露的是 universe 检索盲区，不是 recall 贡献）。
        """
        found = set(self.source_breakdown.get("found_relevant", []))
        return len(found & set(self.paper_ids))

    def found_relevant_outside(self) -> list[str]:
        """known_relevant_outside_universe：Agent 已确认 relevant 但不在 universe 的
        论文（KnownRelevantContainment 诊断——正式 audit 前要求 containment=100%）。"""
        found = set(self.source_breakdown.get("found_relevant", []))
        return sorted(found - set(self.paper_ids))

    def known_relevant_total(self) -> int:
        """Agent 已确认 relevant 论文总数（canonical 去重后，含 universe 内外）。"""
        return len(self.source_breakdown.get("found_relevant", []))

    def known_relevant_containment(self) -> float:
        """KnownRelevantContainment = F / known_relevant_total（用户定：正式 audit
        前必须 = 100%——否则明知 universe 有洞还证明 universe 内 recall 无意义）。"""
        total = self.known_relevant_total()
        return self.found_relevant_count() / total if total else 1.0

    def check_accounting(self) -> dict:
        """账目硬断言（用户定）：|U| = F + N_remaining。"""
        f = self.found_relevant_count()
        n = self.remaining_pool_size()
        total = len(self.paper_ids)
        assert total == f + n, \
            f"账目不一致: total({total}) != F({f}) + N_remaining({n})"
        return {"total": total, "F": f, "N_remaining": n}

    def remaining_pool(self) -> list[str]:
        """U_remaining：剩余池（universe − found_relevant 交集）——Auditor 从这抽。"""
        found = set(self.source_breakdown.get("found_relevant", []))
        return [p for p in self.paper_ids if p not in found]

    def remaining_pool_size(self) -> int:
        return len(self.remaining_pool())

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "UniverseSnapshot":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def freeze_universe(topic_id: str, paper_ids: list[str],
                    kb_version: str = "",
                    search_run_ids: list[str] | None = None,
                    source_breakdown: dict | None = None,
                    universe_id: str | None = None,
                    source_type: str = EXTERNAL_AUDIT_UNIVERSE,
                    definition_version: str = "") -> UniverseSnapshot:
    """冻结一个 universe（抽样前调用一次，之后只读）。

    source_breakdown 必须含 found_relevant（F 的来源）；remaining_pool 由
    paper_ids − found_relevant 推导，不显式传（防两处不同步）。
    source_type（用户定 P1.5）：正式审计只接受 EXTERNAL_AUDIT_UNIVERSE；
    AGENT_SEEN_POOL 只用于 debug/coverage accounting，create_audit 会拒绝。
    """
    if source_type not in VALID_SOURCE_TYPES:
        raise ValueError(f"非法 source_type: {source_type}")
    bd = dict(source_breakdown or {})
    found = set(bd.get("found_relevant", []))
    if not found and paper_ids:
        raise ValueError("source_breakdown['found_relevant'] 为空——"
                         "必须显式给出 F（搜索系统判定 relevant 的论文），"
                         "否则 remaining_pool 无法定义")
    uid = universe_id or f"{topic_id}-{_now()[:19].replace(':', '')}"
    snap = UniverseSnapshot(
        topic_id=topic_id, universe_id=uid, paper_ids=paper_ids,
        kb_version=kb_version, search_run_ids=list(search_run_ids or []),
        source_breakdown=bd, source_type=source_type,
        definition_version=definition_version)
    return snap


def load_snapshots(path: str | None = None) -> list[dict]:
    path = path or SNAPSHOT_PATH
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else data.get("universes", [])
    except Exception:
        return []


def save_snapshot(snap: UniverseSnapshot, path: str | None = None) -> None:
    """append 保存；同 universe_id 幂等替换（防重跑双写）。"""
    path = path or SNAPSHOT_PATH
    snaps = [s for s in load_snapshots(path) if s.get("universe_id") != snap.universe_id]
    snaps.append(snap.to_dict())
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snaps, f, ensure_ascii=False, indent=1)
