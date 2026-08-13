"""材料学科知识库 — 自动化搜索组件。

基于 Scopus 高级搜索 + CloakBrowser 的文献搜索引擎。
含 LLM 查询生成层。
"""

from .models import (
    Paper, Author, SearchResult, SearchCost, SearchIntent,
    ScoredPaper, TermMatrix, QueryEntry, RouteCoverage,
)
from .compiler import ScopusQueryCompiler
from .parser import ScopusParser
from .engine import ScopusSearchEngine, ScopusAccessError
from .csv_exporter import CsvExporter
from .cache import SearchCache
from .query_generator import QueryGenerator
from .llm import DeepSeekBackend, OllamaBackend, create_backend
from .term_matrix import TermMatrixGenerator
from .query_population import QueryPopulation
from .coverage_map import CoverageMap
from .iterative_searcher import IterativeSearcher
from .citation_tracker import CitationTracker
from .mmr import MMRReranker

__all__ = [
    "Paper",
    "Author",
    "SearchResult",
    "SearchCost",
    "SearchIntent",
    "ScoredPaper",
    "TermMatrix",
    "QueryEntry",
    "RouteCoverage",
    "ScopusQueryCompiler",
    "ScopusParser",
    "ScopusSearchEngine",
    "ScopusAccessError",
    "CsvExporter",
    "SearchCache",
    "QueryGenerator",
    "DeepSeekBackend",
    "OllamaBackend",
    "create_backend",
    "TermMatrixGenerator",
    "QueryPopulation",
    "CoverageMap",
    "IterativeSearcher",
    "CitationTracker",
    "MMRReranker",
]
