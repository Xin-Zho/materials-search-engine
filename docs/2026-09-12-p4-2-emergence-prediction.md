# P4-2：Emergence Score 排序（1000 篇 TRAIN 概念图）

日期：2026-09-12 ｜ 预测器 `p4_2_emergence_v1` ｜ **已冻结**
工具：`tools/predict_emergence_p4_2.py` ｜ 测试：`tests/test_p4_2_emergence.py`（17 项）

用户 P4-2 规格：**v1 不做机器学习，先做 ranking** ——
`growth / acceleration / novelty / connectivity / cross-domain → Emergence Score → Top-K`

---

## 一、反泄漏：本工具的第一属性

| 机制 | 实现 |
|---|---|
| 只用 TRAIN | 启动即断言 `max(year) <= 2020`，否则 `SystemExit`（不是警告） |
| 拒绝 EVAL | 断言 split 里没有 `EVAL` —— EVAL 只用于对答案 |
| 禁用引用快照 | **v1 的五个指标根本不用引用**；且源码里不得出现 `citation_count`（AST 断言，非文本搜索） |
| 先冻结后验证 | 产物记录 `concepts_sha256` + `db_sha256` + 权重 + 时间戳，P4-3 验证时**不得再调参** |

冻结指纹：

```
predictor_version  p4_2_emergence_v1
concepts_sha256    2b7c8368e0d0a816…
db_sha256          5662a77da99b7392…
leakage_guard      {max_year: 2020, split: {TRAIN: 1000}}
n_metrics          7359 概念
n_candidates       86（support>=3 且 type ∈ {direction, challenge}）
```

> 反泄漏断言的测试用 **AST** 而不是文本搜索：字符串 `citation_count` 必然出现在
> **解释它被禁用**的注释里，文本搜索会把"说明"当成"违规"（本项目在 static guard
> 上已踩过同一个坑）。

---

## 二、为什么必须按期份额归一化

数据集是**分层抽样**（A 高影响 / B 快速增长 / C 长尾），**不是按年份等比例**：

```
early 2011-2015 : 1,818 篇
late  2016-2020 : 2,891 篇
```

直接比 `df_late / df_early` 会把这个**采样偏斜算成增长**。故一律用**论文份额**
（`df / 该期论文数`）+ Laplace 平滑 0.5：

```
rate(period) = (df_period + 0.5) / (n_period + 1)
growth       = rate(late) / rate(early)
```

`acceleration` = 2018-2020 三年**年度份额**的最小二乘斜率（同样按年度论文数归一）。

**测试固化**：构造「前后期出现率相同、但期规模 2 vs 8」的合成数据，
断言 `growth ∈ [0.8, 1.25]`（直接比计数会得到 4.0）。

---

## 三、评分：为什么用百分位排名 + 分类型

1. **百分位排名**而非原始值加权：`growth` 分布极度长尾（平滑会把 `df: 0→1`
   放大成很大的比值），原始值加权会让一个只出现一次的噪声概念压过真正持续增长的方向。
2. **分类型独立排名**：跨类型不可比 —— 见下文发现 3。
3. 权重（冻结）：`growth 0.30 / acceleration 0.25 / connectivity 0.20 / cross_domain 0.15 / novelty 0.10`

---

## 四、结果：Top-20（type ∈ {direction, challenge}）

| # | score | sup | growth | xdom | 首现 | type / concept |
|---|---|---|---|---|---|---|
| 1 | 0.824 | 35 | 2.45 | 12 | 1997 | challenge `shrinkage` |
| 2 | 0.812 | 8 | 2.31 | 5 | 2010 | direction **`thiol-ene click chemistry`** |
| 3 | 0.811 | 8 | 5.66 | 4 | 1995 | challenge `residual stress` |
| 4 | 0.797 | 4 | 5.66 | 3 | 2016 | direction **`frontal photopolymerization`** |
| 5 | 0.781 | 14 | 2.14 | 4 | 2001 | challenge `secondary caries` |
| 6 | 0.772 | 3 | 3.14 | 3 | 2009 | challenge `high shrinkage` |
| 7 | 0.745 | 6 | 2.31 | 4 | 2012 | challenge `anisotropic shrinkage` |
| 8 | 0.718 | 21 | 1.47 | 12 | 1986 | challenge `volume shrinkage` |
| 9 | 0.713 | 6 | 8.18 | 5 | **2019** | direction **`4d printing`** |
| 10 | 0.705 | 4 | 1.89 | 4 | 2002 | challenge `internal stress` |
| 11 | 0.701 | 4 | 1.05 | 4 | 2006 | challenge `photopolymerization shrinkage` |
| 12 | 0.699 | 7 | 2.31 | 4 | 2002 | challenge `linear shrinkage` |
| 13 | 0.691 | 7 | 0.81 | 3 | 2013 | direction `bulk-fill resin composite` |
| 14 | 0.689 | 10 | 1.64 | 6 | 2008 | challenge `low toughness` |
| 15 | 0.689 | 3 | 4.40 | 2 | 2017 | challenge `porosity` |
| 16 | 0.682 | 25 | 2.34 | 4 | 2012 | direction `additive manufacturing` |
| 17 | 0.674 | 4 | 3.14 | 3 | 2004 | direction `soft lithography` |
| 18 | 0.655 | 84 | 0.78 | 30 | 1988 | challenge `polymerization shrinkage` |
| 19 | 0.654 | 7 | 0.81 | 2 | 2015 | direction `bulk-fill composite` |
| 20 | 0.638 | 7 | 6.92 | 4 | 2006 | challenge `high viscosity` |

方向性较强的命中：`thiol-ene click chemistry`（低收缩主线）、`frontal
photopolymerization`（2016 首现）、`4d printing`（2019 首现，growth 8.18）、
`additive manufacturing`、`bulk-fill resin composite`。

**但这份榜单不能直接当成论文级结论** —— 下面三个问题必须先解决。

---

## 五、三个发现（都必须在 P4-3 之前决定）

### 发现 1：**统计功效不足**（最硬的问题）

| support 门槛 | 候选概念 | 其中 `direction` |
|---|---|---|
| >= 2 | 1,005 | 77 |
| >= 3 | 429 | **29** |
| >= 5 | 171 | **14** |
| >= 8 | 76 | 5 |
| >= 10 | 54 | 3 |

`direction` 是用户定义的「研究方向」粒度，而 **support>=3 时只有 29 个候选**
（support>=5 时只剩 14 个）—— 要在 29 个候选里做 Top-20「预测」，
统计上几乎是「把候选都列出来」，且 growth 由 1→2 篇这类单次计数决定，噪声极大。

**根因是语料规模**：抽取只覆盖 1,000 篇（采样），而干净视图 TRAIN 侧有 **5,545 篇**。

**建议**：把抽取扩到全部 5,545 篇。成本 **约 $10**、墙钟约 35 分钟（并发 10），
support 分布会整体上移约 5.5 倍，`direction` 候选预计从 29 → 百余个。

> 采样分层（A/B/C）的设计目的是**在有限预算下保证概念多样性**，
> 而趋势指标需要的是**覆盖度**。两件事的最优抽样策略不同 ——
> 对 P4-2 而言，全量 TRAIN 比 1,000 篇分层样本更合适（且没有采样偏斜）。

### 发现 2：**跨类型排名不成立**，且我尝试的判别量被证伪

跨类型排名的污染实证（未过滤时的 Top-20 里混进了）：
`scanning electron microscopy`、`tensile testing`、`light scattering`、
`finite element method` —— 这些是**表征/分析手段**，随论文数例行增长，
不是研究方向。

**尝试的修法（失败，已撤回）**：定义 `dir_ratio` = 该概念参与的
「解决问题型」关系占比（`addresses`/`enables`/`improves`/`causes`/`alternative_to`），
假设真方向会 `addresses` 挑战、表征手段只在 `requires`/`part_of` 位置。

**实测证伪**：门槛 0.30 挡下的是

```
photoinitiator(0.12)   vinylcyclopropane(0.00)   epoxy resin(0.15)
composite resin(0.00)  methyl methacrylate(0.00) acrylate monomer(0.11)
```

这些**真概念**，而 `scanning electron microscopy` **并没有被挡下**。
根因：材料类概念在图里天然处于 `requires`/`part_of` 位置
（"photopolymerization --requires--> photoinitiator"），
所以这个量与「是否表征手段」**正交**。

**采用的修法（构造性依据）**：预测只取 `direction` + `challenge`
—— 这两类在抽取时的语义定义就是「研究方向的完整表述」与「未解决的问题」，
**按构造**即方向性。其余类型（`material`/`mechanism`/`fabrication_method`/
`application`）单列为**支撑信号**（`digital light processing` 的 growth 8.60
留在支撑信号里，未被丢弃）。

**回归测试**：`test_dir_ratio_must_not_be_a_gate` 断言
「`photoinitiator` 的 `dir_ratio=0` 时仍必须进入预测榜」——
防止这个被证伪的启发式复活。

**未决**：`fabrication_method` 混了「加工方法」（真方向）与「表征手段」（噪声），
需要在 v2 用**一次廉价的 LLM 分类**把候选概念拆开（约 $0.02 / 数百个概念），
而不是靠图论或手写黑名单。

### 发现 3：**词形变体主导榜单** —— 需要一份经评审的 canonical map

Top-20 里有 7 个是同一个概念的不同写法：

```
shrinkage · high shrinkage · anisotropic shrinkage · volume shrinkage
· linear shrinkage · photopolymerization shrinkage · polymerization shrinkage
```

这不是 bug，是**有意为之**：抽取阶段只做机械规范化（小写/单数/去尾标点），
**不做同义合并** —— 合并不可逆，且"两个概念是不是同一个"是领域判断。

**代价**就是榜单被变体稀释：Top-20 实际只覆盖约 10 个不同想法。

**建议**：建一份 committed 的 `concept_canonical_map.yaml`，对
`support >= 5` 的 171 个概念做人工（或 LLM 提议 + 人工确认）归并，
每条记录 `canonical` + `members` + `basis`。这是**评审资产**，不是自动合并。

---

## 六、P4-3 验证设计（尚未执行）

**顺序不可颠倒**：必须先冻结预测（已完成），再抽 EVAL 概念当答案。

```
1. 冻结预测                      ✅ 已完成（本文件）
2. 抽 EVAL 侧概念（只用于对答案）  ❌ 未做
3. 在 EVAL 侧算「真实增长」        ❌ 未做
4. 比较 Top-K 命中率              ❌ 未做
```

要点：
- EVAL 概念抽取**必须在预测冻结之后**，且抽取结果**永不回流**到特征侧
- 「真实增长」口径需事先定好并冻结：同 `growth` 定义（份额化 + 平滑），
  期窗口为 2021-2025，`first_seen` 不在窗口内算「新出现」
- 评估指标：`Precision@K`、`Recall@K`（相对 EVAL 真实增长集）、
  以及**随机基线**对照（同 support 档随机抽 K 个，重复多次给分布）——
  没有随机基线的命中率无法解释
- 用户原定的额外校验：Top-20 是否出现在 Nature / Science / Advanced Materials /
  AFM / Nature Communications 等期刊（作为定性佐证，不作主指标）

成本：EVAL 侧 4,423 篇（视图口径）约 **$8**。

---

## 七、需要用户裁定

| # | 决策 | 我的建议 |
|---|---|---|
| 1 | 是否把抽取扩到全部 5,545 篇 TRAIN（约 $10） | **建议做** —— 29 个 direction 候选不足以支撑 Top-20 预测，这是本阶段最硬的瓶颈 |
| 2 | 是否建 `concept_canonical_map.yaml`（评审归并） | **建议做** —— 否则榜单被 7 个 shrinkage 变体稀释 |
| 3 | `fabrication_method` 是否用一次 LLM 分类拆分（约 $0.02） | 建议做，但可推到 v2 |
| 4 | 9 个 `pending_review` 主题是否纳入（上轮遗留） | 与 1 一起做，改 yaml 即可 |

**若 1、2、4 都做**：总成本约 $18，之后 P4-3 验证约 $8。
实验闭环的总成本在 **$30 以内**。

---

## 八、附：本次冻结的产物

| 文件 | 内容 |
|---|---|
| `emergence_scores_p4_2.json` | 86 条候选完整指标 + rank + 配置 + 输入哈希 + `rejected_heuristic` 记录 |
| `emergence_top50.csv` | Top-50 便于查看 |

产物不入 git（`datasets/.gitignore`）——可由工具重建；**冻结协议与权重在代码与本文档里**。
