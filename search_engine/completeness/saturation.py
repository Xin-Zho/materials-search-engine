"""Phase 3 P1 — Citation/search 边际饱和 diagnostic（用户定 2026-08-27）。

只描述**边际新增趋势**，不判断"已经搜全"：
- marginal_new_papers / cumulative_unique_papers / zero_gain_streak / relative_gain
- **区分 retrieval saturation 与 relevant saturation**：
    Round5: new papers=50, new relevant=0  ≠  Round5: new papers=0
  分别记录 new_unique_papers / new_relevant_papers / new_direct_evidence_papers
  （P1 第一版有哪个记录哪个，不为此改搜索架构）
- 本模块**无任何停止判定**——源码测试锁死：不得出现停止条件词；停止权唯一属于
  recall_bound（正式统计审计的 Recall LCB）。"""

from dataclasses import dataclass, field, asdict

AVAILABLE = "AVAILABLE"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


@dataclass
class RoundGain:
    round_id: int | str
    new_unique_papers: int = 0
    new_relevant_papers: int | None = None    # 有就记，没有 None
    new_direct_evidence_papers: int | None = None
    cumulative_unique_papers: int = 0
    relative_gain: float | None = None        # NewPapers_r / CumulativePapers_{r-1}


@dataclass
class SaturationReport:
    rounds: list[RoundGain] = field(default_factory=list)

    # retrieval saturation（按 new_unique_papers 算）
    zero_gain_streak: int = 0
    # relevant saturation（按 new_relevant_papers 算；无此数据则 None）
    relevant_zero_gain_streak: int | None = None

    status: str = AVAILABLE
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _streak(values: list[int | None]) -> int | None:
    """连续 0 的轮数（从最近一轮往回数）；None 数据 → None。"""
    if not values:
        return None
    if any(v is None for v in values):
        return None
    streak = 0
    for v in reversed(values):
        if v == 0:
            streak += 1
        else:
            break
    return streak


def saturation_report(rounds: list[dict]) -> SaturationReport:
    """输入 rounds: [{round_id, new_unique_papers, new_relevant_papers?, ...}]。

    输出：每轮 marginal/cumulative/relative_gain + 两类 zero_gain_streak。
    relative_gain_r = NewPapers_r / CumulativePapers_{r-1}（首轮 None）。
    """
    if len(rounds) < 2:
        return SaturationReport(rounds=[], status=INSUFFICIENT_DATA,
                                reason="需 ≥2 轮才有边际趋势")

    cum = 0
    gains: list[RoundGain] = []
    for i, r in enumerate(rounds):
        new_u = int(r.get("new_unique_papers", 0) or 0)
        cum += new_u
        prev_cum = cum - new_u
        rel_gain = round(new_u / prev_cum, 4) if (prev_cum > 0 and i > 0) else None
        gains.append(RoundGain(
            round_id=r.get("round_id", i + 1),
            new_unique_papers=new_u,
            new_relevant_papers=r.get("new_relevant_papers"),
            new_direct_evidence_papers=r.get("new_direct_evidence_papers"),
            cumulative_unique_papers=cum,
            relative_gain=rel_gain,
        ))

    streak = _streak([g.new_unique_papers for g in gains]) or 0
    rel_streak = _streak([g.new_relevant_papers for g in gains])

    return SaturationReport(rounds=gains,
                            zero_gain_streak=streak,
                            relevant_zero_gain_streak=rel_streak,
                            status=AVAILABLE)
