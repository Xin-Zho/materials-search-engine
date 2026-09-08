# R06 三层 Recall Audit 报告（Search vN = S6+S7 freeze 后首审）

- **日期**：2026-09-08
- **audit_id**：`pc_001::20260908012830` ｜ universe：`pc_001-2026-08-31T162811`（OpenAlex FRAME_V2 wide, n=5890）
- **抽样**：pool = 5890 − R05 sample 500 → 5390；n=500，seed=13（独立于 R05 的 41）
- **判定**：外部 blind（500/500 全判）→ **R 194 / U 123 / I 183**；UNCERTAIN rate 24.6%（diagnostic）
- **契约**：North Star §4.1/§4.2（R1+R2 双报冻结；恒等式 E2E=Retrieval×Sens）
- 入口：`tools/compute_r06_recall.py --labels data/exports/completeness_labels/pc_001__20260908012830_filled.json`

## 1. 三层 recall（正式数字）

### R2 strict（A = RELEVANT）
| 层 | resolved 主口径 | ultra（Identity Unknown 全 miss） |
|---|---|---|
| Retrieval Recall | **23/166 = 13.9%** | 23/194 = 11.9% |
| Screening Sensitivity | **4/23 = 17.4%** | 同左 |
| End-to-End Recall | **4/166 = 2.4%** | 2.1% |

### R1 operational（A = RELEVANT ∪ UNCERTAIN，U ∈ FinalKB 对象）
| 层 | resolved | ultra |
|---|---|---|
| Retrieval Recall | **34/283 = 12.0%** | 34/317 = 10.7% |
| Screening Sensitivity | **4/34 = 11.8%** | 同左 |
| End-to-End Recall | **4/283 = 1.4%** | 1.3% |

恒等式自洽：R2 ✓（2.4% = 13.9%×17.4%）｜ R1 ✓（1.4% = 12.0%×11.8%）
FinalKB ⊆ CandidateDB：✓（doi 与 W id 双通道均满足）

### Diagnostics（不混入三层）
- Identity Unknown Rate：RELEVANT 内 28/194 = **14.4%**；A^R1 内 34/317 = 10.7%
- UNCERTAIN Rate：123/500 = 24.6%
- Backend Reachability：样本 500 全量可经 openalex_cache 解析 title/abstract/doi（100% 命中）；48 篇 OpenAlex 记录无 DOI（MAG-only 老文），cache 顶层与 locations 均无 doi、无 scopus id 可桥接 → 该 48 篇对 doi/EID 主键体系 identity 不可 reconcile

## 2. 样本判定分布

| label | n | 占比 | no-doi |
|---|---|---|---|
| RELEVANT | 194 | 38.8% | 30 |
| UNCERTAIN | 123 | 24.6% | 7 |
| IRRELEVANT | 183 | 36.6% | 11 |
| 合计 | 500 | 100% | 48 |

## 3. 结构性归因（为什么低——三处系统性错位）

### 3.1 Search vN 覆盖域 vs audit frame：重叠 7.0%
- universe 5890 中仅 **410 篇（7.0%）** 在 CandidateDB（多通道：doi 390 + W id 通道增量 20）
- R06-500 全体命中 42/500 = 8.4%，R05-500 全体命中 30/500 = 6.0% —— **两个独立样本与 universe 全体自洽**（抽样无偏）
- audit-relevant 子集命中 13.9% > 全体 8.4%：相关富集存在但弱
- 解读：Search vN（S5–S7，Scopus 为主 + S6 部分 OpenAlex semantic bridge）与 8/31 宽 frame（OpenAlex relevance 检索）的检索策略近乎正交。Search vN 的 seen 总量 24593 远大于 universe，但两者交集极小 → 各自检索到不同论文群。

### 3.2 KB 收录域 vs audit frame：仅 38.4% 重叠
- KB 216 篇中仅 **83 篇（38.4%）** 在 universe 内（W 通道 36 + doi-only 47）
- KB 61.6% 的论文在 frame 外 → 本 frame 的 audit 永远抽不到它们，对 R06 无贡献
- 期望值校验：universe 内 KB 83 篇 → 500 样本期望抽中 ≈ 83/5890×500 ≈ 7 篇；实际 RELEVANT 中在 KB 4 篇（另见 3.4 收录漏斗）

### 3.3 Identity 层盲区（EID 通道不可逆）
- S6_SEEN/S7 的 seen 记录以 Scopus EID 为主（17923+）；audit 样本只有 OpenAlex W id/doi，**无 W↔EID 映射**，EID-only 记录对样本不可匹配
- 30 篇 RELEVANT no-doi（MAG-only）中仅 2 篇经 W id 通道 reconcile 成功（1 篇在 KB）；余 28 篇保守记 Identity Unknown（可能实际在 EID 通道被 seen 过，无法证实）
- 过程缺陷记录：`r06_candidate_db_dois.json` 构建时丢弃了 S6_SEEN 的 1083 个 W id 键（本报告已用 `extra_identity_channels()` 从 s6_seen_set.json 补回）

### 3.4 Sens 层收录漏斗（23 retrieved-relevant → 4 KB）
23 篇 retrieved relevant（R2 resolved）中仅 4 篇在 FinalKB：
- 4 篇 KB 命中：W4411150150（SLA 陶瓷 warpage）、W2181354506（RAFT 应力松弛，no-doi W 通道）、W7168136911（vat photopolymerization 流变）、W2046639120（全息 thiol-ene）
- 19 篇 seen 但未收录：S6/S7 QA 判 I/U、或判 R 但无 abstract 未抽取、或 U 缓收——需逐篇复核（清单 `r06_retrieved_relevant_detail.json`）

## 4. 与 R05 的关系（禁横比）
- R05 的 37.37% 是 **S5 检索层 conservative recall**（S4 vs S5 同 sample paired 增量对比：resolved 42.5%→46.25%），含 UNKNOWN 混合口径，**不是 E2E**（North Star §6 已裁定）
- 佐证口径一致性：R05-500 全体命中 6.0% 与 R06-500 全体命中 8.4% 同量级 → 两轮审计的检索层覆盖自洽
- R06 是首次在 freeze 后以 fresh sample 完整测量三层（Retrieval×Sens=E2E）并满足恒等式

## 5. 结论
1. 按冻结契约，**Search vN 相对 8/31 宽 frame 的端到端覆盖 R2 ≈ 2.4%**（resolved）——极低。
2. 低的主因**不是**"检索到的论文被漏收"（Sens 17.4% 虽也低），而是 **Search vN 的检索/收录域与审计抽样框系统性错位**：seen∩frame = 7%、KB∩frame = 38%。frame 内 141/164 篇 audit-relevant 从未被检索到；而 Search vN 收录的 KB 论文 61.6% 在 frame 外。
3. 这不否定 S6/S7 在自身目标社区的产出（内部 QA 富集 R 率 4.6%/14.1% 为真），而是暴露 **audit frame 与探索策略解耦**（8 坑 #3 Region coverage ≠ recall、#7 frame 禁横比的现实样本）。
4. **R06 不能作为"KB 绝对质量"的测量**，它是"Search vN 对该冻结 frame 的覆盖审计"。若需 KB 绝对 recall，须以 KB∪seen 重建 frame 另做 fresh audit（与 R06 frame 不同，禁横比）。

## 6. 建议（按优先级）
- **P0**：round5 / S7+ 启动前，先做 **frame 重建对齐**（以 KB∩frame 83 篇 + seen 已覆盖社区 + s8 知识薄区为种子扩 frame），而非在旧 frame 内继续加 query
- **P0**：身份层修复——canonical seen 记录**三通道全保留**（doi / Scopus EID / OpenAlex W id），seen set 不再丢弃 W id；`r06_candidate_db_dois.json` 重建为多通道 id 集
- **P1**：Sens 收录漏斗修复——QA 判 R 但无 abstract 的收录缺口；U（staging 未排除）缓收规则落地
- **P1**：对 19 篇 seen-未收录 relevant 逐篇复核 QA 判定（`r06_retrieved_relevant_detail.json`），区分 screening 误判与机制缺口

## 7. 产物
- `data/exports/completeness_labels/pc_001__20260908012830_filled.json`（回填 labels，含 provenance _meta）
- `data/exports/terminology/r06_recall_report.json`（结构化统计）
- `data/exports/terminology/r06_retrieved_relevant_detail.json`（23 篇 retrieved-relevant 清单含 KB 状态）
- `tools/compute_r06_recall.py`（多通道 + R1/R2 + resolved/ultra + 恒等式校验）

## 8. 冻结口径（2026-09-08 用户终裁，v1.0 发布起永久生效，禁改写）

R06 的对外/对内标准表述，此后任何文档/汇报/论文一律照此引用：

> **R06 external-frame Retrieval Recall = 13.9%（resolved strict, R2），E2E Recall = 2.4%；该结果针对冻结的 OpenAlex FRAME_V2（`pc_001-2026-08-31T162811`，n=5890），不代表材料领域的绝对召回率，不得单独以 2.4% 表述为「系统召回率」。**

- 禁止引用示例：~~「系统召回率只有 2.4%」~~、~~「S7/Search vN 只找回了 2.4%」~~。
- 正确归因链（把两个问题拆开，不得合并成一个"检索差"结论）：

```text
外部 OpenAlex wide frame（FRAME_V2, n=5890）
   ↓  Search vN 触达其中 13.9%（R2 resolved：23/166）
触达的 relevant 中进入 KB 17.4%（4/23）
   ↓
E2E = 2.4%（4/166）
```

- **Retrieval / frame alignment**：13.9% 首先反映"该 frozen frame 大量区域根本不是当前 Agent 的搜索目标"——Search vN 沿 Scopus + relation/community 主动探索构建，与 8/31 OpenAlex wide frame 的搜索空间正交（universe∩seen = 7.0%、KB∩frame = 38.4%），而非 S7 把论文漏光。
- **Screening / promotion**：23 篇已触达 relevant 仅 4 篇在 KB——与搜索质量无关的独立工程缺陷（QA 判 I/U、R 无 abstract 未抽取、U 缓收），修复空间明确（§6 P1，移交 Roadmap v2）。

版本切分定位：本报告是 **v1.0 Literature Discovery Agent** 的独立审计证据（回答"Search vN 对该冻结 frame 的覆盖"），同时作为 **v2.0 Audit frame v2 重建 / completeness proof** 的输入；R06 数值与 frame 解耦，不做绝对 recall 声明。
