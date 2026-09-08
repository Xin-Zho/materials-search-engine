# v3.0 → S7 Agent 架构 North Star（架构基准 · 防漂移锚点）

**状态**：NORTH STAR FROZEN（2026-09-04 用户裁决）
**角色**：本项目的权威架构基准。所有"为什么不能直接用 audit miss 生 query"、"为什么 S5 数字不能跟 S3 比"一类决策，一律引用本文件，避免架构反复漂移。
**维护规则**：§1（冻结边界）与 §2.1（现状链路）随 S 演进更新；§3/§4/§6 为铁律与契约，仅经用户拍板可修订。Obsidian 镜像：`D:\Obsidian_personal\1-Projects\材料学科知识库-NorthStar-架构基准.md`，改动先改本 master。
**出处**：用户 2026-09-04 系统定义长文 + S5/R05 诊断 + S6 设计讨论裁决。

---

## 0. 系统一句话定义（用户原话）

> **高召回文献发现 Agent + 永久 Candidate DB + 相关性筛选 + 独立统计 Audit**

核心不变式（用户定，全程不可破）：
1. 搜索器负责找全，独立审计器负责判"还可能漏多少"，两者彻底分离。
2. **Retrieved Candidates ≠ Relevant Papers ≠ Final KB**——三个集合永久区分，禁止把 candidate 数量当 KB 数量。
3. 宁可多留 irrelevant，不错杀 relevant——relevance classifier 优先优化 **Sensitivity** 而非 precision（与常规搜索相反）。
4. 内部 saturation 只决定"暂时停止探索"，**不证明 completeness**；最终必须独立 Audit。
5. 固定 ontology（Property/Structure/Formulation/Application/…）是**组织框架**，不是**搜索空间边界**——允许 Agent 发现 ontology 无法表达的 region 并扩展 schema。

---

## 1. S0–S6 已冻结边界（不可覆盖）

| 轮次 | 内容 | 冻结产物 / 数字 | 状态 |
|---|---|---|---|
| S0 基线 | 初始检索 | seen 5986 | frozen |
| Terminology Repair | 95 miss → TermFamily(113) → Queryability Gate → Pilot → Relevance | S1_FINAL=19 queries | frozen |
| S1 | 19 queries @ depth=500 | S1_SEEN_SET=**8532**（S0 5986 ∪ repair 3471） | FROZEN |
| S2 | 19 queries @ depth=1000 | S2_SEEN_SET=**9873**（Δ vs S1 +1341）；R02 miss 追回 8/78，residual=70 | FROZEN；depth=1000 为基础设施参数不再调 |
| S3 | 70 residual → 四通道 repair → S3 | R03 独立审计：S3=**83.3%**（FRAME_V1 narrow） | 已冻结；**narrow frame，禁与 wide 横比** |
| R04 | fresh paired（FRAME_V2 wide） | S3=50.0% → S4=**51.8%**（+1.8pp @+42.9%）；S4 canonical=**19194** | **R04 已转 development data** |
| S5 | cross-layer query expansion | canonical=**20417**（nominal +1368 → canonical +1223 / +6.37%）；S5_IDENTITY_VERIFIED，IDENTITY_UNKNOWN=0 | FROZEN |
| R05 | fresh paired（N=3854, n=500, RELEVANT=99） | S4[34,46,19] → S5[**37**,43,19]；resolved **46.25%**（37/80）；efficiency ≈14×（descriptive） | **R05 已转 development data；R05 miss 不供词** |
| — | R05 miss taxonomy（43） | QUERY_EXPRESSIVITY 19(44.2%) / RETRIEVAL 3(7.0%) / BACKEND_UNREACHABLE 11(25.6%) / UNKNOWN 10(23.3%)；Scopus-reachable 22 miss 中 **19/22=86.4%** 为 query expressivity；Miss Query Reachability：MATCHES 10/43、**MATCHES_NO_QUERY 33/43=77%** | 诊断结论：主矛盾=query 表达范围，非同义词/深度 |
| S6 | LLM TermFamily V2 + semantic bridge（DESIGN FROZEN 2026-09-01，**2026-09-04 裁决不插架构改动**） | 两层架构：LLM TermFamily（semantic_role×domain 双轴）→ 确定性代码组装 Boolean query；三层验证 **VERIFIED_EXACT**（唯一进正式 query）/ **SEMANTIC_CANDIDATE**（只作 secondary 观测，见 §6-裁决2）/ REJECTED；7 domain 分批（dental 23/composites 32/sla 11/coatings 6/optics 12/packaging 2/general 16）；semantic bridge query `(curing OR polymerization OR photocuring) AND (observable) AND (context)` **不强制 AND shrinkage**；产物 s6_term_bank.json | DESIGN FROZEN；待跑 V2 TermBank → pilot/QA → freeze S6 |

**纪律红线**：
- R03 narrow 83.3% 与 R04/R05 wide 数字**不可横比**；可靠比较只发生在同一个 audit sample 的 paired 对比（S4↔S5 同 R05 sample）。
- R04/R05 全部为 development data：miss 只给 gap 方向，**禁供词**。
- **S6 一次只改一个变量**：只改 query 表达机制，不动 depth/后端/screening。

---

## 2. 演进路径：S5 现状 → S7 North Star

### 2.1 S5 真实链路（= 当前 = 检索系统的成熟版，不是完整 Agent）

```text
用户研究要求 → 初始 Query/领域词 → Scopus 检索 → Citation Expansion
  → Candidate Pool → Identity Reconciliation → Canonical Seen Set
  → Cross-layer Query Expansion（Property×Application / Structure×Formulation）
  → S5 Seen Set → 独立 OpenAlex Audit
```

S5 证明：cross-layer query 结构有效（+6.37% canonical），且效率远高于 S4 的大规模 citation expansion。瓶颈已定位 = **搜索系统不知道应该表达哪些搜索语言**（query expressivity），而非 Scopus 搜得不够深。

### 2.2 S7 North Star 目标链路（完整 Agent）

```text
用户研究要求 → Scope/Relevance Rubric
  → [OpenAlex Explorer（观察环境·DEV pool）] ↔ [已有 Knowledge Base]
  → High-recall Triage → Seed Relevant Corpus
  → LLM Knowledge Analyzer → Literature Region Map
  → LLM Query Planner（Region → Strategy → Query）
  → Scopus Search → Candidate DB（永久）→ Identity Reconciliation
  → Relevance Triage（RELEVANT/UNCERTAIN/IRRELEVANT 全保留）
  → [Final KB] / [Internal Evaluation → 下一轮搜索决策 → Query Planner ── loop]
  → 搜索达到停止条件 → Freeze Search vN
  → Fresh Independent Audit → Retrieval/Screening/E2E Recall → Recall LCB
```

### 2.3 S7 核心概念（用户定义，落地时不得走样）

- **OpenAlex Explorer = Search Agent 的观察环境**，不是最终检索器。解决冷启动："不知道领域叫什么 → 永远搜不到"。
- **Region ≠ 普通聚类**：不是 embedding→KMeans→20 簇。Region 是**具有语义和搜索意义的 literature community / research hypothesis**（例：Dental polymerization stress / Vat photopolymerization dimensional error / EMC package warpage / Composite cure-induced distortion），允许 merge / split / discover new region，不固定类别数。
- **Region → Strategy → Query 三层**是 S7 主算法层级。一个 Region 展开多个 Strategy（Mechanism / Observable / Formulation / Process / Measurement），每个 Strategy 再产生若干 query。
- **软证据通道**：Evidence term（高置信）与 Discovery term（探索）分流；Evidence 决定**可信度与优先级**，不决定"搜索资格"（假 term 成本≈0，宽泛错误 term 由 pilot/relevance yield 淘汰）。
- **搜索内四级评估**：Query 级（new unique/new relevant/precision/cost）→ Strategy 级（跨 query 重合度/独立论文群）→ Region 级（新增 relevant 趋势/新 concept/新 relation/coverage gap）→ Global 级（继续/换 strategy/换 region/发现新 region）。
- **P2 才上 Bandit/RL**：等 state/region/strategy/query/reward/cost 大量日志后再学"哪个 Region+Strategy+Query 收益最高"。现在 action space 未定义，直接 RL 无意义。

### 2.4 里程碑依赖

```text
S6（TermFamily→Query，semantic bridge 已冻结 2026-09-07）
  → freeze S6 trajectory（S6_SEEN=21782；轻量 freeze，可引用版本）
  → S7 Query Planner v1（LLM relation proposal + deterministic executor + reward 筛选，
     不用 RL——样本不足；OpenAlex Explorer 作观察环境按 P1 顺序引入）
  → Search vN freeze（S6 bridge ∪ S7 v1 首批 query 整体冻结）
  → R06 三层 recall audit（§4；一次审计回答"S6+S7 vs S5"的 paired Δ，
    不为过渡版 S6 单独烧一次 audit）
  → 达标/诊断
  → P2（RL/contextual bandit——攒够 100-500+ trajectory 后；budget allocation +
    统计停止 + multi-backend routing）
```

> **里程碑变更（2026-09-07 用户裁决）**：R06 不再紧贴 S6 单独执行。S6 是第一个
> semantic operator 而非最终系统；R06 若现在跑只回答"S6 bridge 比 S5 好多少"，
> 而真正要回答的是"Agent Query Planner 能否持续发现新区域"——故 R06 推迟到
> Search vN（S6+S7 v1）整体 freeze 后合并审计。

---

## 3. Explorer / DEV / AUDIT 数据隔离规则（铁律）

1. **R04、R05 均已转 development data**：禁用于 S6/S7 词、query、region、policy 的供词/拟合，只允许提供 gap 方向。
2. **Audit sample 在 Search vN freeze 之前，不得以任何形式参与 query/term/region/policy 决策**（含间接：如被 LLM prompt 引用）。
3. **OpenAlex DEV / Explorer Pool**：可以给 Agent 看（观察环境，用于学习真实论文语言）。
4. **OpenAlex AUDIT Holdout**：Agent 永远不能看。最终 fresh sampling 只从 holdout / 独立 universe 出。
5. 更强形式（可升级）：Explorer = OpenAlex；Audit = **独立 multi-source universe**。
6. 被 Explorer 看过的论文，**不得再作为独立测试证据**（防 Search Agent 已见测试集）。
7. Audit miss 归因后的修复方向 → 转 dev，进下一轮搜索；**禁止在同一 audit sample 上重报 recall 作为改进证据**。

---

## 4. R06 三层 Recall 契约（2026-09-04 拍板，首次正式拆分）

**口径纠正（用户裁决，写死防复发）**：R05 的 37/99=37.37% **不是 End-to-End Recall**。它是混合口径——分母 99 含 19 个 identity/seen UNKNOWN 被保守当作未找到，不是"检索成功但 screening 错杀"的专门度量。R06 才第一次按下面公式正式拆干净。

三个集合操作定义：
- **AuditRelevant** = R06 fresh audit sample 中独立判定相关的论文全集
- **CandidateDB** = S6 freeze 后 canonical seen set（永久保留，无删除）——retrieval 层的产物
- **FinalKB** = 用户最终拿到的知识库论文（通过 relevance screening + 收录判定）——screening 层的产物

三层指标（R06 起唯一正式口径）：

$$
Recall_{retrieval} = \frac{|AuditRelevant \cap CandidateDB|}{|AuditRelevant|}
$$

$$
Sensitivity_{screen} = \frac{|AuditRelevant \cap FinalKB|}{|AuditRelevant \cap CandidateDB|}
$$

$$
Recall_{E2E} = \frac{|AuditRelevant \cap FinalKB|}{|AuditRelevant|}
= Recall_{retrieval} \times Sensitivity_{screen}
$$

**报告要求**：
- 必须 paired：同一 audit sample 上报告 S5 vs S6，只看 Δ。
- resolved 主口径 + ultra 保守口径（UNKNOWN 全按 miss）双报告。
- **Backend Reachability / Identity Unknown / token 失败等为独立 diagnostic 维度，不混入三层指标名称**；miss 归因沿用 R05 taxonomy（QUERY_EXPRESSIVITY / RETRIEVAL / BACKEND_UNREACHABLE / UNKNOWN）作为独立分层，禁止把所有失败统称 "Search Miss"。
- 用户最终关心的唯一指标：$\boxed{Recall_{E2E}}$；诊断时看三层分解定位瓶颈（retrieval 没捞到 vs screening 错杀）。

### 4.1 R06 前必须固定的规则（用户：审计前定死，禁止审计后再裁决）

**① Audit 侧 UNKNOWN（identity 无法 reconcile）如何计入**——推荐（与 R02/R05 历史一致）：
- resolved 主口径：AuditRelevant 分母先排除 UNKNOWN，再算三层；
- ultra 保守口径：UNKNOWN 全部按 retrieval miss 计入（Recall_retrieval 分子不加）。
- 主结论用 resolved；ultra 作保守下界；两数都报。

**② CandidateDB membership**：canonical identity 匹配即 ∈ CandidateDB；无法 reconcile 者记 IDENTITY_UNKNOWN diagnostic，不计入 retrieval hit 也不算 clean miss。

**③ FinalKB membership——screening UNCERTAIN 归属**（**2026-09-04 已冻结 R1+R2 双报**，用户拍板）：
- **主口径 R1（operational）**：**UNCERTAIN ∈ FinalKB**（RELEVANT + UNCERTAIN 都视为进入 FinalKB）。理由：与 staging 实际语义一致——UNCERTAIN 是"未排除"，不是负例，且继续进入 extraction。系统真实运行口径。
- **严格下界 R2（strict）**：仅 RELEVANT 计入 FinalKB，UNCERTAIN 全视作 screening miss。用于直接观察 screening 不确定性对最终 recall 的影响。

**④ UNKNOWN（Audit 侧 identity）按 resolved/ultra 双报**（①已定）。三组概念——**UNKNOWN（identity）、screening miss、retrieval miss——禁止合并成一个数字**。

### 4.2 R06 固定输出（2026-09-04 冻结）

```text
Retrieval Recall
Screening Sensitivity
- R1 operational      （UNCERTAIN ∈ FinalKB）
- R2 strict           （仅 RELEVANT ∈ FinalKB）
End-to-End Recall
- R1 operational
- R2 strict
Identity Unknown Rate     # ← diagnostic，不混入三层
Backend Reachability      # ← diagnostic，不混入三层
UNCERTAIN Rate            # ← diagnostic，不混入三层
```

恒等式（R06 报告必须自洽）：

$$
Recall_{E2E}^{R1} = Recall_{Retrieval} \times Sensitivity_{Screen}^{R1}
$$

$$
Recall_{E2E}^{R2} = Recall_{Retrieval} \times Sensitivity_{Screen}^{R2}
$$

---

## 5. 逻辑坑清单（长期盯，8 项，用户定义）

1. **Search / Audit 相互污染**（最危险）：audit miss → 直接生 query → 又同 sample 报 recall。禁止。
2. **把内部 saturation 当"搜全"**：最近 3 轮只 +1 篇 ≠ 没有另一个 disconnected 文献社区。saturation 只是暂停探索的理由。
3. **把 Region coverage 当 recall**：Dental✓/SLA✓/Packaging✓ ≠ 每个方向内部完整。
4. **用 Explorer 数据证明最终 recall**：给 Agent 看过的 warp/spring-in/curl 论文不能再作独立测试证据。
5. **classifier 高 precision 杀 recall**：本任务宁滥勿缺，优化 Sensitivity。
6. **把 Candidate 数量当 KB 数量**：Retrieved ≠ Relevant ≠ FinalKB，永远分开。
7. **不同 frame 的 recall 横比**：83.3%（narrow）与 46.25%（wide）不可直接比较；同 sample paired 才可靠。
8. **固定 ontology 限制未知方向发现**：ontology 是组织框架不是搜索边界，允许 Agent 提出"现在 ontology 表达不了的 region"并扩 schema。

---

## 6. 决策记录（拍板链，防漂移）

| 日期 | 决策 | 影响 |
|---|---|---|
| 2026-09-01 | LLM 进入 Query Generator（LLM 创造动作 / RL 选择动作）；两层架构（TermFamily→确定性 query）；V2 三层验证（semantic_role×domain）；7 domain 分批；semantic bridge 不强制 AND shrinkage | 冻结 S6 设计 |
| 2026-09-04 | 系统定义 = §0；S7 North Star = §2.2；8 坑 = §5；P0 三项 audit 侧已成立，无插队代码改动 | 本文件落盘 |
| 2026-09-04 | **张力 A**：S6 不动 VERIFIED_EXACT 主通道；SEMANTIC_CANDIDATE 只记录 secondary pilot 信号，**R06 后**再决定是否升级 discovery channel | S6 完全保持冻结 |
| 2026-09-04 | **张力 B**：S6 继续 TermFamily→Query；Region→Strategy→Query 整体作为 **S7 核心架构**，与 OpenAlex Explorer 一起引入 | Region 不在 S6 做 |
| 2026-09-04 | **R06 三层 recall 契约**（§4 公式）；Backend Reachability / Identity Unknown 作 diagnostic 不混入指标名；R05 37.37% 不叫 E2E | §4 冻结 |
| 2026-09-04 | UNCERTAIN 归属、UNKNOWN 计入规则在 R06 前定死（§4.1 默认建议待用户终裁） | R06 freeze 前完成 |
| 2026-09-04 | **R06 契约终裁：R1+R2 双报冻结**——R1(operational)=RELEVANT+UNCERTAIN∈FinalKB（与 staging"未排除"语义一致）；R2(strict)=仅 RELEVANT∈。输出格式与两条恒等式见 §4.2。UNKNOWN/screening miss/retrieval miss 禁止合并 | §4.2 冻结 |
| 2026-09-04 | 架构决策整体冻结：**S6 不再插改；R06 三层 recall + R1/R2 双口径；R06 完成后才启动 S7 Agent 化** | 推进顺序锁定 |
| 2026-09-07 | **S6 pilot 完成并 FROZEN**：RUBRIC_V1 冻结（硬前提+三边界裁决）；全量 QA R63/U57/I1245；去重主结论 **A_non_exp R43、B 独占 R=0** → shrink-anchor 相关层 ⊂ anchor-free（S6 假设成立且更强）；产物 s6_seen_set(21782)/trajectory/manifest | S6 = FROZEN_DEVELOPMENT_PILOT（可引用） |
| 2026-09-07 | **里程碑修正：R06 推迟**。S6 只是第一个 semantic operator，R06 现在跑只答"S6 vs S5"；R06 移至 **Search vN（S6+S7 v1）整体 freeze 后合并审计**（§2.4） | R06 里程碑依赖变更 |
| 2026-09-07 | **S7 Query Planner v1 定形**：LLM relation proposal（概念关系层，非 query 层）+ deterministic executor；不用 RL（样本 17 太少）；Reward v1 固定为 **ranking function**：`2R + U − 0.5·Redundancy − 0.01·log(hits+1)`（非最终 reward，1000+ trajectory 后再 bandit/offline RL） | S7 设计基线 |
| 2026-09-07 | S6 教训入 training：C 类失败（机制×机制关系在论文语言中不存在）、D 类 domain drift（跨域需第三约束）→ relation 生成必须带"是否可能出现在论文表达中"的 LLM 判断 + 失败 trajectory 反馈 | S7 输入设计 |

---

## 附：当前唯一行动（截至 2026-09-04，勿扩散）

```cmd
set DEEPSEEK_API_KEY=sk-你的key
.venv\Scripts\python.exe tools\llm_term_expander.py --provider deepseek --model deepseek-chat
```
→ 看 VERIFIED_EXACT 规模 + domain 分布 → semantic bridge query 组装 → pilot/QA → freeze S6 → fresh R06（按 §4 契约出报告）。
若 VERIFIED_EXACT 仍仅 20 来条且集中 dental → 结论：63 篇 corpus 语言边界太窄，扩 corpus 而非调 prompt。
