"""标准化数据模型。所有模块共享这些类型。"""

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class Author:
    """作者。"""
    surname: str        # 姓
    given_name: str     # 名（首字母或全名）
    scopus_id: str | None = None

    def __str__(self) -> str:
        if self.given_name:
            return f"{self.surname} {self.given_name}"
        return self.surname


@dataclass
class Paper:
    """从 Scopus 提取的标准化论文记录。"""
    paper_id: str               # Scopus EID（优先）或 DOI
    title: str
    authors: list[Author] = field(default_factory=list)
    year: int | None = None
    abstract: str | None = None
    doi: str | None = None
    scopus_url: str | None = None
    venue: str | None = None    # 期刊/会议名
    volume: str | None = None
    pages: str | None = None
    citation_count: int | None = None
    document_type: str | None = None    # Article / Review / Conference Paper / ...
    source: str = "scopus"              # 数据来源标记


@dataclass
class SearchResult:
    """一次搜索的完整返回。"""
    query: str                  # 实际发送的 Scopus 查询字符串
    papers: list[Paper]         # 返回的论文列表
    total_count: int            # Scopus 报告的总命中数
    pages_fetched: int          # 实际翻了多少页
    time_taken: float           # 总耗时（秒）
    source: str = "scopus"


@dataclass
class SearchCost:
    """一次检索任务的累计成本统计。"""
    queries: int = 0                    # 搜索请求次数
    pages_loaded: int = 0               # 翻页次数
    detail_pages_visited: int = 0       # 论文详情页访问次数
    fulltext_attempts: int = 0
    fulltext_successes: int = 0
    total_browser_time: float = 0.0     # 浏览器累计操作时间（秒）
    api_calls_by_type: dict[str, int] = field(default_factory=dict)

    def record(self, action_type: str, time_spent: float = 0.0, pages: int = 0):
        """记录一次操作的成本。"""
        if action_type == "search":
            self.queries += 1
            self.pages_loaded += pages
        elif action_type == "detail":
            self.detail_pages_visited += 1
        elif action_type == "fulltext":
            self.fulltext_attempts += 1
        self.total_browser_time += time_spent
        self.api_calls_by_type[action_type] = \
            self.api_calls_by_type.get(action_type, 0) + 1


@dataclass
class SearchIntent:
    """结构化的搜索意图。查询编译器用它生成 Scopus 语法。

    这是 LLM 查询生成层和搜索组件的接口。
    """
    keywords: list[str] = field(default_factory=list)
    synonyms: dict[str, list[str]] = field(default_factory=dict)
    must_include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    author: str | None = None
    affiliation: str | None = None
    year_range: tuple[int, int] | None = None
    document_type: Literal["ar", "re", "cp"] | None = None
    subject_area: list[str] | None = None       # e.g. ["MATE", "CHEM"]
    language: str = "english"


@dataclass
class ScoredPaper:
    """一篇论文的相关性评分 + 结构化标签。

    承载"相关性"和"覆盖"两个维度的信息，供覆盖地图和缺口分析使用。
    """
    paper: Paper
    score: int              # 最终分数（等于 LLM 原始相关性分数）
    raw_score: int = 0      # LLM 原始相关性分数 0-100
    reason: str = ""        # 中文理由
    category: str = ""      # 中文分类（与问题哪方面相关）
    route: str = ""         # 技术路线（如"相分离增韧"）
    info_gain: float = 0.0  # 信息增益 0-1（相对当前集合新增了什么）
    evidence_type: str = "" # 证据类型: original_experiment / review / simulation / perspective / patent
    has_limitation: bool = False  # 是否报告限制/反例/权衡


@dataclass
class TermMatrix:
    """术语矩阵 — 研究问题拆解成多维度术语表。

    每个维度是研究问题的一个侧面，维度内是候选术语。
    """
    dimensions: dict[str, list[str]] = field(default_factory=dict)
    route_families: list[dict] = field(default_factory=list)  # strategy_route 归一化后的 family

    # 标准维度（strategy_route + physical_mechanism 分离，替代模糊的 structure_mechanism）
    DIMENSIONS = [
        "material_system",      # 材料体系
        "composition",          # 组成
        "strategy_route",       # 技术/化学/工艺路线（可独立检索，第一轮强制覆盖对象）
        "physical_mechanism",   # 底层物理/化学机制（探索支路，不强制覆盖）
        "process",              # 工艺
        "target_properties",    # 目标性能
        "application",          # 应用
        "failure_problem",      # 问题/失效
        "metrics",              # 测试指标
    ]

    def get(self, dim: str) -> list[str]:
        return self.dimensions.get(dim, [])

    def all_terms(self) -> list[str]:
        """展平所有维度的术语（去重）。"""
        seen = set()
        result = []
        for terms in self.dimensions.values():
            for t in terms:
                if t and t.lower() not in seen:
                    seen.add(t.lower())
                    result.append(t)
        return result


@dataclass
class QueryEntry:
    """查询种群中的一条查询记录。"""
    query_id: str
    query: str
    strategy: str = ""              # 生成策略（main_route / term_discovery / counter_example / ...）
    parent_query: str | None = None # 派生自哪条查询
    result_papers: list[str] = field(default_factory=list)  # 查询返回的全部 paper_id（去重前）
    new_candidates: int = 0         # 去重后的新增候选论文数
    new_scored: int = 0             # 成功评分的论文数
    new_relevant: int = 0           # score ≥ 阈值的新增相关论文数
    new_routes: int = 0             # 新增技术路线数
    duplicate_rate: float = 0.0     # 重复率 0-1
    cost: float = 0.0               # 成本（时间秒）
    return_score: float = 0.0       # 累计收益


@dataclass
class RouteCoverage:
    """覆盖地图中的一条技术路线。"""
    route_name: str
    paper_ids: list[str] = field(default_factory=list)
    paper_count: int = 0
    latest_year: int | None = None
    has_review: bool = False
    has_counter_example: bool = False
    has_original: bool = False
    coverage_score: float = 0.0     # 0-1 覆盖度


@dataclass
class Mechanism:
    """物理机制（因果三元组 + evidence-based 字段）。"""
    cause: str = ""       # 触发因素（如 ring-opening polymerization）
    mechanism: str = ""   # 中间机制（如 volumetric expansion）
    effect: str = ""      # 目标效果（如 offsets shrinkage）
    canonical: str = ""   # 归一化后的标准机制名（如 stress relaxation）
    evidence: str = ""    # 为什么认为论文有这个机制（原文依据）
    confidence: float = 0.0  # 置信度 0-1


@dataclass
class SearchHypothesis:
    """搜索假设：从论文证据泛化出的、可驱动新搜索的方向。"""
    hypothesis: str = ""             # 泛化假设（超出论文具体化学）
    rationale: str = ""              # 为什么这个假设成立
    support_type: str = ""           # 证据类型: direct_experiment / mechanism_inference / literature_suggestion / speculative
    evidence: str = ""               # 论文中支撑该假设的具体证据（原文片段）
    queries: list[str] = field(default_factory=list)  # 具体搜索 query


@dataclass
class KnowledgeRecord:
    """从一篇相关论文结构化提取的、可继续用于搜索的知识。

    核心不是摘要，而是"后面还能拿去继续搜索的知识"。
    关键区分：
    - strategy_routes（技术路线）vs characterization_methods（表征方法）
    - physical_mechanisms（因果三元组）vs 结果变量
    - historical_terms（可驱动历史文献检索的旧称/别名）
    - search_hypotheses（Paper Evidence → Generalized Hypothesis → Query）
    """
    paper_id: str                                          # 来源论文（去重键）
    problem: str = ""                                      # 论文解决的问题
    strategy_routes: list[str] = field(default_factory=list)      # 技术/化学/工艺路线
    materials: list[str] = field(default_factory=list)            # 材料体系
    physical_mechanisms: list[Mechanism] = field(default_factory=list)  # 因果机制
    characterization_methods: list[str] = field(default_factory=list)   # 表征/实验方法
    concepts: list[str] = field(default_factory=list)              # 概念
    synonyms: list[str] = field(default_factory=list)              # 严格同义词
    broader_terms: list[str] = field(default_factory=list)         # 上位概念
    historical_terms: list[str] = field(default_factory=list)      # 旧称/别名（可驱动历史检索）
    search_hypotheses: list[SearchHypothesis] = field(default_factory=list)  # 泛化搜索假设
    source_text: str = ""                                  # 提取依据的原文片段（追溯用）
    extractor_version: str = "1.0"                         # 提取器版本
    confidence: float = 0.0                                # 提取置信度 0-1

    def all_terms(self) -> list[str]:
        """展平所有可用于检索的术语（去重）。"""
        seen = set()
        result = []
        for terms in (self.strategy_routes, self.materials, self.concepts,
                      self.synonyms, self.broader_terms, self.historical_terms):
            for t in terms:
                if t and t.lower() not in seen:
                    seen.add(t.lower())
                    result.append(t)
        for m in self.physical_mechanisms:
            for t in (m.cause, m.mechanism, m.effect):
                if t and t.lower() not in seen:
                    seen.add(t.lower())
                    result.append(t)
        return result
