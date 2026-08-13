"""Benchmark & Evaluator — 对照基准集计算搜索覆盖度。

把"考试卷"用起来：给定搜索结果，检查命中了多少 gold set 文献，
报告覆盖率（命中数/总数）、各路线命中情况、遗漏文献。

使用方式:
    benchmark = Benchmark("benchmarks/benchmarks_v1.json")
    result = benchmark.evaluate("pc_001", found_papers)
    print(result["coverage"])   # 0.6 = 命中了 60% 的 gold set
"""

import json
import logging
from pathlib import Path
from .models import Paper

logger = logging.getLogger(__name__)


def normalize_doi(doi: str | None) -> str:
    """规范化 DOI：小写、去尾部标点、去 URL 前缀。"""
    if not doi:
        return ""
    doi = doi.strip().lower()
    doi = doi.replace("https://doi.org/", "").replace("http://doi.org/", "")
    doi = doi.rstrip(".,;:)]}")
    return doi


class Benchmark:
    """基准集加载器 + 覆盖度评估。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text(encoding="utf-8"))

    def questions(self) -> list[dict]:
        return self.data.get("questions", [])

    def get_question(self, question_id: str) -> dict | None:
        for q in self.questions():
            if q.get("id") == question_id:
                return q
        return None

    def evaluate(
        self,
        question_id: str,
        found_papers: list[Paper],
    ) -> dict:
        """评估搜索结果对某问题的覆盖度。

        Args:
            found_papers: 搜索找到的论文（ScoredPaper 或 Paper 均可，需有 .paper 或直接是 Paper）

        Returns:
            dict: coverage / gold_hits / gold_total / hits / missed / route_coverage
        """
        question = self.get_question(question_id)
        if not question:
            raise ValueError(f"未知问题 ID: {question_id}")

        # 兼容 ScoredPaper 和 Paper
        papers = []
        for p in found_papers:
            papers.append(p.paper if hasattr(p, "paper") else p)

        # 提取找到的 DOI 集合
        found_dois = {normalize_doi(p.doi) for p in papers if p.doi}

        # 全部 key_papers 和 gold set
        all_key = question.get("key_papers", [])
        gold = [k for k in all_key if k.get("must_hit", True)]

        # 命中判断
        hits = []
        missed = []
        for k in all_key:
            if normalize_doi(k.get("doi")) in found_dois:
                hits.append(k)
            else:
                missed.append(k)

        gold_hits = [k for k in gold if normalize_doi(k.get("doi")) in found_dois]

        # 按路线统计
        route_coverage = {}
        for k in all_key:
            route = k.get("route", "未分类")
            route_coverage.setdefault(route, {"total": 0, "hits": 0})
            route_coverage[route]["total"] += 1
            if normalize_doi(k.get("doi")) in found_dois:
                route_coverage[route]["hits"] += 1

        coverage = len(gold_hits) / len(gold) if gold else 0.0
        result = {
            "question_id": question_id,
            "total_found": len(found_dois),
            "key_total": len(all_key),
            "key_hits": len(hits),
            "gold_total": len(gold),
            "gold_hits": len(gold_hits),
            "coverage": coverage,
            "hits": hits,
            "missed": missed,
            "route_coverage": route_coverage,
        }

        logger.info("覆盖度评估: gold set %.0f%% (%d/%d), key papers %d/%d",
                     coverage * 100, len(gold_hits), len(gold), len(hits), len(all_key))
        return result

    def format_report(self, result: dict) -> str:
        """格式化评估报告。"""
        lines = []
        lines.append(f"=== 覆盖度评估 [{result['question_id']}] ===")
        lines.append(f"Gold set 覆盖率: {result['coverage']*100:.0f}% "
                     f"({result['gold_hits']}/{result['gold_total']})")
        lines.append(f"全部关键文献: {result['key_hits']}/{result['key_total']}")
        lines.append("")

        if result["route_coverage"]:
            lines.append("按路线:")
            for route, rc in sorted(result["route_coverage"].items()):
                lines.append(f"  {route}: {rc['hits']}/{rc['total']}")

        if result["missed"]:
            lines.append("")
            lines.append("遗漏的关键文献:")
            for k in result["missed"]:
                lines.append(f"  - [{k.get('year')}] {k.get('title','')[:60]} "
                             f"({k.get('route','')})")

        return "\n".join(lines)
