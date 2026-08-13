"""Scopus HTML 结果解析器。

从 Scopus 搜索结果页面的 HTML 中提取 Paper 对象。

注意：Scopus 页面结构可能随版本变化。
如果解析失败，启用 debug 模式保存原始 HTML 以便调整选择器。
"""

import re
import logging
from bs4 import BeautifulSoup, Tag

from .models import Paper, Author

logger = logging.getLogger(__name__)


class ScopusParser:
    """解析 Scopus 搜索结果页面 HTML。"""

    def parse_search_results(self, html: str) -> tuple[list[Paper], int]:
        """从搜索结果页 HTML 中提取论文列表和总数。

        Returns:
            (papers, total_count)
        """
        soup = BeautifulSoup(html, "lxml")
        papers: list[Paper] = []

        # 提取总结果数
        total = self._extract_total_count(soup)

        # 定位每篇论文的条目
        entries = self._find_result_entries(soup)

        for entry in entries:
            try:
                paper = self._parse_entry(entry)
                if paper and paper.title:  # 标题为空则跳过
                    papers.append(paper)
            except Exception as e:
                logger.debug(f"解析单条结果失败: {e}")

        return papers, total

    def _extract_total_count(self, soup: BeautifulSoup) -> int:
        """提取搜索结果总数。"""
        # 尝试多种可能的选择器
        candidates = [
            soup.select_one("span.results-count"),
            soup.select_one("[data-testid='results-count']"),
            soup.find("span", string=re.compile(r"\d[\d,]*\s+(?:document|result)", re.I)),
        ]
        for el in candidates:
            if el:
                text = el.get_text(strip=True)
                match = re.search(r"([\d,]+)", text)
                if match:
                    return int(match.group(1).replace(",", ""))
        return 0

    def _find_result_entries(self, soup: BeautifulSoup) -> list[Tag]:
        """定位搜索结果中的每篇论文条目。"""
        # 主要尝试：ID 为 resultDataRow 开头的 <tr>
        entries = soup.select("tr[id^='resultDataRow']")
        if entries:
            return entries

        # 备选：通用的文档列表条目
        entries = soup.select("div.document-list-item, div.search-result-item, article.result-item")
        if entries:
            return entries

        # 最后尝试：任何包含标题链接的 <tr>
        return soup.select("tr:has(a[id^='resultDataRow'])")

    def _parse_entry(self, entry: Tag) -> Paper | None:
        """解析单条搜索结果条目。"""
        title_el = entry.select_one("a[id^='resultDataRow']") or \
                   entry.select_one("a.document-title-link") or \
                   entry.select_one("a[title]")

        if not title_el:
            return None

        title = title_el.get_text(strip=True)
        scopus_url = title_el.get("href", "")
        if scopus_url and not scopus_url.startswith("http"):
            scopus_url = "https://www.scopus.com" + scopus_url

        # 作者
        authors = self._parse_authors(entry)

        # 年份
        year = self._parse_year(entry)

        # DOI
        doi = self._parse_doi(entry)

        # 期刊
        venue = self._parse_venue(entry)

        # 卷/页
        volume, pages = self._parse_volume_pages(entry)

        # 被引次数
        citation_count = self._parse_citation_count(entry)

        # 摘要
        abstract = self._parse_abstract(entry)

        # 文献类型
        doc_type = self._parse_doc_type(entry)

        # 生成内部 ID
        paper_id = f"scopus:{doi}" if doi else f"scopus:{title[:80]}"

        return Paper(
            paper_id=paper_id,
            title=title,
            authors=authors,
            year=year,
            abstract=abstract,
            doi=doi,
            scopus_url=scopus_url,
            venue=venue,
            volume=volume,
            pages=pages,
            citation_count=citation_count,
            document_type=doc_type,
        )

    def _parse_authors(self, entry: Tag) -> list[Author]:
        """提取作者列表。"""
        authors: list[Author] = []

        # Scopus 通常把作者放在 span.doc-author 或类似容器
        author_container = entry.select_one("span.doc-author") or \
                           entry.select_one("span[class*='author']") or \
                           entry.select_one("div.author-list")

        if author_container:
            # 移除 "..." 或 "View all authors" 等多余文本
            text = author_container.get_text(strip=True)
            # 移除括号中的 Scopus Author ID
            text = re.sub(r"\([\d]+\)", "", text)
            # 按逗号分割作者
            for part in text.split(","):
                part = part.strip().rstrip(".")
                if part and not part.lower().startswith(("view", "et al", "…")):
                    # 尝试分割姓和名
                    names = part.split()
                    if len(names) >= 2:
                        surname = names[0]
                        given = " ".join(names[1:])
                    else:
                        surname = part
                        given = ""
                    authors.append(Author(surname=surname, given_name=given))

        return authors

    def _parse_year(self, entry: Tag) -> int | None:
        """提取出版年份。"""
        # 尝试多种位置
        year_el = entry.select_one("span.doc-year") or \
                  entry.select_one("span[class*='year']") or \
                  entry.select_one("time")

        if year_el:
            text = year_el.get_text(strip=True)
            match = re.search(r"(19|20)\d{2}", text)
            if match:
                return int(match.group(0))

        # 从出版信息中提取
        pub_el = entry.select_one("span.doc-publication") or \
                 entry.select_one("span[class*='pub']")
        if pub_el:
            text = pub_el.get_text(strip=True)
            match = re.search(r"(19|20)\d{2}", text)
            if match:
                return int(match.group(0))

        return None

    def _parse_doi(self, entry: Tag) -> str | None:
        """提取 DOI。"""
        # 直接从文本中匹配 DOI 模式
        text = entry.get_text()
        match = re.search(r"10\.\d{4,}/[^\s]+", text)
        if match:
            doi = match.group(0)
            # 清理尾部标点
            doi = doi.rstrip(".,;:)")
            return doi
        return None

    def _parse_venue(self, entry: Tag) -> str | None:
        """提取期刊/会议名。"""
        venue_el = entry.select_one("span.doc-publication") or \
                   entry.select_one("span[class*='journal']") or \
                   entry.select_one("span[class*='source']")
        if venue_el:
            text = venue_el.get_text(strip=True)
            # 通常格式: "Journal Name, Volume, Pages"
            # 取第一个逗号前的部分
            if "," in text:
                text = text.split(",")[0].strip()
            # 移除年份
            text = re.sub(r"\b(19|20)\d{2}\b", "", text).strip()
            if text:
                return text
        return None

    def _parse_volume_pages(self, entry: Tag) -> tuple[str | None, str | None]:
        """提取卷号和页码。"""
        volume = None
        pages = None

        pub_el = entry.select_one("span.doc-publication") or \
                 entry.select_one("span[class*='pub']")
        if pub_el:
            text = pub_el.get_text(strip=True)

            # 卷号: "Volume XX" 或 ", XX, "
            vol_match = re.search(r"(?:Volume|Vol\.?)\s*(\d+)", text, re.I)
            if vol_match:
                volume = vol_match.group(1)
            else:
                # 尝试 ", 39, " 模式（数字在逗号间）
                parts = [p.strip() for p in text.split(",")]
                for p in parts:
                    if p.isdigit() and len(p) <= 3:
                        volume = p
                        break

            # 页码: "pp. XX-YY" 或 "XX-YY"
            page_match = re.search(r"(?:pp?\.?\s*)?(\d+[-–]\d+)", text)
            if page_match:
                pages = page_match.group(1)

        return volume, pages

    def _parse_citation_count(self, entry: Tag) -> int | None:
        """提取被引次数。"""
        cite_el = entry.select_one("span.doc-citation-count") or \
                  entry.select_one("span[class*='citation']") or \
                  entry.select_one("span[class*='cited']")

        if cite_el:
            text = cite_el.get_text(strip=True)
            match = re.search(r"(\d+)", text)
            if match:
                return int(match.group(1))
        return None

    def _parse_abstract(self, entry: Tag) -> str | None:
        """提取摘要。"""
        # 主要选择器
        abstract_el = entry.select_one("div.doc-abstract") or \
                      entry.select_one("div[class*='abstract']") or \
                      entry.select_one("p[class*='abstract']")

        if abstract_el:
            text = abstract_el.get_text(strip=True)
            # 移除 "Abstract:" 前缀
            text = re.sub(r"^Abstract\s*[:：]\s*", "", text, flags=re.I)
            if text:
                return text

        # 备选：从隐藏的摘要区域提取
        hidden_abstract = entry.select_one("div.AbstractHidden") or \
                          entry.select_one("span[class*='abstract']")
        if hidden_abstract:
            text = hidden_abstract.get_text(strip=True)
            if text:
                return text

        return None

    def _parse_doc_type(self, entry: Tag) -> str | None:
        """提取文献类型。"""
        type_el = entry.select_one("span.doc-type") or \
                  entry.select_one("span[class*='doctype']")
        if type_el:
            text = type_el.get_text(strip=True)
            if "review" in text.lower():
                return "Review"
            elif "conference" in text.lower():
                return "Conference Paper"
            elif "article" in text.lower():
                return "Article"
            return text
        return None
