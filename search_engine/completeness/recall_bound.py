"""Phase 3 Statistical Completeness Audit — Recall 下置信界（数学核心，用户定 2026-08-27）。

核心公式（用户定案）：
```
F          —— 搜索系统找到的 relevant papers
U (|U|=N)  —— 冻结 universe 的剩余池（搜索结束后未被判 relevant 的文献）
n          —— Auditor 从 U 不放回随机抽样数
m          —— 样本中被判定 relevant 的篇数（Agent 漏检）
p_upper    —— 剩余池 relevant 比例的单侧 95% 上置信界
M_upper    —— N × p_upper（剩余池中漏检 relevant 的上界）
Recall_lower = F / (F + M_upper)
```

**方法：有限总体 hypergeometric inversion**（不是正态近似，也不是放回二项——
U 有限、抽样不放回，X ~ Hypergeometric(N, M, n)，M = 剩余池真实 relevant 总数）。

M_upper = 最大的 M 使得 P(X ≤ m | N, M, n) ≥ α   （α = 1 − confidence_level）
- X 是"样本中抽到 relevant 的篇数"，观测到 m
- 若真实 M 更大，观测到 ≤m 的概率 < α → 该 M 被拒绝（95% 置信下不可能）
- m=0 也不能说 100%：P(X≤0) ≥ α 允许的 M 仍 > 0 → p_upper > 0（用户 invariant）

纯 Python 实现（无 scipy）：超几何 PMF 用**浮点递推**（连乘比例），O(n+m) 每
次求值 + 二分 O(log N) 次——N~10^5 内毫秒级。
"""

import math


def hypergeom_cdf_le(m: int, N: int, M: int, n: int) -> float:
    """P(X ≤ m)，X ~ Hypergeometric(N, M, n)（不放回抽 n 个，抽到 relevant ≤ m）。

    浮点递推（无 scipy）：
      P(X=0)   = ∏_{i=0}^{n-1} (N−M−i)/(N−i)
      P(X=k+1) = P(X=k) · (M−k)/(k+1) · (n−k)/(N−M−n+k+1)
    """
    if M <= 0:
        return 1.0
    if m >= min(M, n):
        return 1.0
    if n <= 0:
        return 1.0 if m >= 0 else 0.0
    # P(X=0)：连乘（全部项 < 1，浮点稳定）
    p0 = 1.0
    for i in range(n):
        p0 *= (N - M - i) / (N - i)
    if p0 <= 0:
        return 0.0
    cdf = p0
    pk = p0
    # 递推 P(X=k+1)/P(X=k)
    for k in range(min(m, M)):
        if pk <= 0 or N - M - n + k + 1 <= 0:
            break
        pk *= (M - k) / (k + 1) * (n - k) / (N - M - n + k + 1)
        if pk <= 0:
            break
        cdf += pk
        if cdf > 1.0:
            cdf = 1.0
            break
    return min(cdf, 1.0)


def missed_relevant_upper_bound(N: int, n: int, m: int,
                                alpha: float = 0.05) -> int:
    """M_upper：剩余池漏检 relevant 总数的单侧上置信界（超几何反演）。

    最大的 M 使 P(X ≤ m | N, M, n) ≥ α——超过它的 M 在 (1−α) 置信下被拒绝。
    二分 O(log N) 次 hypergeom 求值。M 下界 = m（观测到 m 篇，真实至少 m）。
    """
    if n <= 0 or N <= 0:
        return 0
    if n >= N:               # 全池被检查 → m 即全部漏检
        return m
    lo, hi = m, N
    best = m
    while lo <= hi:
        mid = (lo + hi) // 2
        if hypergeom_cdf_le(m, N, mid, n) >= alpha:
            best = mid       # 这个 M 仍可接受（没有证据拒绝）→ 上探
            lo = mid + 1
        else:
            hi = mid - 1     # M 太大 → 拒绝 → 下探
    return best


def recall_lower_bound(F: int, N: int, n: int, m: int,
                       confidence_level: float = 0.95) -> dict:
    """Phase 3 正式统计量（用户定案）：Recall 下置信界。

    参数：
      F —— found relevant（搜索系统已找到）
      N —— remaining pool size（|U|）
      n —— 抽样数
      m —— 样本中漏检 relevant 数
      confidence_level —— 0.95 / 0.99（99% 更保守 → M_upper 更大 → LCB 更低）

    返回：{M_upper, p_upper, recall_lower, confidence_level}
    """
    alpha = 1.0 - confidence_level
    M_upper = missed_relevant_upper_bound(N, n, m, alpha=alpha)
    p_upper = M_upper / N if N else 0.0
    recall_lower = F / (F + M_upper) if (F + M_upper) else 0.0
    return {
        "M_upper": M_upper,
        "p_upper": p_upper,
        "recall_lower": recall_lower,
        "confidence_level": confidence_level,
    }


# ── 数值 sanity（数学 invariant，测试也覆盖）──

def _sanity() -> None:
    """用户验收数字示例（N=5000, n=300, m=1 → p_upper≈0.018, recall≈90.9%）。"""
    r = recall_lower_bound(F=900, N=5000, n=300, m=1, confidence_level=0.95)
    print(f"N=5000 n=300 m=1: M_upper={r['M_upper']} p_upper={r['p_upper']:.4f} "
          f"recall_lower={r['recall_lower']:.4f}")
    # m=0 也不能 100%
    r0 = recall_lower_bound(F=900, N=9100, n=500, m=0, confidence_level=0.95)
    print(f"N=9100 n=500 m=0: M_upper={r0['M_upper']} p_upper={r0['p_upper']:.5f} "
          f"recall_lower={r0['recall_lower']:.4f}  (p_upper>0 即使 m=0)")


if __name__ == "__main__":
    _sanity()
