# v2 双模式运行原则（Standard Topic Run vs Audit/Benchmark Run）

- 日期: 2026-09-08（用户终裁：方向纠正）
- 状态: 生效（v2 主题流程总纲）

## 问题：上一轮把"普通 Agent 搜论文"过成了"benchmark 人工调参"

P1-B1 曾在 query 条数（34 vs 36）、簇归并、gate 通道上做多轮人工终裁。对 **pc001（光固化，核心研究主题）** 这种精细是合理的——它的 recall 数字要可发表、可审计。但 **thermochromic（第二个主题）** 只是"再跑一遍拿论文表"，不该享受同等人工密度。把两件事混成一个流程，导致：
- 每次跑新主题都要求用户在 rubric/seeds/termbank/query 层做人工终裁；
- API 预算被用于"调 query 结构"而非产出；
- 主题质量差异与 recall 测评（audit）耦合，无法独立演进。

## 双模式定义

### Standard Topic Run（普通模式，默认）
用户给定主题描述 → Agent **自主完成**：

```
topic draft（研究问题一句，人工仅确认主题定义）
→ seeds / termbank / query 生成（Agent 判定，资产落盘可追溯）
→ 检索（Scopus/OpenAlex）
→ R/U/I QA（rubric 注入）
→ promote 论文表（relevant/uncertain CSV）+ 写入全局 KB（papers/topic_papers）
```

要点：
- **人工冻结点只有一个**：主题定义（research question）。rubric/seeds/termbank/query 决策由 Agent 以"决策资产"形式落盘（可审可回滚），但**不逐项等用户终裁**；
- query 质量由 **safety gate 等通用机制**保证（specific/generic 分通道），不是人工逐条审；
- 产出 = 论文表 + 入库，recall 数字不承诺（普通模式不做独立 audit）。

### Audit/Benchmark Run（审计模式，按需）
只有需要**可发表的 recall/精度声明**时才启用：

```
冻结 rubric（版本化，禁改写）
→ 独立 audit frame（主题专属，禁复用他主题）
→ 外部盲评（Search Agent 不自证）
→ 三层 recall / R1-R2 双口径统计（R06 合同同款）
→ 冻结裁决（不可被重算覆盖）
```

R06 已封口径继续生效（external-frame 声明、禁横比、frame 与探索策略解耦归因）。审计模式每次都有独立预算与停止条件，不进普通模式的自动链。

## 判定规则（防再混）

| 触发 | 模式 |
|---|---|
| "跑一个新主题/二轮主题/补充检索，最后要论文表" | Standard |
| "这个主题的 recall 到底多少 / 要发数 / 要审计" | Audit |
| 用户在主题 run 中途开始关心 recall 数字 | 停下来问模式，不混跑 |
| 决策密度：一个主题已人工终裁 rubric/seeds/termbank | 之后默认 Standard（除非用户显式开 Audit） |

## 对 thermochromic 的落地（当前）

用户明确：thermochromic 走 **Standard**，产物 = 热致变色论文表 + 全局 KB。因此：
- P1-B1 的 36 条 gated query（cut V2 + query_gate V1，均已 FROZEN）作为**普通模式第一轮检索输入**，不再人工审；
- 本轮 pilot live 产出 → QA → promote → CSV = Standard 链首跑；
- 若后续要 thermochromic 的 recall 声明 → 单独 Audit run（frame 重建，禁横比），本轮数据作候选池。
