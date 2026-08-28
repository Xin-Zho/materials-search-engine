"""Phase 3 P2 — Audit 生命周期编排（用户定 2026-08-27）。

核心原则（用户 hard requirement）：
> **sample labels 必须来自独立审计流程**——Search Agent 不能给自己证明没漏。

生命周期（两个时刻分离）：
```
Step A: create_audit
  Freeze Universe → draw_sample → 导出待审样本
  status = AWAITING_LABELS（此时系统不知道 m——独立 Auditor 还没审）
  ↓
Step B: load_labels
  独立 Auditor 审 n 篇 → RELEVANT / IRRELEVANT（不做 UNCERTAIN）
  全 n 篇有 label 且无 UNRESOLVED → COMPLETED + 精确重建 m
  任何缺失 / UNRESOLVED → INCOMPLETE_LABELS，**不计算正式 Recall_LCB**
  （宁可不出数，也不要默认当 irrelevant）
```

status 三态（用户锁死）：AWAITING_LABELS / INCOMPLETE_LABELS / COMPLETED
只有 COMPLETED 才允许出现 M_upper / Recall_LCB / STATISTICAL_STOP。

F 定义（用户锁死）：UniverseSnapshot 冻结时搜索系统已判定 relevant 的
**唯一 canonical 论文数**（同 DOI 的 preprint/duplicate 去重）；不含 STAGED/
UNCERTAIN/IRRELEVANT。由 universe_builder 注入保证（测试用 synthetic）。
"""

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

from .universe import (
    UniverseSnapshot, freeze_universe, load_snapshots, save_snapshot,
    universe_hash, SNAPSHOT_PATH, EXTERNAL_AUDIT_UNIVERSE, AGENT_SEEN_POOL,
)
from .sampler import (
    SamplingManifest, draw_sample, load_manifests, save_manifest, MANIFEST_PATH,
)
from .recall_bound import recall_lower_bound

COMPLETENESS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "exports")
LABELS_DIR = os.path.join(COMPLETENESS_DIR, "completeness_labels")

AWAITING_LABELS = "AWAITING_LABELS"
INCOMPLETE_LABELS = "INCOMPLETE_LABELS"
COMPLETED = "COMPLETED"

VALID_LABELS = {"RELEVANT", "IRRELEVANT"}   # 统计审计只用二值；UNCERTAIN 不进审计


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


@dataclass
class AuditRecord:
    """一次审计的完整状态（universe + manifest + labels + 统计结果）。"""
    audit_id: str
    topic_id: str
    created_at: str = field(default_factory=_now)

    universe_id: str = ""
    universe_hash: str = ""
    kb_version: str = ""

    F: int = 0                      # found relevant（冻结时搜索系统已判 relevant，canonical 去重后）
    N_remaining: int = 0            # |Universe − FoundRelevant|

    sample_size: int = 0
    seed: int = 0
    confidence_level: float = 0.95
    target_recall: float = 0.95

    status: str = AWAITING_LABELS
    sampled_paper_ids: list[str] = field(default_factory=list)

    # 独立 Auditor 标签：{paper_id: "RELEVANT"|"IRRELEVANT"}（全部来自 labels 文件）
    labels: dict = field(default_factory=dict)
    m: int | None = None            # 样本中 missed relevant（COMPLETED 后才有）

    # 统计结果（只有 COMPLETED 才计算）
    M_upper: int | None = None
    p_upper: float | None = None
    recall_lcb: float | None = None
    statistical_stop: bool | None = None
    diagnostic_warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AuditRecord":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


AUDITS_PATH = os.path.join(COMPLETENESS_DIR, "completeness_audits.json")


def load_audits(path: str | None = None) -> list[dict]:
    path = path or AUDITS_PATH
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else data.get("audits", [])
    except Exception:
        return []


def save_audit(audit: AuditRecord, path: str | None = None) -> None:
    path = path or AUDITS_PATH
    audits = [a for a in load_audits(path) if a.get("audit_id") != audit.audit_id]
    audits.append(audit.to_dict())
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(audits, f, ensure_ascii=False, indent=1)


def find_audit(audit_id: str, path: str | None = None) -> AuditRecord | None:
    for a in load_audits(path):
        if a.get("audit_id") == audit_id:
            return AuditRecord.from_dict(a)
    return None


class InvalidAuditUniverse(Exception):
    """审计总体不合法（用户 P1.5 定）：Agent-seen pool 禁止进正式 statistical audit。"""


# ── Step A：创建审计（冻结 + 抽样 + 导出样本）──

def create_audit(topic_id: str, universe_builder, sample_size: int = 500,
                 confidence_level: float = 0.95, target_recall: float = 0.95,
                 seed: int = 42, audits_path: str | None = None,
                 labels_dir: str | None = None,
                 snapshots_path: str | None = None,
                 manifests_path: str | None = None) -> AuditRecord:
    """创建审计：freeze universe → draw sample → AWAITING_LABELS。

    universe_builder() -> dict{paper_ids, found_relevant, kb_version, search_run_ids,
                               source_type}——**必须 source_type=EXTERNAL_AUDIT_UNIVERSE**
    （由 AuditUniverseDefinition 的外部宽检索构造）；AGENT_SEEN_POOL → raise
    InvalidAuditUniverse（用户 P1.5 硬要求：Agent 接触过的论文不能自证没漏）。

    F = len(found_relevant)（已 canonical 去重，由 builder 保证）；
    N_remaining = |Universe − FoundRelevant|（universe.remaining_pool_size()）。
    """
    uni = universe_builder()
    src_type = uni.get("source_type", EXTERNAL_AUDIT_UNIVERSE)
    if src_type != EXTERNAL_AUDIT_UNIVERSE:
        raise InvalidAuditUniverse(
            f"正式 statistical audit 只接受 EXTERNAL_AUDIT_UNIVERSE，"
            f"收到 {src_type}——Agent-seen pool（KB∪candidate∪staging）不能"
            f"自证没漏：从未被检索到的论文不在其中，无法进入抽样池")
    snap = freeze_universe(
        topic_id=topic_id, paper_ids=uni["paper_ids"],
        kb_version=uni.get("kb_version", ""),
        search_run_ids=uni.get("search_run_ids", []),
        source_breakdown={"found_relevant": uni["found_relevant"]},
        source_type=EXTERNAL_AUDIT_UNIVERSE,
        definition_version=uni.get("definition_version", ""))
    save_snapshot(snap, snapshots_path)

    audit_id = f"{topic_id}::{_now()}"
    manifest = draw_sample(snap.remaining_pool(), sample_size, seed=seed,
                           audit_id=audit_id, universe_id=snap.universe_id,
                           confidence_level=confidence_level)
    save_manifest(manifest, manifests_path)

    audit = AuditRecord(
        audit_id=audit_id, topic_id=topic_id,
        universe_id=snap.universe_id, universe_hash=snap.universe_hash,
        kb_version=snap.kb_version,
        F=snap.found_relevant_count(), N_remaining=snap.remaining_pool_size(),
        sample_size=manifest.sample_size, seed=manifest.random_seed,
        confidence_level=confidence_level, target_recall=target_recall,
        status=AWAITING_LABELS, sampled_paper_ids=manifest.sampled_paper_ids)
    save_audit(audit, audits_path)

    # 导出标签模板（独立 Auditor 待审）
    export_labels_template(audit, labels_dir)
    return audit


def _safe_name(s: str) -> str:
    """audit_id 含 '::'（Windows NTFS 非法文件名）——文件用安全名。"""
    return s.replace("::", "__").replace(":", "-")


def export_labels_template(audit: AuditRecord, labels_dir: str | None = None) -> str:
    """导出待审样本（独立 Auditor 用）。每篇 {paper_id, label: UNRESOLVED, reviewer, reason}。"""
    labels_dir = labels_dir or LABELS_DIR
    os.makedirs(labels_dir, exist_ok=True)
    path = os.path.join(labels_dir, f"{_safe_name(audit.audit_id)}.json")
    payload = {
        "audit_id": audit.audit_id,
        "universe_id": audit.universe_id,
        "labels": [{"paper_id": pid, "label": "UNRESOLVED",
                    "reviewer": "", "reason": ""}
                   for pid in audit.sampled_paper_ids],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return path


# ── Step B：加载独立 Auditor 标签 ──

def load_labels(audit: AuditRecord, labels_data: dict,
                audits_path: str | None = None) -> AuditRecord:
    """加载 Auditor 标签 → 校验完整性 → COMPLETED / INCOMPLETE_LABELS。

    规则（用户定）：
    - 标签数 < sample_size，或任一 UNRESOLVED / 非法值 → INCOMPLETE_LABELS，
      不计算正式 Recall_LCB（宁可不出数，不要默认当 irrelevant）
    - 全部 RELEVANT/IRRELEVANT → COMPLETED，m = RELEVANT 数（精确重建）
    - 只有 COMPLETED 才计算 M_upper / p_upper / recall_lcb / statistical_stop
    """
    raw = labels_data.get("labels", []) if isinstance(labels_data, dict) else labels_data
    labels = {x.get("paper_id"): x.get("label") for x in raw if isinstance(x, dict)}

    sampled = set(audit.sampled_paper_ids)
    invalid = [pid for pid, lab in labels.items()
               if lab not in VALID_LABELS]
    unresolved = [pid for pid in sampled if labels.get(pid) in (None, "UNRESOLVED")]
    missing = [pid for pid in sampled if pid not in labels]

    audit.labels = {pid: labels[pid] for pid in sampled if pid in labels}

    if missing or unresolved or invalid:
        audit.status = INCOMPLETE_LABELS
        audit.m = None
        audit.M_upper = audit.p_upper = audit.recall_lcb = None
        audit.statistical_stop = None
        reason = []
        if missing:
            reason.append(f"{len(missing)} 篇无标签")
        if unresolved:
            reason.append(f"{len(unresolved)} 篇 UNRESOLVED/未裁决")
        if invalid:
            reason.append(f"非法标签值: {invalid[:3]}")
        audit.diagnostic_warnings = reason
        save_audit(audit, audits_path)
        return audit

    audit.status = COMPLETED
    audit.m = sum(1 for lab in audit.labels.values() if lab == "RELEVANT")
    _compute_statistics(audit)
    save_audit(audit, audits_path)
    return audit


def _compute_statistics(audit: AuditRecord) -> None:
    """只有 COMPLETED 才允许：M_upper / Recall_LCB / STATISTICAL_STOP（用户锁死）。"""
    if audit.status != COMPLETED:
        audit.M_upper = audit.p_upper = audit.recall_lcb = None
        audit.statistical_stop = None
        return
    r = recall_lower_bound(
        F=audit.F, N=audit.N_remaining, n=audit.sample_size,
        m=audit.m or 0, confidence_level=audit.confidence_level)
    audit.M_upper = r["M_upper"]
    audit.p_upper = r["p_upper"]
    audit.recall_lcb = r["recall_lower"]
    # 唯一正式停止条件（用户锁死：不再有任何其他条件进入）：
    #   statistical_stop = (COMPLETED and recall_LCB >= target_recall)
    audit.statistical_stop = audit.recall_lcb >= audit.target_recall


# ── 重放：全量可复现 ──

def replay(audit_id: str, audits_path: str | None = None) -> AuditRecord | None:
    """重放：从持久化 audit 重建——universe hash / sample IDs / m / M_upper /
    Recall_LCB / STOP 必须逐位与首次一致（audit 已存全部派生量，重放即回读 +
    重算校验一致性）。"""
    audit = find_audit(audit_id, audits_path)
    if audit is None:
        return None
    # 数学重算校验（防手改数据）——与 P0 函数逐位一致
    if audit.status == COMPLETED:
        r = recall_lower_bound(F=audit.F, N=audit.N_remaining,
                               n=audit.sample_size, m=audit.m or 0,
                               confidence_level=audit.confidence_level)
        assert r["M_upper"] == audit.M_upper
        assert abs(r["recall_lower"] - (audit.recall_lcb or 0)) < 1e-9
    return audit
