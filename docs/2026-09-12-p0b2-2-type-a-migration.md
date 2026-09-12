# P0-B2.2：Type A 30 条 uid 重映射（P0 阶段收口）

日期：2026-09-12
状态：**完成**｜真库已迁移｜399 pytest passed｜三条验收器全绿
工具：`tools/migrate_p0b2_2_type_a.py`｜报告：`data/exports/schema/p0b2_2_migration_report.json`
备份：`data/cache/knowledge_base.db.preP0B22.db`（`sha256=7cab93ae873db613…`）

> 本节完成后，项目**正式退出 P0 基础治理阶段**，进入 P4 主线（见 §7）。

---

## 1. 三条裁定（用户 2026-09-12）

| 类型 | 数量 | 裁定 | 性质 |
|---|---|---|---|
| **A** identifier-column mismatch | 30 | **修复**（真错误） | 数据错误 |
| **B** primary preference drift | 18 | **保留现状，不迁移 UID** | `identity_policy_drift` |
| **C** duplicate entity candidate | 3 组 / 6 篇 | **全部保留为候选，不自动合并** | `identified / not resolved` |

### 1.1 Type B 为什么不统一 DOI 优先（核心口径）

用户给的判据值得逐字记下 —— 它把「UID 是什么」这件事定死了：

> **entity_id** 回答「这是哪篇论文」，应该**稳定**。
> **preferred_identifier** 回答「引用它时哪个 identifier 最优」，**可以变化**。
> 不要把两个概念混在 UID 里。
>
> 类比 git：`commit hash` 永远不变，`branch pointer` 可以移动。
> `paper_uid` 应该像 hash。

所以 Type B 记录为 **`identity_policy_drift`**，而**不是** `identity_corruption`。
未来 resolver 返回即可：

```json
{"entity_id": "openalex:Wxxx", "preferred_identifier": "doi:xxx"}
```

—— 不改写 `entity_id`。

**这条裁定反过来也印证了 P0-B1b 的设计**：`make_paper_uid` 取 `PRIMARY_PRIORITY` 最优，
本身是「生成**新** uid 时的选优规则」；一旦 uid 生成，它就不该再随"哪个标识更优"而变。
即：**规则作用于生成时刻，不作用于存量**。

### 1.2 Type C 的分模式处置

| 模式 | 裁定 | 未来关系 |
|---|---|---|
| `SUPPORTING_INFORMATION` | 进入**人工 merge queue**（可能是 SI / supplementary article / data article） | 可保留 `parent paper ← supporting document`，**不要直接 merge** |
| `PREPRINT_VS_PUBLISHED` | **保持不合并**（两个科研实体） | 建 `preprint --published_as--> journal paper`，对趋势分析有价值 |
| `MULTI_REGISTRANT` | 不自动合并 | 建 `possible_duplicate` 标记即可 |

裁定已写入审计器的 `USER_RULINGS` 常量并落进报告，避免下次重新讨论同一件事。

### 1.3 明确不做的（用户踩刹车）

以下问题存在但**不是 P4 的阻塞项**，P0-B2 到此为止，不再扩展：

* 第二套 DOI 归一化（`search_engine/evaluator.py::normalize_doi`）
* 影子身份层（`tools/pilot_round3_query_utility.py::IdentityResolver`）
* `tools/` 其余债务
* `route_mechanism_edges` 的世代问题

---

## 2. 迁移设计

### 2.1 目标态

```
                 修复前                          修复后
papers.paper_id   doi:2-s2.0-X          ->      scopus:2-s2.0-X
papers.doi        2-s2.0-X（错位）       ->      NULL
papers.scopus_eid 2-s2.0-X（本来就对）    ->      不变
```

**列与 uid 必须同时改**：只清列会留下「前缀声明 DOI、实际身份是 EID」的 uid；
只改 uid 则列里仍是错值。这正是 B2.1 里 `A.uid_projection == 30` 的含义。

### 2.2 影响面（实测，非估计）—— 4 张表

| 表 | 行数 | 更新列 |
|---|---|---|
| `papers` | 30 | `paper_id`、`doi` |
| `paper_identifiers` | 30 | `paper_uid` |
| `topic_papers` | 30 | `paper_id`（R4 / U26） |
| `identity_conflicts` | 30 | `incoming_paper_uid` + `resolution_status` → `RESOLVED_KEPT` |
| `knowledge_claims` / `knowledge_records` / `route_mechanism_edges` | **0** | — |

v1 事实层**零触及** —— 这是 B2.1 的可映射率分析直接给出的结论。

### 2.3 通用而非特判

* 新 uid 由 `paper_writer.classify_metadata` + `identity.make_paper_uid` 算出（**不写死** `scopus:`）
* 被清空的列 = **被判定为错位的列**，其余列原样不动（不做"顺手归一化"）

### 2.4 安全设计

| 装置 | 做法 |
|---|---|
| dry-run 默认 | 只读+预检，零写入 |
| 备份 | `--apply` 前自动 `<db>.preP0B22.db` |
| 单事务 | 任何断言失败 → rollback |
| 预检 5 项 | 新 uid 不撞既有、新 uid 唯一、每行都有新 uid、错位值全部可恢复、改动的条数一致 |
| 迁移后 7 项验证 | 见 §3 |
| 幂等 | 二次运行检出 0 条 → no-op |

---

## 3. 执行结果

```
[backup] knowledge_base.db.preP0B22.db  sha256=7cab93ae873db613…
[applied] stats={'papers': 30, 'paper_identifiers': 30, 'topic_papers': 30,
                 'identity_conflicts': 30}

验证:
  PASS  type_a_zero                    <- identity invariant（用户要求）
  PASS  counts_unchanged               <- reference invariant（用户要求）
  PASS  new_uids_present
  PASS  old_uids_fully_gone
  PASS  identifier_owner_correct
  PASS  no_residual_refs
  PASS  conflicts_resolved_30
```

### 3.1 用户要求的三项验证

| 要求 | 结果 |
|---|---|
| **identity invariant**：迁移前 30 → 迁移后 Type A = 0 | ✅ `Type A: 0` |
| **reference invariant**：`topic_papers` / `knowledge_claims` / `knowledge_records` 数量不变 | ✅ 268 / 1283 / 216 全不变（另有 `papers` 331、`paper_identifiers` 620、`identity_conflicts` 33、`route_mechanism_edges` 700） |
| **hash**：迁移前备份 | ✅ `preP0B22.db` |

### 3.2 真库前后状态

| | 迁移前 | 迁移后 |
|---|---|---|
| sha256 | `7cab93ae873db613…` | `2f5733d31e3fe41c…` |
| uid 前缀分布 | doi 300 / openalex 25 / scopus 6 | **doi 270 / openalex 25 / scopus 36** |
| `EID 在 doi 列` | 30 | **0** |
| `identity_conflicts` | REVIEW_REQUIRED 33 | **RESOLVED_KEPT 30 + REVIEW_REQUIRED 3** |

### 3.3 P0-A 口径自洽（迁移的旁证）

```
             迁移前    迁移后
DOI_PRIMARY    300  ->  270     (-30)
EID_PRIMARY      6  ->   36     (+30)
EFFECTIVE_DOI_PRIMARY 270 -> 270   (不变)
UID_PREFIX_MISMATCH  30  ->   0
```

`EFFECTIVE_DOI_PRIMARY` **不变**是关键：修复只是把「uid 前缀声明为 DOI」的 30 条
重新归类到 EID，而它们本来就不是有效 DOI（`doi` 列里装的是 EID）。
数量在三个口径上同时守恒，说明迁移没有多改也没有少改。

### 3.4 验收器连锁

* `tools/verify_p0b1_acceptance.py` → **17/17 PASS**（新增 1 项，见 §4.2）
* `tools/verify_p0b1b_uid_namespace.py` → **8/8 PASS**，`DELTA-A` 从 30 变为 **0**
* `tools/audit_identity_report.py` → Type A **0** / B 18 / C 3 / D 0 / E 0，不变式 5/5

---

## 4. 迁移暴露的两个自身缺陷（都已修）

### 4.1 `os.path.relpath` 跨盘符会抛 ValueError

「先在副本上验证」是迁移的安全惯例，但副本常在 `%TEMP%`（C:）而仓库在 D: ——
`os.path.relpath(path, BASE)` 直接 `ValueError: path is on mount 'C:', start on mount 'D:'`。

**3 个工具、15 处**统一改为带兜底的 `_rel()`（`tools/migrate_p0b2_2_type_a.py`、
`verify_p0b1_acceptance.py`、`audit_identity_report.py`、`verify_p0b1b_uid_namespace.py`）。

> 附带踩坑：批量替换时把 `_rel()` **自身内部**的 `os.path.relpath` 也替换了，
> 造成无限递归。教训：**先替换调用点，再插入 helper**；或让 helper 的实现模式
> 与替换模式不同。

### 4.2 验收器「空集假绿」

`verify_p0b1_acceptance.py` 原先按 `existing_paper_uid` 定位那 30 条。
迁移把 uid 改成 `scopus:2-s2.0-*` 后，定位函数返回**空集** ——
而它的 16 项检查大多是「对这批行做 X」，在零行上**全部通过**，
于是报出 `16/16 PASS` 但实际什么都没验证。

修法两条：
1. 定位改用 fixture 里冻结的 **EID 值**（`true_identifier_value`，迁移不变）
2. **新增一条断言**把「命中数」本身升为检查项：
   `check(0, "fixture 命中 30/30（防空集假绿）", ...)`

> 这条经验是通用的：**任何"先筛选一批行、再对它们断言"的验收器，
> 都必须把筛选结果的规模本身作为断言**，否则筛选器一旦失灵，验收器会安静地变成空转。

---

## 5. 收口

P0 身份治理到此结束。留下的稳态：

| | 状态 |
|---|---|
| 唯一身份写入入口 | `paper_writer` 三入口（身份/内容/关系），tools/ 直写点 0 |
| uid 命名空间 | 唯一出口在 `identity.py`，static guard G1..G4 白名单最小 |
| 身份异常 | Type A **0** / Type B 18（**已裁定保留**）/ Type C 3（**已裁定保留**）/ D 0 / E 0 |
| 不变式 | 5/5 PASS |
| 审计能力 | `audit_identity_report.py` 可随时重跑（只读 + sha256 + `--fail-on` 可做 CI 门禁） |

---

## 6. 下一步：正式进入 P4

用户 2026-09-12 的路线：

```
P0 Identity (90%) -> P0-B2.2 (只修 30 条) -> ✅ 现在在这里
                                              |
                                              v
                                         P4-0 数据集
                                              |
                                     P4-1 Knowledge Extraction
                                              |
                                     P4-2 Trend Prediction
                                              |
                                     P4-3 Scientific Validation
```

### P4-0：Temporal Benchmark Dataset

**目标**：建立 `photopolymerization_future_prediction.db`，**严格时间隔离**。

```
训练   <= 2020-12-31
预测   2021-2025
禁止未来信息泄漏
```

**不要改主库**，建 snapshot：

```
datasets/
  photopolymerization_v1/
    papers_train.db
    papers_future_eval.db
```

**第一版目标不是完整 Agent，而是证明**：从历史文献结构可以预测未来增长方向。

| Step | 内容 |
|---|---|
| 1 | 论文 → concept extraction：`material` / `method` / `problem` / `application` / `mechanism` |
| 2 | 构建 knowledge graph（Thiol-ene → low shrinkage / DLP printing / biomedical …） |
| 3 | 2015-2020 graph：每方向算 `growth rate` / `novelty` / `citation acceleration` / `author growth` / `cross-domain connection` |
| 4 | 预测 top emerging topics |
| 5 | 验证 2021-2025：Advanced Materials / Nature Communications / Science Advances / Nature Materials 等 |

> 用户判断：**不要把一个搜索基础设施项目拖成数据库重构项目，而是尽快拿到
> 「AI 能否发现未来科研方向」的实验结果。**

---

## 7. 本阶段变更文件

**新增**
```
tools/migrate_p0b2_2_type_a.py       Type A 重映射（dry-run 默认 / 备份 / 单事务 / 幂等）
docs/2026-09-12-p0b2-2-type-a-migration.md
data/exports/schema/p0b2_2_migration_report.json   （data/ 已 gitignore）
```

**修改**
```
tools/audit_identity_report.py            + USER_RULINGS（A/B/C 裁定固化）；_rel 兜底
tools/verify_p0b1_acceptance.py           定位改 EID（跨 uid 变更稳定）+ 防假绿断言；_rel
tools/verify_p0b1b_uid_namespace.py       DELTA-A 期望值改为「当前库 Type A 数」（跨阶段自适应）；_rel
```

**真库**：已迁移（`2f5733d3…`）；备份 `preP0B22.db`（`7cab93ae…`）。
