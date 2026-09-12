# P4-1A：Concept Extraction（1000 篇）

日期：2026-09-12 ｜ 抽取协议 `p4_1a_extraction_v1` ｜ 数据集 `photopolymerization_v1`
用户裁定：用干净视图（不启用 CHANNEL_ONLY）｜首轮 ~1000 篇｜**分层采样，不随机**｜
extraction 必须输出 **concept + relation**，不只是关键词

---

## 一、交付物

| 文件 | 说明 | 入 git |
|---|---|---|
| `prompts/p4_1a_concept_extraction_v1.md` | 冻结 prompt（6 类 concept + 8 类封闭 relation 词表） | ✅ |
| `datasets/photopolymerization_v1/extraction_spec.yaml` | 冻结协议（prompt/tool/sample 三个 sha256 + 模型参数） | ✅ |
| `datasets/photopolymerization_v1/scope_allowlist.yaml` | 范围白名单（11 纳入 + 9 待裁定） | ✅ |
| `tools/build_p4_1a_sample.py` | 分层采样（A300/B400/C300） | ✅ |
| `tools/extract_concepts_p4_1a.py` | 抽取驱动（`--live` 门禁 / 缓存续跑 / 失败分类） | ✅ |
| `tools/audit_extraction_quality.py` | 三项验收审计 | ✅ |
| `tests/test_p4_1a_extraction.py` | 32 项回归（不调用 LLM） | ✅ |
| `sample_v1.jsonl` / `concepts_v1.jsonl` / `extraction_audit.json` | 派生物 | ❌ gitignore |

```bash
python tools/build_p4_1a_sample.py --apply          # 采样（零 LLM 成本）
python tools/extract_concepts_p4_1a.py --freeze     # 冻结协议
python tools/extract_concepts_p4_1a.py --live --concurrency 10
python tools/audit_extraction_quality.py
```

---

## 二、分层采样（1000 篇，seed=13，三层互斥）

| 层 | 目标 | 排序键 | 目的 |
|---|---|---|---|
| A 高影响 | 300 | `citations_asof_cutoff` 降序 | 成熟 foundational 方向 |
| B 快速增长 | 400 | `primary_topic` 增速 `n(2016-2020)/n(2011-2015)`，基准期 >= 3 篇 | emerging 方向 |
| C 长尾 | 300 | `citations_asof <= p25` 内固定种子打乱 | 避免只看热点 |

**约束**：三层互斥（A → B 去 A → C 去 A∪B）；单主题上限 = 层目标 × 15%；
只抽 TRAIN 且摘要 >= 200 字符（可抽池 = 采样的分母）。

结果：**互斥 1000 篇 / 154 个主题 / 最大单主题 13.3%**。

> 主题上限是必要的：池内 `Dental materials` 占 22.6%、
> `Photopolymerization techniques` 占 18.1%。不加限，Layer B 会被牙科占满
> （实测单层被上限挡下 279 篇）。采样覆盖的增速 Top 主题：
> Additive Manufacturing ×4.88、3D Printing in Biomedical ×3.12、Epoxy Curing ×...

`citations_asof_cutoff` 为 NULL 的 789 篇（窗口外旧论文）**不进 Layer A** —— 无法排序就不参与竞争。

---

## 三、抽取协议

### concept 类型（6 类）

`direction`（研究方向的完整表述）｜`material`｜`mechanism`｜
`fabrication_method`｜`application`｜`challenge`（**未解决的问题**）

`direction` 与 `challenge` 是用户特别加的两类：前者是要预测的目标粒度，
后者是「未来热点往往来自未解决问题」的载体。

### relation：**8 类封闭词表**

`addresses`｜`enables`｜`requires`｜`improves`｜`causes`｜`part_of`｜
`alternative_to`｜`combines_with`，词表外降级为 `other`（**不丢弃**，保留信息）。

封闭词表的目的是让图**可分析**：开放式关系会产生长尾同义词
（"solves"/"addresses"/"tackles"/"mitigates"），使任何按关系类型的统计失效。

### prompt 的硬约束（都在 system 里，且被测试断言）

1. 只依据所给文本抽取，不引入外部知识（含「不利用你对后来发生了什么的了解」）
2. 抽**概念**不是抽关键词（给出反例：`thiol`/`DLP`/`polymer` 是词，
   `thiol-ene photopolymerization`/`shrinkage stress` 是概念）
3. 概念名规范化：小写 / 单数 / 名词短语 / 2-6 词 / 不用论文特有写法
4. 每个概念必须给**文本内证据**片段
5. 关系只能用 8 类；source/target **必须指向已声明的概念**
6. 数量预算：概念 6-18 个、关系 0-12 条（**宁少勿滥**，编不出来就返回空数组）

### 冻结纪律

`extraction_spec.yaml` 同时冻结 **prompt sha256 / tool sha256 / sample sha256** + 模型参数。
改了 prompt 不重跑 `--freeze`，`--live` 会**直接拒绝运行**：

```
[refused] prompt 已变更但未重新冻结：
  冻结 97f7a13e… != 当前 3a1b…
  改了 prompt 必须重跑 --freeze（否则产物与协议脱钩）
```

> tool 哈希是首轮跑完后补进 spec 的 —— 只冻结 prompt 时，同一 prompt 下换校验逻辑
> 会产出不同产物，溯源不完整。补入时已验证：本次唯一的校验器改动
> （拒绝「无字母」的概念名）在实际数据上是 **no-op**（255 个概念名中 0 例）。

### 抽取器行为

`--live` 门禁 / 逐篇缓存 `extraction_cache/<uid>.json` 断点续跑 / 并发 /
**失败分类 5 类**（`JSON_INVALID`/`TRUNCATED`/`SCHEMA_INVALID`/`API_ERROR`/`INSUFFICIENT_TEXT`）/
usage + 成本估算（`llm.py` 只增不改地加了 `last_usage`）。

温度 **0.0**（抽取要可复现，不要创造性）；摘要超 8,000 字符截断并**显式标注**
`[TRUNCATED BY PIPELINE]`（截断是成本决策，必须留痕）。

---

## 四、三项验收（用户指定）

### 1) Coverage —— **PASS**

| | |
|---|---|
| 抽 | 1000 篇 |
| OK | **993（99.3%）** |
| `INSUFFICIENT_TEXT` | 6（书籍/勘误/极老论文，标题+摘要均无实质内容） |
| `JSON_INVALID` | 1 |
| **可抽取覆盖率** | **99.9%**（扣除 6 篇文本不足） |

用户目标 ~95% → 达标。两个口径都给：原始覆盖率会被「输入本身不足」拉低，
那是数据问题不是 pipeline 问题。

### 2) Concept diversity —— **OK**

| | |
|---|---|
| 概念总数 | **10,320** |
| distinct | **7,359（71.3%）** |
| 篇均概念 | **10.39**（中位 10） |
| 关系总数 | **7,441**（distinct 7,322） |
| 关系词表 | **8/8 全部用到**（+ `other` 2 条） |
| 最大单概念占比 | **1.12%**（没有单概念垄断 → 不是关键词统计） |
| 单词概念占比 | 14.1% |

类型分布：`material` 3213 / `challenge` 1749 / `mechanism` 1601 /
`direction` 1403 / `fabrication_method` 1325 / `application` 1029
关系分布：`enables` 1871 / `part_of` 1628 / `addresses` 1251 / `requires` 1155 /
`causes` 917 / `improves` 411 / `combines_with` 110 / `alternative_to` 96 / `other` 2

Top 概念：`photopolymerization` 116、`polymerization shrinkage` 84、
`stereolithography` 71、`dental restoration` 55、`photoinitiator` 45、`shrinkage` 35。

判据（工具内置）：distinct >= 500 且最大单概念占比 < 5% 且篇均概念 >= 4。

### 3) Temporal sanity check —— **逐条核过后：真实污染 0**

这项是本阶段的**方法学收获**，值得完整记录。

**实现**：从**全语料**（含 2021-2025）建 token 首现年索引；概念首现年 =
各 token 首现年取**最大值**（概念不能早于它最晚的词）；> cutoff(2020) 即标异常。

**首轮结果**：23 / 10,320 = 0.22% → 我逐条核了**全部 23 条**：

| 现象 | 例 | 判定 |
|---|---|---|
| 单复数/词形变体 | `balloons`→`balloon`、`thixotropic`→`thixotropy`、`dithioesters`→`dithioester`、`Macrocycles`→`macrocycle` | **度量假象** |
| 轻度改写（文本有据） | "not meeting the desired geometrical tolerances" → `geometrical tolerance violation` | **度量假象** |
| 语料覆盖局限 | `oligonucleotide`(1970s)、`polyvinyl butyral`(1930s)、`organogel`(1990s)、`thixotropy`(1920s 概念) | **度量假象** |

**关键核查**：23 条中，**0 条**的文本不含概念核心词，且 evidence 字段
**全部是论文原文引用**（例：`thixotropy` 的 evidence =
"Shear thinning and thixotropic properties are necessary components of the inks…"）。
即模型是在**照抄**，不是在"穿越"。

**修正度量**：引入 **prefix 模式**（共享前缀 5/6 字符回溯最早年份），
吸收词形变体 → 23 条降到 **1 条**。

**最后 1 条**：2011 年论文抽出 `python scripting` —— 但原文写的是
"an in-house **python** code was used to analyse the shape"，
而 `scripting` 这个词在本语料里 2021 年才首次出现。**同属度量假象。**

> **结论：真实时相污染 = 0/10,320。** prompt 的「不引入外部知识」约束成立。
> 工具同时报 `exact`（严、假阳性多）与 `prefix`（宽、假阴性多）两个口径，
> 并明确要求人工复核剩余项 —— 剩余量足够小（首轮 23 条）可以全看。
>
> **两难必须写明**：`exact` 会被词形变体骗；`prefix` 会让「词族内部换代」漏报
> （`transformer` 会被 `transfer`/`transform` 提前"洗白"）。
> 没有词典就无法自动区分「通用词」与「新领域术语」——
> 所以这个检查的价值在于**筛出少量待复核项**，不是给一个精确污染率。
>
> **另一个必须记住的口径**：首现年索引建在 14,318 篇的**窄主题语料**上，
> 不是全部文献。「语料首现年 > cutoff」≠「世界上首次出现 > cutoff」。

---

## 五、成本

| | |
|---|---|
| 调用 | 1000 次（并发 10） |
| token | 输入 703,088 / 输出 1,461,193 |
| 估算成本 | **$1.80** |
| 墙钟 | **6 分 38 秒** |
| 均延迟 | 9.5s/篇 |

单篇约 $0.0018 —— 扩展到 5,545 篇 TRAIN（in_scope 全集）约 **$10**。
价格按 2026-09 公布价估算，工具里明确标注为**预算护栏**而非账单口径。

第一次 pilot（30 篇，$0.05）在范围门修正后被**主动作废并清理缓存** ——
避免新旧门的数据混在同一个产物里。沉没成本 $0.05。

---

## 六、校验层真实拦下的东西

抽取器对 LLM 输出做了二次校验（prompt 是要求，不是保证）。全量运行拦下：

| 项 | 数量 |
|---|---|
| `RELATION_UNDECLARED_ENDPOINT`（指向未声明概念） | 25 |
| `RELATION_BAD_ENDPOINT` | 6 |
| `CONCEPT_BAD_NAME` | 5 |
| `RELATION_OUT_OF_VOCAB`（降级 `other`） | 2 |
| `CONCEPT_BAD_TYPE` | 1 |
| `RELATION_SELF_LOOP` | 1 |

即 **每 1000 条关系里约 4 条越界**。数量小，但若不清洗，悬空边会直接污染
图的连通性统计。

---

## 七、本阶段被自己的测试抓出的 bug

1. **`normalize_concept_name("!!")` 返回 `"!!"`** —— 无字母的串被判为合法概念名。
   已加「至少一个字母或汉字」+ 广义首尾标点剥离。
2. **时相检查里 token 正则含连字符** → `thiol-ene` 成为永久 OOV →
   在 `unknown -> continue` 处**被静默跳过**，整条检查失效。
   已改为纯字母 `[a-z]{3,}`（语料侧与概念侧同口径）。
3. **同名函数重复定义** → Python 后定义者静默覆盖前者，新签名不生效
   （`unexpected keyword argument 'mode'`）。已删除旧实现并留注释警示。
4. **YAML 主题名含冒号**（`Hydrogels: synthesis, ...`）未加引号 → 整份白名单解析失败；
   若被吞成空集，范围门会**静默退化**成纯词面门。已加资产测试。

---

## 八、下一步：P4-2 Trend Prediction

输入已就绪：`concepts_v1.jsonl`（993 篇 TRAIN 侧的概念图）+ `paper_meta.db`。

```
TRAIN concept graph  ->  方向指标  ->  Emergence Score  ->  Top-K 预测
                                              |
                                     EVAL (2021-2025) 验证
```

**反泄漏红线（P4-2 必须遵守）**：
- 引用类特征只用 `citations_asof_cutoff`，**不得**用 `citation_count` 快照
- 概念图只建在 TRAIN 侧；EVAL 侧概念**只用于对答案**，不得参与任何指标计算
- `stereolithography` 等高频概念需按年份切片，避免用末期高频反推早期趋势

技能 `temporal-benchmark-dataset` 已按本阶段实测更新（主题门 > 词面门、
`re.X` 空格坑、时相检查的度量陷阱）。
