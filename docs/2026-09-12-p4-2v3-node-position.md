# P4-2 v3：NODE = 知识树位置（不是规模）—— 目标改对了，但我的操作化被对照检验推翻

日期：2026-09-12 ｜ 预测器 `p4_2v2`（NODE 打分升级为 v3 口径）｜ 分析器 `p4_2_analyst_v1`

---

## 1. 用户的纠正

> 研究方向的潜力（scientific potential）和当前规模（current popularity）应该解耦。
> NODE 不该预测「哪些节点会变大」，而该预测「哪些节点处于知识空间中的**高潜力位置**」。
> `Potential(node) = f(position in knowledge tree)`，不是 `f(paper count)`。

并指出上一轮 NODE 实验失败的真正原因：我不是在找"新兴方向"，而是在找
**"大问题 / 大主题"**（`shrinkage`、`polymerization`、`mechanical property`）——
它们已经成熟，未来本来就不会突然爆发。P4-3 的数据正是这个诊断：
`challenge` 型候选 lift **0.750**，而同分层随机节点的命中率反而更高（49% vs 37%）。

这个纠正成立，且解释了已有的负结果。

---

## 2. 实现

### 2.1 四个结构信号 + 弱化的增长

| 特征 | 权重 | 操作化 |
|---|---|---|
| `structural_novelty` | 0.25 | 该节点的**连接有多新**（新边占比，首现 ≥2017）+ 自身首现新近度 |
| `bridge` | 0.25 | 参与系数 `P = 1 − Σ_c (k_ic/k_i)²`，模块 = 节点的**主场主题**（OpenAlex 自带分类，独立于本图） |
| `problem` | 0.25 | typed 关系指向 `challenge` 型概念，按挑战**成熟度**加权（`Σ log1p(support_c)·n`）—— 解决老瓶颈比解决新问题更值钱 |
| `combination` | 0.20 | 邻居对之间**互不相连的比例**（结构洞 / 中介度） |
| `growth_weak` | **0.05** | 用户明确「w5 → 0」，只留一个弱项 |

### 2.2 规模混淆：不是"不要规模约束"，而是"规模决定竞争组，结构决定组内排名"

第一版直接把参与系数/中介度当分数，立刻被枢纽占据 —— 实测
`photopolymerization`（deg=93）的 `P=0.91`、中介度 `0.97` 都是全榜第一：
度数一大，邻居自然横跨多个模块、邻居对自然也大多互不相连。这是**构造性**的，不是数据问题。

处理：`stratum = small(deg≤3) / mid(4–7) / large(>7)`，所有结构特征**只在层内**做百分位排名，
跨层不可比；主榜取 small+mid（用户目标正是「目前还小、但可能改变知识树结构的方向」），
large 层单列。回归测试 `test_ranking_is_within_stratum_not_across` 断言每层各有自己的 #1。

### 2.3 表征手段的标记（可评审规则）

`dynamic mechanical analysis` / `scanning electron microscopy` 这类概念的**中介度天生很高**
（每篇论文都要报告测量），按度数分层**挡不住**（实测 DMA deg=7 落在 mid 层、中介度仍 0.57）。
按词面模式标记（`NODE_METHOD_LIKE_RE`，正则写进产物供评审），排除出主榜，被排除项全部列出。

### 2.4 可测量下限（不是规模奖励）

`degree ≥ 2` 与 `support ≥ 2` 作为结构量的**可测量前提**（孤点上参与系数无定义），
被它挡掉的计数逐项记录：`node_below_min_degree 6,983` / `node_below_min_support 133`。

---

## 3. 候选面确实变了

```
NODE 243（主榜 162）| 分层 small 101 / mid 72 / large 70
主榜 Top：residual stress / radical photopolymerization / step-growth polymerization /
          ceramic stereolithography / cure shrinkage / holographic data storage /
          internal stress / addition-fragmentation chain transfer(RAFT) /
          chain transfer / crosslinking / cationic ring-opening polymerization /
          epoxy acrylate / photocuring / gelation / additive manufacturing
主榜 support 分布（Top-30）：3–18，多数 5–11（**不再是 84、27 那种大主题**）
```

结构语义明显不同了：出现了 RAFT、阳离子开环聚合、逐步聚合、陶瓷立体光刻、全息数据存储
这类**机制/位置**型候选，而不是"最大的问题"。这一点符合用户的目标。

---

## 4. 但两个检验都不过 —— 而且这不是功效问题

### 4.1 结构对齐判据（伙伴广度）

用户纠正后的目标不是"变大"，所以判据也换成结构对齐的：**该概念是否在 EVAL 期获得新伙伴**
（`|eval_partners| > |train_partners|`，伙伴宇宙 = support≥3 的 429 个概念，共现 ≥2 篇算伙伴）。

| | 命中 | 中位广度增长 | 中位新伙伴占比 |
|---|---|---|---|
| 真实候选（162） | 92/162 = 56.8% | 1.021 | **0.172** |
| 同类型随机基线 | 88/162 = 54.3% | 1.066 | **0.301** |
| | lift **1.045** | z=0.447 | p=0.655 → **NULL** |

反直觉的细节：真实候选的**新伙伴占比反而低于基线**（0.172 vs 0.301）。
也就是说结构分高的节点**更嵌入**（伙伴多为老伙伴），而随机节点反而更多地拿到全新伙伴。

### 4.2 结构打分的阳性对照（决定性）

与词面尺子同样的做法：拿一组**已知在 2020 前后打开新连接**的概念，看它们在结构打分里排在哪。

```
14 个对照概念，只有 5 个在候选图内（9 个根本不在图里）
层内分数百分位中位数 = 0.42（低于中位）
其中 pet-raft polymerization 0.23 / 4d printing 0.36 / thiol-ene click chemistry 0.42
  digital light processing 0.51 / frontal photopolymerization 0.91
不在图内：vat photopolymerization / two-photon polymerization / bioprinting /
          machine learning / anisotropic shrinkage / low shrinkage / …
```

**这决定性地说明：NULL 不是（只）因为数据薄，而是我的操作化方向错了。**
若特征方向是对的，已知扩张的概念至少应该偏上，而不是中位数 0.42。

### 4.3 诊断：三个结构代理是**静态拓扑量**，等于换了个口径的成熟度

`bridge` / `combination` / `problem` 都在测量**当前已经存在的连接结构**。
一个真正的新方向在历史窗口里**恰恰是连接很少的** —— 它还没连上。
所以静态结构量必然偏向"已经是连接者"的节点，即已经成熟的节点。
这与按论文数排名的病理**同源，只是更隐蔽**。

唯一的变化型特征是 `new_edge_share`，而它在多数候选上接近 0（Top-榜里大量 0.00）——
也就是说，真正区分"正在打开连接"的信息，我在打分里只给了很小的权重，且定义过粗。

### 4.4 两个问题同时存在，且可分离

| 问题 | 证据 | 修法 | 成本 |
|---|---|---|---|
| **操作化错**（主因） | 阳性对照中位数 0.42（低于中位） | 结构特征改为**变化型**：节点在 TRAIN 内部的**伙伴获取速率**（≤2017 的伙伴集 vs 2018-2020 的新增），而不是静态拓扑 | 0（改代码） |
| **覆盖不足** | 9/14 对照概念不在候选图内 | 抽取扩到全部 5,545 篇 TRAIN | ~$10 |

---

## 5. 结论（要点）

1. **目标改对了**：把 NODE 从"规模"解耦到"结构位置"是对的，候选面也确实从"最大的问题"
   变成了"机制/位置"型候选。
2. **我的操作化被自己的阳性对照推翻**：三个结构代理是静态拓扑量，等价于换口径的成熟度度量；
   阳性对照落在中位数以下（0.42）是最直接的证据。
3. **因此"结构位置能不能预测未来"这个问题，本轮仍未得到检验** —— 检验的是我的一个失败代理。
   不能把它读成"结构位置无用"。要读成"这个操作化无用"。
4. **唯一通过检验的仍是边层**：PAIR lift **1.454**（z=4.10, p=4.1e-05），
   与用户自己的架构判断一致 —— PAIR 是"新关系事件"，NODE 是"新知识实体"，
   而**新事件**比**新实体**更容易从历史结构里被识别。
5. 词面尺子本身有灵敏度（阳性对照 13/13 = 100%，中位期率比 2.355），
   所以上述判断不是"仪器钝"造成的。

---

## 6. 下一步（按优先级）

| # | 动作 | 依据 | 成本 |
|---|---|---|---|
| 1 | **结构特征改为变化型**：节点在 TRAIN 内的伙伴获取速率 / 新边形成速率，而不是静态 P、中介度 | 阳性对照 0.42 | 0 |
| 2 | 用同一组对照概念**先做特征筛选**（对照必须偏上，否则不采用），再去看未来验证 | 避免再出现"操作化错了却拿去跑验证" | 0 |
| 3 | 抽取扩到全部 5,545 篇 TRAIN | 9/14 对照概念不在图内 | ~$10 |
| 4 | Tier 2（EVAL 概念抽取） | 词面尺子粗糙 | ~$8 |
| 5 | 9 个 `pending_review` 主题 | 上轮遗留 | 0（改 yaml） |

**顺序建议**：先做 1 + 2（免费、且能立刻证伪新特征），通过了再做 3 + 4。
理由：3 只是把样本加厚，如果特征方向还是错的，加厚只会让错的结论更显著。

---

## 7. 产物与验收

| 文件 | 说明 |
|---|---|
| `datasets/photopolymerization_v1/emergence_candidates_v2.json` | 含 `node_weights` / 分层 / `node_method_like_rule` / `structural_positive_control` / `prediction_set_node_rows` |
| `…/validation_report_v1.json` | 新增 `node_structural`（结构对齐判据 + 同层基线 + 显著性） |
| `…/candidate_analysis_v1.json` | Analyst 输出新增 `structural_role` 枚举（本轮 6 例全部为 `bridge`） |
| `tools/discover_emergence_candidates.py` | NODE 打分 v3 + 结构阳性对照 |
| `tests/test_p4_2v3_node_position.py` | 7 项（层内排名 / 弱 growth / 表征手段排除 / 下限记录 / 阳性对照存在） |

pytest **532 passed**；主库只读；Analyst 本轮 18/18 OK、接地度 1.0、$0.01。
