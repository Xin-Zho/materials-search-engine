"""Phase 3 Statistical Completeness Audit — Auditor 独立概率抽样（用户定 2026-08-27）。

核心 invariant：
1. **Auditor 不看 Agent 排名**——从冻结 universe 的 remaining pool 做简单随机
   **不放回**抽样（random.Random(seed).sample），任何人工/系统偏好不得进入抽样。
2. **可复现**：同一 universe + 同一 seed + 同一 sample size → 抽到完全相同的样本
   （SamplingManifest 记录全部参数）。
3. 抽样发生在 **universe 冻结之后**——剩余池在冻结时已定死。

SamplingManifest 保存后供 report/audit 重放与人工标注 relevance_labels。
"""

import json
import os
import random
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

COMPLETENESS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "exports")
MANIFEST_PATH = os.path.join(COMPLETENESS_DIR, "completeness_manifests.json")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class SamplingManifest:
    """一次抽样的完整记录（可重放 + 人工标注 relevance_labels 的载体）。"""
    audit_id: str
    universe_id: str
    created_at: str = field(default_factory=_now)

    random_seed: int = 0
    sampling_method: str = "srs_without_replacement"   # simple random sampling w/o replacement
    sample_size: int = 0
    remaining_population_size: int = 0

    sampled_paper_ids: list[str] = field(default_factory=list)
    # 人工标注：{paper_id: "RELEVANT"|"NOT_RELEVANT"}（Auditor 判断，与 Agent 无关）
    relevance_labels: dict = field(default_factory=dict)

    confidence_level: float = 0.95

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SamplingManifest":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def draw_sample(remaining_pool: list[str], sample_size: int,
                seed: int | None = None,
                audit_id: str = "", universe_id: str = "",
                confidence_level: float = 0.95) -> SamplingManifest:
    """简单随机不放回抽样（可复现）。

    sample_size 超过池大小时取全池（并提示——统计上该池被完全检查）。
    同 (remaining_pool, sample_size, seed) → 完全相同样本（deterministic）。
    """
    import random as _r
    pool = sorted(set(remaining_pool))
    rng = _r.Random(seed)          # 独立 RNG，不污染全局 random
    n = min(sample_size, len(pool))
    sampled = rng.sample(pool, n)
    return SamplingManifest(
        audit_id=audit_id, universe_id=universe_id,
        random_seed=seed if seed is not None else rng.randrange(1 << 30),
        sampling_method="srs_without_replacement",
        sample_size=n,
        remaining_population_size=len(pool),
        sampled_paper_ids=sorted(sampled),
        confidence_level=confidence_level,
    )


def load_manifests(path: str | None = None) -> list[dict]:
    path = path or MANIFEST_PATH
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else data.get("manifests", [])
    except Exception:
        return []


def save_manifest(m: SamplingManifest, path: str | None = None) -> None:
    path = path or MANIFEST_PATH
    manifests = [x for x in load_manifests(path)
                 if x.get("audit_id") != m.audit_id]
    manifests.append(m.to_dict())
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifests, f, ensure_ascii=False, indent=1)
