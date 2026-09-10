# P0-A 统一身份层交付报告

- 日期: 2026-09-10
- 状态: **已落地真库并通过 13/13 验收**
- 范围: 只建立身份层（`paper_identifiers` + `identity_conflicts`）
- 前置: `docs/2026-09-10-identity-and-event-db-proposal.md`（诊断与分期方案，用户四项决策 + 三条纠正）
- 风险定性（用户要求改写）: **低风险、可回滚、暂不影响现有读路径** —— 不是字面意义的「零风险」

---

## 1. 交付物

| 文件 | 作用 |
|---|---|
| `search_engine/identity.py` | 身份层纯函数核心：标识归一化、回填计划、冲突判定、口径统计 |
| `tools/migrate_p0a_identifiers.py` | 迁移：备份 → 单事务建表 + 回填（默认 dry-run，幂等） |
| `tools/verify_p0a_identifiers.py` | 13 项验收（在副本或真库上跑） |
| `tools/audit_identity_gaps.py` | 缺口量化：写入者证据 / 反向解析覆盖 / 未解决缺口 |
| `tests/test_p0a_identity.py` | 36 项回归（归一化、错位、认领冲突、幂等、回滚、真库不变式） |
| 备份 | `data/cache/knowledge_base.db.preP0A.db`（迁移前，3.2MB） |
| 产物（本地，不入 git） | `data/exports/schema/p0a_{identity_migration,acceptance,gap_audit}_report.json` |

---

## 2. 用户三条纠正的落地

### 2.1 `relation_type` 移出这两张表 ✅

原草案把 `IDENTICAL/VERSION_OF/ERRATUM_OF/PART_OF` 放进标识表 —— 错误：那是**论文实体间关系**，
属后续 `paper_relations(source_uid, relation_type, target_uid)`。
`identity_conflicts` 现在只描述**标识认领冲突**，`conflict_type` 取值收敛为：

| conflict_type | 含义 | resolution_status |
|---|---|---|
| `IDENTIFIER_ALREADY_OWNED` | 该标识已被另一个 uid 认领 | UNRESOLVED |
| `MISPLACED_IDENTIFIER` | 值本身是**合法标识**，但出现在错误的列/前缀 | REVIEW_REQUIRED |
| `INVALID_IDENTIFIER` | 原值非空但**匹配不上任何**已知标识类型 | REVIEW_REQUIRED |
| `TITLE_COLLISION_CANDIDATE` | 同题候选（仅证据） | REVIEW_REQUIRED |
| `NO_IDENTIFIER` | 该论文无任何有效外部标识 | REVIEW_REQUIRED |

补了 `MISPLACED_IDENTIFIER`：实测发现「可识别的错位」与「不可识别的垃圾值」处理路径完全不同
（前者可恢复、后者只能丢弃），合并成一个类型会丢诊断信息。

### 2.2 title 同题不自动合并 ✅

同题只产出 `TITLE_COLLISION_CANDIDATE` + `REVIEW_REQUIRED`，两条 uid 全部保留。
provenance 里写明「title equality is evidence only; cannot decide IDENTICAL」。
验收断言 `no_auto_merge`：冲突表中不存在 `RESOLVED_MERGED`。

### 2.3 口径统一 ✅

身份判断全部基于 **normalize 通过的有效值**，不是「列非空」。旧提案里的数字已废弃并重算：

| 术语 | 本报告值 | 说明 |
|---|---|---|
| `W_PRIMARY` | 25 | `paper_id` 前缀为 `openalex:` |
| `W_TRUE_ONLY` | **7** | 有有效 W-ID，无有效 DOI 且无有效 EID |
| `MULTI_ID_WITH_W` | **76** | 有有效 W-ID 且有其他有效身份（旧文档的 58 是「主键为 doi: 且带 W-ID」，口径不同） |
| `NO_DOI` | **43** | 无有效 DOI（旧文档的 13 是「doi 列为空」，漏掉 30 条列里装 EID 的行） |
| `EFFECTIVE_DOI_PRIMARY` | 270 | 前缀 `doi:` 且 DOI 值确实有效 |
| `UID_PREFIX_MISMATCH` | **30** | uid 前缀声明的类型与可用身份不符 |
| `NO_IDENTIFIER` | 0 | 每篇至少一个身份通道 |

**关键约束**：`EFFECTIVE_DOI_PRIMARY = DOI_PRIMARY − UID_PREFIX_MISMATCH` 必须恒成立，
且 terms 与写进 `paper_identifiers` 的身份**完全同源**（都由 `collect_row_claims` 派生）。
首版实现中 `compute_terms` 只看原始列、`plan_backfill` 含恢复与 uid 自证 —— 两套口径打架，
被测试 `test_uid_prefix_mismatch_detected` 抓住后已重构。**双口径本身就是多事实源的复发路径**。

---

## 3. 实测发现的三类真实数据问题

### 3.1 30 条「DOI 列装 Scopus EID」（最严重）

```
paper_id        = doi:2-s2.0-85056983120      ← 前缀错标为 DOI
papers.doi      = 2-s2.0-85056983120          ← 装的是 EID
papers.scopus_eid = 2-s2.0-85056983120        ← 同一值（正确承载）
```

- 来源：`migrate_v2_schema.py` 的 `norm_doi()` 不校验格式，Scopus 记录的 doi 字段有时是 EID，
  于是 `canonical_paper_id()` 派生出错误前缀；
- 影响：这 30 个 uid 被 `topic_papers` 引用 30 行（挂有 relevance 判定），
  另有 0 行在 `knowledge_records` / `route_mechanism_edges` —— **uid 不可就地改写**；
- P0-A 处理：EID 身份照常回填（`source=papers.scopus_eid`，confidence 1.0，`is_primary=0`）；
  错位事实写成 30 条 `MISPLACED_IDENTIFIER` 冲突；uid 前缀不符另记 `UID_PREFIX_MISMATCH=30`。
- **未做**：uid 前缀更正（需另立阶段，改 uid 会断引用）。

### 3.2 3 组同题（版本关系候选）

| 组 | 成员 | 性质 | 需要的判定 |
|---|---|---|---|
| 1 | `10.1021/acs.macromol.4c02601` / `.s001` | supporting information | `PART_OF`（**不该合并**） |
| 2 | `10.1002/pc.29332` / `10.2139/ssrn.4918808` | Wiley 正式版 / SSRN preprint | `VERSION_OF`（保留双 uid） |
| 3 | `10.1002/pi.6364` / `10.34726/3441` | 机构库 / 正式版 | 人工确认（可能是镜像） |

这三行冲突记录就是待建 `paper_relations` 的**直接输入**。

### 3.3 归一化必须收紧

首版 `URL`/`ARXIV`/`ISBN` 归一化过宽（任意非空字符串都能通过），
导致 `detect_actual_type("garbage")` 返回 `ARXIV`、`"10.1/a"` 被判为 `ISBN`，
从而把无效 DOI 悄悄「恢复」成错误身份。已全部加形态校验：

- DOI `^10\.\d{4,9}/\S+$`（注意注册号需 4–9 位 —— `10.1/x` 非法）
- OPENALEX `^W\d+$`、SCOPUS_EID `^2-s2\.0-\d+$`、ARXIV `\d{4}\.\d{4,5}` 或旧式、ISBN 10/13 位、URL 需 host 形态

---

## 4. 验收结果（13/13 PASS，真库与副本各跑一次）

| # | 标准 | 结果 |
|---|---|---|
| 1 | 迁移前后 papers 行数不变 | PASS 331 → 331 |
| 2 | 不修改任何现有 paper_id | PASS 序列一致 + 全行指纹一致（`b1accaf4aa6b…`） |
| 3 | 不修改 S1–S8 冻结产物 | PASS `knowledge_records` 216→216、`route_mechanism_edges` 700→700、`topic_papers` 268→268、`knowledge_claims` 1283→1283 |
| 4 | 每个合法标识均标准化后回填 | PASS 列值 620 + 前缀自证 301，缺失 0 |
| 5 | 同一外部标识只能有一个 owner | PASS 620 键全唯一，多 owner 键 0 |
| 6 | 冲突不覆盖，完整进入 identity_conflicts | PASS 33 行冲突，原值被覆盖 0，自动合并 0 |
| 7 | W-only / EID-only 实体正常回填 | PASS `W_TRUE_ONLY=7` 全部回填，EID-only 异常 0 |
| 8 | 重复运行不新增 identifier / conflict | PASS (620, 33) → (620, 33) |
| 9 | dry-run 不产生任何数据库写入 | PASS md5 不变 + 表集合不变 |
| 10 | 单事务完成，失败完全回滚 | PASS 注入违反约束的 DDL → 异常抛出 + md5 不变 + 新表未残留 |
| 11 | 输出回填/冲突/未承载统计 | PASS 见 §5 |
| 12 | 随机抽查 DOI/W-ID/EID 反向解析 | PASS 三类各 5 条全部回解到原 uid |
| 13 | 旧读路径行为与迁移前一致 | PASS papers/topic_papers/knowledge_records 计数与顺序一致 |

全量回归 `pytest tests/` = **328 passed**（含新增 36 项）。

---

## 5. 缺口量化（用户要求：证明写入者 + 反向解析 + 量化未解决项）

### A. 写入者证据（对比 `search_queries`/`paper_retrievals` 两张 0 行空表）

| 表 | 行数 | 明细 |
|---|---|---|
| `paper_identifiers` | **620** | DOI 288 / OPENALEX 83 / SCOPUS_EID 249；source 全部来自 papers 三列；`is_primary=1` 331 个（每 uid 恰好一个） |
| `identity_conflicts` | **33** | `MISPLACED_IDENTIFIER` 30 / `TITLE_COLLISION_CANDIDATE` 3；状态全为 `REVIEW_REQUIRED` |

### B. 反向解析覆盖

**921 个有效身份（含 uid 前缀自证）→ 缺失 0，覆盖 100.000%。**

### C. 仍未解决的缺口

| 缺口 | 量化 | 归属阶段 |
|---|---|---|
| C1 版本关系 | 3 组同题（SI 1 / preprint↔published 1 / 待人工确认 1） | `paper_relations` |
| C2 历史 JSON | 扫 229 个 JSON，唯一身份 **53,687**，未导入 **53,118** | P0-B / P1 |
| C3 错位列 | 30 条冲突，涉及 30 个 uid | 需 uid 前缀更正阶段 |

**C2 必须按阶段读，否则会误判为「丢了 5 万篇」**：

| 阶段 | 文件 | 唯一身份 | 覆盖率 | 未导入 |
|---|---|---|---|---|
| S8（最终 KB 收录） | 2 | 462 | **100.0%** | **0** |
| S6（pilot / QA） | 4 | 2,892 | 7.5% | 2,675 |
| AUDIT（R05/R06 frame） | 8 | 388 | 11.1% | 345 |
| S7（execute / keep pool） | 20 | 7,497 | 3.8% | 7,214 |
| S1–S5（检索原始层） | 24 | 41,595 | 0.3–0.8% | 41,395 |
| OTHER / BRIDGE / CANDIDATE | 27 | 19,119 | 0.4–10.3% | 19,021 |

**S8 = 100.0% 覆盖**是 P0-A 目标达成的最强证据：**已入 KB 的论文身份 100% 落在身份层**。
S1–S5 的低覆盖不是「丢失」——那些是检索原始命中（universe），本就不全是 KB 成员；
真正的问题是**它们的身份没有统一记录，因此无法回答「这篇我们是否见过/判过」**，
这正是 P0-B（`retrieval_events` + `screening_decisions`）的职责。
AUDIT 组 11.1% 更能反映「已判定但未入身份层」的缺口。

---

## 6. 明确未做（P0-A 边界）

- preprint ↔ published **合并**（用户决策 1：不合并，保双 uid，用 `VERSION_OF` 关联）
- supporting information ↔ 正文关系
- title-based 自动合并（用户纠正 2）
- 历史 JSON 一次性逆向导入（导入需记 `source_file` / 文件 hash / 原始 record locator / 导入时间 / importer version / run/stage / 冲突状态）
- query / retrieval 事件回填（`search_queries` / `paper_retrievals` 仍为 0 行）
- Candidate / KB 状态统一
- `scopus_cache.db`（713MB）**不并库** —— 可重建的检索缓存，非权威事实源

---

## 7. P0-B 准入条件（用户要求：不要立刻上 P0-B）

进 P0-B 前必须先满足：

1. 两张表**确有写入者** ✅（620 / 33，source 可追溯）
2. **可反向解析全部已知身份** ✅（100.000%，缺失 0）
3. **版本关系与历史 JSON 缺口已被量化** ✅（§5 C1/C2/C3）
4. 本阶段**不改任何现有读路径** ✅（验收 13 + 旧路径一致）

再叠加一条本阶段新发现的前置：**P0-B 的第一张表必须同时解决「uid 前缀错标」的写入口**，
否则新写入者会继续把 EID 写进 DOI 列（30 条的根因是写入侧无校验，不是读取侧）。
