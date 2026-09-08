"""search_engine/audit — v3.0 Completeness Auditor（Phase B：Miss Analyzer /
Failure Classifier / Repair Planner）。

原则（用户 2026-08-29 定稿）：
- Analyzer 收集证据，Classifier 做判断——不揉成一个 LLM prompt
- 第一版 deterministic（规则化），不加入 LLM 自由推理
- Repair Planner 只 dry-run，绝不自动执行
- Audit Round 是一级概念（search_snapshot_id / audit_frame_id / sampling_seed /
  sample_size / miss_count / estimated_miss_rate / ci_upper_95 / repair_version）
"""
