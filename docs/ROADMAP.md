# Roadmap — 材料学科知识库 / Literature Discovery Agent

- 更新：2026-09-08 ｜ 当前：**v1.0 已发布**（`release/v1.0`，见 `docs/2026-09-08-v1.0-release.md`）

> **命名消歧**：本文件的 v1.0/v2.0 是**交付版本**轴（用户 2026-09-08 定）；与内部管线阶段的旧命名（Phase 2.0/2.1 "Query-Family Diversification"、`data/search_agent_v2_roadmap.json` 冻结，已完成且属 v1.0 内容）不是同一轴，勿混用。

---

## ✅ v1.0 — Literature Discovery Agent（2026-09-08 交付）

可完整运行的文献发现 Agent，全链路真实跑通：

用户研究问题 → TermBank → Query/Relation Planner → 多后端检索 → Candidate DB → Blind relevance QA → Community discovery → Relation/Failure memory → Reward feedback → 下一轮主动搜索 → 论文输出 → 独立 Audit

实证：S6（R63/U57）、S7（R106/U22，富集 2.31×）、community verdict（6 KEEP 簇/750 篇）、KB 64→216 records、R06 独立审计（external-frame 三层 + R1/R2，恒等式自洽）。

**R06 冻结口径**（禁改写）：external-frame Retrieval Recall = 13.9%（resolved strict）、E2E = 2.4%；针对冻结 OpenAlex FRAME_V2，**不代表材料领域绝对召回率**，不得单拿 2.4% 当系统召回率。

---

## 🚧 v2.0 — Knowledge Completeness Agent（路线图）

v1.0 之后的两条主线（R06 拆出的两个问题 + 未完成的下游环节）：

### 1. Promotion / Knowledge Extractor 修复（工程项，确定性收益）
**问题**：R06 Sens 漏斗——23 篇已触达 relevant 仅 4 篇入 KB。这是与搜索质量无关的明确工程缺陷。
- QA 判 R 但无 abstract 的收录缺口（R+abstract 覆盖仅 152/249）
- UNCERTAIN（staging 未排除）缓收规则落地
- 19 篇 seen-未收录 relevant 逐篇复核（`data/exports/terminology/r06_retrieved_relevant_detail.json`），区分 screening 误判 vs 机制缺口
- 结构化抽取升级：2.0-edges 全量覆盖 → 知识 coverage 常态化 → 统计停止条件

### 2. Audit frame v2 重建 + completeness proof（研究项，回答"找全了多少"）
**问题**：R06 frame 与 Search vN 搜索宇宙严重错位（universe∩seen 7.0%、KB∩frame 38.4%）——旧 frame 审计不到 KB 大量真实内容。
- frame 重建对齐：以 KB∩frame 83 篇 + seen 已覆盖社区 + s8 知识薄区（dental post-op / packaging / coatings-warpage）为种子扩 frame
- seen 身份三通道全保留（doi / Scopus EID / OpenAlex W id），`r06_candidate_db_dois.json` 重建为多通道 id 集
- fresh audit（新 frame，与 R06 禁横比）→ completeness proof 统计框架

### 顺序建议
先做 #1（低代价、高确定性、修复"搜到却没入库"的真实漏损），再启动 #2（研究型、决定以后能否说"这个方向大约找全了多少"）。round5 是否启动建议结合 #1 完成后的 KB 状态 + #2 的新 frame 决定，而非在旧 frame 内继续加 query。

---

## 附：长期目标（v2.0 完成后）

```text
论文 → 结构化知识抽取 → Knowledge Base → 知识 coverage → 统计停止条件
   +  Audit frame construction（本轮暴露的关键缺失环节）
```

- 冻结契约：R1/R2 双报、resolved/ultra 双报、恒等式 E2E=Retrieval×Sens、frame 禁横比（North Star §4.1/§4.2、§6）
- 已知薄区（s8_knowledge_coverage）：dental post-op pain、packaging crack、coatings warpage 检索面大但知识覆盖低
- 留观簇：C-014（109 篇 UNCERTAIN）在 packaging 专项后复核
