# Query Trajectory Schema v2（S7 Query Planner / RL 训练数据规格）

- 日期：2026-09-07
- Master：本文件 | 状态：规格冻结（v1 数据已冻结，本 schema 定义 S7 起新 trajectory 的落盘契约）
- 前置：s6_trajectory.json（v1，17 actions）已冻结——v2 是其超集 + 扩展，不修改 v1 历史产物

## 1. 为什么从 v1 升 v2

v1（freeze_s6_trajectory.py 产物）已含：query provenance + raw features（hits/unique/new/identity_unknown/depth_saturated）+ 三态标签（R/U/I）+ novelty（结果层 pairwise overlap）。

S7 Query Planner 的训练/评估需要额外字段：
- **relation_type**：LLM 提议的是哪种概念关系（RL state 的关键离散特征）
- **term_pair**：relation 的两端（term_A, term_B），供状态泛化
- **generation_source**：query 是谁造的（manual/llm_proposal/planner_selected/…）——审计"提升来自哪层"
- **failure_reason**：query 失败模式的结构化标注（S6 已证 C/D 死因不同，须分类）
- **reward_components**：v1 reward 公式草案的逐项实数值（供定参后回算）

## 2. v2 全字段（每 action 一条记录）

```jsonc
{
  // ── 身份与来源（state 用）──
  "action_id": "S7-A-01",                 // 或 S6 旧 id
  "generation_source": "S6_MANUAL_BRIDGE | S7_LLM_PROPOSAL | S7_PLANNER_SELECTED",
  "round": "S7_v1",
  "status": "FROZEN_DEVELOPMENT_PILOT | PROPOSED | PILOTED | SELECTED | REJECTED",

  // ── relation（S6 v1 无此层——核心新增）──
  "relation": {
    "term_A": "curing kinetics",           // VERIFIED 原词
    "term_B": "void formation",
    "relation_type": "process_to_observable",   // 见 §3 枚举
    "domain": "general",
    "plausibility": "LLM: 论文可能这样表达？Y/N/UNKNOWN",  // S6 C 类教训：防"概念真≠表达真"
    "proposal_rationale": "LLM 一句话依据"
  },

  // ── query（deterministic 组装结果）──
  "query_string": "TITLE-ABS-KEY(...)",
  "contains_shrinkage_anchor": false,
  "terms_used": ["curing kinetics", "void formation"],
  "term_source": "S6_VERIFIED_EXACT",     // 词源纪律追溯
  "context_source": "S5_FROZEN_CONTEXT",

  // ── raw features（v1 同名）──
  "features": {
    "scopus_total_hits": 7,
    "unique_returned": 7,
    "usable_returned": 7,
    "new_vs_S5": 7,
    "identity_unknown": 0,
    "depth_saturated": false
  },

  // ── labels（v1 同名；R1/R2 口径下 R+U 有意义）──
  "labels": {"RELEVANT": 0, "UNCERTAIN": 0, "IRRELEVANT": 7, "R_plus_U": 0},

  // ── novelty / redundancy（v1 同名：结果层 pairwise Jaccard）──
  "novelty": {"max_pairwise_overlap": 0.1, "mean_pairwise_overlap": 0.01},

  // ── failure_reason（结构化标注，§4 枚举；v1 无）──
  "failure_reason": "RELATION_EXPRESSION_ABSENT",   // 可为 null（query 有效）

  // ── reward components（v1 公式草案的实数值；参数未固化）──
  "reward_components": {
    "R": 0, "U": 0,                          // → 2R + U 的 2 与 1 是 v1 ranking 权重
    "redundancy": 0.01,                      // = mean_pairwise_overlap（代理）
    "cost_log_hits": 2.08                    // = log(hits+1)
    // ranking_v1 = 2*R + U − 0.5*redundancy − 0.01*cost_log_hits（参数在 v1 文档声明，此处存组件）
  },

  // ── 新论文集合（v1 有）──
  "new_keys": ["2-s2.0-..."]
}
```

## 3. relation_type 优先级（Tier 冻结 2026-09-07 用户终裁；勿发明新类型）

| Tier | relation_type | 实证/理由 | 生成策略 |
|---|---|---|---|
| **T1** | `process_to_observable` | S6 A 类最强（43R/220new，唯一验证有效） | **默认生成，主力**（curing/photopolymerization/polymerization → void/gap/crack/deflection/deformation…） |
| **T1** | `lexical_to_observable` | S6 B（5R/12new，⊂A） | **默认生成**——非开新社区，补精确入口（polymerization shrinkage → cusp deflection/marginal leakage） |
| **T2** | `mechanism_to_observable` | S6 C_free 失败（0R/8new） | **受控**：LLM 必须给 scientific rationale（论文为何同写二者）。允许 stress relaxation→shrinkage stress reduction；禁止 gel point→fabrication error |
| **T2** | `observable_transfer`（跨域） | S6 D drift（1R/215new） | **受控**：须第三约束 = **process/material vocabulary（非 domain 标签）**——论文作者不用 domain 分类（SLA 可能写 vat photopolymerization/additive manufacturing/resin printing）。坏：fabrication error+3D printing；好：photopolymerization+fabrication error+stereolithography |
| **T3** | `mechanism_to_mechanism` | S6 C_anchor 0hit（概念真≠表达真） | **默认关闭，不禁止主动探索**。开启条件：LLM 给 rationale AND ≥1 term 有 evidence support AND 历史无同类失败。例：addition-fragmentation chain transfer+stress relaxation 在某体系可能即核心机制 |

**generation budget（初始 exploration，非硬比例）**：70% T1 / 20% T2 / 10% T3。

LLM 输出每个 candidate 必须带 `expected_failure_mode`（§4 枚举）——把失败假设前置，pilot 直接验证。

## 4. failure_reason 枚举（自动标注规则）

| failure_reason | 判定规则 | S6 案例 |
|---|---|---|
| `ZERO_HIT` | total_hits=0 | C-14 |
| `NO_NEW` | new_vs_S5=0（hits>0） | C-12/C-13、B-10 |
| `RELATION_EXPRESSION_ABSENT` | new>0 且 R=0 且 U=0（词真关系假） | C-11/C-15 |
| `DOMAIN_DRIFT` | D/transfer 类且 R/(new) 极低 + 命中多为异质域 | D-16 |
| `LOW_YIELD` | R+U/new < 阈值（S7 定，如 5%） | D-17 |
| `REDUNDANT` | mean_pairwise_overlap 高（>0.3，与强 query 重复） | 待 S7 |
| `null` | 有效 query（保留） | A-01/02/03 |

## 5. v1 → v2 回填映射（S6 17 条全部可无损升 v2）

- `generation_source` = 常量 `S6_MANUAL_BRIDGE`
- `relation.term_A/term_B` = 从 `terms_used` 按 strategy 拆分（process 锚词 → term_A，observable → term_B；C 类 mechanism→term_A）
- `relation_type` = strategy 字段映射（`process_to_observable` 等；bridge 组装时的 spec strategy 已含）
- `failure_reason` = 按 §4 自动规则标注
- `reward_components` = 从 labels/novelty/features 派生
- `plausibility` = S6 人工组装即"已认为可能"，标 `Y (human)`；C/D 失败案例事后标 `UNKNOWN (empirically absent)`

## 6. 落盘与纪律

- S7 起新 trajectory 一律写 v2；S6 17 条回填成 v2 作为 RL 冷启动种子（动作：`upgrade_trajectory_v1_to_v2.py`，一次性工具）
- reward 参数**仍不固化**（v1 ranking `2R+U−0.5·Redundancy−0.01·log(hits+1)` 是 S7 v1 的 **selection ranking function**，非学习目标；1000+ trajectory 后另行定参做 bandit/offline RL）
- failure_reason 是 RL 的 terminal-state 信号，须与 labels 一起在 pilot 时写入
