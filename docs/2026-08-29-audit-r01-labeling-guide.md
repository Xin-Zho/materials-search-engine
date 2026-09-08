# AUDIT_R01 独立标注指引（pc_001）

创建日期：2026-08-29
审计对象：`pc_001::20260829043656`（universe_hash `8ffaff83211cfb54`）
样本：500 篇（seed=42，SRS 不放回，**已冻结**——标注过程中不得重新抽样）
对应搜索快照：`v2.1-cross-community-validated`（Search S0；F=25 为 create 时刻 KB 已确认 relevant）

## 1. 本次审计回答的问题

> 在冻结的 pc_001 文献宇宙（1572 篇，OpenAlex 外部宽检索构建，独立于主搜索）中，
> 当前搜索快照 S0 还有多少 relevant 文献没找到？

这是**独立概率估计**（超几何反演 → 剩余 relevant 单侧 95% 上界 → Recall 下置信界），
与 QGS-v1 的 84.8% relative recall 是两种不同的数。

## 2. 判定标准（RELEVANT）

论文主题落在以下任一范畴即标 **RELEVANT**（pc_001 定义 version 4，宽 umbrella 语义）：

- **问题域**：光固化 / 光聚合（photo-polymerized）聚合物体系的聚合收缩（polymerization
  shrinkage）、体积收缩、收缩应力（shrinkage stress / contraction stress）、
  界面脱粘（interfacial debonding / bond failure）
- **材料**：牙科复合树脂（dental composite / resin composite）、树脂修复体、
  光固化涂层、光固化粘接剂、其他光固化聚合材料（含 historical 术语变体：
  flowable / microfilled / posterior composite / glass-ionomer 混合体系等）
- **机制/方法**：收缩的测量方法（dilatometer / linometer / bonded-disc /
  volumetric shrinkage 测量）、固化深度与转化率（degree of conversion）、
  光引发体系（photoinitiator / camphorquinone / light curing 光源）、
  收缩应力测试（cantilever / stress-strain / flexural）、后固化（post-cure）、
  老化对收缩/应力影响
- **临床/应用效应**：边缘密合（marginal adaptation / gap formation）、
  微渗漏（microleakage）、修复体寿命（longevity / failure rates）、
  牙尖偏斜（cuspal deflection）

判定依据：**title + abstract 为主**（模板已提供；abstract 缺失的以 title + doi 推断，
拿不准就标 UNCERTAIN）。

## 3. 三态用法（2026-08-29 冻结，废止 Phase 3 二值规则）

| 标签 | 含义 | 处置 |
|---|---|---|
| `RELEVANT` | 明确落在上述范畴 | 计入统计 positive |
| `IRRELEVANT` | 明确不相关（非光固化、非收缩主题、纯无关领域） | 计入 negative |
| `UNCERTAIN` | 信息不足 / 边缘情况 / 判断不了 | **单独报告 + 后续 adjudication，不混入 negative，不影响 m 统计** |

纪律：

- **不能把未标注默认成 irrelevant**（程序对 UNRESOLVED 拒绝出 Recall_LCB，这是对的）
- 不因"论文老/新"、"语言风格旧"影响判断——historical terminology 论文（如
  methacrylate esters、dilatometer 早期文献）只要主题相关就是 RELEVANT
- 一篇论文只要**任一**相关方面成立即 RELEVANT（宽 umbrella 语义，宁宽勿窄——
  窄判会低估 universe 内 relevant 总量）

## 4. 标注工作流

**推荐入口（交互式逐篇标注，实时保存、断点续标）：**

```cmd
python tools/label_completeness_sample.py --audit-id pc_001::20260829043656
```

每篇显示 `[i/500]` Title / Year / DOI / Abstract，按键：

```text
[R] RELEVANT   [I] IRRELEVANT   [U] UNCERTAIN   [S] 保存进度并退出
```

- 每标一条立即写盘（原子替换，中断不损坏文件）
- 中途退出后重跑同一命令，自动跳过已标注条目继续
- U 时提示填一句 reason（可选）
- 全部标完自动提示下一步统计命令

**或直接编辑模板**：`data/exports/completeness_labels/pc_001__20260829043656.json`
（把 `label` 改为三态之一；不要增删条目、不改 paper_id）。

**500 条全部标完后执行：**

```cmd
python tools/audit_completeness.py --audit-id pc_001::20260829043656 --labels data\exports\completeness_labels\pc_001__20260829043656.json
```

3. 程序输出正式数字：
   - `MissRate = m/500`（m = 样本中 RELEVANT 且未被 S0 找到的论文数——注意：
     样本内 RELEVANT 论文若已在 KB，不算 miss，程序按 agent_seen 判定）
   - `RemainingRelevantUpper_95`（超几何反演，单侧 95%）
   - `RecallLCB_95`（= F / (F + M_upper)）
   - UNCERTAIN 单独计数报告

## 5. 标注后（Phase B 衔接）

真实 misses（RELEVANT ∧ agent_seen=false）自动成为 Miss Expansion Agent 的输入：

```cmd
python tools/analyze_audit_misses.py --mode audit --audit-round pc_001::20260829043656
```

看真实 evidence coverage（citation / community / term / execution）→ 决定下一阶段
补什么数据（citation enrichment 已备好接口 `tools/enrich_citation_graph.py`，等真实
misses 验证是否实际瓶颈再批量投入）。

## 6. 统计纪律提醒

- 本次审计对应 **Search S0**（v2.1 冻结后、Round3 结果未入库的状态）；修复后进入
  Search S1 时，必须用**新的独立 Audit Round**（新样本）验证效果——miss 论文一旦
  用于修复即成为开发数据，不能再作测试证据。
- 核心曲线：`p_hat_miss^(t)` 与 `CI_upper^(t)` 双下降 = 数据库趋于完备的统计证据。
