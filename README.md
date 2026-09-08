# Materials Search Engine — Literature Discovery Agent

> **v1.0 delivers an end-to-end materials literature discovery agent with relation-driven search, relevance screening, community-based exploration, search-memory feedback, and independent external-frame recall auditing.**

状态：**v1.0 已发布**（`release/v1.0` @ `ef11d23` + tag `v1.0`，[GitHub Release](https://github.com/Xin-Zho/materials-search-engine/releases/tag/v1.0)）｜ 后续开发在 `main` 进行（见 [Roadmap v2](#roadmap-v2)）

---

## 这是什么

传统文献检索优化的是 Top-K 相关性；本项目的目标相反——**如何尽可能完整地发现一个研究主题下的相关论文，并能量化系统漏掉了什么、为什么漏、下一步如何自动扩展搜索空间**。

v1.0 是一套**可完整运行的端到端文献发现 Agent**，不是纸面架构：relation-driven search、blind relevance QA、community exploration、search-memory feedback、独立 external-frame audit 均已真实跑通并出审计报告。主题不要求是光固化——重建 seed corpus / TermBank / relevance rubric 即可跑同一套 pipeline（新主题 bootstrap 见 quickstart C 节）。示例主题：**pc_001 = photopolymerization / polymerization shrinkage & shrinkage stress**。

> 三层数据结构原则：所有检索到的论文先进 **Candidate DB**（不因预算物理删除）；再做 **RELEVANT / UNCERTAIN / IRRELEVANT** 三态筛选（IRRELEVANT 保留记录、留审计轨迹）；仅 RELEVANT + 部分 UNCERTAIN 进入 **Knowledge Base** 的结构化抽取。budget 只控制"先处理谁"，不控制"谁有资格被保存"。

## 端到端流水线

```text
用户研究问题
   ↓
初始语义 / TermBank（VERIFIED_EXACT 40，词源三层纪律）
   ↓
Relation / Query Planner（S6 bridge 17 actions / S7 variant families）
   ↓
Scopus retrieval（+ OpenAlex semantic bridge，多后端）
   ↓
Candidate DB（canonical seen，doi ∪ Scopus EID ∪ OpenAlex W id 三通道）
   ↓
Blind relevance QA（LLM blind + 人工校准 RUBRIC_V1，防确认偏差）
   ↓
Community discovery（论文级 verdict → KEEP 池，簇级过滤）
   ↓
Relation memory / failure memory（EX 源 + ZERO_HIT + NO_NEW + trajectory）
   ↓
Reward feedback（Reward_v0：αR_count + βR_density + γCommunity_gain − λCost − μRedundancy）
   ↓
下一轮主动搜索（coverage-guided / community-driven）
   ↓
论文输出（S8 FinalKB 收录 → Knowledge Base 增量）
   ↓
独立 Audit（fresh sample 三层 recall，外部 blind 判定）
```

## 已实证的关键结果（真实跑出的数字）

| 环节 | 证据 | 数值 |
|---|---|---|
| S6 semantic bridge | TermBank 40 VERIFIED_EXACT（lexical 8 / mechanism 13 / observable 19）+ 16 SEMANTIC；17 bridge actions | freeze manifest sha256 锚定 |
| S6 QA | 全量盲评（RUBRIC_V1） | R 63 / U 57 / I 1245 |
| S7 community discovery | 24 簇 → 6 KEEP 簇；论文级 QA 终裁 | **R 106 / U 22 / I 622**（R 率 14.1%，全量 7.4% 的 2.31× 富集）|
| S7 coverage-guided 探索 | 46+10 RUN 全处置；EX-04 单组贡献 84/106 R | 净新增相关 **107 篇** |
| S8 KB 增量 | 轻收录先行、分批 commit | FinalKB 249 篇（R170/U79）；KB records **64 → 216**、edges 274 → 700 |
| R06 独立审计 | fresh sample n=500（R05 无重叠）、外部 blind 500/500 | 三层 recall + R1/R2 双报 + 恒等式自洽（口径见下）|

## R06 审计口径（冻结，禁误读）

> **R06 external-frame Retrieval Recall = 13.9% (resolved strict)，E2E Recall = 2.4%；该结果针对冻结的 OpenAlex FRAME_V2 external audit frame，不代表材料领域的绝对召回率，不是对整个系统 recall 的估计。**

E2E 低是三处 frame/策略系统性错位（`universe ∩ seen = 7.0%`、`KB ∩ frame = 38.4%`）叠加 Sens 漏斗（23 触达 relevant → 4 入 KB）的结果，已拆解为两个独立问题：

1. **Retrieval / frame alignment**（研究型，Roadmap v2 #2）：Search vN 沿 Scopus + relation/community 探索构建，与旧 OpenAlex wide frame 是不同搜索空间
2. **Screening / promotion 漏斗**（工程修复项，Roadmap v2 #1）：23 篇已触达 relevant 仅 4 入 KB——与搜索质量无关，纯 Candidate → KB 晋升管线缺口

完整归因见 [`docs/2026-09-08-r06-recall-audit-report.md`](docs/2026-09-08-r06-recall-audit-report.md)（§8 冻结口径）。

## 快速开始

详见 [`docs/2026-09-08-v1.0-quickstart.md`](docs/2026-09-08-v1.0-quickstart.md)（A 零 API 复核 / B pc_001 端到端复跑 / C 新主题 bootstrap 7 步）。

依赖：`requirements.txt` + `pyproject.toml`（Python ≥ 3.11）。OpenAlex 后端无需登录；Scopus / LLM 路径需密钥（如 `DEEPSEEK_API_KEY`）。数据层纪律：`data/` 不入 git，冻结产物以 `data/exports/releases/v1.0_manifest.json` sha256 锚定；一切写操作 dry-run → 质量复核 → 分批 commit。

```bash
# 安装
python -m venv .venv && .venv/Scripts/pip install -e .[dev]

# 零 API 复核：R06 三层 recall 重算（labels 已回填）
.venv/Scripts/python tools/compute_r06_recall.py --labels data/exports/completeness_labels/pc_001__20260908012830_filled.json

# 零 API 复核：v1.0 完整性校验（39 文件；git 跟踪文件锚 v1.0 tag blob、CRLF 无关）
.venv/Scripts/python tools/verify_v1.0_manifest.py

# 测试
pytest tests/
```

## 仓库结构

```text
search_engine/         # 核心引擎：检索 / canonicalization / QA / 知识抽取与 KB / query & relation 规划 / discovery
tools/                 # 一阶段脚本：S6-S8 pipeline、community、reward、audit、manifest（各自 --help）
docs/                  # 审计报告、release/quickstart、ROADMAP
docs/specs/            # North Star 口径总纲 + 阶段设计 spec
data/                  # 产物与缓存（不入 git；冻结清单见 releases/v1.0_manifest.json）
tests/                 # pytest
```

## 文档索引

| 文档 | 内容 |
|---|---|
| [docs/2026-09-08-v1.0-release.md](docs/2026-09-08-v1.0-release.md) | v1.0 release note：组件 → 文件映射交付清单 |
| [docs/2026-09-08-v1.0-quickstart.md](docs/2026-09-08-v1.0-quickstart.md) | 一键运行：零 API 复核 / 端到端复跑 / 新主题 bootstrap |
| [docs/2026-09-08-r06-recall-audit-report.md](docs/2026-09-08-r06-recall-audit-report.md) | R06 三层 recall 审计报告（含冻结口径 §8）|
| [docs/ROADMAP.md](docs/ROADMAP.md) | Roadmap v2（含命名消歧）|
| [docs/specs/2026-09-04-agent-architecture-north-star.md](docs/specs/2026-09-04-agent-architecture-north-star.md) | 架构口径总纲（8 坑 / 三层 recall 契约 / 恒等式）|
| `data/exports/releases/v1.0_manifest.json` | v1.0 完整性锚（39 文件 sha256）|

## Roadmap v2

v1.0 不背 completeness 承诺——"这个材料方向找全了多少"的证明属 v2：

1. **#1 Promotion / Knowledge Extractor 修复**（先）：23 → 4 收录漏斗、QA R 无 abstract 缺口、U 缓收、knowledge coverage 常态化 + 统计停止条件
2. **#2 Audit frame v2 重建 + completeness proof**（后）：frame 与 Search vN 搜索空间对齐（KB∩frame 83 篇 + seen 社区 + 知识薄区为种子）、seen 三通道全保留、fresh audit

> 命名消歧：交付版本轴（v1.0 / v2.0）与仓库早期内部命名（Phase 2.0/2.1 "Query-Family Diversification"，已随 v1.0 交付）是两条不同轴，勿混用。

## License

MIT（`pyproject.toml`）。维护：Xin-Zho。
