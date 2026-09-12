# P4-0 Step 1：统一元数据表（Temporal Benchmark Dataset）

> ⚠️ **本文档记录的是 `p4_0_v1` 的范围门，已被修订。**
> P4-1A pilot 实测发现 v1 的词面范围门有严重污染（`re.X` 多词短语静默失效 +
> 通用词兜底 → 抽出的 30 篇里 ~17 篇离题）。
> 现行口径见 **`docs/2026-09-12-p4-0-v2-scope-gate-revision.md`**（`p4_0_v2`：
> 主题白名单为主 + 严格词面救援）。
> 本文档保持原样作为历史证据；用户裁定（Q1-Q4）与反泄漏协议**未变**。

日期：2026-09-12 ｜ 构建版本 `p4_0_v1` ｜ 数据集 `photopolymerization_v1`
用户裁定：**Q1** 用 `openalex_cache` 作首版主池 ｜ **Q2** ≤2020 TRAIN / 2021-2025 EVAL ｜
**Q3** 统一元数据表必做 ｜ **Q4** P4-1 抽取 1000-2000 篇分层采样

工具：`tools/build_p4_0_dataset.py`（默认 dry-run，`--apply` 落盘）
产物：`datasets/photopolymerization_v1/`（`benchmark.yaml` 入 git，其余为派生物）

---

## 一、六项验收（用户指定）

| 指标 | 值 |
|---|---|
| **Total papers** | **14,318** |
| **Train count**（≤2020） | **8,070** |
| **Eval count**（2021-2025） | **5,289** |
| **Missing year count** | **4** |
| **Duplicate entity count** | same_uid **0** ｜ same_doi_multi_uid **12** ｜ same_title_year **590** |
| **Identifier coverage** | openalex_id **14,318 (100%)** ｜ doi **13,535 (94.5%)** ｜ scopus_eid **44 (0.3%)** |

池展开：725 个缓存响应 → 18,724 条 → 按 OpenAlex work id 去重 **14,318**
（与 P0 迁移日志的 `cache_w=14318` 交叉吻合）。

`benchmark.yaml` 已生成并冻结协议。

---

## 二、本步最重要的发现：**语料池有范围污染**

### 症状

按引用排序看 TRAIN 侧头部，全池引用最高的论文是：

```
353,396  R: A Language and Environment for Statistical Computing
318,872  PROTEIN MEASUREMENT WITH THE FOLIN PHENOL REAGENT
227,435  Deep Residual Learning for Image Recognition
215,080  Generalized Gradient Approximation Made Simple
190,298  Using thematic analysis in psychology
```

**与光固化毫无关系。** 如果不处理，P4-1 分层采样的 Layer 1（高影响基础论文）
会被这些全球巨引论文占满，concept graph 会长在 R 语言和 PCR 方法学上。

### 根因（可复现的构造缺陷）

缓存响应 URL 显示两种查询形态：

```
filter=title_and_abstract.search:"ring-opening" "ring strain relief" "polymerization shrinkage"
sort=cited_by_count:desc

q = "title/abstract has (\"thiol-ene\" and \"oxygen tolerance\" and \"polymerization shrinkage\")"
sort=cited_by_count:desc
```

两者都是**松散文本匹配**，且**按引用降序**。松散匹配 + 引用降序 =
必然返回全库引用最高的论文。每翻一页都把同样那几篇全球巨引论文一起捞回来。

> 这不是「数据脏」，是「查询语义与排序键的组合在数学上必然产生这个结果」。
> 任何用检索缓存当语料池的项目都会踩到，且**不会报错**。

### 处理：多信号范围门（标记，不丢弃）

逐行判定，证据落库：

| tier | 含义 | 是否 in_scope | 数量 |
|---|---|---|---|
| `CORE` | 标题+摘要命中光固化家族词（含中文词面） | ✅ | 7,335 |
| `ADJACENT` | 仅命中广义材料/高分子词 | ✅ | 4,520 |
| `CHANNEL_ONLY` | 无词面证据但来自定向通道 | ❌（记录，可启用） | 1,118 |
| `OFF_TOPIC` | 无任何证据 | ❌ | 1,345 |

**变体对照（同一次构建内实测，见 `benchmark.yaml → scope.result.variant_comparison`）**：

| 变体 | in_scope | KB 召回 |
|---|---|---|
| `STRONG` 单独 | 7,335 | 83.3% |
| **`STRONG` OR `ADJACENT`（采用）** | **11,855 (82.8%)** | **100.0%** |
| 再加 `CHANNEL` | 12,973 | 100.0%（召回不升，多纳入 1,118 篇 → 不采用） |

四者的「全球巨引残留」均为 **0/10**。

**召回校验方法**：以 KB `paper_identifiers` 已收录的论文为已知相关集，
取其在池中的交集（126 篇），检查范围门是否漏掉 → **漏 0 篇**。

> 召回从 83.3% 提升到 100% 的关键：词面判定用**回填后的摘要**，
> 且词表含中文（`光聚合/光固化/光致聚合物/收缩/树脂/单体/交联`）。
> 中途实测确有召回漏洞：`改善光致聚合物全息记录材料体积收缩率的研究进展`
> 因纯英文词表被误排 —— 记在此处以免后人重踩。

**设计原则**：全部 14,318 行保留在 `paper_meta`，`in_scope` / `scope_tier` /
`scope_evidence` / `provenance_channel` 逐行落库，**判定可复核、可回滚、
不静默丢弃**。

---

## 三、第二个发现：`cited_by_count` 是快照，会泄漏未来

OpenAlex 的 `cited_by_count` 是**当前（2026）快照**，包含 2021-2025 的引用。
拿它当 TRAIN 特征就是把答案喂给模型 —— 而它恰好也是最自然的"高影响"排序键。

**处理**：两个口径同时落库。

| 列 | 含义 | 用途 |
|---|---|---|
| `citation_count` | 2026 快照（含未来引用） | 仅 EVAL 侧 / 参考 |
| `citations_asof_cutoff` | `Σ counts_by_year[year ≤ 2020]` | **TRAIN 侧唯一可用** |
| `counts_by_year_json` | 原始直方图 | P4-2 可任选 cutoff 重算 |

`benchmark.yaml → leakage_policy.forbidden_train_features` 显式禁掉
`citation_count`、`citation_normalized_percentile`、`fwci`。

### 已知截断（必须记住）

OpenAlex `counts_by_year` 只返回约 **15 年窗口**，本池实测 **2012-2026**：

```
W2011295666  1995 年  cited_by_count=406   而 counts_by_year 求和仅 203
```

即 `citations_asof_cutoff` 对 2012 年前的论文**左截断**。故：

- `counts_window_start` / `counts_window_end` 随行保存
- P4-2 若做引用速率类指标，**必须同年份窗口比较**，或改用 TRAIN 内部相对排序

---

## 四、口径设计

### 身份：三个概念，三列

沿用用户 2026-09-12 的裁定（`entity_id` 与 `preferred_identifier` 分离）：

| 列 | 优先级 | 语义 |
|---|---|---|
| `paper_uid` | **OPENALEX > DOI > SCOPUS_EID** | 实体 id（语料以 OpenAlex 为记录源），像 git commit hash，**永不变** |
| `preferred_identifier` | DOI > OPENALEX > SCOPUS_EID | 引用时最优标识，像 branch pointer，可变 |
| `kb_paper_uid` | — | 跨库桥（NULL = 不在 KB），防止出现第二个身份命名空间 |

生成走 `search_engine.identity.make_paper_uid` **唯一出口**，仅把优先级作为
**参数**传入（不是第二套实现）；static guard G2 仍只允许该函数存在一处。
实测 `prefix_and_value_form_match = True`、uid 全唯一、前缀分布
`{openalex: 14318}`。

### 摘要：回填 +79.3%

| 来源 | 篇数 |
|---|---|
| OpenAlex `abstract_inverted_index`（按位置还原） | 9,923 |
| `scopus_cache.db` 回填（OpenAlex 无摘要时） | 1,431 |
| 合计覆盖 | **11,354 (79.3%)** |

`abstract_source` 标注每行来源。scopus_cache 仅用于回填，**不引入新语料**。

### `scopus_eid` 覆盖仅 0.3% —— 这是数据源事实

`scopus_cache.db` 的 92,952 条 `scopus_url` **全部是数字形态**
（`.../publications/105044965840`），**不含** `2-s2.0-*` EID。
故 EID 只能经 KB `paper_identifiers`（249 条）桥接，与池交集 44 篇。
不是构建缺陷，已在 `known_limitations` 记录。

### 排除规则（保留在库、不进 jsonl）

out_of_range(>2025) **887** ｜ no_year **4** ｜ retracted **9** ｜
paratext **16** ｜ 非研究类型 **46** → 纳入 **14,247**

### 两维对账（避免看起来像算术错误）

`split`（时间归属）与 `exclusion_reason`（是否剔出语料）**正交**：
一篇 >2025 的撤稿论文同时属于两个桶。`build_report.json → acceptance.reconciliation`
显式给出 `total = included + excluded` 与 `included = train + eval + out + no_year`。

---

## 五、P4-1 的操作口径

```sql
SELECT * FROM v_photopolymerization;   -- in_scope=1 AND 未排除 AND split ∈ {TRAIN,EVAL}
-- 11,027 行 = TRAIN 6,604 + EVAL 4,423
```

> `in_scope_train` 是 6,636 而视图 TRAIN 是 6,604 —— 差 32 是 `exclusion_reason`
> 那一维（撤稿/paratext/非研究类型），`view_counts` 里已显式列出。

分层采样（用户 Q4）：Layer 1 高影响 **20%** / Layer 2 快速增长 **40%** /
Layer 3 长尾 **40%**，strata 键 `primary_topic`（742 个）。
**Layer 1 排序必须用 `citations_asof_cutoff`，不得用快照。**
**EVAL 侧永不抽取 concept**（只用于对答案）。

`CHANNEL_ONLY` 的 1,118 篇是**已记录未启用**的扩张池，启用前需先评估其主题精度。

---

## 六、验收与回归

- pytest **448 passed**（P0-B2.2 时 399，本步 +49）
- `tests/test_p4_0_dataset.py` 49 项，**合成池、不依赖真库快照**
- 范围污染固化为断言：6 篇全球巨引论文标题必须判为 `OFF_TOPIC`
- 时间切分边界参数化（2020/2021/2025/2026/1857/None）
- **主库全程只读**：`knowledge_base.db` 停在 `2f5733d31e3fe41c…`，未改动

### 本步被自己的守卫抓到两次（说明守卫有用）

1. **G4 静态守卫**命中构建器 `load_scopus_abstract_index` 里的
   `pid[7:] if pid.startswith("scopus:")` → 改走 `scopus_cache_key_value`
   （P0-B1b 刚收口的口径，新代码差一点又开一个口子）。
2. **测试抓到非法 DOI fixture**：`10.1/x` 注册号只有 1 位，
   P0-A 的形态校验正确地拒绝 → 修正为 `10.1000/x`。
   这是"形态校验真的在工作"的正面证据。

---

## 七、已知限制

1. 摘要覆盖 79.3%（无摘要论文在 P4-1 只能靠标题抽取）
2. `scopus_eid` 覆盖 0.3%（数据源不含 EID）
3. `citations_asof_cutoff` 左截断（窗口 2012-2026）
4. 语料来自既有检索缓存，**不是对 OpenAlex 全库的系统抽样** ——
   方向分布受当年检索策略影响（dental materials 2,492 /
   photopolymerization 2,081 / additive manufacturing 1,451）
5. 同题同年 590 行未去重（只标记不合并）；`ADJACENT` 档 4,520 篇
   只有广义高分子证据，可能含非光固化论文 —— 保召回、降精度

---

## 八、下一步

**P4-1 Concept Extraction**（用户已定：不要再回头修基础设施）。
第一版不做 `Search → Learn → Search`，只做
`Paper → extractor → concept graph → trend score → prediction`。
schema：`material / mechanism / method / application / problem / keywords`。
