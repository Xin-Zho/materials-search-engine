"""ScopusBackend — Scopus + CloakBrowser 后端 adapter。

包装 ``search_engine.engine.ScopusSearchEngine``，实现 ``SearchBackend`` 接口。
Scopus 的搜索能力（高级检索式 + CSV 导出）由 ScopusSearchEngine 提供，本类只做
接口适配，让上层能通过统一 ``SearchBackend`` 调用，切换数据源不改上层。

设计取舍：Scopus 无引文追踪 / DOI 查询 / 标题存在性检查能力（这些用 OpenAlexBackend），
故本类只实现 ``search``；基类的 ``get_by_doi`` / ``title_exists`` 走默认实现
（get_by_doi 返回 None，title_exists 返回 True）。
"""

import logging

from ..models import Paper
from .base import SearchBackend

logger = logging.getLogger(__name__)


class ScopusBackend(SearchBackend):
    """Scopus 后端（adapter，包装 ScopusSearchEngine）。"""

    def __init__(self, engine):
        self.engine = engine

    async def search(self, query: str, limit: int = 20, **kwargs) -> list[Paper]:
        """执行 Scopus 高级搜索，返回 Paper 列表。

        kwargs 透传给 ScopusSearchEngine.search（year_range / sort_by / skip_cache）。
        """
        result = await self.engine.search(query, limit=limit, **kwargs)
        return result.papers

    async def close(self):
        await self.engine.close()
