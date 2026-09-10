# 统一身份层与事件库：诊断 + 分阶段方案

- 日期: 2026-09-10
- 状态: **PROPOSAL（待用户拍板）**——不含任何库写入，纯诊断 + 设计
- 起因: 用户指出「系统存在多个事实来源，同一论文在不同文件里有不同 id / 不同 relevance / 不同 promotion 状态，R06 丢 1083 个 W-ID 是直接结果」，建议统一为事件数据库（17 表）并把 `paper_identifiers` 做成多行关系表
- 结论预告: **核心诊断成立、方向正确；但 17 表不应一次全建**（现有库里已有 2 张 0 行空表作为前车之鉴）。建议按「身份层 → 事件层 → 审计层」三批推进，首批 P0-A 只做 `paper_identifiers` + 回填 + 冲突诊断，零风险可逆。

---

## 1. 诊断（实测数据，2026-09-10）

### 1.1 事实源现状

| 载体 | 规模 | 承载身份/状态 |
|---|---|---|
| `data/cache/knowledge_base.db` | 3.2 MB | papers 331 / topic_papers 268 / knowledge_claims 1283 / knowledge_records 216 / route_mechanism_edges 700 / audits 1 / topics 1 / **search_queries 0 / paper_retrievals 0** |
| `data/cache/scopus_cache.db` | **713 MB** | api_cache 878 / papers 98232（引擎原始层，normalized_json）/ search_log 1633 |
| `data/cache/*.pre*.db` | 4 个备份 | preP0 / preRun2 / preS8 / preDisposition |
| JSON 事实源 | **270 个文件** | candidate / staging / registry / provenance / trajectory / audit / completeness_labels |
| 空壳文件 | 2 个（0 B） | `data/knowledge_base.db`、`data/scopus_cache.db`（误导性，应删或在 README 标注） |

**已建但为空的事件表**：`search_queries`(0)、`paper_retrievals`(0)——事件层的意图早就有了，但没有真实写入者。这是「先建表后找生产者」的直接证据，也是本方案反对一次上 17 表的依据。

### 1.2 身份分裂实测

**(a) 主键命名空间混用**（`papers.paper_id`）：

| 前缀 | 行数 |
|---|---|
| `doi:` | 300 |
| `openalex:` | 25 |
| `scopus:` | 6 |

同一篇论文先由 OpenAlex 发现 → `openalex:W...`；先由 Scopus/DOI 发现 → `doi:...`。**身份取决于发现路径，不取决于论文本身**。

**(b) 三字段的填充率与空洞**（`papers.doi / openalex_id / scopus_eid`）：

| 指标 | 值 |
|---|---|
| doi 有值 | 318 / 331 |
| openalex_id 有值 | 83 |
| scopus_eid 有值 | 249 |
| 三字段全空 | 0 |
| 主键为 `doi:` 但带 W-ID | 58（双身份已绑，但无关系表） |
| `openalex:` 主键且**无 doi 无 eid** | 7（纯 W-only，在 doi 主导链路里无处安放） |

→ W-only 记录在「主键=doi」的体系里没有承载位。**这不是 schema 缺表这么简单，而是所有序列化路径都要能带 W-ID**（详见 1.3）。

**(c) 已经发生的重复入库**（title 完全相同）：

| 组 | 两个 uid | 性质 |
|---|---|---|
| 1 | `doi:10.1021/acs.macromol.4c02601` vs `...4c02601.s001` | 正文 vs **supporting information**（不该合并为同一作品） |
| 2 | `doi:10.1002/pc.29332`（Wiley 正式） vs `doi:10.2139/ssrn.4918808`（**SSRN preprint**） | **版本关系**（应关联，是否同 uid 需语义裁决） |
| 3 | `doi:10.34726/3441`（TU Wien 机构库） vs `doi:10.1002/pi.6364`（正式） | **版本关系** |

3 组 / 6 行。列内唯一性都没问题（doi/openalex/eid 各自 0 重复），**问题出在跨列无关系表 + 版本语义完全没有建模**。

### 1.3 关于「R06 丢 1083 个 W-ID」的准确归因

需要区分两件事，否则 P0 做完以为问题已解：

1. **序列化层 bug（直接原因，已修）**：candidate 文件生成时未携带 W-ID 字段 → W 键在传递中丢失。这是工程缺陷，修字段传递即可。
2. **身份层缺陷（结构性原因，本方案治理）**：即使字段传下来，W-only 记录在 `paper_uid = doi:...` 的体系里也**没有位置**（无 doi 就无主键），所以任何 doi 主导的链路都会系统性丢 W-only 记录。

→ 身份关系表是**必要不充分**：必须同时让读写路径都走 `paper_identifiers`。

### 1.4 状态分裂事实

`topic_papers` 全部 268 行状态组合只有两种：

```
(RELEVANT, promoted) 189
(UNCERTAIN, promoted) 79
```

**IRRELEVANT 的筛选决策一条都没有落库**——即"某篇被看过并判为无关"这个事实在库中完全不存在，只存在于 JSON。这直接影响：重复劳动检测、recall 分母核算、审计 frame 重建。用户点出的 `screening_decisions` 缺失**完全成立**。

---

## 2. 对 17 表草案的评估

| 表 | 判定 | 理由 |
|---|---|---|
| `papers` | 已有，需改造 | 去掉 doi/openalex_id/scopus_eid 三列（迁入 identifiers），`paper_uid` 改为不可变代理键 |
| **`paper_identifiers`** | **P0-A 立即做** | 治本。见 §3 的四个补充设计 |
| `topics` | 已有（1 行） | 保持，补 rubric/frame 引用 |
| `search_runs` | 已有（1 行） | 保留 |
| `queries` / `query_executions` | 已有 `search_queries`（0 行） | 合并为一张（registry 是定义、execution 是事实），避免又一张空表 |
| `retrieval_events` | **P0-B** | 现有 `paper_retrievals` 0 行；需要真实写入者（pilot 落 records 时同步写） |
| `topic_paper_states` | 已有 `topic_papers`，需补状态机 | 现在只有 promoted 终态；要能表达 seen/rejected/pending |
| `screening_decisions` | **P0-B（优先级高）** | I 决策丢失是实测缺陷（§1.4） |
| `extraction_jobs` | **延后** | 当前无 job 生命周期写入者；`knowledge_records`(216) 已承载产出。建了就是空表 |
| `knowledge_claims` | 已有 1283 行 | 改引用 `paper_uid` |
| `claim_evidence` | **延后** | 现有 `knowledge_records.record_json` 已含证据；拆分需先明确 claim↔evidence 基数 |
| `discovery_hypotheses` | **延后** | S7 planner 的 hypothesis 目前活在 runs/ JSON；先落 `retrieval_events` 再谈 |
| `audit_frames` / `audit_samples` / `audit_labels` | **P0-C** | 现在只有 `audits` 1 行汇总数字 → **样本级证据丢失**，与项目"冻结裁决不可被重算覆盖"的纪律冲突 |

**裁剪原则**：**每张表必须有真实生产者，否则不建**。`paper_retrievals` / `search_queries` 两张 0 行表的教训在先。

---

## 3. `paper_identifiers` 设计（含草案未覆盖的 4 点）

用户草案：

```
paper_uid | id_type | id_value | source | confidence
```

**补充 1 — uid 不可变 + 合并走关系，不做就地改写**
```
paper_uid 一旦分配永不变更。preprint↔published 合并 = 新增合并事件 + 指定 canonical uid，
旧 uid 保留为 alias（merged_into 字段）。否则 S1–S8 所有历史冻结证据的引用全部断裂。
```

**补充 2 — 版本语义必须显式，不能只有"同一篇"**

草案列了「同论文多个版本 / preprint 与正式发表」，但 §1.2(c) 第 1 组说明：supporting information（`.s001`）与正文**不该合并**。建议 identifier 关系带类型：

```
relation_type ∈ { IDENTICAL, VERSION_OF, ERRATUM_OF, PART_OF }
```
`VERSION_OF` 指同一作品不同版本（SSRN preprint → Wiley 正式），`PART_OF` 指 SI/附录。合并策略按类型分档：IDENTICAL 直接并，VERSION_OF 关联但保双 uid，PART_OF 不并。

**补充 3 — 冲突不覆盖，记事件**

DOI 冲突（同 uid 两个不同 DOI）时**不是更新字段**，而是写 `identity_conflicts` 记录（谁、何时、两个值、来源）。否则诊断信息被静默吞掉——这正是"多事实源"问题的复发路径。

**补充 4 — 无 doi 的记录必须能成为一等公民**

W-only / EID-only 记录直接以对应 identifier 建 uid（`openalex:W...` 本身就可以是 canonical uid），不要求先有 DOI。这直接消除 §1.3 第 2 类丢失。

**DDL 草案（P0-A）**：
```sql
CREATE TABLE paper_identifiers (
  paper_uid      TEXT NOT NULL,
  id_type        TEXT NOT NULL CHECK (id_type IN
                   ('DOI','SCOPUS_EID','OPENALEX','PUBMED','ARXIV','ISBN','URL')),
  id_value       TEXT NOT NULL,
  source         TEXT NOT NULL,          -- Crossref/Scopus/OpenAlex/LLM/Manual
  confidence     REAL NOT NULL DEFAULT 1.0,
  relation_type  TEXT NOT NULL DEFAULT 'IDENTICAL'
                 CHECK (relation_type IN ('IDENTICAL','VERSION_OF','ERRATUM_OF','PART_OF')),
  first_seen_run TEXT,
  created_at     TEXT NOT NULL,
  PRIMARY KEY (id_type, id_value)        -- 一个标识符只能指向一个 paper_uid
);

CREATE TABLE identity_conflicts (       -- 补充 3
  conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
  paper_uid TEXT NOT NULL,
  id_type   TEXT NOT NULL,
  existing_value TEXT, incoming_value TEXT,
  source    TEXT, detected_at TEXT, resolution TEXT
);
```

`PRIMARY KEY (id_type, id_value)` 是关键护栏：**同一 DOI/W-ID 不可能被两个 uid 认领**，重复入库在写入层即被拒（可发现 §1.2(c) 那 3 组）。

### ⚠️ P0-A 实施修订（2026-09-10 已落地，以下 DDL 草案作废）

用户 2026-09-10 复核后指出三处并已全部修正，**实际 DDL 见 `tools/migrate_p0a_identifiers.py`**，
交付与验收见 `docs/2026-09-10-p0a-identity-layer-delivery.md`：

1. **`relation_type` 不属于这两张表** —— 版本关系（`IDENTICAL`/`VERSION_OF`/`ERRATUM_OF`/`PART_OF`）
   是论文实体间关系，移入后续 `paper_relations(source_uid, relation_type, target_uid)`。
2. **唯一约束必须落在标准化值**：`PRIMARY KEY (id_type, normalized_value)`，原始值存 `id_value`；
   否则 DOI 大小写、`https://doi.org/` 前缀、W-ID URL 形式仍会产生重复。
   另补 `is_primary`（每 uid 恰好一个）、`confidence`、`created_at`。
3. **title 同题不得自动合并** —— 只产出 `TITLE_COLLISION_CANDIDATE` + `REVIEW_REQUIRED`，
   自动合并只能依赖高置信身份规则。
4. `conflict_type` 增加 `MISPLACED_IDENTIFIER`（值合法但列/前缀错位，可恢复），
   与 `INVALID_IDENTIFIER`（无法识别，不可恢复）区分。

另：风险定性由「零风险」改为 **「低风险、可回滚、暂不影响现有读路径」** ——
对真实 3.2MB 库执行 DDL 与回填不是字面意义的零风险，须先备份并在副本上验收。

---

## 4. 分阶段执行方案

| 阶段 | 内容 | 风险 | 验收 |
|---|---|---|---|
| **P0-A** | 建 `paper_identifiers` + `identity_conflicts`；从 papers 三列**回填**；产出冲突诊断报告（25 W-only / 58 双身份 / 3 组重复 / 7 W-only 无 doi 明细） | **零**（纯新增表，不改现有读路径） | 回填后 uid↔product 映射 1:1；冲突清单人工可核；现有 pytest 全绿 |
| **P0-B** | `screening_decisions` + `retrieval_events`；pilot/QA 写库（双写过渡，JSON 仍产出）；补 `topic_paper_states` 状态机（seen/rejected 可表达） | 低（新增写入者） | thermo R1 的 I 决策能落库；重复劳动可查 |
| **P0-C** | `audit_frames` / `audit_samples` / `audit_labels`（样本级） | 低 | R06 的 500 样本 + 外部盲评判定可完整重建 |
| **P1** | 改造 knowledge_claims/records 引用 `paper_uid`；清理 270 个 JSON 中的代数增长版本（`_filled`/`_final`/`_seen`） | 中（触及冻结产物引用） | v1.0 产物只读引用不复制 |

**禁用事项**：不再新增「合并后的某某 JSON」。新需求一律先进事件库。

---

## 5. 待拍板的 4 个决策点

1. **preprint ↔ published 是否合并为同一 uid？**
   建议：**不合并**，用 `VERSION_OF` 关联 + 保双 uid（合并会污染 relevance 判定的独立性——preprint 与正式版可能判定不同）。可选：加 `canonical_uid` 指向优选版本。
2. **17 表是否接受裁剪为「3 批 8 表 + 4 表延后」？**
   建议接受（§2）。延后表：`extraction_jobs` / `claim_evidence` / `discovery_hypotheses` / 独立 `queries+query_executions`。
3. **是否允许历史 JSON 逆向导入，还是只做前向双写？**
   建议：P0-B/C 允许**一次性导入**已有的 screening/audit 证据（否则历史信息永久留在 JSON），但导入后 JSON 转为只读归档。
4. **`scopus_cache.db`（713 MB）是否纳入统一库？**
   建议**不纳入**——它是引擎层缓存（可重建），与事实层分离即可；只需保证它的 `papers` 表能反查 identifiers。713 MB 并库会让事实库失去可提交性。

---

## 6. 本方案不做的事

- 不迁移 v1.0 冻结产物的内容（只登记引用）
- 不动 R06 冻结口径（13.9% / 2.4% 禁改写）
- 不重算任何历史裁决（事件层 append-only 的意义即在此）
- 不做 `if topic ==` 式特判（身份层天然 topic 无关）
