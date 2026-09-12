# P0-B1b：写入路径收口（唯一身份写入入口落地）

日期：2026-09-12
状态：**完成**（380 pytest passed｜真库零改动｜P0-B1 验收 16/16｜B1b 验收 8/8）
前置：P0-A 统一身份层（`824638a`）、P0-B1 唯一身份写入入口（`a532a2b`）

---

## 0. 本阶段要回答的问题

P0-B1 交付了 `resolve_or_create_paper`，但**它当时只是"建好了入口"，没人必须走它**。
于是 30 条 `doi:2-s2.0-*` 的土壤还在：只要还有第二个人能直写 `papers`、
还有第二处能自己拼 `doi:` / `scopus:`，同类错误就会以新形式复发。

用户 2026-09-12 批准的四条指令，把这个问题从"补丁"变成"结构"：

1. W1/W2 全部迁移到 `paper_writer`
2. 禁止生产路径直接 `INSERT papers`
3. 替换 uid 手拼
4. 加 static guard（检测 `INSERT INTO papers` / make uid / `f"doi:"` / `f"scopus:"`）

> 完成这四条，库才真正进入**可审计状态**：任何一条论文记录都能回答
> 「谁写的、依据什么判定身份、有没有冲突被静默吞掉」。

---

## 1. 收口前的状态（实测，非估计）

### 1.1 写入者盘点：`331 = 312 + 19`

| 写入者 | 目标 | papers 行 | 性质 |
|---|---|---|---|
| W1 `tools/migrate_v2_schema.py` | `knowledge_base.db` | **312** | 30 条错位的生产端 |
| W2 `tools/disposition_r06_funnel.py` | 同上 | **19** | 同源缺陷 + 摘要补全职责 |
| W3 `tools/run_s8_extraction.py` → `knowledge_base.py` | `knowledge_records` | 216/700 | **近失事件现场** |
| W4 `tools/migrate_p0a_identifiers.py` | 身份两表 | 620 | 干净（三列派生 + 形态校验） |
| W6 `search_engine/cache.py` | **`scopus_cache.db`** | 98,232 | 同名不同库 |

### 1.2 拼接点盘点：29 处

`grep` 只找到一部分 —— 真正的清点是让 AST 逐文件解析
（见 §5「为什么必须用 AST」）。29 处里**生产层 5 处**：

| 位置 | 旧写法 | 后果 |
|---|---|---|
| `parser.py:116` | `f"scopus:{doi}"` | 借 scopus 命名空间装标题 |
| `engine.py:517` | 同上 | 同上 |
| `backends/openalex.py:410` | `f"openalex:{openalex_id}"` | openalex_id 是完整 URL |
| `iterative_searcher.py:42` | `"doi:" + doi.strip().lower().rstrip(".")` | 与 KB 侧归一化不同源 |
| `knowledge_extractor.py:159` | `f"doi:{doi}" if doi else paper.paper_id` | **直接写进 KB 事实层** |

其余 24 处在 `tools/`，分三类：查引擎缓存键（9）、uid 反解（10）、身份生成（5）。

---

## 2. 三条设计判定

### 2.1 权限分离：身份与内容是两个入口

W2 的真实职责一半是"建论文"，一半是"给**已存在**论文补摘要"。如果只给它一个万能写入器，
它就会顺带获得改写身份的权限。因此：

```text
resolve_or_create_paper   身份 —— 新建 uid / 复用既有 uid
backfill_paper_fields     内容 —— 只允许 title / abstract / year
register_topic_paper      关系 —— 且要求论文已存在
```

`backfill_paper_fields` 对 `PROTECTED_PAPER_FIELDS`
（`paper_id` / `doi` / `openalex_id` / `scopus_eid` / `source_json` / `created_at`）
**显式抛 `ValueError`，而不是静默忽略**。

> 静默忽略会把「越权意图」藏起来 —— 而"静默接受一个看起来像 DOI 的 EID"
> 正是那 30 条得以发生的方式。

`register_topic_paper` 要求 `paper_uid` 已存在：**关系不得先于实体**。
若这里允许自动建论文，入口之外就凭空多了第二个身份生产者，R1 当场失守。

### 2.2 命名唯一出口：G3 白名单为空

`<prefix>:<value>` 的构造与解析全部收进 `search_engine/identity.py`：

| 函数 | 用途 |
|---|---|
| `make_paper_uid` | KB 身份（从 claims 生成，DOI > W > EID） |
| `make_record_id` | 检索层源记录 ID（**语义不同，切勿互换**） |
| `make_canonical_uid` | 便捷入口：从常见声明字段产出 canonical uid |
| `uid_id_type` | uid → 它**声明**的 id_type（不许再手写 `startswith("doi:")`） |
| `extract_from_paper_uid` | uid 反解（带形态校验） |
| `scopus_cache_key` / `scopus_cache_key_value` | **引擎缓存库**记录键，读写对称 |
| `UID_PREFIX_BY_TYPE` / `LOCAL_UID_PREFIX` | 前缀的权威表 |

两个易混点被写进了 docstring：

* `make_paper_uid`（KB 身份，值必须是标准化后的合法标识）
  vs `make_record_id`（源记录 ID，值可以是标题片段）；
* `scopus_cache_key`（`scopus_cache.db.papers`，9.8 万行）
  vs `make_paper_uid`（`knowledge_base.db.papers`，331 行）—— **同名不同库**。

反解也必须对称：`scopus_cache_key_value` **不能**用 `extract_from_paper_uid` 代替 ——
后者做形态校验，而缓存键里的值可能是 DOI 或空串，形态校验会把合法键判成 `None`。

### 2.3 迁移脚本走入口后，重放产出**更正确**的库

W1/W2 是一次性重建脚本（W1 有防重入）。走入口后它们的 uid 由**值形态**决定，因此
从零重建的库与现库会有差异。本阶段的立场是：**不掩盖差异，而是证明差异有界且已知**。

---

## 3. 重放差异的机器证明

`tools/verify_p0b1b_uid_namespace.py`（真库**只读**，运行前后校验 sha256）做集合代数：

### DELTA-A：`UID_PREFIX_MISMATCH`（真错配，30 条）

```
W1 重放:  plan=312  db=331  shared=282
现库侧:   doi:2-s2.0-*   ×30      ← 前缀声明 DOI，值其实是 EID
重放侧:   scopus:2-s2.0-* ×30     ← 按真实类型归属
判据:     同一标识集合的两种形态 = True
附带变化: 0（282 = 312 − 30）
```

`tools/migrate_v2_schema.py` 的 dry-run 同步给出两个新断言：

```
[check] uid_prefix_consistent: PASS
  recovered_from_misplaced_column: 30      ← 恰好等于已知错位数量
  uid_prefix_mismatch_new:         0       ← 构造保证，不是事后检查
```

### DELTA-B：`PRIMARY_CHOICE_INCONSISTENT`（规则差异，18 条）

```
W2 重放:  to_write=19  一致=1  差异=18
W84902791      openalex:W84902791   ->  doi:10.17077/etd.h67ffoee
W2170762733    openalex:W2170762733 ->  doi:10.1002/pola.22318
...
```

**性质与 DELTA-A 完全不同，不可混为一谈**：这 18 条的 uid 前缀与值形态
**都合法**（`openalex:` + `W...`），只是旧写入者按 key 形态硬编码前缀，
而 `make_paper_uid` 按 `PRIMARY_PRIORITY` 取 DOI 优先。它们**不违反**
`uid_type_matches_value`，因此不属于 P0-A 定义的 30 条错配。

> 这是 P0-A 口径里**尚未定义**的一个新类别：不是形态错误，而是**主身份选择不一致**。
> 是否要把这 18 条也纳入 uid 重映射，需要用户裁定（见 §7）。

两个 DELTA 都不是本阶段的回归：真库经 REUSE 路径保护，uid 不变；
报告只量化「从零重建会差多少」。

---

## 4. W2 重放幂等（真库副本实测）

在真库**副本**上重放 W2 `--apply`：

| 表 | delta |
|---|---|
| `papers` | **+0** |
| `topic_papers` | **+0** |
| `paper_identifiers` | **+0** |
| `identity_conflicts` | **+0** |

`[ok] KB 新建 0 篇、摘要升级 0 篇、待人工裁决 0 篇`
真库 sha256 `7cab93ae873db613…` 前后一致。

---

## 5. 静态守卫：为什么必须用 AST

`tests/test_p0b1b_static_guards.py` 守四类：

| | 检查 | 白名单 |
|---|---|---|
| G1 | `INSERT ... INTO papers` | `paper_writer.py`（KB）+ `cache.py`（引擎缓存库） |
| G2 | `def make_paper_uid` / `def make_record_id` | `identity.py` |
| G3 | `f"<prefix>:…"`、`"<prefix>:" + x` | **空** |
| G4 | `startswith("<prefix>:")` | `identity.py`（`normalize_identifier` 必须能剥前缀） |

**为什么不能用 grep** —— 这是本次踩出来的，不是理论洁癖：

1. **注释会误报。** 迁移脚本的头注释必须逐字引用旧代码
   （`con.execute("insert into papers ...")`）才能说明"这里曾经错在哪"。
   文本扫描把这种解释当成新的直写点，于是守卫被逼着加豁免 —— 很快就没有守卫了。
   本阶段 `test_p0b1_paper_writer.py` 与 `tools/verify_p0b1_acceptance.py`
   里的文本扫描**真的各自误报过一次**，包括把检测用的正则常量本身当成违规。
2. **AST 能切开「受控前缀」与「硬编码命名空间」。**
   收口写法 `f"{uid_prefix(t)}:{v}"` 的第一段是 `FormattedValue`；
   手拼 `f"doi:{v}"` 的第一段是常量 `"doi:"`。这条分界线恰好就是我们要的那条。

另外 `test_whitelists_are_minimal` 持续证明每个白名单项**仍然命中** ——
防止豁免变成僵尸。它在收口过程中真的报出过一次：`identity.py` 原本在 G3 白名单里，
收口后改用受控拼接，G3 命中归零，白名单随之缩到空。

---

## 6. 验收结果

| 项 | 结果 |
|---|---|
| pytest | **380 passed**（P0-B1 时 350；+23 写路径、+6 静态守卫、−2 重复文本扫描、+3 disposition） |
| P0-B1 验收器 | **16/16 PASS**（其中第 2 项期望值从「清单只许缩短」改为「tools/ 直写点为空」） |
| B1b 验收器 | **8/8 PASS** |
| W2 副本重放 | 四表 delta 全 0 |
| 真库 | sha256 前后一致；`papers 331 / topic_papers 268 / knowledge_claims 1283 / knowledge_records 216 / route_mechanism_edges 700 / paper_identifiers 620 / identity_conflicts 33` **全部不变** |
| 那 30 条 | 仍在原处（`EID 在 doi 列 = 30`）—— 处置属独立的 uid 重映射阶段 |

**tools/ 直写点：2 → 0。生产层直写点：1（`paper_writer.py`）+ `cache.py`（不同库）。**

---

## 7. 需用户裁定 / 移交下一阶段

### 7.1 待裁定：DELTA-B 的 18 条

现库有 18 条论文**拥有有效 DOI 但 uid 是 `openalex:W...`**（P0-A 术语：`MULTI_ID_WITH_W`）。
两个选项：

* **保留**：尊重 uid 不可变 + P0-A 已把 `W_PRIMARY` 列为合法类别。
  代价：库里长期并存两种主身份风格。
* **纳入 uid 重映射**：与那 30 条一起处理。代价：要同步重写
  `topic_papers` / `knowledge_records` 的引用，属高风险操作。

**本阶段不擅自处理**，且它不阻塞 P0-B2（读写分离后，错标前缀已不再产生错误身份）。

### 7.2 B1b 阶段新发现的债务

| 债务 | 位置 | 影响 |
|---|---|---|
| **影子身份层** | `tools/pilot_round3_query_utility.py::IdentityResolver`（584 行） | 与 `identity` 模块并行的第二套身份解析；pilot 已跑完，未影响本阶段 |
| **第二套 DOI 归一化** | `search_engine/evaluator.py::normalize_doi` | 与 `normalize_identifier` 并存；目前只用于检索覆盖度统计，非 KB 身份 |
| 手造匹配键 | `search_engine/foundational_recovery.py:85,112` | `key = normalize_doi(bp.doi) or bp.paper_id` —— 不是 uid 拼接，但同样是"自己拼身份键" |
| 借命名空间装标题 | `parser.py` / `engine.py` 的 `make_record_id("scopus", title[:80])` | 值保留原样（缓存键兼容），但**已不再向 KB 传播** |

### 7.3 未跟踪文件

`tools/review_r06_funnel_19.py`：mtime 09-08，`git log --all` 无记录，**从未被跟踪**。
按用户要求不删除、不纳入提交。它的 G3 豁免由
`test_untracked_debt_is_still_isolated` 守着 —— 一旦该文件被 git 跟踪，测试立刻失败，
豁免理由即失效。

补充事实：`tools/disposition_r06_funnel.py:36` 定义了 `REVIEW_PATH` 指向它的产物，
但该常量全文只出现 1 次（定义处）—— **死常量，从未被消费**。

---

## 8. 下一步

1. **P0-B2**：让 `search_queries` / `paper_retrievals` 获得真实写入者（两张表目前都是 0 行）
2. **历史错位修复**：那 30 条（含 7.1 裁定的 18 条）的 uid 重映射阶段
3. **Phase 1 Knowledge Extractor**：抽取器接入唯一身份入口

---

## 附：本阶段改动文件

**新增**
```
tests/test_p0b1b_static_guards.py        6 项（G1..G4 + 白名单最小性 + 未跟踪隔离）
tests/test_p0b1b_write_path.py          23 项（权限分离 / 关系后置 / 命名出口）
tools/verify_p0b1b_uid_namespace.py      重放差异机器证明（8 项）
docs/2026-09-12-p0b1b-write-path-closure.md
```

**修改（收口）**
```
search_engine/identity.py                前缀权威表 + 6 个命名工具；OPENALEX 循环剥离
search_engine/paper_writer.py            backfill_paper_fields / register_topic_paper
search_engine/parser.py                  make_record_id
search_engine/engine.py                  make_record_id
search_engine/backends/openalex.py       make_record_id
search_engine/iterative_searcher.py      dedup 键改走 normalize_identifier（跨源去重不再漏）
search_engine/knowledge_extractor.py     canonical_paper_id 改走 make_paper_uid
tools/migrate_v2_schema.py               W1 全走入口；extract_w/EID 改走 normalize_identifier
tools/disposition_r06_funnel.py          W2 全走入口
tools/run_s8_extraction.py               近失事件现场修复
tools/{audit_paper_identity,restore_paper,re_extract_seed_papers}.py   反解/身份生成收口
tools/{build_s6_qa_corpus,build_s7_community_memory,build_s7_keep_pool,
       build_s7_uncertain_topup,build_s8_finalkb_catalog,
       label_completeness_sample,run_search_s1,run_search_s2,
       run_terminology_pilot}.py         引擎缓存键收口（20 处）
tools/verify_p0b1_acceptance.py          A1 扫描改 AST；期望值更新为 tools/ 直写归零
tests/test_disposition.py                fixture 补身份层两表 + 非法 EID 形态回归
tests/test_p0b1_paper_writer.py          删除与新 G1 重复的文本扫描守卫
```
