"""Phase 3 P2 — Audit 报告（presentation only，用户定 2026-08-27）。

硬边界：
- report.py **只负责"已有数据 → 报告"**，不负责建 universe/抽样/加载 label/算 bound
  （那些在 audit.py orchestration）。
- **diagnostic 对象物理上不传入 stopping function**——本文件不 import
  recall_bound，不计算任何统计量；STATISTICAL_STOP 是 audit.py 算好传入的
  `audit.statistical_stop`，report 只负责展示。
- A 区 Diagnostic Evidence 任何指标都不能改变 B 区 STOP——DIAGNOSTIC_WARNINGS
  只提示，不改 audit.statistical_stop（数学层与工程风险层不混）。
- 报告措辞固定写 "Within the predefined literature universe..."（外部有效性
  caveat：Auditor 只能证明 predefined universe 内的 recall，永远发现不了
  根本没进入 UniverseSnapshot 的论文）。
"""


def _fmt_pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _pct_recall(d: dict) -> str:
    return f"{d.get('found', 0)} / {d.get('total', 0)} = {_fmt_pct(d.get('recall', 0))}"


def _fmt_round_gain(g: dict) -> str:
    r = g.get("round_id", "?")
    new_u = g.get("new_unique_papers", 0)
    rel = g.get("new_relevant_papers")
    direct = g.get("new_direct_evidence_papers")
    s = f"Round {r:<6} +{new_u} unique"
    if rel is not None:
        s += f" / +{rel} relevant"
    if direct is not None:
        s += f" / +{direct} direct"
    rel_gain = g.get("relative_gain")
    if rel_gain is not None:
        s += f"   (rel_gain={rel_gain:.4f})"
    return s


def build_report(audit, diagnostics: dict | None = None) -> str:
    """两区报告（audit：AuditRecord，含 audit.statistical_stop；diagnostics：纯展示）。

    diagnostics = {
        "goldset": {...}, "saturation": {...}, "capture": {...}
    }——只进 A 区，不参与任何停止判定。
    """
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append("Phase 3 Completeness Audit")
    lines.append(f"audit_id: {audit.audit_id}   topic: {audit.topic_id}")
    lines.append("=" * 60)

    diag = diagnostics or {}

    # ── A. Diagnostic Evidence（辅助解释，无停止权）──
    lines.append("")
    lines.append("=" * 60)
    lines.append("A. Diagnostic Evidence")
    lines.append("=" * 60)

    # Gold Set
    gs = diag.get("goldset") or {}
    lines.append("")
    lines.append("Gold Set")
    lines.append("-" * 40)
    if gs.get("status") == "INSUFFICIENT_DATA":
        lines.append("  status: INSUFFICIENT_DATA")
    else:
        layers = gs.get("layers", {})
        for L in ("foundational", "representative", "frontier"):
            if L in layers:
                lines.append(f"  {L.capitalize():<15} {_pct_recall(layers[L])}")
        mh = gs.get("must_hit", {})
        lines.append(f"  {'Must-hit':<15} {_pct_recall(mh)}")
        missing = gs.get("missing_papers", [])
        if missing:
            lines.append("  Missing:")
            for m in missing[:12]:
                lines.append(f"    - {m.get('doi', '')}  {m.get('title', '')[:48]}")
            if len(missing) > 12:
                lines.append(f"    ... (+{len(missing) - 12} more)")

    # Saturation
    sat = diag.get("saturation") or {}
    lines.append("")
    lines.append("Citation / Retrieval Saturation")
    lines.append("-" * 40)
    if sat.get("status") == "INSUFFICIENT_DATA":
        lines.append("  status: INSUFFICIENT_DATA")
    else:
        for g in sat.get("rounds", []):
            lines.append(f"  {_fmt_round_gain(g)}")
        lines.append(f"  zero_gain_streak:          {sat.get('zero_gain_streak', 0)}")
        if sat.get("relevant_zero_gain_streak") is not None:
            lines.append(f"  relevant_zero_gain_streak: {sat['relevant_zero_gain_streak']}")

    # Capture-recapture
    cap = diag.get("capture") or {}
    lines.append("")
    lines.append("Capture-recapture")
    lines.append("-" * 40)
    lines.append(f"  status: {cap.get('status', 'N/A')}")
    if cap.get("N_hat") is not None:
        lines.append(f"  N_hat: {cap['N_hat']}  (diagnostic only)")
    if cap.get("assumption_warning"):
        lines.append(f"  warning: {cap['assumption_warning']}")
    if cap.get("reason"):
        lines.append(f"  reason: {cap['reason']}")
    lines.append("  NOTE: capture-recapture 是辅助诊断，不参与停止判定")

    # ── B. Independent Statistical Audit（唯一裁判）──
    lines.append("")
    lines.append("=" * 60)
    lines.append("B. Independent Statistical Audit")
    lines.append("=" * 60)

    lines.append("")
    lines.append("Universe")
    lines.append("-" * 40)
    lines.append(f"  universe_id:  {audit.universe_id}")
    lines.append(f"  universe_hash: {audit.universe_hash}")
    lines.append(f"  Found relevant F:            {audit.F}")
    lines.append(f"  Remaining pool N_remaining:  {audit.N_remaining}")
    lines.append("  (Within the predefined literature universe — Auditor 只能")
    lines.append("  证明 universe 内的 recall，无法发现未进入 UniverseSnapshot 的论文)")

    lines.append("")
    lines.append("Sampling")
    lines.append("-" * 40)
    lines.append("  method:    Simple Random Sampling Without Replacement")
    lines.append("  seed:      " + str(audit.seed))
    lines.append(f"  sample n:  {audit.sample_size}")
    lines.append(f"  status:    {audit.status}")

    if audit.status == COMPLETED if False else audit.status in (
            "COMPLETED", "INCOMPLETE_LABELS", "AWAITING_LABELS"):
        lines.append("")
        lines.append("Statistical bound")
        lines.append("-" * 40)
        if audit.status != "COMPLETED":
            lines.append(f"  (status={audit.status}——label 未完整，不输出正式 Recall_LCB)")
            lines.append("  宁可不出数，也不要默认当 irrelevant")
        else:
            lines.append(f"  missed relevant m:  {audit.m}")
            lines.append(f"  M_upper:            {audit.M_upper}")
            lines.append(f"  p_upper:            {_fmt_pct(audit.p_upper)}")
            lines.append(f"  confidence:         {_fmt_pct(audit.confidence_level)}")
            lines.append("")
            lines.append(f"  Recall_LCB:   {_fmt_pct(audit.recall_lcb)}")
            lines.append(f"  Target:       {_fmt_pct(audit.target_recall)}")
            lines.append("")
            lines.append(f"  STATISTICAL_STOP:  "
                         f"{'YES' if audit.statistical_stop else 'NO'}")

    # DIAGNOSTIC_WARNINGS：只提示，不改 STOP（数学层与工程风险层不混）
    warnings = audit.diagnostic_warnings
    if warnings:
        lines.append("")
        lines.append("DIAGNOSTIC_WARNINGS:")
        for w in warnings:
            lines.append(f"  - {w}")
    if audit.status == "COMPLETED" and audit.statistical_stop:
        lines.append("")
        lines.append("WARNING: Diagnostic evidence may reveal known gaps despite "
                     "statistical threshold.")

    return "\n".join(lines)
