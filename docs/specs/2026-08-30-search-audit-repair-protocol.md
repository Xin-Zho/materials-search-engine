# Search → Audit → Repair 评测协议（Search-Audit-Repair Evaluation Protocol）

**状态：v1 FROZEN（2026-08-30 09:48，用户定）→ v2（2026-08-30 23:50，S4 完成后升级）**
**出处**：D:\materials-knowledge-base 材料学知识库 v3.0 Completeness Audit，S0→S4 全链路实证后提炼。

---

## 1. 核心思想

一个搜索系统是否在迭代中**真正提高召回率**，不能靠"在漏检集上修得好"来证明——那只是过拟合。
必须用**独立审计样本**验证泛化：

```
Search → IndependentAudit → MissDiagnosis → Repair → FreshAudit（→ 循环）
```

每一轮 repair 的输入是上一轮审计的 miss（development data）；每一轮的效果必须由**下一轮
全新抽取、盲标、与所有历史样本不相交**的审计来判定。

## 2. 角色与数据隔离（纪律，不可破）

| 数据 | 角色 | 可用性 |
|---|---|---|
| 审计样本 N | 该轮 test set | 标注后立即"封账"，参与下一轮抽样**排除** |
| 该轮 miss | 下一轮 repair 的 development data | 只用于开发，**永不回用于评价** |
| Search snapshot | 被审计对象 | 冻结后不得因审计结果修改 |

三条铁律：
1. **R{i+1} sample ∩ R{i} sample = ∅**（整个历史 sample 全部排除，不只 relevant）。
   防止已参与开发的论文重新进入 test set。
2. **blind labeling**：relevance 标注全程不读 agent_seen / seen set——先判论文是否相关，
   再独立做 identity cross-check，最后才结合。
3. **LCB 的 N = 抽样时点的完整抽样框**（universe − KB known relevant − 历史 excluded），
   **绝不用"抽样后剩余数量"**。脚本断言 `hypergeom_population_N == frozen_sampling_pool_size`。

## 3. 流程（每轮审计 R{i}）

```
1. Fresh universe snapshot
   同一外部 frame definition（OpenAlex 宽检索全量分页），新 universe_id + hash；
   total 变化如实记录，不人为对齐。
2. Exclude 全部历史 audit sample（R01..R{i-1} 的 sampled_paper_ids union）
   → 抽样框 = universe − KB found − excluded
3. SRS without replacement，n 固定（本项目 500），新 seed（≠ 所有历史 seed）
4. Blind relevance labeling（R/I/U 三态，reviewer 不见 seen 信息）
5. labels 完成后 join 冻结的 S{i}_SEEN_SET（三通道：EID/DOI/title；模糊→UNKNOWN 不强判 FALSE）
6. 统计四核心数 + Search Recall_LCB（见 §4）
```

## 4. 统计口径（v1 冻结）

**四核心数**（T=Seen TRUE, F=Seen FALSE, U=Seen UNKNOWN，均限于 RELEVANT 内）：
```
resolved recall     = T / (T+F)
conservative recall = T / (T+F+U)
resolved miss       = F / (T+F)          ← Primary
conservative miss   = (F+U) / (T+F+U)
unknown rate        = U / (T+F+U)        ← Guardrail（提升不能靠塞 UNKNOWN）
```

**Search Recall_LCB**（超几何反演，单侧 95%）：
```
N      = frozen sampling pool size（audit.N_remaining，抽样前）
n      = sample size（500）
x      = sample FALSE（resolved）/ FALSE+UNKNOWN（ultra）
M_upper_pool = missed_relevant_upper_bound(N, n, x)
F_search,S   = |KB found ∩ universe ∩ S{i}_SEEN| + |sample RELEVANT ∧ Seen=TRUE|
kb_missed    = |KB found ∩ universe| − |KB found ∩ universe ∩ S{i}_SEEN|   ← 已知 miss 不得消失
Recall_LCB   = F_search / (F_search + kb_missed + M_upper_pool)
```

**pool 构成必须落盘**（防口径混用）：
`universe_total / known_relevant_excluded / excluded_historical_samples /
sampling_pool_size_before_sample / sample_n / sample_false / sample_unknown`

## 5. 成功判据（不预设任意 recall 目标）

```
PRIMARY:    resolved miss rate decreases（Δp_miss < 0）
SECONDARY:  miss CI_upper（Wilson 95% 单侧）decreases
GUARDRAILS: unknown rate 不显著恶化；relevant yield / usable coverage 不崩
```

## 6. 归因表述（防过度声明）

单轮 Δ 只能归因于"**该轮完整 repair pipeline**"，不能拆到单个机制——
除非中间某步有独立审计。本项目经验：
> The full S1→S3 repair pipeline improved independent resolved recall from 71.4% to 83.3%
> (+11.9pp); development diagnostics indicate that most of the gain is unlikely to be
> explained by depth alone, with citation-first and targeted-query repair providing the
> main new mechanisms.

dev 指标（如 development recovery、dev recall）只作开发集参考，**永不参与正式比较**。

## 7. 本项目实证轨迹（S0→S3，2026-08-30 09:53 用户最终拍板口径 A）

**正式报告结构（论文/协议用）**：
- **主性能轨迹**（resolved recall，独立审计对独立审计）：**71.4% → 83.3%（+11.9pp）**
- **fresh-frame LCB**（口径 A）：**52.8% → 80.7%**
- **R01 7.0% 单列为 legacy KB-scope**（F=25，未含审计样本 seen），不与后二者连线
- **LCB +27.9pp 不归因于 Search 改进本身**——recall 回答"搜索效果"，LCB 回答"我们
  对搜索效果有多确定"，两者分开报告

| 版本 | 机制 | seen | 独立审计 | resolved recall | Recall_LCB（口径 A） |
|---|---|---|---|---|---|
| S1 | terminology repair，depth=500 | 8,532 | R02 | 71.4% | 52.8%（N=1047） |
| S2 | depth 500→1000 | 9,873 | —（dev step） | 74.4% dev | — |
| S3 | citation-first（16 seeds 1-hop）+ targeted query（7） | 13,430 | **R03** | **83.3%** | **80.7%（N=547）** |
| S4 | diverse citation（64 seeds 1-hop）+ quality-gated query（18）+ canonical identity | 19,194 | **R04**（进行中） | ？ | ？（N=R04 frozen pool） |

S4 关键中间数（identity reconciliation 后，2026-08-30 23:37 VERIFIED）：
- nominal seen 19445 → canonical **19194**（合并 251 个重复 identity 节点）
- citation_query_overlap：0 → **478**（OpenAlex/Scopus namespace 统一后真实共享论文——
  overlap=0 是 identity namespace 假象，**不是真零重叠**）
- S4 真正新增量 **5764**（= 19194 − 13430），候选成本增长率 +42.9%
- identity_unknown 885 → 873（candidate DQ 指标，≠ audit Seen=UNKNOWN）

口径 A 定义（§4 冻结）：每轮 N = 排除所有历史 audit samples 后、当前轮抽样前冻结的
实际 eligible sampling frame（R02: 1047 = 1572−25−500；R03: 547 = 1572−25−1000）。
口径 B（R02 用未排除 1547 → 43.0%）**不采用**——R03 样本从未从未排除池抽样，两轮间不成立；
仅存档审计性参考（spec §12）。

## 8. 可复用资产（本项目 tools/）

- `build_audit_universe.py`：fresh universe snapshot（同 frame）
- `audit_completeness.py --create --exclude-audit a,b`：排除多历史 sample 后 SRS
- `label_completeness_sample.py`：blind R/I/U 标注（断点续标）
- `build_r0X_seen.py`：Seen 三通道 join + 四核心数 + LCB（assert N）+ audit record 回写
- `recall_bound.py`：超几何反演单侧 95%
- `reconcile_s3_identity.py` / `reconcile_s4_identity.py`：多通道（EID/DOI/WID/title）
  canonical paper identity——**任何双通道 Search 执行器的红线断言：canonical 后 overlap 必须 > 0**
  （S4 实证：0→478；identity reconciliation 必须是 Search freeze 前的正式步骤，不是清洗附属项）

## 8b. Identity reconciliation 纪律（v2 新增，S4 实证）

1. **canonical paper identity 是 Search freeze 前的正式步骤**：双通道（Scopus EID namespace +
   OpenAlex DOI/WID namespace）执行器直接比较 key 恒为 0 是假象；必须 union-find 统一后再报
   overlap / seen size / 增量。
2. **title-based merge 必须 conservative normalized-title match**（完全相等，与 resolve_seen_s1
   同口径）；**禁止模糊语义 merge**（防误并不同论文）。title fallback 在跨数据库场景是重要
   canonicalization channel（S4：merge2 same-title/diff-IDs = 526），但只能用精确归一化匹配。
3. **candidate identity unknown ≠ audit Seen=UNKNOWN**：前者是候选数据质量（S4: 873/19194），
   后者是审计判定（join 后仍无法归属才标）；两者分别记录。
4. 已见判定对 WID-only 且 title 缺失的论文必须回查 canonical keys（S3_SEEN 有 262 WID keys）：
   三通道（eids/dois/titles）覆盖不到 → 补 `wids ∩ seen_keys` 直查。

## 9. 下一轮：R04（S4 已冻结，2026-08-30 23:50 用户拍板）

**S4 状态**：CONFIG / QUERY_POLICY(18) / CITATION_POLICY(P2_DIVERSE_64) / COMMUNITY_POLICY /
RETRIEVAL / IDENTITY 全 FROZEN-VERIFIED；S4_SEEN_SET = 19194；R04_READY = YES。
**停止 S4 development**——不因候选成本（+5764, +42.9%）回头缩 seed/query；ΔRecall vs
ΔCandidateCost 由 R04 后统一评价。

### 9.1 R04 协议补丁（用户 2026-08-30 23:50 拍板）

1. **frame 升级**：窄 frame（`241a7a93def7`, n=1572）被 R01+R02+R03 历史样本吃光
   （N_remaining=47 < 500，抽样框枯竭）→ R04 升级到宽 frame（`bae4cd5a5dc6`, n=5890，
   含 citation 扩展通道，`pc_001-2026-08-30T011615` 现成 snapshot）。N ≈ 4365，n=500 可行。
2. **paired comparison（关键补丁）**：R04 的**同一批 fresh sample** 上同时 join
   S3_SEEN_SET(13430) 和 S4_SEEN_SET(19194)——frame 变化不混叠，Δ(S3→S4) 是干净的同池
   配对增量。
3. **R03/S3=83.3%（窄 frame）只作历史轨迹上下文**，不与 R04/S4 直接连成同一 frame 下的
   纯性能轨迹。主指标 = paired Δ(S4−S3)。

**R04 流程**：
```
Fresh external audit universe（宽 frame bae4cd5a5dc6，同一 topic 外部定义）
→ exclude R01+R02+R03 整个历史 sample（--exclude-audit 三 id）
→ freeze eligible sampling frame（口径 A：N_R04 = 抽样前排除后池）
→ SRS n=500，新 seed（≠42, ≠7, ≠11, ≠21）
→ blind relevance labeling（R/I/U，不见 seen 信息）
→ join #1: S3_SEEN_SET = 13430（EID → DOI → WID → normalized title，模糊→UNKNOWN）
→ join #2: S4_SEEN_SET = 19194（同口径）
→ paired: Recall(S3|sample) vs Recall(S4|sample)，Δ = S4−S3
```

**R04 四组对比**：
1. **主指标（paired）**：R04/S3 vs R04/S4 resolved recall——同一 fresh sample 上 S4−S3 增量
2. conservative recall：S3 vs S4（同 sample，看 identity uncertainty 是否下降）
3. Recall_LCB：S3 vs S4（同一 N=4365 口径 A，paired）
4. 候选成本 13430 → 19194（+5764, +42.9%）→ ΔRecall/ΔCandidates 评价 generalization
   policy 成本是否值得

R04 第一次真正检验：**学到的是 repair policy，还是只是当前领域里扩大了候选池**。

### 9.2 R04 Paired 结果（2026-08-31 00:13 正式落盘）

```
FRAME_V1 narrow（241a7a93def7, n=1572）：
  S3/R03 resolved recall = 83.3%（core literature space 基准）

FRAME_V2 wide（bae4cd5a5dc6, n=5890, N_pool=4307, seed=31, n=500）：
  S3/R04 resolved recall = 50.0%（T/F/U=[27,27,10]）
  S4/R04 resolved recall = 51.8%（T/F/U=[28,26,10]）
  paired Δ = +1.8pp resolved / +1.6pp conservative / unknown 持平
  incremental rescue rate = 1/27 = 3.7%

S4 candidate growth: +5764 / +42.9%
Conclusion:
  S4 shows a small positive but not substantial cross-frame generalization gain
  (S4_GENERALIZATION_GAIN = WEAK / INCONCLUSIVE, NOT_VALIDATED).
  The dominant new finding is a large frame-dependent coverage gap.
```

**Frame V2 发现（比 +1.8pp 更重要）**：
- 窄 frame 大概率主要覆盖 Search 已熟悉的核心空间（83%）；宽 frame 暴露大量外围/长尾社区
  （50%）——`Core≈83% / Broader≈50%`，此前"83% → 再修 → 90%"的假设不成立。
- **Frame-level / domain-subspace overfitting**：S4 repair 学自 R03 narrow-frame miss
  distribution（dental-measurement 密集），R04 宽 frame 的 26 篇 miss 几乎全是
  epoxy/composites、electronics packaging、SLA/3DP、日文工业期刊——全新 miss distribution。
  R03 residual 37 vs R04 S4 residual 26 分布显著不同 → 针对上一轮 miss 优化的 repair
  policy 泛化有限。
- **LCB 归因纪律**：R04 LCB 14.5% ≠ completeness 从 80% 掉到 14%——sampling fraction
  11.6%（4307 抽 500）vs R03 91.4%（547 抽 500），超几何下界天然更宽。最可信性能比较 =
  paired（同 sample 同 frame）：S3 50.0% vs S4 51.8%。

### 9.3 下一步：Step 7 Cross-Frame Generalization Diagnosis（用户定，不叫 S5 repair）

S4 正式评估结束（不再修 S4）；R04 close → 26 篇 confirmed FALSE 转 development data。
第一步不是 TERM/CITATION，而是 miss distribution shift 分析：

```
问题：R04 26 篇为什么是 narrow-frame repair policy 从来没学到的？
比较：R03 residual 37 vs R04 S4 residual 26
维度：communities / years / vocabulary / citation connectivity / doc type /
      applications / mechanism category / graph distance to S3/S4 seeds
目标：证明 MissDistribution_R03 ≠ MissDistribution_R04
升级方向：被动 miss→repair → 主动 frame→discover communities→estimate undercovered
         regions→allocate search budget→audit（self-improving cross-community high-recall）
```

### 9.4 R05 — Independent Paired Evaluation of S5 Cross-Layer Query Expansion

S5 was frozen before R05 evaluation. Its canonical seen set contained 20,417 papers, compared
with 19,194 for S4, corresponding to 1,223 additional canonical candidates (+6.37%). R04
residual papers were not used as term sources for formal S5 query generation, and R04 recovery
was not used for S5 action selection.

R05 used the active wide audit frame (FRAME_V2_WIDE). After exclusion of previous audit
samples, the eligible frame contained N=3854 papers. A fresh simple random sample of n=500 was
independently labeled under the frozen blind title/abstract protocol, producing 99 RELEVANT,
399 IRRELEVANT, and 2 UNCERTAIN records.

The same R05 sample was joined independently against the frozen S4 and S5 seen sets.

For the 99 RELEVANT papers:

- S4: T/F/U=[34,46,19]
- S5: T/F/U=[37,43,19]

Resolved recall increased from 34/(34+46)=42.5% to 37/(37+43)=46.25%, for a paired gain of
**+3.75 percentage points**. Conservative recall increased from 34.34% to 37.37%, a gain of
+3.03 percentage points. The relevant-paper identity-unknown rate remained unchanged at 19.2%.
The fresh-frame recall lower confidence bound increased from approximately 11.6% to 12.7%.

S5 newly recovered three papers that were resolved misses under S4. Therefore the miss-rescue
rate among S4 resolved misses was **3/46 = 6.52%**, while the same three papers correspond to
the 3/80 = 3.75 percentage-point increase in resolved recall.

The S4→S5 transition therefore produced a positive independent paired recall gain while
expanding the canonical candidate set by only 6.37%. As an engineering-efficiency diagnostic,
3.75 pp / 6.37% = 0.589 percentage points of paired recall gain were obtained per 1%
candidate-set expansion.

For descriptive comparison, S3→S4 expanded the candidate set by 42.9% and produced a +1.8 pp
paired gain on R04, corresponding to approximately 0.042 pp per 1% candidate expansion. Thus
the observed gain-per-candidate-expansion ratio for S5 is approximately 14× that observed for
S4. This comparison is descriptive rather than inferential because R04 and R05 are different
independent samples.

R05 therefore provides the first fresh-audit evidence that the cross-layer query mechanism
generalizes beyond its development audit. However, the magnitude of the improvement remains
statistically uncertain because the paired improvement corresponds to only three additional
resolved relevant papers. The result should therefore be described as a positive and
substantially more candidate-efficient generalization signal, not as a statistically
established large recall improvement.

Cross-frame absolute recall values must not be interpreted longitudinally. In particular, S5
recall on R05 must not be directly compared with S4 recall on R04 because the audits use
different fresh samples with different relevant densities. The valid causal comparison for S5
is the paired S4-versus-S5 comparison within R05.

Status (2026-09-01 用户拍板):
- S5_INDEPENDENT_EVALUATION = COMPLETE
- S5_GENERALIZATION_SIGNAL = POSITIVE
- S5_GENERALIZATION_MAGNITUDE = MODEST / STATISTICALLY_UNCERTAIN
- S5_CANDIDATE_EFFICIENCY = STRONGLY_IMPROVED
- 不建议写 S5_GENERALIZATION = VALIDATED（3 篇新增救回对方向性证据足够，对幅度不够强）
