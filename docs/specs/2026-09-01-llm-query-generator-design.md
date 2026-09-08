# LLM Query Generator v1 设计（S6 Query Mechanism 候选）

**状态**：DESIGN FROZEN（2026-09-01 用户拍板）
**出处**：R05 诊断 → 33/43 MATCHES_NO_QUERY（77%）→ query 表达范围太窄不是同义词问题

## 问题定义：三层表达缺口（用户定）

不是"polymerization shrinkage 同义词没找全"，而是同一个物理机制在不同语境被不同词描述：

1. **Lexical expansion（传统同义词）**
   polymerization shrinkage / curing shrinkage / cure shrinkage / volume contraction /
   volumetric contraction / chemical shrinkage
2. **Mechanism expansion（机制重构）**
   cure-induced stress / residual strain / process-induced deformation / internal strain /
   contraction stress
3. **Observable expansion（可观测结果）**
   warpage / dimensional error / dimensional instability / shape distortion / cuspal
   deflection / positional displacement
4. **Domain translation（领域术语体系）**
   - Dental → cuspal deflection, marginal gap, contraction stress
   - Composite manufacturing → spring-in, warpage, residual strain
   - Stereolithography/3DP → curl distortion, dimensional accuracy, compensation factor
   - Optical assembly → positional drift, angular displacement
   - Electronics packaging → package warpage, molding compound deformation

```text
Core Mechanism（curing/polymerization → volume change → stress/deformation）
        ×
Domain Expression（各领域如何描述它）
        ×
Application
```

## 架构定位（用户定）

```text
Known relevant knowledge
  → LLM Query Generator（创造动作：本工具）
  → 大量 semantic / cross-domain query candidates
  → Bandit / learned policy（选择动作：后续，非本轮）
  → Search → new relevant yield
```

LLM 负责创造动作，RL/Bandit 负责选择动作——两者不同问题，LLM 在前。

**两层分离（用户 2026-09-01 16:33 拍板）**：
- 第一层 `tools/llm_term_expander.py`：LLM 只做概念扩展——输出 TermFamily
  {core_concept, lexical_terms, mechanism_terms, observable_terms,
  application_specific_terms, evidence}，**不生成 query**
- 第二层（确定性代码）：TermFamily → Boolean query（LLM 不碰 Scopus 语法/括号/AND/OR）
- 分工：**LLM 负责语义理解，程序负责 query 语法**——便于审计
- 第一版 prompt 六条约束：①只基于 provided known-relevant 文献生成术语
  ②每个新术语必须给 evidence paper_id ③区分 lexical/mechanism/observable/application
  ④禁 R05 miss 词作 evidence ⑤严格 JSON ⑥不生成 query
- evidence 双校验（程序侧）：evidence_paper_id 必须真实存在于 corpus + term 必须
  子串命中该 paper 的 title/abstract；未通过 → UNVERIFIED 不进后续 query

## 输入（v1 实现，tools/llm_cross_layer_generator.py）

1. Core phenomenon（固定：polymerization/curing induced volume change → stress/deformation）
2. KnownRelevantKnowledge 四层 families（复用 build_cross_layer_queries 的 LAYER_RULES/
   FAMILY_ALIASES/extract_paper_tags，词源=found_relevant+openalex abstract；实测 63 篇 →
   PROPERTY 6 / APPLICATION 12 / FORMULATION 13 / STRUCTURE 6）
3. 已执行 query（S5 36 条 frozen）→ prompt 防重复 + 规则 novelty gate（token 重叠 ≥70% 拒）
4. Gap hint（R05 诊断的**方向**：缺 OBSERVABLE/DOMAIN 类表达；**不含任何 R05 miss 术语**）
5. Output schema：{expression_family, application, expansion_type, query, rationale}

## 硬规则（prompt + 代码双层）

- 每个 query 必须含 shrinkage 核心锚（SHRINKAGE_CORE 类）AND 至少一个 domain/observable/
  mechanism family——裸 "warpage" / "3d print" 禁止（S5 V1 爆炸教训）
- **term-level 证据链（v1.1 新增，用户 2026-09-01）**：LLM 只输出 term family，
  不直接输出 query；工具对每个 term 做 corpus 验证（子串命中 known relevant 论文的
  title/abstract → 记录 source_paper + source_sentence）；**验证失败的 term 标
  UNVERIFIED 且不进 query**（防 LLM 编造，可追溯）
- query 由 verified terms 组装（SHRINKAGE_CORE AND verified family terms）
- 术语必须来自提供的 corpus families / 四类 expansion——禁止编造新材料名
- 不重复已执行 query（novelty gate）
- specificity gate：query 必须含领域标记词（warpage/spring-in/curl/deflect/packaging/sla/
  optical/...），防 "shrinkage AND resin" 退化

**corpus 词汇实证（63 篇 found_relevant，2026-09-01）**：
可验证的 OBSERVABLE/mechanism 词：stress(22) strain(8) gap(4) warpage(2) deflection(2)
accuracy(1) deformation(1)；corpus 零命中的 domain 词（LLM 输出会被 UNVERIFIED drop）：
distortion / displacement / spring-in / curl。→ S6 的 term source 边界由 corpus 决定，
corpus 没有的 domain 词不进正式 query（宁可 drop 不编造）。

## 纪律（写死，不可破）

```text
R05 miss papers as term source = FALSE（R05 已用于评价 S5，只作 development evidence）
Gap hint is direction-only = TRUE
S6 评估 = fresh R06 paired（S5_SEEN vs S6_SEEN，同一 fresh sample）
Pipeline = 生成 → run_s3_query_pilot（--new-basis s4 dev 旁路）
        → quality gate → candidate QA → freeze S6 → R06
```

## 验证链（复用现有，零新增机制）

```text
llm_cross_layer_generator.py  → s6_llm_query_candidates.json（actions 兼容）
  → run_s3_query_pilot.py --actions ... --new-basis s4（dev 旁路）
  → quality gate（hits/new/new_ratio/saturation/candidate QA 同 S5）
  → freeze S6 → formal retrieval → identity reconciliation → S6_SEEN
  → fresh R06 paired（Δ = Recall(S6|R06) − Recall(S5|R06)）
```

## 与本轮其他产物的关系

- ScopusReachability（tools/scopus_reachability_check.py）：并行推进——它把 43 miss
  拆成 BACKEND_UNREACHABLE / QUERY_EXPRESSIVITY / RETRIEVAL，与 LLM 生成互补
  （reachability 决定哪些 miss 算 query 的锅，LLM 修 query 的锅）
- depth 实验（run_search_s5_depth.py）：INVALID（offset 失效），不阻塞本工具

### V2（domain-conditioned expansion，用户 2026-09-01 17:01 拍板）

1. **双轴数据结构**：term = semantic_role（lexical/mechanism/observable）× domain
   （general/dental/composites/sla/coatings/optics/packaging）——不再用 `type+application`
   单轴（V1 把 "shrinkage force" 误标成 composites 应用表达）。
2. **三层验证**：
   - VERIFIED_EXACT —— evidence paper 有原字符串（正式 S6 query 唯一允许）
   - SEMANTIC_CANDIDATE —— LLM 语义归纳但原 paper 无原词（不删；全 corpus 反查真实
     语言证据，corpus_hit 或 none 都保留待更大 TermBank 反查）
   - REJECTED —— evidence id 编造且全 corpus 无该词
   - 分工：LLM 提出概念，corpus validation 找真实语言证据。
3. **domain 分批**：63 篇按 DOMAIN_RULES 多标签分组（实测：dental 23 / composites 32 /
   sla 11 / coatings 6 / optics 12 / packaging 2 / general 16），每批一个 prompt
   "该领域如何描述 cure shrinkage 的机制/观测/表达"——目标几十到几百条 TermBank。
4. **日志守恒**：raw_terms / dedup_dropped / validated_terms / VERIFIED_EXACT /
   SEMANTIC_CANDIDATE / REJECTED 全打印 + assert 守恒。
5. **semantic bridge query（后续 query composition 用）**：允许
   (curing OR polymerization OR photocuring) AND (warpage OR cusp deflection ...)
   AND (context)——不再强制 AND shrinkage（防杀 expressivity；77% MATCHES_NO_QUERY
   的根源之一）。
6. 产物：s6_term_bank.json（V1 的 s6_llm_term_candidates.json 保留不动）。
