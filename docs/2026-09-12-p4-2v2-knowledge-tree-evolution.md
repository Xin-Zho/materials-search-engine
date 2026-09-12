# P4-2 v2：知识树演化 → 候选方向（LLM 只解释，不预测）+ P4-3 未来验证

日期：2026-09-12 ｜ 预测器 `p4_2v2` ｜ 分析器 `p4_2_analyst_v1` ｜ 验证器 `p4_3_v1`

---

## 0. 本轮的架构修正（用户裁定）

```
旧： 论文 → LLM 抽取 → 知识图 → 趋势评分 → **LLM 判断哪些方向有潜力**

新： 论文 → 知识树构建 → **纯算法发现候选方向** → LLM 只做解释（Analyst）
                                        ↘ 未来文献验证（P4-3）判真伪
```

为什么必须改：问模型「哪些方向有潜力」得到的是**模型自己的先验**（训练语料里的
热门话题、对领域的既有印象），它无法归因到这份语料、无法复现、也无法被证伪。
改成问「解释这个已被算法观测到的结构变化」之后，输出的每一条都能对照证据检视，
而**证伪的责任落回未来文献**，不落在语言模型身上。

v1 的 `tools/predict_emergence_p4_2.py`（concept popularity 排序）保留为历史，
已被本文件取代 —— 见 v1 文档顶部的 SUPERSEDED 标注。

---

## 1. 边层为什么以「共现」为基础（实测数据）

P4-1 抽出的 typed 关系语义清晰但**极度稀疏**：

| 量 | 数值 |
|---|---|
| typed 关系总数 | 7,441 |
| distinct (source, relation, target) | 7,322 |
| 其中出现次数 ≥2 的 | ~102 |
| 单条边最大支撑度 | **4** |

直接拿它做时序信号没有统计质量。因此边层以**共现对**为基础，
typed 关系作为**语义标注**与**候选质量门**：

| 量 | 数值 |
|---|---|
| 共现对实例 | 52,522 |
| distinct 共现对 | 50,798 |
| support ≥2 | 1,323 |
| support ≥3 | 226 |
| 其中同时有 typed 关系的 | 3,519 |

这不是让步：要检测的事件是「两个概念之间的连接是否新近形成/增强」，
共现是这一事件的直接观测；typed 关系回答的是「这条连接的语义是什么」。
两者都在候选记录里分别给出（`edge_grade` = `TYPED` / `CO_OCCUR_ONLY`）。

**顺带否掉一个假设**：机械变体归并（词集相同）只形成 22 簇、涉及 44 个概念，
support≥2 的对从 1,323 → 1,331。**变体归并不是当前瓶颈**，canonical map
的优先级可以往下调。

---

## 2. 候选生成（四类信号，全部算法化）

`tools/discover_emergence_candidates.py`（真库只读，反泄漏硬断言）

| 信号 | 落点 | 依据 |
|---|---|---|
| (1) 节点增长 | `NODE` 候选 | 按期份额增长 + 加速度 + 首现新近度 + 连通度 |
| (2) 新边形成 | `PAIR` 特征 `new_edge` | 首现年 ≥ 2017（`first_seen` 的归一化新近度） |
| (3) 跨域连接 | `PAIR` 特征 `cross_domain` | 两端端点的**主场主题**不同 |
| (4) 知识缺口 | `PAIR_GAP` 候选 | **尚未共现** + Adamic-Adar 链路预测高 |

候选池：**NODE 429**（预测口径 direction+challenge = 86）｜**PAIR 424**｜**GAP 155**。
被排除计数与原因全部记录（`pair_low_support` 49,475 / `pair_low_endpoint_support` 629 /
`pair_no_semantic_anchor` 270）—— 范围门只**标记**不静默丢弃。

### 2.1 一个必须修的方法学缺陷：连通度奖励枢纽

首版把「连通度」当加权特征，Top-20 立刻被 `photoinitiator + 组织工程` 这类配对
占据 —— 枢纽概念（photoinitiator / resin / monomer）与任何概念的新配对都"看起来是新的"。
改用**关联强度的变化**替代：

```
assoc_growth = 0.5 · [ (P(b|a)_late − P(b|a)_early) + (P(a|b)_late − P(a|b)_early) ]
```

### 2.2 这个特征的第一版是错的，被我自己的测试推翻

最初实现用 **ΔPMI**（`log2[p(a,b)/(p(a)p(b))]`）。问题在分母：概念罕见时**边际概率**
落到 Laplace 地板 `0.5/n`，于是「早期两个端点都还没出现」的**新兴配对**拿到一个
虚高的早期 PMI，ΔPMI 变成大负数：

| 合成用例 | ΔPMI | Δconf |
|---|---|---|
| 特异性新兴配对（早期 0 篇、晚期 4 篇） | **−2.92** | **+0.40** |
| 长期平凡的枢纽配对（两期各 20 篇） | +0.25 | ≈0.00 |

也就是说 ΔPMI **系统性地惩罚我们正在找的东西**，与它自己的文档说明正好相反。
Δconf 的分子分母都是计数、地板效应同阶因而相消。回归测试
`test_assoc_growth_rewards_specific_over_ubiquitous` 盯着这个方向性。

### 2.3 跨域判据第一版**不可达**（另一个真 bug）

首版定义为「两端端点的主题集合不相交（Jaccard == 0）」—— 但**共现对必然共享那篇
论文的主题**，Jaccard 恒 > 0，该判据永不成立（实测跨域候选 = **0**）。
改为「两端端点的**主场主题**（各自论文里出现最多的主题）是否不同」：

> 一个只在牙科论文里相遇的配对不是跨域；跨域是「一端的主场在别处」。

修复后跨域候选 157 个。回归测试 `test_cross_domain_is_reachable`。

### 2.4 评分

五个特征百分位排名后加权（**同 kind 内**排名，跨 kind 不可比）：

```
growth .20 | new_edge .20 | assoc_growth .25 | cross_domain .15 | gap .20
```

`connectivity` 降级为**诊断量**（不进权重），`hub_endpoint` 标记 p95 度端点：
全体候选 67% 含枢纽、Top-20 90%（p95 阈值 = 12）。

### 2.5 冻结

`datasets/photopolymerization_v1/emergence_candidates_v2.json`
指纹：`concepts_sha256=…` `tool_sha256=…` `candidates_sha256=ba94e6af231d9ae3…`
（权重、阈值、四条判据、`validation_plan`、被拒绝的启发式全部写进产物）。

---

## 3. LLM Analyst：解释层

`prompts/p4_2_candidate_analysis_v1.md` + `tools/analyze_candidates_llm.py`

硬约束（写进 prompt）：不得预测；只能用给定证据；每条推理要能追溯到 `paper_uid`；
证据不足必须明说并调高 `uncertainty`；不得提及任何影响力指标；只输出 JSON。

输出字段固定：`candidate` / `structural_change` / `evidence` /
`reasoning` / `alternative_explanations` / `uncertainty` / `falsifiable_checks`。

两个实现要点：

1. **必须显式给出输出 schema**。首版 prompt 只写了"输出 JSON"，模型立刻自造字段名
   （`interpretation`、`analysis.emerging_signal`，甚至回显 `{"type": "json_object"}`），
   7 个候选里 5 个被判无效。补上字段清单后 28/28 全部通过。
2. **接地度**（groundedness）= evidence 条目里引用了本次提供的 `paper_uid` 或
   年份的比例。它是解释层的主要失效模式的度量（模型自说自话）。
   首版 NODE 候选没有证据包 → 接地度 0.2 → 校验层直接判无效；
   补上「早期/晚期各若干篇 + 原文片段」的证据包后接地度 **1.0**。

反泄漏：输入是**白名单构造**（只取候选记录里的指定字段），因此引用量/未来信息
在结构上进不去；另外断言证据年份 ≤ 2020。

结果：**28/28 OK，$0.041，uncertainty 均值 0.586，接地度 1.0**。
每次调用约 500 token 输入，扩到全部 424+155+86 候选约 **$0.3**。

---

## 4. P4-3 Tier-1 验证

`tools/validate_emergence_p4_3.py`（无需 LLM）

### 4.1 度量

两端概念在 TRAIN(2011-2020) 与 EVAL(2021-2025) 两段的**词面**计数：
论文标题+摘要中，每个端点的显著 token（len≥5 且非通用词）**全部**命中、
且所有端点都命中（整体合取）。token 匹配包含：相等 / 文本 token 以概念 token 开头 /
概念 token 以文本 token 开头 / 共享 ≥6 字符前缀 / 概念 token 被更长复合词包含
（`polymerization ⊂ photopolymerization`，用词尾桶实现）。

两侧用**同一把尺子**（都是词面计数），因此不存在「TRAIN 用 LLM 计数 vs EVAL 用词面
计数」的尺度错配。TRAIN 侧在完整 in-scope 视图上测量（3,184 篇），不是 993 篇抽样。

判据：`eval_lex > 0` 且 **关联强度增强** ——
PAIR 用词面 ΔPMI > 0；**NODE 用期率比 > 1**（单概念的 PMI = log2(p/p) ≡ 0，
首版误用它导致节点命中率恒为 0，那是度量假象）。

### 4.2 随机基线（必需）

按**同 kind、同支撑度分层**抽等量随机候选，用完全相同的度量：
PAIR 从共现图里同 support 的随机对抽；GAP 从「成熟节点间未共现、共同邻居数相同」
的随机对抽；NODE 从同 type 同 support 的随机节点抽（严格同 support 池耗尽时逐级放宽并记录）。

### 4.3 阳性对照（度量灵敏度检验）

13 个本领域公认在 2021-2025 扩张的概念（additive manufacturing / 4d printing /
digital light processing / tissue engineering / bioprinting / soft robotics…）
跑同一把尺子：**命中 13/13（100%），中位期率比 2.355**。

作用：把「候选不好」与「尺子太钝」分开。若对照也命中不了，候选的零结果就不可解释。
**这是事后构造的对照，只用于校验度量，不构成方法有效性的证据** —— 已写进报告。

### 4.4 结果

| 类别 | 真实候选 | 随机基线 | lift | z | p | 判定 |
|---|---|---|---|---|---|---|
| **PAIR_PRESENT** | 189/424 = 44.6% | 114/372 = 30.7% | **1.454** | **4.10** | **4.1e-05** | **SIGNAL** |
| PAIR_GAP | 75/155 = 48.4% | 84/155 = 54.2% | 0.893 | −1.02 | 0.31 | NULL |
| NODE | 31/86 = 36.1% | 37/86 = 43.0% | 0.838 | −0.94 | 0.35 | NULL |

分层敏感性：

| 分层 | lift |
|---|---|
| PAIR support ≥2 | 1.454 |
| PAIR support ≥3 | 1.144 |
| PAIR support ≥4 | 1.273 |
| PAIR support ≥5 | 0.847（n=31/7，噪声大） |
| NODE `direction` | 0.833 |
| NODE `challenge` | **0.750** |

### 4.5 怎么读这个结果

**支持了架构修正的方向**：携带未来信号的是**知识树的演化（边/新连接）**，
不是**概念频次（节点）**。用户把预测对象从 concept 改成 knowledge tree evolution，
这一轮的数据支持这个判断。

三个必须一起读的事实：

1. **PAIR 有信号，但幅度温和**（lift 1.45，绝对差 14 个百分点）。它来自
   TRAIN 期新形成的连接在 EVAL 期继续增强 —— 这是**外样本的延续性**，
   不是"发现了尚未可见的东西"。诚实地说，它更接近动量外推而非科学发现。
2. **NODE 与随机基线无可区分，且 `challenge` 型更差（lift 0.75）**。
   原因清楚：按"节点增长/连通度"排序选出来的是 `polymerization shrinkage`、
   `secondary caries` 这类**长期存在的问题**，它们大而熟，但不在前沿。
   同一个 `challenge` 分层内，随机抽取的节点命中率反而更高（49% vs 37%）。
   这条对候选定义是可执行的批评：**"最大的问题"≠"新兴的问题"**。
3. **GAP 候选未通过**，且 Tier-1 **在原理上无法检验「缺口被填补」**：
   「TRAIN 期从未共现」是**抽取样本（993 篇）上的事实**，不是语料事实 ——
   词面在 TRAIN 期就能为同一对找到上百次共现。缺口语义的检验需要 Tier 2。

---

## 5. 局限（不要跳过这一段）

* **Tier 1 是粗尺子**：词面合取匹配存在**同形异义**（`shrinkage` 在陶瓷烧结语境
  含义不同，实测 1,124 篇 TRAIN 论文含 `shrinkage`），且无法识别同一概念的不同说法
  （变体稀释 → 偏保守）。它把「关联是否增强」测成一个大而糙的数。
* **抽取覆盖面**：候选侧只有 993 篇 TRAIN（干净视图有 3,184 篇 in-scope、
  5,545 篇未排除的 TRAIN）。support=2 意味着"两篇论文"，特征噪声大。
* **Tier 2 未执行**：对 EVAL 论文跑同一套 concept+relation 抽取（约 $8），
  才能用与候选完全同构的口径做判定。**必须在候选冻结之后执行，结果永不回流。**
* **基线可复现性**：基线的抽样是 (候选列表及其顺序, seed) 的确定性函数 ——
  候选排序变化会改变基线取样序列（因为抽中的对会被标记为已用）。
  报告里记了 `candidates_sha256`，因此可复现，但**不要跨版本比较基线数值**。
* 阳性对照是事后构造，只证明尺子有灵敏度。

---

## 6. 待用户裁定

| # | 决策 | 依据 |
|---|---|---|
| 1 | **重定义 NODE 候选**：不用"最大/增长最快的 challenge"，改为按"新出现 + 尚未被大量解决"筛选 | NODE lift 0.838，`challenge` 分层 0.75 |
| 2 | **修 PAIR 候选的低支撑噪声**：抽取扩到全部 5,545 篇 TRAIN（~$10 / 35min），候选 support 分布整体上移约 5.5x | support≥2 的 lift 1.454 与 ≥5 的 0.847 都不稳 |
| 3 | **Tier 2**：EVAL 概念抽取（~$8），用同构口径重判 GAP 与 PAIR | GAP 在 Tier 1 原理上不可检验 |
| 4 | 9 个 `pending_review` 主题（上轮遗留） | 与 2 一起做，改 yaml 即可 |

全做约 **$18**。

---

## 7. 产物

| 文件 | 说明 |
|---|---|
| `datasets/photopolymerization_v1/emergence_candidates_v2.json` | 冻结候选（含权重/阈值/判据/证据包） |
| `…/emergence_candidates_v2.csv` | 候选表（三 kind 合并，带特征列） |
| `…/candidate_analysis_v1.json` | LLM 解释层产物（28 条，含接地度与可证伪项） |
| `…/analyst_spec.yaml` | 分析器协议冻结（prompt/tool/candidates 三 hash） |
| `…/validation_report_v1.json` / `.csv` | P4-3 报告（含基线、敏感性、阳性对照、逐候选） |
| `tools/discover_emergence_candidates.py` | 候选发现（四类信号 + Δconf + 缺口链路预测） |
| `tools/analyze_candidates_llm.py` | LLM Analyst（白名单输入 + 接地度校验） |
| `tools/validate_emergence_p4_3.py` | Tier-1 验证（倒排词面索引 + 分层基线 + 显著性） |
| `tests/test_p4_2v2_p4_3_pipeline.py` | 17 项回归（含 4 个真实 bug 的方向性守卫） |

pytest **525 passed**；主库全程只读（`sha256=2f5733d31e3fe41c…`）。
