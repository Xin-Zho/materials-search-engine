"""CSV 导出 — 将 SearchResult 或 Paper 列表导出为 CSV 文件。

规格:
    - 编码: UTF-8 with BOM
    - 分隔符: 逗号
    - 引号: 所有字段用双引号
    - 换行: CRLF (Windows)
"""

import csv
from datetime import datetime, timezone
from pathlib import Path

from .models import Paper, SearchResult


# 导出列定义
CSV_COLUMNS = [
    "title",
    "authors",
    "year",
    "doi",
    "journal",
    "volume",
    "pages",
    "citation_count",
    "document_type",
    "abstract",
    "scopus_url",
    "search_query",
    "retrieved_at",
]


class CsvExporter:
    """将论文数据导出为 CSV。"""

    def __init__(self, output_dir: str | Path = "data/exports"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def export(
        self,
        papers_or_result: list[Paper] | SearchResult,
        filename: str | None = None,
    ) -> Path:
        """导出论文列表或搜索结果到 CSV。

        Args:
            papers_or_result: Paper 列表或 SearchResult
            filename: CSV 文件名（不含路径）。为 None 时自动生成。

        Returns:
            实际写入的文件路径。
        """
        if isinstance(papers_or_result, SearchResult):
            papers = papers_or_result.papers
            query = papers_or_result.query
        else:
            papers = papers_or_result
            query = ""

        if filename is None:
            timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            filename = f"{timestamp}_search_export.csv"

        filepath = self.output_dir / filename

        with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f, quoting=csv.QUOTE_ALL)

            # 表头
            writer.writerow(CSV_COLUMNS)

            # 数据行
            retrieved_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            for paper in papers:
                writer.writerow(self._paper_to_row(paper, query, retrieved_at))

        return filepath

    def _paper_to_row(self, paper: Paper, search_query: str, retrieved_at: str) -> list[str]:
        """将 Paper 转换为 CSV 行。"""
        return [
            paper.title or "",
            "; ".join(str(a) for a in paper.authors) if paper.authors else "",
            str(paper.year) if paper.year else "",
            paper.doi or "",
            paper.venue or "",
            paper.volume or "",
            paper.pages or "",
            str(paper.citation_count) if paper.citation_count is not None else "",
            paper.document_type or "",
            paper.abstract or "",
            paper.scopus_url or "",
            search_query,
            retrieved_at,
        ]

    def export_multi(
        self,
        results: list[SearchResult],
        filename: str | None = None,
    ) -> Path:
        """将多次搜索的结果合并导出到一个 CSV。"""
        all_papers: list[Paper] = []
        for result in results:
            for paper in result.papers:
                # 将各自的查询记录到 paper 上
                all_papers.append(paper)

        if filename is None:
            timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            filename = f"{timestamp}_multi_search.csv"

        # 简单合并——如果同一篇论文出现在多次搜索中，会保留多条
        # 但去重是 cache 层的职责
        return self.export(all_papers, filename)

    def export_scored(
        self,
        scored: list,
        filename: str | None = None,
        question: str = "",
    ) -> Path:
        """导出带评分的论文（含 score / category / route / reason）。

        Args:
            scored: list[ScoredPaper]
        """
        if filename is None:
            timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            filename = f"{timestamp}_scored.csv"

        filepath = self.output_dir / filename

        columns = ["title", "authors", "year", "doi", "score", "category", "route",
                   "info_gain", "reason", "abstract"]

        with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f, quoting=csv.QUOTE_ALL)
            writer.writerow(columns)

            for sp in scored:
                writer.writerow([
                    sp.paper.title or "",
                    "; ".join(str(a) for a in sp.paper.authors) if sp.paper.authors else "",
                    str(sp.paper.year) if sp.paper.year else "",
                    sp.paper.doi or "",
                    str(sp.score),
                    sp.category or "",
                    sp.route or "",
                    f"{sp.info_gain:.2f}" if sp.info_gain else "",
                    sp.reason or "",
                    (sp.paper.abstract or "")[:1000],
                ])

        return filepath
