"""CitationTracker — OpenAlex 引文追踪（向前/向后/共被引）。

补充关键词搜索的盲区：不同材料领域用不同术语描述相同机制，
引文网络能找到关键词搜不到的核心论文。

使用方式:
    tracker = CitationTracker()
    # 向后追踪（种子论文引用了什么）
    backward = await tracker.backward(doi="10.xxx/yyy", limit=20)
    # 向前追踪（谁引用了种子论文）
    forward = await tracker.forward(doi="10.xxx/yyy", limit=20)
    # 共被引/相似论文
    related = await tracker.related(doi="10.xxx/yyy", limit=20)
"""

import logging
import httpx
from .models import Paper, Author

logger = logging.getLogger(__name__)


class CitationTracker:
    """OpenAlex 引文追踪器。"""

    BASE_URL = "https://api.openalex.org"

    def __init__(self, mailto: str | None = None):
        self.mailto = mailto
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self):
        headers = {"User-Agent": f"materials-search/0.1 (mailto:{self.mailto})" if self.mailto
                   else "materials-search/0.1"}
        self._client = httpx.AsyncClient(headers=headers, timeout=30)
        return self

    async def __aexit__(self, *args):
        if self._client:
            await self._client.aclose()
            self._client = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"User-Agent": f"materials-search/0.1 (mailto:{self.mailto})" if self.mailto
                       else "materials-search/0.1"}
            self._client = httpx.AsyncClient(headers=headers, timeout=30)
        return self._client

    async def _get_json(self, url: str, params: dict | None = None) -> dict:
        """GET JSON，带重试。"""
        client = self._get_client()
        for attempt in range(3):
            try:
                resp = await client.get(url, params=params)
                if resp.status_code == 429:
                    import asyncio
                    await asyncio.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return {}
                if attempt == 2:
                    raise
                import asyncio
                await asyncio.sleep(2 ** attempt)
        return {}

    async def _get_work_by_doi(self, doi: str) -> dict:
        """通过 DOI 获取 OpenAlex Work 对象。"""
        url = f"{self.BASE_URL}/works/doi:{doi}"
        return await self._get_json(url)

    async def backward(self, doi: str, limit: int = 20) -> list[Paper]:
        """向后追踪 — 种子论文引用的文献。

        Returns:
            引用论文的 Paper 列表
        """
        work = await self._get_work_by_doi(doi)
        referenced_ids = work.get("referenced_works", [])
        if not referenced_ids:
            logger.info("向后追踪: %s 无参考文献数据", doi)
            return []

        # 批量查询（OpenAlex 支持 filter 最多 50 个）
        papers = await self._fetch_works_by_ids(referenced_ids[:min(limit * 2, 50)])
        logger.info("向后追踪: %s → %d 篇参考文献", doi, len(papers))
        return papers[:limit]

    async def forward(self, doi: str, limit: int = 20) -> list[Paper]:
        """向前追踪 — 引用种子论文的文献。"""
        work = await self._get_work_by_doi(doi)
        if not work:
            return []
        openalex_id = work.get("id", "").split("/")[-1]
        if not openalex_id:
            return []

        url = f"{self.BASE_URL}/works"
        params = {
            "filter": f"cites:{openalex_id}",
            "per-page": min(limit, 200),
            "sort": "cited_by_count:desc",
        }
        data = await self._get_json(url, params)
        papers = [self._work_to_paper(w) for w in data.get("results", [])]
        logger.info("向前追踪: %s → %d 篇被引", doi, len(papers))
        return papers[:limit]

    async def related(self, doi: str, limit: int = 20) -> list[Paper]:
        """共被引/相似论文 — 经常一起被引的相关工作。"""
        work = await self._get_work_by_doi(doi)
        related_ids = work.get("related_works", [])
        if not related_ids:
            return []
        papers = await self._fetch_works_by_ids(related_ids[:min(limit * 2, 50)])
        logger.info("共被引: %s → %d 篇相关", doi, len(papers))
        return papers[:limit]

    async def search(
        self,
        query: str,
        year_before: int | None = None,
        limit: int = 20,
    ) -> list[Paper]:
        """OpenAlex 关键词搜索，可选年份过滤（用于历史文献关键词召回）。"""
        url = f"{self.BASE_URL}/works"
        filters = []
        if query:
            filters.append(f"title.search:{query}")
        if year_before:
            filters.append(f"publication_year:<{year_before}")

        params = {
            "filter": ",".join(filters),
            "per-page": min(limit, 200),
            "sort": "cited_by_count:desc",
        }
        data = await self._get_json(url, params)
        papers = [self._work_to_paper(w) for w in data.get("results", [])]
        logger.info("关键词搜索: %s (year<%s) → %d 篇", query, year_before, len(papers))
        return papers[:limit]

    async def _fetch_works_by_ids(self, openalex_ids: list[str]) -> list[Paper]:
        """批量按 OpenAlex ID 查询。"""
        if not openalex_ids:
            return []

        # 用 filter 批量查询
        ids_filter = "|".join(openalex_ids)
        url = f"{self.BASE_URL}/works"
        params = {"filter": f"openalex_id:{ids_filter}", "per-page": 50}
        data = await self._get_json(url, params)

        papers = [self._work_to_paper(w) for w in data.get("results", [])]
        return [p for p in papers if p.title]

    @staticmethod
    def _work_to_paper(work: dict) -> Paper:
        """OpenAlex Work → Paper。"""
        title = work.get("title") or work.get("display_name") or ""

        # 作者
        authors = []
        for auth in work.get("authorships", []):
            a = auth.get("author", {})
            name = a.get("display_name", "")
            if name:
                parts = name.split()
                surname = parts[-1] if parts else name
                given = " ".join(parts[:-1]) if len(parts) > 1 else ""
                authors.append(Author(surname=surname, given_name=given))

        # 摘要重建
        abstract = CitationTracker._reconstruct_abstract(
            work.get("abstract_inverted_index")
        )

        # 期刊
        venue = None
        loc = work.get("primary_location") or {}
        source = loc.get("source") or {}
        venue = source.get("display_name")

        doi = (work.get("doi") or "").replace("https://doi.org/", "")

        openalex_id = work.get("id", "")
        paper_id = f"openalex:{openalex_id}" if openalex_id else f"openalex:{title[:80]}"

        return Paper(
            paper_id=paper_id,
            title=title,
            authors=authors,
            year=work.get("publication_year"),
            abstract=abstract,
            doi=doi or None,
            scopus_url=None,
            venue=venue,
            citation_count=work.get("cited_by_count"),
            document_type=work.get("type"),
            source="openalex",
        )

    @staticmethod
    def _reconstruct_abstract(inverted_index: dict | None) -> str | None:
        """从 abstract_inverted_index 重建摘要文本。"""
        if not inverted_index:
            return None
        positions = []
        for word, indices in inverted_index.items():
            for idx in indices:
                positions.append((idx, word))
        positions.sort()
        return " ".join(word for _, word in positions)
