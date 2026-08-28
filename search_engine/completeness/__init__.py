"""Phase 3 Statistical Completeness Audit（search_engine/completeness）。

模块分层（用户定 2026-08-27）：
- P0 数学核心：universe.py（冻结快照）/ sampler.py（独立抽样）/ recall_bound.py（Recall LCB）
- P1 diagnostics（无停止权）：goldset.py / saturation.py / capture.py
- P1.5 审计总体构造：universe_builder.py（AuditUniverseDefinition + build_audit_universe；
  build_agent_seen_pool 仅 debug 用，禁止进正式审计）
- P2 编排与呈现：audit.py（生命周期：create→labels→complete/replay）/ report.py（两区报告）
"""
