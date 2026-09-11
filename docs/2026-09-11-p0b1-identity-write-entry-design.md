# P0-B1 统一身份写入入口 —— 分析与设计

- **日期**：2026-09-11
- **状态**：设计与入口实现完成（B1a）；**写者迁移（B1b）与执法（B1c）待批准后执行**
- **前置**：P0-A 统一身份层（commit `824638a`），13/13 验收通过
- **产物**：
  - `search_engine/paper_writer.py`（唯一身份写入入口）
  - `tests/test_p0b1_paper_writer.py`（24 项回归）
  - `tests/fixtures/p0b1_misplaced_30.json`（30 条真实错位模式，冻结）
  - `tools/verify_p0b1_acceptance.py`（真库副本 16 项验收，**16/16 PASS**）
  - `data/exports/schema/p0b1_acceptance_report.json`

---

## 0. 结论先行

| 问题 | 结论 |
|---|---|
| 30 条错位由谁产生？ | **`tools/migrate_v2_schema.py`**（唯一生产者），上游坏值由 `tools/build_s8_finalkb_catalog.py` 制造 |
| 根因是「表设计」还是「写入路径」？ | **写入路径**。建多少新表都修不了——必须收口生产者 |
| 缺陷是否只污染了 `papers`？ | **否，只差一个 abstract 就会污染 `knowledge_records`**（§3.3 近失事件） |
| 入口能否让这类错误不可能发生？ | 能。`doi:` 前缀错配在新写入中**由构造保证为 0**（§5.2 结构性不变式） |
| 是否修了那 30 条？ | **没有，且不应该**。旧 uid 被 `topic_papers` 引用，按纪律保持不变 |

---

## 1. 用户裁决与本阶段边界

用户 2026-09-11 裁决，逐条对应：

| # | 要求 | 落点 |
|---|---|---|
| 1 | 盘点所有写入 `papers` 的代码路径 | §2 |
| 2 | 找出 30 条错位由哪个具体写入者产生 | §3 |
| 3 | 定义唯一入口 `resolve_or_create_paper(metadata, source)` | §4 |
| 4 | 入口先识别标识真实类型，不信字段名 | §5.1 |
| 5 | DOI 字段收到 EID：不写 DOI 列 / 按 EID 处理 / 记 `MISPLACED_IDENTIFIER` / 保留原始输入与来源 | §5.3 |
| 6 | 查身份统一走 `paper_identifiers`，不再自拼 `doi:`/`openalex:` | §5.4 |
| 7 | 旧 UID 不变，新论文用稳定生成规则 | §5.2 |
| 8 | 禁止其他模块直接 `INSERT INTO papers` | §5.5 |
| 9 | 用那 30 条真实错位模式建回归测试 | §7 |

**明确不做**：不建事件表（`screening_decisions` / `paper_retrievals` 留待 P0-B2）。
理由：事件层若引用分裂的论文身份，会把这个缺陷放大成两份历史。

---

## 2. 写入者盘点（权威）

### 2.1 事实层 `data/cache/knowledge_base.db`

| # | 代码路径 | 写入表 | 产出行数 | 缺陷状态 |
|---|---|---|---|---|
| **W1** | `tools/migrate_v2_schema.py:445` | `papers` / `topics` / `topic_papers` / `knowledge_claims` / `search_runs` | `papers` **312** | ⛔ **30 条错位的生产者** |
| **W2** | `tools/disposition_r06_funnel.py:259` | `papers` / `topic_papers`（+ `UPDATE papers.abstract`） | `papers` **19** | ⚠️ **潜伏**：直接信任 `p.get("doi")` |
| **W3** | `tools/run_s8_extraction.py` → `search_engine/knowledge_base.py:164` | `knowledge_records` / `route_mechanism_edges` | 216 / 700 | ⚠️ **潜伏**：`canonical_paper_id = "doi:" + p["doi"]` |
| **W4** | `tools/migrate_p0a_identifiers.py:198` | `paper_identifiers` / `identity_conflicts` | 620 / 33 | ✅ 只读 `papers`，从三列派生且带形态校验 |
| **W5** | `tools/re_extract_seed_papers.py` → 同 W3 | 同 W3 | — | ⚠️ 同 W3 |

**331 = 312 + 19** 由 `created_at` 指纹验证：312 行共享同一时间戳 `1788839006.3926`，
19 行 `source_json.origin = "r06_disposition"`。写入者指纹可完全分离。

### 2.2 引擎缓存层 `data/cache/scopus_cache.db`（**不同库**）

| # | 代码路径 | 写入表 | 说明 |
|---|---|---|---|
| **W6** | `search_engine/cache.py:99` ← `search_engine/engine.py:204` | `papers` | 98,232 行，3 列表结构 |

> ### ⚠️ 命名陷阱：两个同名 `papers` 表，语义完全不同
>
> | 库 | 列 | 行数 | 角色 |
> |---|---|---|---|
> | `knowledge_base.db.papers` | 9 列（`paper_id`/`doi`/`openalex_id`/`scopus_eid`/`title`/`abstract`/`year`/`source_json`/`created_at`） | 331 | **事实层**（唯一真源） |
> | `scopus_cache.db.papers` | 3 列（`paper_id`/`normalized_json`/`retrieved_at`） | 98,232 | 引擎缓存（可重建） |
>
> 因此「禁止直写 `papers`」的守卫**必须按库界定，不能按表名界定**。
> 已落为测试 `test_engine_cache_writer_is_confined_to_cache_db`
> （断言 `cache.py` 绝不引用 `knowledge_base.db`）。

### 2.3 未登记读写者

`tools/generate_relation_candidates*.py`、`search_engine/discovery/*` 等只**读** KB，
不写 `papers`。`search_queries` / `paper_retrievals` 仍为 **0 行**（P0-B2 目标）。

---

## 3. 30 条错位的根因链

### 3.1 两级缺陷：生产者造坏值 → 消费者不校验

```
tools/build_s8_finalkb_catalog.py:181
    doi_by_key[k] = norm_doi(r.get("doi")) or r.get("eid")
                    └─ 无 doi 时**兜底塞入 EID**，却仍叫 doi
                              ↓
tools/build_s8_finalkb_catalog.py:187-193
    papers[k] = {..., "doi": doi, ...}      ← EID 落进名为 doi 的字段
    （行 188 已显式判断 doi.startswith("2-s2.0")，说明作者**知道**值可能是 EID，
      却仍把它持久化进 doi 字段 —— 名字与内容脱钩的典型）
                              ↓
tools/migrate_v2_schema.py:50-57   norm_doi()   ← 只去前缀，**不做形态校验**
tools/migrate_v2_schema.py:268     doi = norm_doi(p.get("doi"))   ← 全盘接受
tools/migrate_v2_schema.py:216-221 ent_key(doi, wid, eid)          ← 以 ("doi", EID) 为实体键
tools/migrate_v2_schema.py:293-298 canonical_paper_id → "doi:" + e["doi"]
tools/migrate_v2_schema.py:445     INSERT INTO papers              ← doi 列装 EID，uid 前缀说 doi
```

**三级类型混淆**：字段命名 → 归一化 → uid 前缀生成。任意一级做形态校验都能拦住。

### 3.2 为什么不是「表设计问题」

`papers.doi` 没有 `CHECK` 约束，加了也只能**拒绝写入**，不能告诉写入者
「这是 EID，应该去 `scopus_eid` 列」。修复必须发生在**写入之前**，即生产者。

### 3.3 近失事件（重要）：`knowledge_records` 差一步被污染

`tools/run_s8_extraction.py:87-88` 用的是**同一个**信任字段名的写法：

```python
"paper_id_db": "scopus:" + p["key"],
"canonical_paper_id": ("doi:" + p["doi"]) if p["doi"] else "",
```

这 30 条中有 **4 条 label = RELEVANT**（其余 26 条 UNCERTAIN）。若它们有摘要，
抽取闸门（`label == "RELEVANT" and abstract`，`run_s8_extraction.py:174`）就会放行，
`knowledge_records.canonical_paper_id` 将出现 `doi:2-s2.0-*`。

**实测：30 条摘要全为空 → 抽取命中 0 条 → `knowledge_records` 中 `doi:2-s2.0` 计数 = 0。**

即：这个缺陷没有扩散到第二张表，**仅因为缺摘要这一巧合**，不是因为有多层防护。
这直接证明「逐点修补」不可靠 —— 必须收口写入入口。

---

## 4. 唯一入口契约

```python
resolve_or_create_paper(conn, metadata, source, *,
                        dry_run=False, first_seen_run=None,
                        resolver_version="p0b1_v1") -> ResolutionOutcome
```

| 参数 | 说明 |
|---|---|
| `metadata` | `doi` / `openalex_id` / `scopus_eid` / `title` / `abstract` / `year` / `identifiers`(list)。**字段名是声明，值形态才是事实。** |
| `source` | 写入者标识，**必填**（空 → `ValueError`）。无溯源的写入不允许进 KB。 |
| `dry_run` | 只判定不落库。 |

### 4.1 状态机

```
                    ┌─ 0 owner ──────────→ CREATED   （生成新 uid + 写 papers/identifiers/conflicts）
metadata → claims ──┼─ 1 owner ──────────→ REUSED    （**不修改 papers 行**）
                    └─ ≥2 owners / 跨 uid → CONFLICT_REVIEW（不合并、不新建、只记录）
```

### 4.2 返回对象

`ResolutionOutcome`：`status` / `paper_uid` / `primary_type` / `identifier_hits` /
`misplaced` / `conflicts` / `created_rows` / `reused_from` / `note`，可 `as_dict()`
序列化入审计产物。

---

## 5. 九项设计决策落地

### 5.1 识别优先于字段名（R2）

```python
resolve_identifier("2-s2.0-12345678", declared_type="DOI")
# → ("SCOPUS_EID", "2-s2.0-12345678")   而不是报错，也不是当成 DOI
```

先按**声明类型**校验；失败则遍历 `ID_TYPES` 按**值形态**识别（复用 P0-A 的
`normalize_identifier` 形态正则，已含 DOI 注册号 4–9 位等约束）。

### 5.2 稳定 uid 生成规则（R5 + 结构性不变式）

```python
make_paper_uid(claims=..., title=..., year=...)
# 有标识 → "<prefix>:<normalized_value>"，按 PRIMARY_PRIORITY (DOI > OPENALEX > SCOPUS_EID > ...) 取最优
# 无标识 → "local:<sha256(norm_title|year)[:16]>"
```

**不变式**：`make_paper_uid` 与 `assign_primary` 都按 `PRIMARY_PRIORITY` 取最优 →
**CREATE 路径下 uid 前缀错配不可能发生**。

> 因此「新写入 `UID_PREFIX_MISMATCH = 0`」不是事后检查出来的，而是**由构造保证**的。
> 这正是 P0-B1 的目标形态。

`local:` 命名空间同时修掉了另一类缺陷：引擎层 `f"openalex:{title[:80]}"` /
`f"scopus:{title[:80]}"` 把标题塞进外部命名空间（§6）。

**旧 uid 永不重算**：既有 uid 一律经 `lookup_owners` 解析后原样复用。

### 5.3 DOI 列收到 EID 时的完整行为（R3）

| 要求 | 实现 |
|---|---|
| 不写入 DOI 列 | `cols["doi"]` 只由 `id_type == "DOI"` 的 claim 填充；错位值不参与 |
| 作为 Scopus EID 处理 | 重建为 `(SCOPUS_EID, norm)` claim，`source="recovered_from:doi"`，`confidence=0.9` |
| 记录 `MISPLACED_IDENTIFIER` | 落 `identity_conflicts`，`resolution_status="REVIEW_REQUIRED"` |
| 保留原始输入与来源 | `incoming_value` = 原值；`provenance` 含 `declared_column` / `looks_like` / `routed_to` / `raw_input_preserved` / `writer` / `resolver_version` |

### 5.4 身份查询统一入口（R7）

```python
resolve_identifier(raw, declared_type=None)   # 这个值实际上是什么
lookup_owners(conn, id_type, norm)            # 谁认领了它
find_paper_uid(conn, raw, declared_type=None) # 任意原始值 → uid
```

调用方**不再**需要 `f"doi:{x}"`。存量债务见 §6。

### 5.5 唯一生产者与执法（R1）

- **静态守卫**：`tests/test_p0b1_paper_writer.py`
  - `test_only_paper_writer_touches_papers_in_production` —— `search_engine/` 内事实层写入者只能是 `paper_writer.py`
  - `test_legacy_writers_list_does_not_grow` —— `tools/` 直写点冻结清单（W1/W2），**只许缩短**
  - `test_engine_cache_writer_is_confined_to_cache_db` —— 命名陷阱守卫
- **未采用 DB 触发器**：会让 schema 与业务耦合，且历史迁移脚本无法再运行。
  静态守卫 + 冻结债务清单已足够，且可在 CI 拦截。

### 5.6 口径约定（跨阶段契约，必须统一）

`identity_conflicts.id_type` = **声明类型 / 声明列**（问题被观察到的地方），
**不是**值的真实类型。真实类型放 `provenance.looks_like` / `routed_as`。

P0-A 对那 30 条冻结的写法是 `id_type='DOI'` + `normalized_value=<EID>`。
入口最初写成 `id_type='SCOPUS_EID'`（真实类型）→ **`dedup_key` 永远匹配不上 →
同一事实被记两次（30 变 60）**。已对齐，并落为两项测试：

- `test_misplaced_conflict_uses_declared_type_not_actual_type`
- `test_conflict_dedup_matches_p0a_convention`（预置 P0-A 口径冲突行 → 重放新增 0 条）

> 教训：**双写入者即双口径风险**。同一张表的语义约定必须在第一个写入者之后立即冻结。

---

## 6. 存量债务：29 处手工拼接 uid 前缀

| 位置 | 数量 |
|---|---|
| `search_engine/`（**生产代码**） | **5** |
| `tools/` | 23 |
| `tests/` | 1 |

生产侧 5 处：

| 文件:行 | 写法 | 风险 |
|---|---|---|
| `search_engine/parser.py:116` | `f"scopus:{doi}"` | **DOI 被塞进 `scopus:` 命名空间**（与 30 条互为镜像） |
| `search_engine/engine.py:517` | `f"scopus:{doi}" else f"scopus:{title[:80]}"` | 同上 + 标题进 ID 命名空间 |
| `search_engine/backends/openalex.py:410` | `f"openalex:{title[:80]}"` | 标题进 ID 命名空间 |
| `search_engine/iterative_searcher.py:42` | `"doi:" + paper.doi...` | 无双口径问题，但绕过统一入口 |
| `search_engine/knowledge_extractor.py:159` | `f"doi:{doi}"` | 绕过统一入口 |

**为何未被 30 条缺陷波及**：`parser.py` / `engine.py` 写的是 `scopus_cache.db`
（引擎缓存），不是事实层；且 `identity.uid_prefix_type` 对
`scopus:10.xxx/yyy` 会因形态校验失败而**返回空**，不会产生错误的身份声明。
实测真库中 `scopus:` 前缀但值为 DOI 的 uid = **0 条**。

**处置（P0-B1b）**：逐一替换为 `make_paper_uid` / `find_paper_uid`。
注意 `scopus:` 前缀在 v1 引擎层实为「Scopus 后端记录」命名空间，与身份层的
`SCOPUS_EID` 类型**语义过载** —— 迁移时需区分，不可机械替换。

---

## 7. 回归测试：用那 30 条真实模式驱动

`tests/fixtures/p0b1_misplaced_30.json`（`sha256=f088088efc6c9a33…`，冻结）：
从真库按 `doi LIKE '2-s2.0-%'` 抽取，每条保留
`existing_paper_uid` / `misplaced_doi_column_value` / `true_identifier_value` /
`title` / **`replay_metadata`**（复现原始输入 `{"doi": <EID>}`）。

测试分两组：

**A 组 — 空库重放（检验新写入形态）**
- `doi` 列必须全空、EID 必须落 `scopus_eid`、uid 必须是 `scopus:2-s2.0-*`
- 前缀错配 = 0

**B 组 — 真库副本重放（检验不破坏旧数据）**
- 30 条全部 `REUSED`，`papers` 零新建，不产生 `scopus:2-s2.0-*` 副本
- `papers` / `topic_papers` / `paper_identifiers` 逐位不变
- 冲突不新增（P0-A 已记录）、重复导入幂等

另含 24 项，覆盖 W-only/EID-only 一等实体、title-only 不借外部命名空间、
跨 uid 合并不自动执行、dry-run 零写入、`source` 必填等。

**全量回归：`pytest tests/` = 350 passed**（P0-A 的 328 + 本节 24，零回归）。

---

## 8. 验收结果

`tools/verify_p0b1_acceptance.py` 在**真库副本**上运行（源库 `sha256=7cab93ae873db613…`
在运行前后一致，仅操作 temp copy）：

```
基线: papers=331 topic_papers=268 identifiers=620 conflicts=33 | fixture 命中 30/30

[A1] 唯一生产者
  [ 1] PASS | 事实层 papers 唯一生产写入者 = paper_writer.py
  [ 2] PASS | 遗留 tools/ 直写点已被冻结登记（只许缩短）
[A4/A6/A7/A8] 真库副本重放 30 条
  [ 3] PASS | A4 同 EID 不创建第二个 UID            statuses={'REUSED': 30}；新增 uid 0 个
  [ 4] PASS | A7 旧 331 个 UID 完全不变              papers 331 -> 331
  [ 5] PASS | A7 topic_papers 引用完全不变           topic_papers 268 -> 268
  [ 6] PASS | A6 冲突只记录、不覆盖                  identifiers 620 -> 620
  [ 7] PASS | A6 错位事实在冲突表中可查              MISPLACED 30；本次新增 0
  [ 8] PASS | A8 重复导入幂等（四张表逐位相同）
  [ 9] PASS | A7 源真库零改动
[A2/A3] 空库重放 30 条
  [10] PASS | A2 新写入 UID_PREFIX_MISMATCH = 0      新建 30；错配 0
  [11] PASS | A2 新 uid 形态正确（scopus:<EID>）
  [12] PASS | A3 EID 不可能进入有效 DOI 列          doi 列装 EID 0；任何 DOI 值 0
  [13] PASS | A3 EID 被正确归属到 scopus_eid 列      30/30
[A5] W-only / EID-only 一等实体
  [14] PASS | A5 W-only 可反查为 uid                7 条；不可反查 0
  [15] PASS | A5 EID-only 可反查为 uid              6 条；不可反查 0
  [16] PASS | A6/A7 重放期间 topic_papers 零变化

16/16 PASS | all_passed=True
```

用户 8 项验收标准 → 检查项映射：

| 用户标准 | 检查项 | 结果 |
|---|---|---|
| 所有新增论文写入都有唯一生产者 | 1, 2 | PASS |
| 新写入中 `UID_PREFIX_MISMATCH=0` | 10, 11 | PASS |
| EID 不可能进入有效 DOI 列 | 12, 13 | PASS |
| 相同 DOI/W-ID/EID 不会创建第二个 UID | 3 | PASS |
| W-only 和 EID-only 能正常成为一等实体 | 14, 15 | PASS |
| 冲突只记录、不覆盖 | 6, 7 | PASS |
| 旧 331 个 UID 及其 `topic_papers` 引用完全不变 | 4, 5, 16 | PASS |
| 重复导入幂等 | 8 | PASS |

---

## 9. 分期与下一步

| 阶段 | 内容 | 状态 |
|---|---|---|
| **P0-B1a** | 入口实现 + 30 条回归 + 验收器 + 设计文档 | ✅ **本阶段完成** |
| **P0-B1b** | 迁移 W1 `migrate_v2_schema` / W2 `disposition_r06_funnel` 走入口；替换 5 处生产侧拼接；`topic_papers` 也纳入入口（或建 `link_topic_paper` 唯一入口） | ⏸ 待批准 |
| **P0-B1c** | 执法升级：CI 纳入守卫测试；遗留清单清零 | ⏸ 待批准 |
| **P0-B2** | `screening_decisions` + `paper_retrievals` 获得真实写入者 | ⏸ B1 完成后 |

### 处置那 30 条错位 uid（独立议题，不在 B1 范围）

`docs/2026-09-10-p0a-identity-layer-delivery.md` §7 C3 记录在案。
三条约束：
1. 旧 uid 被 `topic_papers` 引用 30 行，**禁就地改 uid**；
2. 改名须走 `paper_relations` / uid 重映射阶段，需保留反向引用；
3. 改名前 `paper_identifiers` 已经能正确回答「这个 EID 属于谁」——
   **读写分离已使错标前缀不再产生错误身份**，故不阻塞后续阶段。

---

## 10. 未做与已知风险

- **未实现**：`topic_papers` 的唯一写入入口（当前 W1/W2 直写）。属 B1b。
- **未实现**：`papers` 的 `CHECK` 约束（如 `doi NOT LIKE '2-s2.0-%'`）。
  会阻断历史迁移脚本重跑，改为在入口层强制 + 守卫测试。
- **未实现**：DB 触发器级执法。理由见 §5.5。
- **已知风险 1**：B1b 迁移 W1/W2 时若不小心，可能改变既有 331 行。
  缓解：B1b 必须复用本阶段的「真库副本 + 指纹比对」验收器模式。
- **已知风险 2**：`republish` 类脚本（`disposition_r06_funnel`）带 `UPDATE papers.abstract`
  分支，绕过了入口。B1b 需为「补字段」定义第二个受限入口，并保证入口不越权改身份列。
- **命名债务**：两个同名 `papers` 表（§2.2）。已用测试固化，但**建议后续重命名**
  `scopus_cache.db` 的表为 `cached_papers`，消除隐患。
