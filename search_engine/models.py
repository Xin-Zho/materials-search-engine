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
    score: int              # 最终分数（含年代加权）
    raw_score: int = 0      # LLM 原始相关性分数 0-100
    reason: str = ""        # 中文理由
    category: str = ""      # 中文分类（与问题哪方面相关）
    route: str = ""         # 技术路线（如"相分离增韧"）
    info_gain: float = 0.0  # 信息增益 0-1（相对当前集合新增了什么）


@dataclass
class TermMatrix:
    """术语矩阵 — 研究问题拆解成多维度术语表。

    每个维度是研究问题的一个侧面，维度内是候选术语。
    """
    dimensions: dict[str, list[str]] = field(default_factory=dict)

    # 标准 8 维度
    DIMENSIONS = [
        "material_system",      # 材料体系
        "composition",          # 组成
        "structure_mechanism",  # 结构/机制
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
    result_papers: list[str] = field(default_factory=list)  # 命中的 paper_id
    new_papers: int = 0             # 新增相关论文数
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
