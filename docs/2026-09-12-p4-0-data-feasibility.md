# P4-0 数据可行性评估（Temporal Benchmark Dataset 前置）

日期：2026-09-12
状态：**评估完成，方案待确认**（P0 已收口，见 `2026-09-12-p0b2-2-type-a-migration.md`）

> 用户 2026-09-12：**立即进入 P4 数据集构建**。
> 本文件是进入前的数据盘点 —— 先确认「时间隔离数据集」在现有资产上能不能直接建，
> 以及真正的第一步是什么。

---

## 1. 结论

**P4-0 可做，但第一步不是"按年份切分"，而是"建一张统一元数据表"。**

原因：现有资产里，**主题相关性**与**年份**分散在不同数据源，没有任何单一来源同时具备两者：

| 数据源 | 规模 | 有 year | 主题相关 | 标识类型 |
|---|---|---|---|---|
| **`openalex_cache.json`（解包后）** | **14,318 work** | **100%** | 需过滤 | W-ID / DOI |
| `scopus_cache.db` | 98,232 | 100% | 需过滤 | `scopus:<DOI>` |
| KB `papers` | 331 | **29.9%** | ✅ 已裁 | canonical uid |
| KB `topic_papers` | 268 | — | ✅ | canonical uid |
| `s6_seen_set` | **21,782 DOI** | **0%** | ✅ 主题 seen | 裸 DOI |
| `s7_community_memory` | **2,806 EID** | **0%** | ✅ 社区成员 | Scopus EID |
| `s8_finalkb_catalog` | 249 | **0%** | ✅ 已收录 | EID / W-ID |
| R06 标注 | 500 | 部分 | ✅ 已盲评 | 混合 |

**两个方向都缺一半**：

* 主题池（`s6_seen_set` 21,782 / `s7_community_memory` 2,806 / catalog 249）—— **有主题，没年份**
* 缓存池（`openalex_cache` 14,318 / `scopus_cache` 98,232）—— **有年份，主题相关性未知**

实测：`s6_seen_set` 的 21,782 个 DOI 只与 `scopus_cache.db` 重合 **394 条（1.8%）**——
这不是小瑕疵，而是说明两条链路（CloakBrowser 抓取的 seen 集 vs 缓存）**没有共同的键**。

---

## 2. 年份可得性的实测数字

### 2.1 `openalex_cache.json`（推荐主池）

它是一个**API 响应缓存**：顶层 725 个键是请求 URL，值是 `{meta, results:[...]}`。
解包后：

```
响应对象 725  ->  results 总条目 18,724  ->  去重 work 14,318  ->  有 year 14,316 (100.0%)
```

（14,318 与 P0 迁移日志里的 `cache_w=14318` 完全吻合 —— 可交叉验证解包正确。）

年份切分（按用户建议的隔离点）：

| 段 | 数量 |
|---|---|
| ≤2010 | 3,409 |
| 2011-2015 | 1,818 |
| 2016-2020 | 2,891 |
| **train (≤2020) 合计** | **8,118** |
| **eval (2021-2025)** | **5,311** |
| >2025（含预发表） | 887 |

### 2.2 `scopus_cache.db`

```
98,232 条，100% 有 year（1936-2027）
  ≤2010      13,929
  2011-2015   6,505
  2016-2020  10,564   -> train 合计 30,998
  2021-2025  36,351   -> eval
```

规模大得多，但它是「S1–S7 检索到的**全部**命中」，**主题相关率未经审核** ——
直接用会把大量离题论文当成"未来方向"。

---

## 3. 三个缺口

| # | 缺口 | 影响 |
|---|---|---|
| **G1** | 主题池与缓存池**没有共同的键**（DOI 形态不一致 / 分属两条链路） | 无法直接给主题池补年份 |
| **G2** | `s8_finalkb_catalog` 与 `s6_seen_set` **零年份** | 已收录/已浏览语料无法参与时间切分 |
| **G3** | concept extraction 只完成极少（`s8_extraction_output` 的 `papers` 段） | P4-1 的成本门槛，见 §5 |

G1 是真正的阻塞点，而它的解法是纯数据工程（不需要 LLM）。

---

## 4. 建议的 P4-0 Step 0：统一元数据表

在**不改主库**的前提下（用户要求"不要直接改主库，建 snapshot"）：

```
datasets/photopolymerization_v1/
  paper_meta.db          <- Step 0 产物：统一元数据（union）
  papers_train.db        <- Step 1：<= 2020-12-31
  papers_future_eval.db  <- Step 1：2021-2025
```

`paper_meta.db` 的推荐 schema：

```sql
CREATE TABLE paper_meta (
    uid          TEXT PRIMARY KEY,   -- 经 paper_writer/identity 的规则，形态合法
    doi          TEXT,               -- 标准化后
    openalex_id  TEXT,               -- W-ID（去 URL）
    scopus_eid   TEXT,               -- 2-s2.0-*
    title        TEXT,
    abstract     TEXT,
    year         INTEGER,            -- NULL 表示年份未知（禁止猜）
    source_json  TEXT,               -- 溯源：哪个缓存/产物贡献了这一行
    topic_status TEXT                -- SEEN / COMMUNITY / CATALOG / R06_LABELED / UNKNOWN
);
```

**关键设计点**：

1. **union 而非 join**：三个池子分别导入，按 identity 规则归并（复用 `search_engine/identity.py`，
   不新写一套归一化）
2. **`year` 允许 NULL，且禁止插值/猜测** —— 时间隔离数据集里一个假的年份比缺一个样本危险得多
3. **`topic_status` 显式标注**：让后续可以「只在主题池上训练 / 在全池上验证"离题论文是否也被预测到"」
4. **`source_json` 溯源**：便于审计"这条记录从哪来的"

预期产出（估算）：

| | 规模 |
|---|---|
| union 后总规模 | 约 10–11 万（去重后） |
| 有 year 且有主题标记 | 取决于 G1 的解决程度 |
| 建议**首版实验语料** | `openalex_cache` 的 8,118 train / 5,311 eval（100% 有年份，先用它跑通链路） |

---

## 5. P4-1 的成本门槛（提前暴露）

Concept extraction 需要 LLM。现状：

| 池 | 需要抽取的篇数 |
|---|---|
| 首版实验（openalex_cache 段） | **13,429**（8,118 + 5,311） |
| 若扩到 scopus_cache | 约 6.7 万 |
| 若只用主题池（seen 21,782） | 21,782 |

**建议**（与用户"防 burn API"的一贯风格一致）：

* **第一阶段只抽 train 段的头部**（按被引数排序取 top-N，N 先取 1,000–2,000），
  跑通「graph → 方向指标 → 预测 → 验证」全链，**先证明链路成立**
* 链路成立后再按预算扩样本
* eval 段**永远不抽 concept**（它只被用来"对答案"：看预测的方向在 2021-2025 是否真的增长）

---

## 6. 待用户确认的四件事

| # | 问题 | 建议 |
|---|---|---|
| **Q1** | 首版实验语料用哪个池？ | `openalex_cache` 解包（100% 有年份，规模适中） |
| **Q2** | 时间切分点 | `<= 2020-12-31` / `2021-2025`（用户已给） |
| **Q3** | 是否先做 G1（建统一元数据表）？ | **是**，它是纯数据工程、不需要 LLM，且后面所有步骤都依赖它 |
| **Q4** | P4-1 首版抽取预算 | 先 1,000–2,000 篇（train 段按被引排序头部），跑通链路再扩 |

> 另外：`s7_community_memory` 的成员是 **EID**（2,806），`s6_seen_set` 是 **DOI**（21,782）——
> 两者之间也需要一张映射才能合并，这属于 G1 的一部分。

---

## 7. 本轮不动主库

本次评估全部为**只读探查**：

* `knowledge_base.db` 未改动（P0-B2.2 迁移后的 `2f5733d31e3fe41c…`）
* `datasets/` 尚未创建
* `openalex_cache.json` / `scopus_cache.db` 仅读取
