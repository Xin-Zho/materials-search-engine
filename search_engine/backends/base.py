"""SearchBackend — 文献搜索后端统一抽象。

不同数据源（OpenAlex API、Scopus + CloakBrowser）实现同一接口，上层
（iterative_searcher / experiments）通过 backend 交互，切换数据源不改上层。

设计取舍：各 backend 能力不同（Scopus 强在高级检索式，OpenAlex 强在引文追踪
和元数据完整），基类只定义共同最小接口 ``search``；额外能力（引文追踪、DOI 查询、
标题存在性检查）由各 backend 自行扩展，不强行塞进基类——避免基类膨胀成并集。
"""

import abc
from ..models import Paper


class SearchBackend(abc.ABC):
    """文献搜索后端抽象基类。子类必须实现 search。"""

    @abc.abstractmethod
    async def search(self, query: str, **kwargs) -> list[Paper]:
        """执行搜索，返回 Paper 列表。"""
        raise NotImplementedError

    async def get_by_doi(self, doi: str) -> Paper | None:
        """按 DOI 取单篇论文（可选能力，默认不支持）。"""
        return None

    async def title_exists(self, title: str) -> bool:
        """标题是否存在于此数据源（可选能力，默认保守返回 True，不误判缺失）。"""
        return True

    async def search_strict(self, query: str, **kwargs) -> list[Paper]:
        """严格检索（仅 title/abstract 限定），用于 gap closure precision。

        两级查询的 Tier 1：要求 route + mechanism + anchor 同时出现在 title/abstract。
        默认退化到普通 search（broad），具体后端可覆盖成 OQL / title-abs-key 检索。
        """
        return await self.search(query, **kwargs)

    async def close(self) -> None:
        """释放底层资源（HTTP client 等），默认无操作。"""
        return None
