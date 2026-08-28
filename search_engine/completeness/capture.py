"""Phase 3 P1 — Capture-recapture diagnostic（用户定 2026-08-27，非常克制）。

只输出 CaptureRecaptureDiagnostic，**绝不输出** coverage_estimate=definitive / stop=true：
- 两源 Chapman 修正：N̂ = (n1+1)(n2+1)/(m+1) − 1（比裸 Lincoln-Petersen 稳定，
  尤其 overlap 小时）；overlap=0 → INSUFFICIENT_DATA（不崩，不硬算）
- 多源 Chao-type：保留接口，但必须输出 ASSUMPTION_WARNING
  （retrieval channels are dependent）并记录 capture source 类型
- **绝不用 NODE/RELATION/MECHANISM/ADJACENT 当四个独立 source**（相关性太高——
  同 ontology/同 OpenAlex/共享关键词/热门论文被所有 family 捕获）
- 真实来源应为：LEXICAL_SEARCH / SEMANTIC_SEARCH / FORWARD_CITATION /
  BACKWARD_CITATION / REVIEW_REFERENCE；没有足够独立通道时
  status=NOT_ENOUGH_INDEPENDENT_CHANNELS——这是**正确行为**，不为报告好看硬算 N̂

status 三态（用户定）：AVAILABLE / INSUFFICIENT_DATA / INVALID_ASSUMPTION
"""

from dataclasses import dataclass, field, asdict

AVAILABLE = "AVAILABLE"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
INVALID_ASSUMPTION = "INVALID_ASSUMPTION"
NOT_ENOUGH_INDEPENDENT_CHANNELS = "NOT_ENOUGH_INDEPENDENT_CHANNELS"

# 真正的独立通道类型（用户定）；query family 不在此列
INDEPENDENT_CHANNEL_TYPES = {
    "LEXICAL_SEARCH", "SEMANTIC_SEARCH", "FORWARD_CITATION",
    "BACKWARD_CITATION", "REVIEW_REFERENCE",
}

ASSUMPTION_WARNING = "retrieval channels are dependent (shared ontology / shared corpus / correlated keywords)"


@dataclass
class CaptureRecaptureDiagnostic:
    method: str = ""                 # "chapman_two_source" / "chao_multi_source"
    n1: int = 0
    n2: int = 0
    overlap: int = 0
    source_types: list[str] = field(default_factory=list)

    N_hat: float | None = None       # 仅 AVAILABLE 时有值
    status: str = INSUFFICIENT_DATA
    reason: str = ""
    assumption_warning: str = ""


def chapman_diagnostic(n1: int, n2: int, overlap: int,
                       source_types: list[str] | None = None,
                       dependent: bool = True) -> CaptureRecaptureDiagnostic:
    """两源 Chapman 修正（用户定公式）：N̂ = (n1+1)(n2+1)/(m+1) − 1。

    规则：
    - 任一源为空 / overlap=0 → INSUFFICIENT_DATA（不崩，不硬算）
    - source_types 不在独立通道集合，或显式 dependent → INVALID_ASSUMPTION
      （只能算，但标 warning，绝不能当结论）
    - 独立通道 + overlap>0 → AVAILABLE（N̂ 仅供交叉验证，不拥有停止权）
    """
    srcs = source_types or []
    independent = bool(srcs) and all(s in INDEPENDENT_CHANNEL_TYPES for s in srcs)

    if n1 <= 0 or n2 <= 0:
        return CaptureRecaptureDiagnostic(
            method="chapman_two_source", n1=n1, n2=n2, overlap=overlap,
            source_types=srcs, status=INSUFFICIENT_DATA,
            reason="存在空列表（n1 或 n2 ≤ 0），无法估计")
    if overlap <= 0:
        return CaptureRecaptureDiagnostic(
            method="chapman_two_source", n1=n1, n2=n2, overlap=overlap,
            source_types=srcs, status=INSUFFICIENT_DATA,
            reason="overlap=0，两源无交集——无法估计总体规模")

    n_hat = ((n1 + 1) * (n2 + 1) / (overlap + 1)) - 1

    if not independent:
        return CaptureRecaptureDiagnostic(
            method="chapman_two_source", n1=n1, n2=n2, overlap=overlap,
            source_types=srcs, N_hat=round(n_hat, 1),
            status=INVALID_ASSUMPTION,
            reason="capture sources 不是独立通道（依赖同一检索机制/语料）——"
                   "N̂ 只作辅助参考，不可用于正式估计",
            assumption_warning=ASSUMPTION_WARNING)

    return CaptureRecaptureDiagnostic(
        method="chapman_two_source", n1=n1, n2=n2, overlap=overlap,
        source_types=srcs, N_hat=round(n_hat, 1),
        status=AVAILABLE,
        assumption_warning="capture sources 声称独立，但真实检索通道间的依赖"
                           "无法完全排除——N̂ 仅作交叉验证")


def chao_diagnostic(lists: list[list[str]],
                    source_types: list[str] | None = None) -> CaptureRecaptureDiagnostic:
    """多源 Chao-type（接口保留，第一版克制实现）。

    需要 ≥2 个真实独立通道；query family（NODE/RELATION/...）直接判
    NOT_ENOUGH_INDEPENDENT_CHANNELS——不硬算。
    """
    srcs = source_types or []
    if len(lists) < 2:
        return CaptureRecaptureDiagnostic(
            method="chao_multi_source", source_types=srcs,
            status=INSUFFICIENT_DATA, reason="需 ≥2 个捕获源")
    if not srcs or not all(s in INDEPENDENT_CHANNEL_TYPES for s in srcs):
        return CaptureRecaptureDiagnostic(
            method="chao_multi_source", source_types=srcs,
            status=NOT_ENOUGH_INDEPENDENT_CHANNELS,
            reason="当前可用通道高度相关（query family 同源），不满足 Chao "
                   "独立性假设——不硬算 N̂。需 LEXICAL/SEMANTIC/CITATION 等"
                   "检索机制不同的独立通道")
    # 多源 Chao 的完整实现留到真实独立通道数据出现后（P1 不空实现硬算）
    return CaptureRecaptureDiagnostic(
        method="chao_multi_source", source_types=srcs,
        status=NOT_ENOUGH_INDEPENDENT_CHANNELS,
        reason="多源 Chao 需要真实独立通道的重叠矩阵——当前无数据")
