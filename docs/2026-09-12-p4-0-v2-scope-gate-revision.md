# P4-0 范围门修订（p4_0_v1 → p4_0_v2）

日期：2026-09-12 ｜ 触发：P4-1A pilot 实测

> **这是一次**修订**，不是覆盖。** `docs/2026-09-12-p4-0-step1-paper-meta.md` 与
> commit `dfd938b` 里的 `p4_0_v1` 记录**保持原样**作为历史证据；
> `benchmark.yaml` 会重新生成并带 `build_version=p4_0_v2`。
> 用户裁定（Q1-Q4、不启用 CHANNEL_ONLY、1000 篇分层）**未变**。

---

## 一、怎么发现的

P4-1A pilot 抽了 30 篇（旧门的 Layer A = 按 `citations_asof_cutoff` 降序），
抽出结果里出现了 **DNA 甲基化 / 5-甲基胞嘧啶 / TET1 蛋白**：

```
openalex:W2108664872  2009  "Conversion of 5-Methylcytosine to 5-Hydroxymethylcytosine"
  [material ] 5-methylcytosine / 5-hydroxymethylcytosine / tet1 protein
  [mechanism] active dna demethylation
```

逐篇核对 30 篇，**约 17 篇离题**：DNA 甲基化、石墨烯、有机光伏、染料敏化电池、
热重分析、T 细胞免疫、动物剂量换算、蛋白序列比对、MOF、酶固定化、大气气溶胶。

**如果带着这个语料跑 P4-2，得到的是「漂亮但完全错误」的结果** ——
正是用户点名的那个风险：「预测失败到底是方法失败，还是 corpus 污染？」

---

## 二、根因（两级，都是我的实现缺陷）

### 级 1：`re.X` 模式下空格被忽略 → 多词短语从未生效

```python
STRONG_RE = re.compile(r"""
 ...
 | polymerization shrinkage | shrinkage stress | dental composite | degree of conversion
 ...
""", re.I | re.X)          # ← verbose 模式下**模式里的空白被忽略**
```

实测：

```
'polymerization shrinkage stress'  -> False
'shrinkage stress'                 -> False
'dental composite resin'           -> False
'degree of conversion'             -> False
```

`polymerization shrinkage` 被编译成 `polymerizationshrinkage`，**永远匹配不上**。
于是「强特征层」形同虚设，范围判定实际只剩单字词在兜底。

### 级 2：单字词兜底 + 通用词 → 离题论文批量涌入

`ADJACENT_RE` 里有 `shrink`、`conversions?`、`\bresin\b`、`\bpolymeri[sz]` 等。
这些词在**别的材料学科里同样高频**：

- `conversion` → DNA 甲基化转换、物种间剂量换算、蛋白序列比对、能量转换、热重分析
- `polymer` / `composite` → 石墨烯复合材料、碳纳米管复合材料、PLGA
- `epoxy` / `thiol` → 环氧纳米复合材料、石墨烯官能化

而且它**单独成立即判 in_scope**。第一级失效后，全靠它，污染就放大到 17/30。

---

## 三、修法：从「词面门」改为「主题门 + 严格词面救援」

### 尝试过的中间方案（都不够）

| 方案 | in_scope | KB 召回 | 结果 |
|---|---|---|---|
| 修 `\s+` + ADJACENT 合取(>=2 词族) | 11,590 | 100% | 旧 pilot 30 篇仍漏 8 篇 |
| 只用主题白名单 | 7,662 | 84.1% | 主题被 OpenAlex 误分类的真相关论文被丢 |
| **主题白名单 OR 严格词面（采用）** | **10,128** | **97.6%** | 旧 pilot 30 篇排除 25/30 |

中间方案仍有石墨烯/光伏漏网，因为 `epoxy`、`thiol`、`polymer` 无论怎么组合
都能在别的材料学科里凑够 2 个词族。**词面门在这个粒度上无法做到高精度** ——
所以换成语义级信号。

### 采用的判定（`build_p4_0_dataset.scope_of`）

```
1. primary_topic ∈ scope_allowlist.yaml   -> TOPIC_ALLOW  (in_scope)
2. STRICT_RE 严格词面命中                  -> CORE_TEXT    (in_scope)
3. 仅 >=2 个广义词族                       -> ADJACENT_ONLY  仅记录
4. 有定向通道但无词面证据                   -> CHANNEL_ONLY   仅记录
5. 其余                                   -> OFF_TOPIC
```

**主题门负责精度，严格词面门负责召回救援。**

`scope_allowlist.yaml`（committed，可评审）纳入 11 个主题：
Photopolymerization techniques and applications / Dental materials and restorations /
Additive Manufacturing and 3D Printing Technologies / Photorefractive and Nonlinear
Optics / Synthetic Organic Chemistry Methods（以上 5 个有 KB 正例种子）+
3D Printing in Biomedical Research / Epoxy Resin Curing Processes /
Nanofabrication and Lithography Techniques / Click Chemistry and Applications /
Cyclopropane Reaction Mechanisms / Radical Photochemical Reactions。

`STRICT_RE` 删掉了 `\bepoxy\b`、`\bthiol\b`、`acrylat`、`step-?\s?growth`、
`additive\s+manufactur` 等「单独不足以定范围」的词，只留光固化专有短语。

---

## 四、验收（用户要求的对象级证据）

### 旧 pilot 30 篇：**排除 25 / 30**，剩下 5 篇全为真相关

留下的：`Thiol–Ene Click Chemistry`、`Click Chemistry` ×2、
`Resin composite—State of the art`（牙科）、`Principles of Polymerization`。
排出的：DNA 甲基化、石墨烯 ×3、有机光伏 ×2、热重分析 ×3、电催化、Ru 染料、
大气气溶胶、染料敏化光伏、碳纳米管、T 细胞、剂量换算、银纳米球、蛋白比对、
酶催化、MOF、动物实验、聚合物/二氧化硅纳米复合。

### KB 召回：97.6%（126 篇漏 3）

3 篇漏报**逐条核过**：

| 论文 | 判定 |
|---|---|
| Sulfur-Based Dynamic Covalent Polymers | **正确排除**（动态共价聚合物，非光固化） |
| Improving the fracture toughness … epoxy using nanomaterials | **正确排除**（环氧纳米复合材料） |
| Chemistry of silanes: Interfaces in dental polymers and composites | ⚠️ **真实漏报** —— 主题是 `Silicone and Siloxane Chemistry`（在 pending_review） |

即：**实际只有 1 篇真漏，且原因正好落在一个待裁定主题上**。

### 其它

| | p4_0_v1 | p4_0_v2 |
|---|---|---|
| in_scope | 11,855 (82.8%) | **10,128 (70.7%)** |
| in_scope TRAIN / EVAL | 6,636 / 4,439 | **5,545 / 3,905** |
| 强特征层 | 7,335（多词短语失效） | TOPIC_ALLOW 7,662 + CORE_TEXT 2,466 |
| KB 召回 | 100%（但含污染） | 97.6%（漏 3，其中 2 篇为正确排除） |

pytest **458 passed**。新增回归断言：**13 篇具体离题论文标题**必须判范围外、
6 篇真相关标题必须判范围内、`re.X` 多词短语必须真的匹配、
committed 白名单必须可解析且非空。

---

## 五、新增测试挡住的三个坑（都由测试真实抓出）

1. **`re.X` 空格 bug** → `test_multiword_patterns_actually_match`
   （同时加反例：`conversion`/`dialysis`/`gene conversion` 不得靠强特征词通过）
2. **YAML 主题名含冒号**：`Hydrogels: synthesis, properties, applications` 未加引号
   会让整份白名单解析失败 → 白名单一旦成空集，范围门会**静默退化**成纯词面门。
   已加 `test_topic_allowlist_asset_loads_and_is_nonempty`
3. **`thiol-ene` 被当成单个 token** → 时相检查里永远 OOV 而被 `continue` **静默跳过**
   （整条检查被削弱）。token 正则统一改为纯字母 `[a-z]{3,}`，语料侧与概念侧口径一致。

---

## 六、未决：9 个待裁定主题

`pending_review` 里的主题**默认不纳入**（宁缺勿滥），纳入与否会明显改变语料分布：

| 主题 | 池内篇数 | KB 正例 | 问题 |
|---|---|---|---|
| Polymer composites and self-healing | 348 | 3 | 复合材料/自修复通常不是光固化体系 |
| Advanced Polymer Synthesis and Characterization | 230 | 1 | 通用高分子合成 |
| Polymer Nanocomposites and Properties | 129 | 1 | 通用复合材料 |
| biodegradable polymer synthesis and properties | 126 | 0 | 可降解高分子 |
| Silicone and Siloxane Chemistry | 111 | **1** | 硅氧烷用于牙科/3D 打印 —— **唯一致使 KB 漏报的主题** |
| Liquid Crystal Research Advancements | 83 | 0 | 液晶（注意与 thermochromic 主题线区分） |
| Synthesis and properties of polymers | 79 | 0 | 通用高分子合成 |
| Hydrogels: synthesis, properties, applications | 70 | 0 | 光交联水凝胶是子领域，但主题粒度过粗 |
| Photochromic and Fluorescence Chemistry | 50 | 0 | 属另一条主题线（光致变色），非光固化 |

**需要用户裁定**：哪些主题纳入。裁定后仅需改 `scope_allowlist.yaml` + 重跑
build/采样/抽取（工具链已就绪，无需改代码）。
