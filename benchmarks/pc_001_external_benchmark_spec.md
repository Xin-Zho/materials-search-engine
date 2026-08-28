# pc_001 External Benchmark 构造协议 v2（P3 评价设计，已冻结 2026-08-27）

> 用户定 2026-08-27（v2 修订：QGS/Must-Hit 分离、era/role 拆字段、去 difficulty、
> review discovery 多入口）。方法学依据：relative recall / quasi-gold standard
> （2006 Cochrane 105 reviews reference set；2015 16 综述 → 298 trials gold set）。
> 主评价 = Relative Recall；Scopus stratified audit 仅作可选 P3+。

## 0. 核心原则

> **External QGS 要尽量"自然产生"，不要人为塑造成漂亮、均衡、容易解释的
> benchmark。** 独立权威综述 → 自然产生 candidate → 固定 relevance criteria 筛
> → 去重 → Scopus eligibility → Freeze。绝不为了 route/era 平衡去补论文。

## 1. 两套独立集合（关键修改①）

| | External QGS（主指标分母） | Expert Must-Hit Set（单独报告） |
|---|---|---|
| 来源 | 独立权威综述人工确认 relevant 的论文 + 预定义规则下的 citation chasing | 10–30 篇领域专家认为绝不能漏的奠基/经典 |
| 指标 | `RR = |R ∩ QGS_Scopus| / |QGS_Scopus|` | `MustHitRecall = |R ∩ MustHit| / |MustHit|` |
| 回答 | 对独立构造的相关文献集合整体找回多少 | 会不会漏掉专家认为绝不能漏的论文 |
| 为什么分开 | 人工命题会污染 RR 分母 | 经典重要，但它是另一个问题 |

## 2. Development Benchmark（不用于主 RR）

- `benchmarks/benchmarks_v1.json`（15 篇）+ 67 篇 DEV/HOLDOUT split（已冻结）
- 用途：regression / debug / failure analysis / Agent development

## 3. 标注 schema（关键修改②：era 与 role 拆开）

```json
{
  "doi": "10.1016/...",
  "title": "...",
  "year": 1997,
  "era": "PRE_2006 | 2006_2020 | POST_2020",
  "role": "FOUNDATIONAL | REPRESENTATIVE | FRONTIER",
  "route": "thiol-ene | ring-opening | AFCT | filler | monomer-design | dual-curing | RAFT | other | null",
  "in_scopus": true,
  "scopus_eid": "2-s2.0-...",
  "sources_from": ["review:<doi>", "citation_chase:<from>"],
  "inclusion_reason": "人工筛 selected/relevant 依据（每篇必填，可审计）"
}
```

- `era`：客观属性（按年份），程序自动算
- `role`：人工/来源定义——老论文不一定是 foundational，新论文不一定是 frontier
- 报告时分别输出：Recall by era / by role / by route / by source review
- **无 difficulty 字段**（修改③，方案 B）：评价结束后对 missed 做 failure analysis
  分类（LEXICAL_GAP/DIRECTION_GAP/...），不预先标结果驱动的标签

## 4. Review discovery 多入口（关键修改④）

单条 `photopolymerization AND shrinkage` 有 lexical bias——只找得到明确写这两个词的
综述。用 6 条独立 broad problem query（**不含 route 名**，避免依赖 Agent 已发现路线）：

```
DOCTYPE(re) AND TITLE-ABS-KEY(photopolymer* AND shrinkage)
DOCTYPE(re) AND TITLE-ABS-KEY(photocur* AND shrinkage)
DOCTYPE(re) AND TITLE-ABS-KEY("polymerization stress" AND photocur*)
DOCTYPE(re) AND TITLE-ABS-KEY("volume contraction" AND photopolymer*)
DOCTYPE(re) AND TITLE-ABS-KEY("photocurable resin" AND shrinkage)
DOCTYPE(re) AND TITLE-ABS-KEY("vat photopolymerization" AND shrinkage)
```

流程：6 query union 去重 → 20–30 篇 review candidates → 按 5 条标准冻结 5–10 个
source reviews：①真正覆盖本课题 ②系统/全面 ③未参与 Agent 开发 ④年代多样
⑤来源独立（不同社区）。**已用综述（W3026448945/W4280619650）不得入选。**

## 5. 构造流程（B1→B4）

```
B1  多入口找 20–30 篇 review candidates → 冻结 5–10 个 source reviews
B2  提取 references → candidate benchmark papers
    ⚠️ 不是全部 references 自动 relevant：按【预冻结 inclusion criteria】
    人工 relevance screening（经典 relative-recall 只用综述"已纳入研究"）
B3  去重：DOI > Scopus EID > title+year fallback
B4  Scopus eligibility：分别保存 B_total / B_scopus / B_not_scopus
    （B_not_scopus = database coverage limitation，不进分母、不进 failure analysis）
→ 冻结 pc_001_external_qgs_v1.json（版本/时间戳/来源清单/评审记录）
→ 此后只作评价，禁止调参
```

## 6. 规模目标

- **External QGS：target 150–250 unique Scopus-indexed relevant papers**
  （分母是 B_Scopus——总量做 150、30 篇不在 Scopus 就只剩 120）
- route-level RR：只对"来源自然产生且样本充足"的 route 报告；
  n 小的（如 n=4）报告 4/4 但标注 `small subgroup, descriptive only`
- **不为平衡 benchmark 而补论文**（那是额外 diagnostics，不污染主分母）

## 7. Reviewer 规范

优先：Reviewer A（领域专家）+ Reviewer B（另一名研究者），disagreement →
adjudication。
人力不足时最低标准：单一领域专家 + 预冻结 inclusion/exclusion criteria +
每篇保存 inclusion_reason（可审计）。

## 8. 评价输出

- `RR_overall`（主指标）+ Recall by era / role / route / source review
- `MustHitRecall`（Expert Must-Hit Set 单独报告）
- Failure analysis：对 `B_Scopus − R` 逐篇分类
  `DIRECTION_GAP`（没发现方向）/ `QUERY_GAP`（方向知道但无命中 query）/
  `RETRIEVAL_RANKING_GAP`（命中但没进候选）/ `SCREENING_FALSE_NEGATIVE`（候选被筛掉）/
  `IDENTITY_ERROR`（找到但匹配失败）/ `METADATA_GAP`（metadata 导致难检索）
- 复用 goldset.py 分层引擎（升级 role/era/route/source 维度 + in_scopus 过滤）

## 9. 现有资产角色

| 资产 | 角色 |
|---|---|
| benchmarks_v1.json（15 篇） | Development benchmark（regression） |
| pc_001_dev_holdout.json（67 篇） | Development benchmark（regression） |
| goldset.py | 分层引擎 → 升级为 P3-C 主引擎 |
| tools/fetch_review_candidates.py | B1 多入口综述候选拉取 |
| tools/probe_scopus_sampling.py | 搁置（P3+ 可选层再用） |

## 10. B3 冻结：inclusion/exclusion criteria v1（用户 2026-08-27，先于 screening 冻结）

### Inclusion（一篇论文满足以下任一即 RELEVANT）
直接研究、测量、解释或提出机制/材料/配方/工艺来影响**光固化或光聚合体系**中的：
- 聚合收缩 / 体积收缩 / 线性收缩 / 固化收缩
- 收缩应力 / 聚合应力 / 固化诱导应力
- 上述问题的直接物理成因（如固化动力学、转化率-收缩耦合）或缓解机制
  （如低收缩单体、添加剂、填料、工艺控制、应力吸收层）

### Exclusion（满足任一即 IRRELEVANT）
- 只提到 photocuring/photopolymerization，完全不研究 contraction/stress
- 只研究机械/光学/生物性能，且与收缩机制无直接联系（除非收缩是其研究变量）
- 纯应用文章，shrinkage 仅作背景一句话带过
- **source reviews 自身**：7 篇 source reviews 不进 benchmark（避免来源循环；
  但 source review 引用的"已纳入研究"论文正常进入）

### UNRESOLVED
- 边界案例：摘要不足以判断 → UNRESOLVED，由 Reviewer 决策并记录 reason

### 评审规范
- Reviewer：领域专家（用户），可选第二位研究者 adjudication
- 每篇必存 `inclusion_reason` / `exclusion_reason`
- **先 relevance 后 Scopus eligibility**（B4→B5）：所有判定 RELEVANT 的论文
  再查 Scopus 收录，分别报告 B_total / B_scopus / B_not_scopus
