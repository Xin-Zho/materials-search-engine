"""ScopusSearchEngine — 基于 CloakBrowser + Scopus 高级搜索的文献搜索引擎。

使用 Scopus 内置 CSV 导出功能获取结构化数据，不依赖 HTML 解析。
"""

import time
import uuid
import asyncio
import csv
import io
import logging
from pathlib import Path

from .models import Paper, SearchResult, SearchCost, SearchIntent, Author
from .compiler import ScopusQueryCompiler
from .cache import SearchCache
from .csv_exporter import CsvExporter

logger = logging.getLogger(__name__)


class ScopusAccessError(Exception):
    """Scopus 不可达（未登录或会话过期）。"""
    pass


class ScopusSearchEngine:
    """基于 Scopus + CloakBrowser 的文献搜索引擎。

    使用 persistent context 保存登录会话。
    首次使用需要运行 `python -m search_engine login` 手动登录。
    """

    ADVANCED_SEARCH_URL = "https://www.scopus.com/search/form.uri?display=advanced"
    RESULTS_PER_PAGE = 20

    def __init__(
        self,
        data_dir: str | Path = "data",
        headless: bool = True,
        humanize: str = "careful",
        viewport_width: int = 1920,
        viewport_height: int = 1080,
    ):
        self.data_dir = Path(data_dir)
        self.headless = headless
        self.humanize = humanize
        self.viewport = {"width": viewport_width, "height": viewport_height}
        self._profile_dir = self.data_dir / "scopus_profile"

        self.compiler = ScopusQueryCompiler()
        self.cache = SearchCache(self.data_dir / "cache" / "scopus_cache.db")
        self.exporter = CsvExporter(self.data_dir / "exports")
        self.cost = SearchCost()

        self._browser = None
        self._context = None
        self._page = None
        self._session_id: str | None = None

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.close()

    # ── 生命周期 ──────────────────────────────────────

    async def start(self):
        self.cache.init_tables()
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        await self._launch_browser()
        await self._check_access()

    async def close(self):
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
            self._context = None
            self._page = None
        self.cache.close()

    async def _launch_browser(self):
        try:
            from cloakbrowser import launch_async
        except ImportError:
            raise ImportError("CloakBrowser 未安装。pip install cloakbrowser")

        self._browser = await launch_async(headless=self.headless, humanize=self.humanize)

        # 加载已保存的登录会话
        state_path = self._profile_dir / "state.json"
        storage_state = str(state_path) if state_path.exists() else None

        self._context = await self._browser.new_context(
            viewport=self.viewport,
            storage_state=storage_state,
        )
        self._page = await self._context.new_page()
        logger.info("CloakBrowser 已启动 (profile: %s, session: %s)",
                     self._profile_dir, "loaded" if storage_state else "none")

    async def _check_access(self):
        await self._page.goto(
            self.ADVANCED_SEARCH_URL,
            wait_until="domcontentloaded",
            timeout=60000,
        )
        await asyncio.sleep(3)
        url = self._page.url.lower()
        if "login" in url or "signin" in url or "id.elsevier.com" in url:
            raise ScopusAccessError(
                "Scopus 未登录或会话已过期。\n请运行: python -m search_engine login"
            )
        logger.info("Scopus 会话有效")

    # ── 搜索（CSV 导出方式）────────────────────────────

    async def search(
        self,
        query: str,
        limit: int = 20,
        year_range: tuple[int, int] | None = None,
        sort_by: str = "relevance",
        skip_cache: bool = False,
    ) -> SearchResult:
        """执行 Scopus 高级搜索，通过 CSV 导出获取结果。"""
        full_query = query
        if year_range:
            start, end = year_range
            if start and end:
                full_query += f" AND PUBYEAR > {start - 1} AND PUBYEAR < {end + 1}"
            elif start:
                full_query += f" AND PUBYEAR > {start - 1}"
            elif end:
                full_query += f" AND PUBYEAR < {end + 1}"

        if not skip_cache:
            cached = self.cache.get_cached_result(full_query)
            if cached:
                logger.info("命中缓存: %d 篇", len(cached.papers))
                return cached

        t0 = time.time()

        # 1. 执行搜索
        await self._navigate_and_search(full_query)
        # 调试：保存页面文本和 URL
        page_text = await self._page.evaluate("() => document.body.innerText")
        debug_dir = self.data_dir / "cache"
        (debug_dir / "scopus_after_search_text.txt").write_text(
            f"URL: {self._page.url}\n\n{page_text[:5000]}", encoding="utf-8"
        )
        await self._page.screenshot(path=str(debug_dir / "scopus_after_search.png"))
        logger.debug("页面文本已保存 (%d 字符), URL: %s", len(page_text), self._page.url[:100])

        total_count = await self._get_result_count()
        logger.info("搜索完成: %d 命中", total_count)

        if total_count == 0:
            result = SearchResult(query=full_query, papers=[], total_count=0, pages_fetched=0, time_taken=time.time() - t0)
            return result

        # 2. 通过 Scopus Export API 获取 CSV
        csv_text = await self._export_via_api(full_query, limit)
        # 保存原始 CSV 以便调试字段名
        (self.data_dir / "cache" / "scopus_export_raw.csv").write_text(csv_text, encoding="utf-8")
        logger.info("CSV: %d 字符 → data/cache/scopus_export_raw.csv", len(csv_text))
        papers = self._parse_scopus_csv(csv_text)
        logger.info("解析: %d 篇论文", len(papers))

        elapsed = time.time() - t0
        self.cost.record("search", time_spent=elapsed)

        result = SearchResult(
            query=full_query,
            papers=papers[:limit],
            total_count=total_count,
            pages_fetched=(limit + self.RESULTS_PER_PAGE - 1) // self.RESULTS_PER_PAGE,
            time_taken=elapsed,
        )

        if result.papers:  # 不缓存空结果
            self.cache.set_cached_result(full_query, result)
            self.cache.store_papers(papers[:limit])
        self.cache.log_search(
            session_id=self._session_id or "unknown",
            step=self.cost.queries,
            action_type="keyword_search",
            query_string=full_query,
            result_ids=[p.paper_id for p in papers[:limit]],
            cost_time=elapsed,
        )

        logger.info("搜索完成: %d 篇 / %d 命中 / %.1fs", len(result.papers), total_count, elapsed)
        return result

    # ── Scopus Export API ─────────────────────────────

    async def _export_via_api(self, query: str, limit: int, fields: list[str] | None = None) -> str:
        """通过 Scopus Export REST API 导出 CSV（不经 UI）。"""
        if fields is None:
            fields = ["titles", "year", "doi", "abstract"]

        # 转换 &gt; 等 HTML 实体
        clean_query = query.replace("&gt;", ">").replace("&lt;", "<")

        body = {
            "searchRequest": {
                "query": clean_query,
                "documentClassification": "PRIMARY",
                "sortBy": [
                    {"fieldName": "datesort", "order": "desc"},
                    {"fieldName": "relevance", "order": "desc"},
                ],
                "resultSet": {"offset": 0, "itemCount": limit},
            },
            "fileType": "CSV",
            "exportType": "PUBLICATION",
            "fieldGroupIdentifiers": fields,
            "locale": "zh-CN",
            "userQuery": clean_query,
        }

        import json
        base_url = "https://www.scopus.com/gateway/export-service-reactive/export"

        # Step 1: 发起导出
        result = await self._page.evaluate(f"""
            async () => {{
                try {{
                    const res = await fetch('{base_url}/bulk-job/initiate', {{
                        method: 'POST',
                        headers: {{ 'content-type': 'application/json' }},
                        body: JSON.stringify({json.dumps(body)}),
                        credentials: 'include',
                    }});
                    if (!res.ok) {{
                        return {{ error: 'HTTP ' + res.status }};
                    }}
                    return await res.json();
                }} catch (e) {{
                    return {{ error: e.message }};
                }}
            }}
        """)
        if not result or result.get("error"):
            err = (result or {}).get("error", "未知错误")
            logger.error("导出发起失败: %s (可能是 Scopus 会话过期，请重新 login)", err)
            if "Failed to fetch" in str(err) or "HTTP 401" in str(err) or "HTTP 403" in str(err):
                raise ScopusAccessError(
                    "Scopus 导出失败：会话可能已过期。\n"
                    "请运行: python -m search_engine login 重新登录。"
                )
            return ""
        job_id = result.get("bulkExportId")
        if not job_id:
            logger.error("导出发起失败: 未返回 bulkExportId，%s", result)
            return ""
        logger.debug("导出 job: %s", job_id)

        # Step 2: 轮询 job 状态
        for _ in range(30):  # 最多等 30 秒
            await asyncio.sleep(1)
            jobs = await self._page.evaluate(f"""
                async () => {{
                    const res = await fetch('{base_url}/bulk-jobs');
                    return await res.json();
                }}
            """)
            for job in jobs.get("jobs", []):
                if job.get("jobId") == job_id:
                    if job.get("status") == "COMPLETED":
                        file_url = job.get("fileUrl", "")
                        # Step 3: 生成预签名 URL 并下载
                        csv_content = await self._page.evaluate(f"""
                            async () => {{
                                const genRes = await fetch(
                                    '{base_url}/bulk-job/{job_id}/generate-url',
                                    {{ method: 'POST' }}
                                );
                                const genData = await genRes.json();
                                if (!genData.presignedUrl) return '';
                                const csvRes = await fetch(genData.presignedUrl);
                                return await csvRes.text();
                            }}
                        """)
                        return csv_content
                    elif job.get("status") == "FAILED":
                        logger.error("导出失败: %s", job)
                        return ""
                    break

        logger.warning("导出超时")
        return ""

    # ── 浏览器操作：搜索 ───────────────────────────────

    async def _navigate_and_search(self, query: str):
        """导航到高级搜索页面并提交查询。"""
        current_url = self._page.url
        if self.ADVANCED_SEARCH_URL not in current_url:
            await self._page.goto(
                self.ADVANCED_SEARCH_URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )
        await self._page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(3)

        # 找到搜索框 — 加重试机制
        textarea = None
        for attempt in range(5):
            try:
                textarea = await self._page.query_selector(
                    "textarea, [role='textbox']"
                )
                if textarea:
                    break
            except Exception:
                pass
            await asyncio.sleep(2)

        if not textarea:
            debug_path = self.data_dir / "cache" / "scopus_no_searchbox.html"
            debug_path.write_text(await self._page.content(), encoding="utf-8")
            raise RuntimeError(f"未找到搜索框 (已重试5次)。HTML: {debug_path}")

        # 填入查询
        await textarea.click(force=True)
        await textarea.fill("", force=True)
        await textarea.fill(query, force=True)
        await asyncio.sleep(0.5)

        # 提交
        search_btn = await self._page.query_selector(
            "button[id*='search'], button[type='submit'], button:has-text('Search'), button:has-text('搜索')"
        )
        if search_btn:
            await search_btn.click(force=True)
        else:
            await self._page.keyboard.press("Enter")

        # 等待结果加载
        await asyncio.sleep(5)
        try:
            await self._page.wait_for_function(
                "() => document.body.innerText.includes('document results') || "
                "document.body.innerText.includes('documents') || "
                "document.body.innerText.includes('No results')",
                timeout=20000,
            )
        except Exception:
            pass
        await asyncio.sleep(2)

    async def _get_result_count(self) -> int:
        """获取搜索结果总数。"""
        try:
            text = await self._page.evaluate("() => document.body.innerText")
            import re
            # 中文: "找到 142 篇文献", "1,234 条结果"
            # 英文: "1,234 document results", "About 1,234 results"
            patterns = [
                r"找到\s*([\d,]+)\s*篇",
                r"([\d,]+)\s*篇\s*文[献档]",
                r"([\d,]+)\s*(?:document|doc)\s*results?",
                r"([\d,]+)\s*results?\s*found",
                r"([\d,]+)\s*条\s*(?:结果|检索)",
                r"([\d,]+)\s*(?:件|个)(?:\s*(?:文[献档]|结果))?",
            ]
            for pat in patterns:
                match = re.search(pat, text, re.I)
                if match:
                    return int(match.group(1).replace(",", ""))
        except Exception:
            pass
        return 0

    # ── CSV 解析 ──────────────────────────────────────

    def _parse_scopus_csv(self, csv_text: str) -> list[Paper]:
        """将 Scopus 导出的 CSV 解析为 Paper 列表。兼容中英文表头。"""
        if not csv_text.strip():
            return []

        reader = csv.DictReader(io.StringIO(csv_text))
        papers: list[Paper] = []

        for row in reader:
            try:
                paper = self._row_to_paper(row)
                if paper and paper.title:
                    papers.append(paper)
            except Exception as e:
                logger.debug("CSV 行解析失败: %s", e)

        return papers

    # Scopus CSV 表头映射（中英文）
    _HEADER_MAP = {
        "文献标题": "title", "Title": "title",
        "年份": "year", "Year": "year",
        "DOI": "doi",
        "链接": "scopus_url", "Link": "scopus_url",
        "摘要": "abstract", "Abstract": "abstract",
        "作者": "authors", "Authors": "authors",
        "来源出版物名称": "venue", "Source title": "venue",
        "卷": "volume", "Volume": "volume",
        "页": "pages", "Pages": "pages", "Page start": "pages",
        "被引频次": "cited_by", "Cited by": "cited_by",
        "文献类型": "document_type", "Document Type": "document_type",
    }

    def _row_to_paper(self, row: dict) -> Paper | None:
        """将 Scopus CSV 的一行转换为 Paper。"""
        get = lambda *keys: next(
            (row[k].strip() for k in keys if row.get(k, "").strip()), ""
        )

        title = get("文献标题", "Title")
        if not title:
            return None

        # 作者
        authors: list[Author] = []
        authors_str = get("作者", "Authors")
        if authors_str:
            for name in authors_str.split(","):
                name = name.strip().rstrip(".")
                if name:
                    parts = name.split()
                    surname = parts[0] if parts else name
                    given = " ".join(parts[1:]) if len(parts) > 1 else ""
                    authors.append(Author(surname=surname, given_name=given))

        year = None
        year_str = get("年份", "Year")
        if year_str:
            try:
                year = int(year_str)
            except ValueError:
                pass

        doi = get("DOI") or None
        paper_id = f"scopus:{doi}" if doi else f"scopus:{title[:80]}"

        citation_count = None
        cite_str = get("被引频次", "Cited by")
        if cite_str:
            try:
                citation_count = int(cite_str)
            except ValueError:
                pass

        return Paper(
            paper_id=paper_id,
            title=title,
            authors=authors,
            year=year,
            abstract=get("摘要", "Abstract") or None,
            doi=doi,
            scopus_url=get("链接", "Link") or None,
            venue=get("来源出版物名称", "Source title") or None,
            volume=get("卷", "Volume") or None,
            pages=get("页", "Pages", "Page start") or None,
            citation_count=citation_count,
            document_type=get("文献类型", "Document Type") or None,
        )

    # ── 简化接口 ──────────────────────────────────────

    async def search_by_intent(self, intent: SearchIntent, limit: int = 20) -> SearchResult:
        return await self.search(self.compiler.compile(intent), limit=limit)

    def to_csv(self, result: SearchResult, filename: str | None = None) -> Path:
        return self.exporter.export(result, filename)

    def to_csv_multi(self, results: list[SearchResult], filename: str | None = None) -> Path:
        return self.exporter.export_multi(results, filename)

    def get_cost_summary(self) -> SearchCost:
        return self.cost

    def reset_cost(self):
        self.cost = SearchCost()

    def new_session(self):
        self._session_id = uuid.uuid4().hex[:12]
        self.reset_cost()
        logger.info("新会话: %s", self._session_id)
