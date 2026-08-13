"""CoverageMap — 覆盖地图，把论文聚类成技术路线，识别缺口，缺口驱动下一轮。

这是"覆盖驱动"的核心：从"相关论文最多"变成"相关且覆盖全面"。

使用方式:
    cm = CoverageMap()
    cm.build(scored_papers)
    gaps = cm.identify_gaps()
    gap_description = cm.describe_gaps()   # 喂给下一轮查询生成
"""

import logging
from collections import defaultdict
from .models import ScoredPaper, RouteCoverage

logger = logging.getLogger(__name__)


class CoverageMap:
    """覆盖地图管理器。"""

    def __init__(self):
        self.routes: dict[str, RouteCoverage] = {}

    def build(self, scored_papers: list[ScoredPaper]):
        """按 route 聚类论文，构建覆盖地图。"""
        self.routes = {}
        for sp in scored_papers:
            route = sp.route or sp.category or "未分类"
            if route not in self.routes:
                self.routes[route] = RouteCoverage(route_name=route)

            rc = self.routes[route]
            rc.paper_ids.append(sp.paper.paper_id)
            rc.paper_count += 1

            # 最新年份
            if sp.paper.year and (rc.latest_year is None or sp.paper.year > rc.latest_year):
                rc.latest_year = sp.paper.year

            # 类型判断
            doc_type = (sp.paper.document_type or "").lower()
            if "review" in doc_type or "综述" in (sp.category or ""):
                rc.has_review = True
            if sp.category and ("反例" in sp.category or "限制" in sp.category or "失效" in sp.category):
                rc.has_counter_example = True
            if sp.info_gain >= 0.5:
                rc.has_original = True

        logger.info("覆盖地图: %d 条技术路线", len(self.routes))

    def identify_gaps(self) -> list[str]:
        """识别覆盖薄弱的路线。

        Returns:
            覆盖薄弱或缺失的路线描述列表
        """
        gaps = []
        for name, rc in self.routes.items():
            if rc.paper_count <= 1:
                gaps.append(f"{name}（仅 {rc.paper_count} 篇）")
            elif not rc.has_review and rc.paper_count >= 3:
                gaps.append(f"{name}（缺综述）")
            elif not rc.has_counter_example and rc.paper_count >= 3:
                gaps.append(f"{name}（缺反例/限制）")
            if rc.latest_year and rc.latest_year < 2023:
                gaps.append(f"{name}（最新仅 {rc.latest_year} 年）")

        return gaps

    def describe_gaps(self, target_routes: int = 5) -> str:
        """生成缺口描述文本，供下一轮查询生成使用。

        Returns:
            中文缺口描述，例如 "相分离增韧只有1篇；动态共价网络缺综述；..."
        """
        if not self.routes:
            return "第一轮 — 尚无覆盖信息，需要广覆盖探索。"

        lines = []
        for name, rc in sorted(self.routes.items(), key=lambda x: x[1].paper_count):
            status = []
            if rc.paper_count <= 1:
                status.append(f"仅{rc.paper_count}篇")
            if not rc.has_review and rc.paper_count >= 3:
                status.append("缺综述")
            if not rc.has_counter_example and rc.paper_count >= 3:
                status.append("缺反例")
            if rc.latest_year and rc.latest_year < 2023:
                status.append(f"最新{rc.latest_year}年")

            if status:
                lines.append(f"{name}：{'、'.join(status)}")

        if not lines:
            return "各路线覆盖较均衡，主要缺口是探索未出现的新路线。"

        return "；".join(lines) + "。请生成填补以上缺口的查询。"

    def summarize(self) -> str:
        """覆盖地图摘要（用于日志/展示）。"""
        if not self.routes:
            return "(空)"
        lines = []
        for name, rc in sorted(self.routes.items(), key=lambda x: x[1].paper_count, reverse=True):
            lines.append(
                f"  {name}: {rc.paper_count}篇 "
                f"(最新{rc.latest_year}, 综述={rc.has_review}, 反例={rc.has_counter_example})"
            )
        return "\n".join(lines)
