"""OpenAlexBackend — OpenAlex API 后端（搜索 + 引文追踪 + 元数据）。

封装 OpenAlex 的所有 HTTP 调用：关键词搜索、DOI 查询、标题存在性检查、
向前/向后/共被引追踪。带本地缓存 + rate-limit 保护。

历史名 ``CitationTracker`` 保留为别名（见文件末尾），旧代码
``from search_engine import CitationTracker`` 零改动。

使用方式:
    async with OpenAlexBackend(mailto="you@example.com") as oa:
        papers = await oa.search("self-healing polymer")
        exists = await oa.title_exists("Some Paper Title")
        backward = await oa.backward(doi="10.xxx/yyy", limit=20)
"""

import logging
import httpx

from ..models import Paper, Author
from .base import SearchBackend

logger = logging.getLogger(__name__)


class RateLimitError(Exception):
    """OpenAlex rate limit 超限（daily budget 用完或请求过快）。"""
    pass


class RateLimitExhaustedError(Exception):
    """OpenAlex 每日额度已耗尽（请求前 /rate-limit 检查 remaining<=0）。"""
    pass


class OpenAlexBackend(SearchBackend):
    """OpenAlex 后端（带本地缓存 + rate-limit 保护）。

    实现 ``SearchBackend.search``；额外提供引文追踪（backward/forward/related）、
    DOI 查询、标题存在性检查等 OpenAlex 专属能力——这些不进基类，只在本类扩展。
    """

    BASE_URL = "https://api.openalex.org"

    def __init__(self, mailto: str | None = None,
                 api_key: str | None = None,
                 cache_path: str | None = "data/cache/openalex_cache.json",
                 trust_env: bool = False):
        self.mailto = mailto
        self.api_key = api_key
        self.trust_env = trust_env
        # trust_env=False（默认）：不走 HTTP(S)_PROXY 环境变量代理。
        # 实测（2026-08-27）本地代理 127.0.0.1:6268 对 api.openalex.org
        # TLS 握手失败（httpcore.ConnectError），而直连 200 稳定可用。
        self.cache_path = cache_path
        self._client: httpx.AsyncClient | None = None
        self._cache: dict = self._load_cache()
        # credit 计数（2025+ OpenAlex credit 制：singleton=1, list=10 credits/请求）
        self.credits_used: int = 0
        self._credits_by_type: dict[str, int] = {"singleton": 0, "list": 0}

    def _load_cache(self) -> dict:
        try:
            import json
            with open(self.cache_path, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_cache(self):
        import json
        import os
        os.makedirs(os.path.dirname(self.cache_path) or ".", exist_ok=True)
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump(self._cache, f)

    @staticmethod
    def _cache_key(url: str, params: dict | None) -> str:
        import json
        return url + "?" + json.dumps(params or {}, sort_keys=True)

    async def __aenter__(self):
        headers = {"User-Agent": f"materials-search/0.1 (mailto:{self.mailto})" if self.mailto
                   else "materials-search/0.1"}
        self._client = httpx.AsyncClient(headers=headers, timeout=30,
                                         trust_env=self.trust_env)
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"User-Agent": f"materials-search/0.1 (mailto:{self.mailto})" if self.mailto
                       else "materials-search/0.1"}
            self._client = httpx.AsyncClient(headers=headers, timeout=30,
                                             trust_env=self.trust_env)
        return self._client

    async def _get_json(self, url: str, params: dict | None = None) -> dict:
        """GET JSON，带缓存 + rate-limit 保护。

        - 命中本地缓存直接返回（不消耗 API credits）
        - 429（rate limit）显式抛 RateLimitError，绝不静默返回空结果
        """
        key = self._cache_key(url, params)
        if key in self._cache:
            return self._cache[key]

        client = self._get_client()
        # polite pool + API key：以 URL query 参数发送（官方识别方式），不写入
        # cache key——同一 query 带不带 mailto/api_key 共享缓存，避免缓存分裂。
        # api_key 是配额核心：无 key 仅 1000 credits/天（list 请求 10 credits/次，
        # 全量扩网一次 4000+ credits 必然打爆）；免费 key 100k credits/天。
        request_params = dict(params or {})
        if self.mailto:
            request_params.setdefault("mailto", self.mailto)
        if self.api_key:
            request_params.setdefault("api_key", self.api_key)
        for attempt in range(3):
            try:
                resp = await client.get(url, params=request_params)
                self._count_credits(url)
                if resp.status_code == 429:
                    remaining = resp.headers.get("X-RateLimit-Remaining", "?")
                    raise RateLimitError(
                        f"OpenAlex rate limit exceeded (X-RateLimit-Remaining={remaining}). "
                        f"Daily credit budget 可能已用完，请明天重试或加 mailto 进 polite pool。"
                    )
                resp.raise_for_status()
                data = resp.json()
                self._cache[key] = data
                self._save_cache()
                return data
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return {}
                if attempt == 2:
                    raise
                import asyncio
                await asyncio.sleep(2 ** attempt)
            except (httpx.ConnectError, httpx.ConnectTimeout,
                    httpx.ReadTimeout, httpx.ReadError, httpx.TransportError):
                # 网络层抖动（代理不稳/瞬时断连）重试 3 次，退避 1/2/4s
                if attempt == 2:
                    raise
                import asyncio
                await asyncio.sleep(2 ** attempt)
        return {}

    async def check_rate_limit(self) -> int:
        """请求前查 /rate-limit，remaining<=0 抛 RateLimitExhaustedError。

        ⚠️ 2025+ OpenAlex 要求 api_key 才能查 /rate-limit（无 key 返回 401，
        静默 pass，靠响应头 X-RateLimit-Remaining 兜底）。
        """
        client = self._get_client()
        try:
            params = {}
            if self.mailto:
                params["mailto"] = self.mailto
            if self.api_key:
                params["api_key"] = self.api_key
            resp = await client.get(f"{self.BASE_URL}/rate-limit", params=params)
            if resp.status_code == 200:
                data = resp.json()
                remaining = (data.get("rate_limit") or {}).get(
                    "credits_remaining",
                    (data.get("rate_limit") or {}).get("daily_remaining_usd", 1))
                if isinstance(remaining, (int, float)) and remaining <= 0:
                    raise RateLimitExhaustedError(
                        "OpenAlex 每日额度已耗尽（remaining=0），实验终止。请明天重试。")
                return int(remaining) if isinstance(remaining, (int, float)) else -1
        except RateLimitExhaustedError:
            raise
        except Exception:
            pass  # /rate-limit 查询失败不阻断（header 检查兜底）
        return -1

    def _count_credits(self, url: str):
        """按端点类型估算本次请求消耗的 credits（singleton=1, list=10）。

        2025+ OpenAlex credit 制（官方文档）：/works/W123 等单实体 = 1 credit；
        /works 带 filter/search/cursor 分页 = 10 credits/请求。缓存命中不消耗
        credits（_get_json 命中缓存提前 return，不会走到这里）。
        """
        import re
        if "?" not in url and re.search(r"/works/", url):
            # 无 query 参数的 /works/{id|doi:...|.../ngrams} —— singleton 端点
            self._credits_by_type["singleton"] += 1
        else:
            self._credits_by_type["list"] += 1
        self.credits_used = (self._credits_by_type["singleton"]
                             + self._credits_by_type["list"] * 10)

    def credit_summary(self) -> dict:
        """本次会话消耗的 credits 估算（universe builder 打印用）。"""
        return dict(self._credits_by_type, total=self.credits_used)

    async def _get_work_by_doi(self, doi: str) -> dict:
        """通过 DOI 获取 OpenAlex Work 对象。"""
        url = f"{self.BASE_URL}/works/doi:{doi}"
        return await self._get_json(url)

    async def get_by_doi(self, doi: str) -> Paper | None:
        """按 DOI 取单篇论文（SearchBackend 可选能力）。"""
        work = await self._get_work_by_doi(doi)
        if not work:
            return None
        return self._work_to_paper(work)

    async def title_exists(self, title: str) -> bool:
        """标题是否存在于 OpenAlex（SearchBackend 可选能力）。

        用 title.search 查；查询失败保守返回 True（不误判缺失，与历史逻辑一致）。
        """
        url = f"{self.BASE_URL}/works"
        params = {"filter": f"title.search:{title[:120]}", "per-page": 1}
        try:
            data = await self._get_json(url, params)
            return len(data.get("results", [])) > 0
        except Exception:
            return True

    async def backward(self, doi: str, limit: int = 20) -> list[Paper]:
        """向后追踪 — 种子论文引用的文献。"""
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
        """OpenAlex 全文搜索。

        接受 Scopus 风格 query（'"route" AND "mechanism" AND "anchor"'）或自由
        关键词，统一清理成 OpenAlex 全文 search（空格分隔词）。OpenAlex 的
        filter=title.search: 不支持 AND 布尔，复杂 gap query 必须走 search 全文召回，
        否则返回 0 命中（这是 loop 第一版 search "失败"的根因）。
        """
        import re
        url = f"{self.BASE_URL}/works"
        # 清理 Scopus 布尔语法：去引号，AND/NOT/OR 转空格
        clean = re.sub(r'"', "", query or "")
        clean = re.sub(r"\b(?:AND|NOT|OR)\b", " ", clean, flags=re.I)
        clean = re.sub(r"\s+", " ", clean).strip()

        filters = []
        if year_before:
            filters.append(f"publication_year:<{year_before}")
        params = {"per-page": min(limit, 200), "sort": "cited_by_count:desc"}
        if clean:
            params["search"] = clean
        if filters:
            params["filter"] = ",".join(filters)

        data = await self._get_json(url, params)
        papers = [self._work_to_paper(w) for w in data.get("results", [])]
        total_hits = data.get("meta", {}).get("count", len(papers))
        logger.info("OpenAlex search %r → %d 篇 (总命中 %d)", clean, len(papers), total_hits)
        self.last_total_hits = total_hits
        return papers[:limit]

    async def search_strict(self, query: str, limit: int = 20) -> list[Paper]:
        """title/abstract 严格布尔检索（filter=title_and_abstract.search 多短语 AND）。

        实证（2026-08-25）：OpenAlex /works 的 `q` 参数会被静默忽略——不应用检索条件，
        meta.count 退化为 corpus（约 3.2 亿），这是 loop 首轮 total=321958325 的根因；
        `oql` 参数直接 400 非法。唯一有效的 title+abstract 限定是
        `filter=title_and_abstract.search:"a" "b" "c"`，多短语为 AND 语义
        （count 随短语数递减：machine learning→145万，+neural→24.9万，+deep→6.5万；
        "ring opening"+"polymerization shrinkage"→32 篇）。

        Tier 1 gap closure precision：只搜 title + abstract。
        普通 search 保留作 Tier 2 fulltext rescue。
        """
        import re
        url = f"{self.BASE_URL}/works"
        terms = [t.strip().strip('"')
                 for t in re.split(r"\s+AND\s+", query or "", flags=re.I) if t.strip()]
        phrases = " ".join(f'"{t}"' for t in terms)
        params = {
            "filter": f"title_and_abstract.search:{phrases}",
            "per-page": min(limit, 200),
            "sort": "cited_by_count:desc",
        }
        data = await self._get_json(url, params)
        papers = [self._work_to_paper(w) for w in data.get("results", [])]
        total_hits = data.get("meta", {}).get("count", len(papers))
        logger.info("OpenAlex strict search %s → %d 篇 (总命中 %d)", phrases, len(papers), total_hits)
        self.last_total_hits = total_hits
        return papers[:limit]

    async def search_relevance(self, query: str, limit: int = 20) -> list[Paper]:
        """title/abstract 短语 AND 检索 + **relevance 排序**（Phase 2 verification 专用）。

        为什么要有它：普通 search / search_strict 都用 `sort=cited_by_count:desc`——
        对 gap 搜索 OK，但 verification 要的是"候选相关的领域论文"，不是"高被引综述"。
        bulk-fill dental composite 论文被引普遍低于 hydrogel/MOF 综述 → 被引排序
        把候选相关论文全部挤出 top-N（实测：49 篇全是 hydrogels/scaffolds/MOF）。
        用 filter + sort=relevance_score:desc 让候选/目标词匹配度决定排序。

        query 格式同 search_strict：'"candidate phrase" AND "target phrase"'。
        """
        import re
        url = f"{self.BASE_URL}/works"
        terms = [t.strip().strip('"')
                 for t in re.split(r"\s+AND\s+", query or "", flags=re.I) if t.strip()]
        phrases = " ".join(f'"{t}"' for t in terms)
        params = {
            "filter": f"title_and_abstract.search:{phrases}",
            "per-page": min(limit, 200),
            "sort": "relevance_score:desc",
        }
        data = await self._get_json(url, params)
        papers = [self._work_to_paper(w) for w in data.get("results", [])]
        total_hits = data.get("meta", {}).get("count", len(papers))
        logger.info("OpenAlex relevance search %s → %d 篇 (总命中 %d)", phrases, len(papers), total_hits)
        self.last_total_hits = total_hits
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
        abstract = OpenAlexBackend._reconstruct_abstract(
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


# 向后兼容：旧代码 from search_engine import CitationTracker / RateLimitError
CitationTracker = OpenAlexBackend
