# P0-B2.1：身份异常审计报告（identity audit report）

日期：2026-09-12
状态：**完成**（399 pytest passed｜真库零改动）
前置：P0-A 身份层（`824638a`）、P0-B1 写入入口（`a532a2b`）、P0-B1b 写入路径收口（`1aee165`）
工具：`tools/audit_identity_report.py`｜测试：`tests/test_p0b2_1_identity_audit.py`（19 项）
报告：`data/exports/schema/identity_audit_report.json`（+ `identity_anomalies_{A,B,C}.csv`）

---

## 0. 本阶段定位

用户 2026-09-12 的裁决：**P0-B2 不是"立即修 30 条"**，而是先拆成三步 ——

| | 内容 | 状态 |
|---|---|---|
| **P0-B2.1** | 建立 identity audit report，**先统计** | ✅ 本阶段 |
| P0-B2.2 | 30 条迁移（backup → resolve → update refs → validate → commit） | 待批准 |
| P0-B2.3 | 重跑知识库一致性检查（`knowledge_records` 曾有污染风险） | 待执行 |

理由：在不知道「有几类异常、各多少条、影响面多大」之前动手，等于边修边发现，
而这类修复**不可回滚**（uid 被 `topic_papers` 引用）。先统计是风险控制，不是流程冗余。

---

## 1. 输出（用户要求的格式）

```
Identity anomalies:
  Type A: identifier-column mismatch         30   其中 30 条在 uid 侧同时错标
  Type B: primary preference drift           18   形态合法，仅主身份选择不一致
  Type C: duplicate entity candidate          3   候选（涉及 6 篇），禁自动合并
  Type D: reference integrity                 0   悬空引用 / 孤立 / primary 唯一性
  Type E: knowledge_records contamination     0   v1 事实层污染；可映射率 100.0%

Invariants:
  PASS  no_multi_owner_identifier
  PASS  exactly_one_primary_per_uid
  PASS  no_orphan_paper
  PASS  no_dangling_v2_refs
  PASS  paper_identifiers_covers_all_papers
```

**Type C 的答案是 3 组（涉及 6 篇）。** Type D/E 是本阶段实测新增（用户原清单里没有）。

---

## 2. Type A：identifier-column mismatch（30）

**定义**：列名声明一种类型，值形态是另一种。可恢复（值本身是合法标识，只是放错了列）。

```
declared_column=doi  <-  looks_like=SCOPUS_EID   ×30
```

样例：`paper_uid=doi:2-s2.0-85056983120`, `doi 列 = "2-s2.0-85056983120"`。

### A.uid_projection：同一批记录的第二个观测面

这 30 条的 uid 前缀也错标了（`doi:` 装了 EID），所以 A 的 uid 投影数 = 30 = A 本身。

> 这不是独立类型。**修复时必须列与 uid 同时处理** —— 只清 `doi` 列而不重映射 uid，
> 会留下一个前缀声明 DOI、实际身份是 EID 的 uid；只改 uid 而不清列，则列里仍是错值。

**影响面**：`topic_papers` 30 行；`knowledge_claims` / v1 事实层 0 行。

---

## 3. Type B：primary preference drift（18）

**定义**：uid 前缀与值形态**都合法**，但主身份选择与全局规则（`DOI > OPENALEX > SCOPUS_EID`）不一致。

```
declared_type=OPENALEX  ->  best_available=DOI   ×18
```

样例：`openalex:W1964247526` —— 该论文同时有有效 DOI `10.5395/jkacd.2002.27.2.135`。

### 与 Type A 的区别（必须分清，否则报告失去决策价值）

| | Type A（30） | Type B（18） |
|---|---|---|
| `uid_type_matches_value` | **False**（形态不符） | **True**（形态合法） |
| 性质 | 真错配 | 规则差异 |
| P0-A 已定义？ | ✅ 是（`UID_PREFIX_MISMATCH`） | ❌ **否 —— 新类别** |
| 是否阻塞 | 是（B2.2 候选） | **待用户裁定** |

工具里显式做了交叉验证：`B_excludes_prefix_mismatch = (B.cross_check_prefix_mismatch_n == A.count) == True`。

**两个选项**（报告 `readiness.user_decision_required`）：

* **保留**：尊重 uid 不可变 + P0-A 已把 `W_PRIMARY` / `MULTI_ID_WITH_W` 列为合法类别。
  代价：库里长期并存两种主身份风格。
* **纳入重映射**：与 A 的 30 条一起统一为 DOI 优先。代价：额外影响 `topic_papers` 18 行。

**影响面**：`topic_papers` 18 行；`knowledge_claims` / v1 事实层 0 行。

---

## 4. Type C：duplicate entity candidate（3 组 / 6 篇）

**P0-A 铁律：同题不得自动合并**（SI 与正文、preprint 与 published、不同论文、
机构库镜像都会同题）。所以状态是 **CANDIDATE**，且审计器只读。

本阶段新增**模式识别**（只降人工成本，不做裁决）：

| 组 | 题目 | 两个 DOI | 模式 | 建议 |
|---|---|---|---|---|
| 1 | Highly Strained Tricyclic Oxanorbornenes… | `10.1021/acs.macromol.4c02601` / `…4c02601.s001` | **SUPPORTING_INFORMATION** | 建议人工确认后合并或标记为附件 |
| 2 | Physicomechanical properties and polymerization shrinkage… | `10.1002/pc.29332` / `10.2139/ssrn.4918808` | **PREPRINT_VS_PUBLISHED** | **不合并**（用户 2026-09-10 已裁定） |
| 3 | Regulated acrylate networks as tough photocurable… | `10.1002/pi.6364` / `10.34726/3441` | **MULTI_REGISTRANT** | 需人工判定（后者的 `10.34726` 是机构库前缀） |

识别规则：
- `.sNNN` 后缀 DOI → 期刊补充材料（SI），通常是同一篇的附件
- `10.2139/ssrn.` / `10.48550/arxiv.` / `10.1101/` / `10.31219/osf.` → preprint
- 同题但 DOI 注册机构不同 → 可能是机构库镜像或版本关系

**影响面最大的一组**：`knowledge_claims` **49 行**、v1 事实层 `knowledge_records` 6 行 +
`route_mechanism_edges` 33 行。

> 这解释了为什么"顺手合并一下"是危险的：**3 组里只有 1 组（SI）可能真该合并**，
> 而它的影响面反而最小 —— 若不先分模式，最省事的做法（都合并）会动到
> 82 行 v1 事实层。

---

## 5. Type D：reference integrity（0）

四项全零：

| 检查 | 结果 |
|---|---|
| v2 引用表悬空（`topic_papers` / `knowledge_claims`） | **0** |
| papers 无任何 identifier（孤立实体） | **0** |
| identifier 无恰好一个 primary | **0**（331 个 uid 全部恰好 1 个） |
| 标识被多 uid 认领 | **0** |

平均每个 uid 有 **1.87** 个标识。

### ⚠️ 本阶段最重要的口径修正：按**世代**分表

第一次实现时我把 `route_mechanism_edges` 也放进 v2 引用表，得到「悬空 214」——
差点把它当成数据损坏。**它是 v1 世代**：

| 世代 | 表 | `paper_id` 形态 | 谁写的 |
|---|---|---|---|
| v2 | `topic_papers` / `knowledge_claims` | canonical uid | `migrate_v2_schema` / `paper_writer` |
| v1 | `knowledge_records` / `route_mechanism_edges` | `scopus:<DOI>` / `openalex:<URL>` / 裸 DOI | `knowledge_base.store` |

拿 v1 表的 `paper_id` 去 `papers(uid)` 里找，**必然"全部悬空"** —— 那是域不同，不是坏数据。
正确的口径是「经 identity 反解的可映射率」（见 Type E）。

这个修正已写进工具注释 + 测试 `test_type_d_v1_tables_are_not_in_v2_scope`，
避免下一个人重踩。

---

## 6. Type E：knowledge_records contamination（0）

用户点名的「之前 knowledge_records 有污染风险」——**本次全量扫描确认：风险未实现。**

| 检查 | 结果 |
|---|---|
| `record_json.doi` 形态非法 | **0** |
| `canonical_paper_id` 形态非法 | **0** |
| `paper_id` 无法识别 | **0** |

原因（P0-B1 已记录，此处闭环）：那 30 条错位中有 4 条 `label=RELEVANT`，
**仅因摘要为空**才没走到抽取闸门，所以 `doi:2-s2.0-*` 从未进入 `record_json`。
这不是设计防住了，是巧合 —— 而 P0-B1b 的入口现在把这条路径堵死了。

### 跨世代可映射率（100%）

| 表 | 可映射 / 总数 | 率 |
|---|---|---|
| `knowledge_records` | **216 / 216** | 100.0% |
| `route_mechanism_edges` | **215 / 215** | 100.0% |

方法：`extract_from_paper_uid` + record 内的 `doi` / `openalex_id` 经
`paper_identifiers` 反查 —— **不是字符串比对**。

**这是 P0-B2.2「update references」的基础**：v1 事实层 100% 能对上 v2 身份，
所以 uid 重映射时能精确算出哪些 v1 记录需要同步。

另外确认：`canonical_paper_id != paper_id` 出现 209 次，**是设计如此**
（前者是论文最优身份 DOI 优先，后者是 v1 源记录键），不构成异常。

---

## 7. 就绪判定

```json
{
  "B2.2_ready": true,
  "B2.2_candidate_set": {"A": 30, "B": 18, "A_plus_B": 48, "C_separate": 3},
  "blockers": [],
  "impact_if_migrated": {
    "A": {"topic_papers": 30, "knowledge_claims": 0,
          "knowledge_records(via_identity)": 0, "route_mechanism_edges(via_identity)": 0},
    "B": {"topic_papers": 18, "knowledge_claims": 0,
          "knowledge_records(via_identity)": 0, "route_mechanism_edges(via_identity)": 0},
    "C": {"topic_papers": 0, "knowledge_claims": 49,
          "knowledge_records(via_identity)": 6, "route_mechanism_edges(via_identity)": 33}
  }
}
```

**库状态：结构性完好，无数据损坏**（五项不变式全 PASS）。

**待用户裁定两项** —— ✅ **已于同日裁定**（详见 `2026-09-12-p0b2-2-type-a-migration.md` §1）：

1. **Type B 的 18 条** → **保留，不迁移 UID**。性质 = `identity_policy_drift`（非 corruption）：
   `entity_id` 必须稳定（像 git commit hash），「最优引用标识」可变（像 branch pointer），
   两者不可混进 UID。resolver 返回 `{entity_id, preferred_identifier}` 即可。
2. **Type C 的 3 组** → **全部保留为候选**。SI 进人工 merge queue；
   preprint 保持不合并（未来建 `published_as` 关系）；机构库建 `possible_duplicate`。

裁定已固化进审计器的 `USER_RULINGS` 常量并写入报告，避免下次重新讨论。

---

## 8. P0-B2.2 / B2.3 的输入（已就绪）

**B2.2 的候选工作集**：A(30) + 依裁定的 B(0 或 18)。
影响面明确：每组 `topic_papers` 30/18 行，v1 事实层 0 行（A/B 都不碰 v1）。

用户给定的流程与本阶段数据的对接：

| 步骤 | 本阶段提供的基础 |
|---|---|
| backup | 既有备份约定（`<db>.preP0A.db` / `.preDisposition.db`） |
| resolve identity | Type A 的 `looks_like=SCOPUS_EID` 已确定真实类型；`recoverable=True` 30/30 |
| update references | v1 事实层可映射率 100%（精确算出联动行）；v2 侧 30/18 行 |
| validate graph consistency | 五项不变式 + `topic_papers` 行数锚（268） |
| commit | 单事务 + 幂等（`paper_writer` 的 `INSERT OR IGNORE`） |

**B2.3 的输入**：本报告的 Type E 全量口径（含 `route_mechanism_edges`），
重跑时应仍为 0 污染 + 100% 可映射；同时可顺带验证 uid 重映射后 v1 映射率是否保持 100%。

---

## 9. 工具使用

```bash
# 只读扫描（默认）
.venv/Scripts/python tools/audit_identity_report.py

# 附行级明细 CSV（A/B/C）
.venv/Scripts/python tools/audit_identity_report.py --details

# CI 门禁：A 类非空则退出码 1
.venv/Scripts/python tools/audit_identity_report.py --fail-on A
```

真库只读，运行前后校验 `sha256`（`7cab93ae873db613…` 不变）并写入报告。

---

## 10. 本阶段变更文件

**新增**
```
tools/audit_identity_report.py        5 类异常扫描 + 5 项不变式 + CSV 明细
tests/test_p0b2_1_identity_audit.py   19 项（合成库，不依赖真库）
docs/2026-09-12-p0b2-1-identity-audit-report.md
data/exports/schema/identity_audit_report.json
data/exports/schema/identity_anomalies_{A,B,C}.csv
```

**未改动**：`knowledge_base.db`（sha256 前后一致）。
