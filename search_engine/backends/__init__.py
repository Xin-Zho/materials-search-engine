"""文献搜索后端子包（Search Backends）。

统一接口 ``SearchBackend``；具体实现：
- ``openalex.OpenAlexBackend`` — OpenAlex API（搜索 + 引文追踪 + 元数据，带缓存/rate-limit）
- ``scopus.ScopusBackend``     — Scopus + CloakBrowser（高级检索式 + CSV 导出）

上层（iterative_searcher / experiments）通过 ``SearchBackend`` 交互，切换数据源不改上层。
历史名 ``CitationTracker`` 保留为 ``OpenAlexBackend`` 别名，旧代码零改动。
"""

from .base import SearchBackend
from .openalex import (
    OpenAlexBackend,
    CitationTracker,
    RateLimitError,
    RateLimitExhaustedError,
)
from .scopus import ScopusBackend

__all__ = [
    "SearchBackend",
    "OpenAlexBackend",
    "ScopusBackend",
    "CitationTracker",
    "RateLimitError",
    "RateLimitExhaustedError",
]
