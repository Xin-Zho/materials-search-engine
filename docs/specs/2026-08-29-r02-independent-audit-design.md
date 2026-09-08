# R02 独立完备性审计设计（Search S1 之后）

- 日期：2026-08-29
- 状态：DESIGN（待用户确认决策点后执行）
- 前置：S1 已正式冻结（S1_FINAL=19，SEARCH_S1_SEEN_UNION=8532，snapshot_id=S1_20260829_193732_0bb85088，`data/exports/terminology/s1_seen_set.json`）

## 1. 目的

在 S1（terminology repair，+2546 new unique / +42.5% 候选空间）之后，用**独立审计 R02** 回答：
Search S1 相对 Search S0，**真实 generalization effect** 是否存在（miss rate 是否下降），
而不是用 R01 development misses 自证。R01 95 misses / TermFamily / 972 candidates / pilot /
rewrite relevance 全部为 closed development data，**不得再进入 evaluation**。

## 2. R01 基线（冻结，不可覆盖）

- AUDIT_R01 = pc_001::20260829043656，universe=pc_001-2026-08-29T043656（1572，hash=8ffaff83211cfb54）
- sample：seed=42，n=500（SRS without replacement，remaining pool）
- labels：RELEVANT 270 / IRRELEVANT 230 / UNCERTAIN 0
- agent_seen（R01 检索 = S0 depth run ∪ Round1 ∪ Round3）：TRUE 149 / FALSE 95 / UNKNOWN 26
- miss = RELEVANT ∧ agent_seen=FALSE → **p_hat_miss_R01 = 95/270 = 35.2%**
- seen 口径点估计：149/(149+95) = 61.1%；保守：149/270 = 55.2%
- KB Completeness：F=25，m_KB=270 → Recall_LCB=2.8%（KB 层面，非 Search 层面）

## 3. R02 四个独立性条件 + 一条硬约束（用户定，2026-08-29 拍板）

1. **Fresh universe**：不从 R01 remaining pool 继续抽；从同一预定义外部 frame
   （AuditUniverseDefinition，OpenAlex 宽检索全量分页）**重新 snapshot** →
   universe_id_R02 + universe_hash_R02。total 若从 1572 变化，如实记录，不人为对齐。
2. **Fresh random sample**：seed=7，SRS without replacement，n=500。
3. **★ R01 sample exclusion（比 seed≠42 更关键）**：
   **R02_sample ∩ R01_sample = ∅** —— R01 的 500 篇已参与 S1 开发，即使 fresh
   universe 后碰巧抽中，也不得进入 R02 test set（防训练数据放回测试集）。采样流程：
   `fresh external universe snapshot → exclude all R01 audited paper_ids → SRS w/o replacement → n=500, seed=7`
4. **Auditor independent of S1**：先 blind relevance 三态（只给 title/year/doi/abstract），
   不暴露 agent_seen；再独立 identity cross-check（Seen_S1）；最后结合。
   时序硬约束：**先完成 R02 relevance，再做 seen 交叉**。
5. **时序写死**：S1_QUERY_SET_FROZEN → S1_RETRIEVAL_COMPLETE → S1_SEEN_SET_FROZEN →
   R02_UNIVERSE_FROZEN → R02_SAMPLE_DRAWN → R02_LABELING → R02_SEEN_CROSSCHECK。

## 3b. found_relevant 口径拆清（用户拍板第 3 点）

- **构建 universe 时**：found_relevant 沿用 R01 的 KB-known relevant 口径
  （不把 S1 development 的 53 RMCG / repair relevant 判定塞进去）。
- **审计完成后**：F_search,S1 **不能**继续用 KB 的 25；必须等 R02 relevance +
  agent_seen 都完成后，在**同一个 R02 audit universe** 里重新定义：
  F_search,S1 = |{x: Relevant(x) ∧ Seen_S1(x), within frozen R02 universe}|

## 4. 执行流程（工具复用 + 一处改造）

```
Step 1  Fresh universe
  python tools/build_audit_universe.py --topic pc_001
  → completeness_universes.json append: pc_001-2026-08-29T20xx (+universe_hash_R02)
  （同一 AuditUniverseDefinition；外部 frame 若已变化 total 可能 ≠1572，如实记录）

Step 2  Fresh sample（seed≠42）
  python tools/audit_completeness.py --topic pc_001 --create --sample-size 500 --seed 7
  → completeness_audits.json: audit_id_R02 + sampled_paper_ids（SRS w/o replacement）

Step 3  Blind relevance adjudication（复用 label_completeness_sample.py，天然不显示 agent_seen）
  python tools/label_completeness_sample.py --audit-id <audit_id_R02>
  → data/exports/completeness_labels/pc_001__<R02>.json（R/I/U + reviewer + reason）

Step 4  Seen_S1 交叉判定（唯一改造点）
  新 tools/build_r02_seen.py（或 agent_seen.py 加 --seen-set）：
    found sets 数据源 = s1_seen_set.json（8532 canonical keys，DOI/EID 主判）
                        + s1_raw_records.json（title 兜底通道，可选）
    对样本每篇（OpenAlex WID）→ WID↔DOI（openalex_cache）↔ EID（scopus_cache/IdentityResolver）
    → Seen_S1 ∈ {TRUE, FALSE, UNKNOWN}；UNKNOWN 不塞 FALSE
  → labels 文件补 agent_seen_s1 字段（三态）

Step 5  统计 + 报告（复用 audit_completeness.py --replay + agent_seen predicate）
  F_search,S1 = |{x: Relevant(x) ∧ Seen_S1(x), within frozen audit universe}|（审计后口径）
  m_search    = |{x ∈ sample: Relevant(x) ∧ Seen_S1(x)=FALSE}|
  p_hat_miss_R02 = m_search / n_relevant_R02
  Recall_LCB_search = F_search / (F_search + M_upper_search)（超几何反演，单侧 95%）
  报告：formal resolved estimate / conservative estimate / identity unresolved rate
```

## 5. 统计定义（用户定，双轨口径，2026-08-29 拍板冻结）

**双轨口径（R02 不得再混用）**：
```
resolved miss rate      = FALSE / (TRUE + FALSE)            （Primary metric）
conservative miss frac  = (FALSE + UNKNOWN) / total relevant
resolved recall         = TRUE / (TRUE + FALSE)
conservative recall     = TRUE / total relevant
unknown rate            = UNKNOWN / total relevant
```
**R01 基线（冻结，用户修正——resolved 口径分母不含 UNKNOWN）**：
```
resolved miss rate      = 95/244 = 38.9%
conservative miss frac  = (95+26)/270 = 44.8%
resolved recall         = 149/244 = 61.1%
conservative recall     = 149/270 = 55.2%
unknown rate            = 26/270 = 9.6%
```
（注：之前误写的 95/270=35.2% 是 conservative miss fraction 的另一种写法——已废弃，
双轨明确分开。）

**Search 层面（审计完成后，同一 R02 universe）**：
- `F_search,S1 = |{x: Relevant(x) ∧ Seen_S1(x) within frozen R02 universe}|`
  （不得用 KB 的 25；等 R02 relevance + agent_seen 完成后重新定义）
- `m_search = |{x ∈ sample: Relevant(x) ∧ Seen_S1(x) = FALSE}|`
- `Recall_LCB_search = F_search / (F_search + M_upper_search)`（超几何反演，单侧 95%）
- identity 三态 TRUE/FALSE/UNKNOWN；UNKNOWN 不偷偷塞进 FALSE。

## 6. R02 contract（最终冻结，用户 2026-08-29 拍板）

```
Universe definition        = SAME FRAME / FRESH SNAPSHOT（total 变化如实记录）
R01 sample exclusion       = REQUIRED（R02_sample ∩ R01_sample = ∅）
Sample size                = 500
Seed                       = 7
Reviewer                   = BLIND TO agent_seen
Relevance rule             = FROZEN v1（recall-first 三态；KNOWN_RELEVANCE_RULE_BOUNDARY 不热修）
Seen source                = S1_SEEN_SET 8532（data/exports/terminology/s1_seen_set.json）
Identity                   = DOI/EID/WID → normalized title fallback（高置信）
Ambiguous identity         = UNKNOWN（模糊 title 匹配不得强判 FALSE）
Primary metric             = resolved miss rate（Δp_miss < 0）
Secondary                  = conservative miss frac 下降 + one-sided upper bound 下降
GUARDRAILS                 = identity UNKNOWN rate 不显著恶化；relevant yield / usable coverage 不崩
```

## 7. 成功判据（不预设任意 recall 目标）

```
PRIMARY:    resolved miss rate decreases      Δp_miss < 0（38.9% → 更低）
SECONDARY:  conservative miss frac 下降；one-sided miss upper bound 下降
GUARDRAILS: identity UNKNOWN rate 不显著恶化；relevant yield / usable coverage 不崩
```

- 例：38.9% → ~20% = 真实 generalization effect（即使未 statistical stop）
- 例：38.9% → 38% = R01 development misses 上修得好但泛化有限

## 8. 决策点拍板记录（用户 2026-08-29）

1. seed = **7** ✅
2. n = **500** ✅（与 R01 同规模，miss rate 直接可比）
3. found_relevant = **构建时 R01 KB 口径**；F_search,S1 审计后在同一 R02 universe 重定义 ✅
4. title fallback = **开启**，仅高置信精确匹配；模糊 → UNKNOWN，不强判 FALSE ✅
5. universe = **现在立即重建**，同一 frame definition、新 universe_id/hash；total 变化如实记录 ✅

## 9. 已知边界

- relevance 判定规则 v1 保持（recall-first 三态）；anti-shrinking 词形边界 =
  KNOWN_RELEVANCE_RULE_BOUNDARY，不热修（改词形需 v2 统一重判全部，R02 前单独版本化决定）
- R02 的 auditor 判定与 R01 同一协议（label_completeness_sample.py R/I/U），避免协议漂移
- tools/build_r02_seen.py 冒烟预览（R01 labels）：S1 found sets 下 resolved miss
  38.9%→19.3%、UNKNOWN 26 篇完全一致——只作工具正确性 sanity check，**非 R02 正式结论**
  （R01 样本参与过 S1 开发，正式判定必须等独立 R02 sample）

## 10. R02 执行结果（2026-08-29 21:09 完成，正式）

- R02 audit = pc_001::20260829130129（universe=pc_001-2026-08-29T130129，1572/hash 8ffaff83211cfb54，
  与 R01 同 frame 同结果；seed=7；n=500；excluded=R01 sample 500；N_remaining=1047）
- labels：RELEVANT 305 / IRRELEVANT 195 / UNCERTAIN 0（reviewer=ChatGPT，blind 标注）
- Seen_S1（S1_SEEN_SET 8532）：TRUE 195 / FALSE 78 / UNKNOWN 32

### 双轨指标（R01 → R02）
```
resolved miss rate      38.9% → 28.6%   Δ=-10.4pp  PRIMARY 通过 ✓
conservative miss frac  44.8% → 36.1%
resolved recall         61.1% → 71.4%
conservative recall     55.2% → 63.9%
unknown rate             9.6% → 10.5%   GUARDRAIL 轻微上升（可接受）
miss CI_upper (Wilson95) 44.2% → 33.3%  SECONDARY 通过 ✓
```

### Search 统计最终化（R02 universe 口径，F_search 正式定义）
```
KB found ∩ universe           = 25（seen 19 / missed 6）
sample RELEVANT ∧ Seen_S1=TRUE = 195
F_confirmed                   = 214（19 + 195）
m_search（resolved miss）      = 78
抽样母体 N = 1047（= 1572 − 25 − 500 R01 excluded）→ M_upper_pool = 185（含样本 78）
kb_missed_s1（已知未 seen）     = 6
M_miss_upper_total = 6 + 185  = 191
Recall_LCB_search = 214/405   = 52.8%   （R01 时未定义，本次正式解决）
```

### 统计口径修正（2026-08-29 21:27 用户审，两处）

1. **超几何 population 必须是真实抽样母体**：N = 1047（= universe 1572 − KB found 25 −
   R01 excluded 500），**不是**"抽样后剩余"。R02 的 500 篇样本是从 1047 池 SRS 抽取的
   （exclusion 硬约束的必然结果）；抽完剩 547 未审。用 N=1547（不排除）会把被排除的
   R01 sample 500 篇也当作 R02 可推断对象——但 R02 样本从未从那里抽样，无法对它们推断。
   （1547 口径下 M_upper=278、Recall_LCB=43.0%，属错误口径，不采用。）
2. **KB found 未 seen 必须计入已知 miss**：25 = 19 seen + 6 missed；这 6 篇已确认
   relevant 但 Search S1 未看到，是已知 miss，不得在 bound 里消失。
   → M_miss_upper_total = kb_missed_s1(6) + M_upper_pool(185) = 191；
   Recall_LCB_search = 214/(214+191) = **52.8%**（原 53.6% 未计 6 篇已知 miss，修正后下调）。

### 结论
**S1 的 terminology repair 在独立审计 R02 中确认真实 generalization effect**：
resolved miss rate 显著下降（38.9%→28.6%）、resolved recall 上升（61.1%→71.4%）、
miss 上置信界下降（44.2%→33.3%）。R02 样本与 R01 完全不相交（exclusion 硬约束），
非 development 自证。

Formal universe-level Search Recall LCB = **52.8%**（F_confirmed=214，M_miss_upper_total=191，
N=1047 真实抽样母体，含 6 篇 KB known miss；2026-08-29 统计口径修正后）。

产物：data/exports/completeness_labels/pc_001__20260829130129_filled.json（原始 blind labels）、
_seen.json（追加 agent_seen_s1 三态 + 双轨统计 + search_stats_final）；
completeness_audits.json 的 R02 record 已更新为 COMPLETED（audit_metric_scope=SEARCH_S1，
recall_lcb=0.528 为 Search 口径，与 R01 KB 口径 0.070 不同）。

## 12. R03 独立审计结果（2026-08-30 09:44 完成，正式）

- R03 audit = pc_001::20260830011713（universe=pc_001-2026-08-30T011713，1572/hash 8ffaff83211c
  同 frame；seed=11；n=500；excluded=R01+R02 全历史 sample 1000；sampling pool N=547）
- labels：RELEVANT 247 / IRRELEVANT 232 / UNCERTAIN 21（reviewer=ChatGPT，blind，未读 S3_SEEN）
- Seen_S3（S3_SEEN_SET 13430 canonical 三通道）：TRUE 189 / FALSE 38 / UNKNOWN 20

### 四核心数（R02/S1 → R03/S3，独立审计对独立审计）
```
resolved recall       71.4% → 83.3%   +11.9pp  PRIMARY 泛化确认 ✓
conservative recall   63.9% → 76.5%   +12.6pp
resolved miss         28.6% → 16.7%   -11.9pp
conservative miss     36.1% → 23.5%   -12.6pp
unknown rate          10.5% →  8.1%   GUARDRAIL 下降 ✓（S3 更全 found sets 改善 identity）
```

### Search Recall_LCB（R03 修正口径，N=抽样前池 547）
```
F_search,S3 = 20 + 189 = 209（KB∩universe∩S3_SEEN 20 + 样本 seen 189）
kb_missed_s3 = 5
m=38 → M_upper=45 → LCB_resolved = 209/259 = 80.7%
m_ultra=58 → M_upper=58 → LCB_ultra = 76.8%
miss CI_upper（Wilson95）= 21.2%
```

### 结论（2026-08-30 09:48 用户修正：因果归因严谨化 + LCB 轨迹口径标注）

**正式表述（写 spec/paper 用）**：
> The full S1→S3 repair pipeline improved independent resolved recall from 71.4% to 83.3%
> (+11.9pp); development diagnostics indicate that most of the gain is unlikely to be
> explained by depth alone, with citation-first and targeted-query repair providing the
> main new mechanisms.

（不做"全部归因于 citation+query"的强因果声明——S1→S3 中间含 S2 的 depth 500→1000，
depth 未做独立审计；dev 诊断显示 depth 仅追回 8/78，但严格统计上不排除其部分贡献。）

**Recall_LCB 轨迹口径说明（2026-08-30 09:53 用户最终拍板：正式采用口径 A）**：
- **口径定义（协议 §4 冻结）**：每轮 hypergeometric 的 N = 排除所有历史 audit samples 后、
  当前轮抽样前冻结的实际 eligible sampling frame。
- **正式值**：R02 LCB = **52.8%**（N=1047 = 1572 − 25 KB∩uni − 500 R01 excluded）、
  R03 LCB = **80.7%**（N=547 = 1572 − 25 − 1000 excluded）——两者都正确。
- R01 的 7.0% 是 **legacy KB-scope**（F=25，未含审计样本 seen），与 Search 口径不同量，
  **单列不连线**（正式论文/协议中不与 R02/R03 直接连线）。
- 口径 B（R02 用未排除 N=1547 → 43.0%）记录为**不采用**的对照：R03 样本从未从
  未排除池抽样，该口径在两轮间不成立；仅存档供审计性参考。
- **LCB 提升 +27.9pp（52.8%→80.7%）不归因于 Search 改进本身**——LCB 回答"我们对
  搜索效果有多确定"，recall 回答"搜索效果"；两者分开报告，不互相推导。
- R03 的 80.7%：N=547，抽样前池（R03 时点），已落盘（r03_seen.json）。

**其余结论不变**：S3 dev 87.2% vs 独立 83.3% 差距 3.9pp；unknown rate 10.5%→8.1%
（提升非靠塞 UNKNOWN，guardrail 关键证据）。

产物：data/exports/completeness_labels/pc_001__20260830011713_filled.json（原始 blind labels）、
r03_seen.json（四核心数+pool contract+LCB）；completeness_audits.json 的 R03 record 已
COMPLETED（audit_metric_scope=SEARCH_S3，recall_lcb=0.8069 / recall_lcb_ultra=0.7684）。
