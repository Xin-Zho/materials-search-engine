"""MMRReranker — 最大边际相关性重排，避免 top-k 结果被同类论文占满。

MMR(d) = λ·Rel(d,q) − (1−λ)·max_{d'∈S} Sim(d,d')

同时考虑：
- 论文与问题的相关性（Rel = score/100）
- 论文与已选集合的相似度（避免重复）

相似度用混合信号：技术路线 + 分类标签 + 标题词重叠。
无外部依赖，纯 Python。

使用方式:
    reranker = MMRReranker(lambda_param=0.7)
    diverse = reranker.rerank(scored_papers, top_k=10)
"""

import re
import logging
from .models import ScoredPaper

logger = logging.getLogger(__name__)

# 英文停用词（简化版）
_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "in", "on", "for", "with", "by",
    "to", "at", "from", "as", "is", "are", "was", "were", "be", "been",
    "using", "based", "via", "under", "over", "into", "between", "among",
    "effect", "effects", "study", "studies", "their", "its", "this", "that",
    "these", "those", "new", "novel", "high", "low",
}


class MMRReranker:
    """MMR 多样性重排器。"""

    def __init__(self, lambda_param: float = 0.7):
        """lambda_param: 相关性权重 (0.6~0.75 推荐)。越高越偏相关性，越低越偏多样性。"""
        self.lambda_param = lambda_param

    def rerank(
        self,
        scored_papers: list[ScoredPaper],
        top_k: int = 10,
    ) -> list[ScoredPaper]:
        """从候选集中贪心选出 top_k 个既相关又多样的论文。

        不改变"哪些论文达标"——只改变从达标论文里选哪些进 top_k。
        """
        if not scored_papers:
            return []

        remaining = list(scored_papers)
        selected: list[ScoredPaper] = []

        while remaining and len(selected) < top_k:
            best_sp = None
            best_mmr = float("-inf")

            for sp in remaining:
                # 相关性（归一化到 0-1）
                rel = sp.score / 100.0 if sp.score else 0.0

                # 与已选集合的最大相似度
                max_sim = 0.0
                for sel in selected:
                    sim = self.similarity(sp, sel)
                    if sim > max_sim:
                        max_sim = sim

                mmr = self.lambda_param * rel - (1 - self.lambda_param) * max_sim
                if mmr > best_mmr:
                    best_mmr = mmr
                    best_sp = sp

            if best_sp is None:
                break

            selected.append(best_sp)
            remaining.remove(best_sp)

        logger.info("MMR 重排: %d 篇候选 → %d 篇 (λ=%.2f)",
                     len(scored_papers), len(selected), self.lambda_param)
        return selected

    def similarity(self, a: ScoredPaper, b: ScoredPaper) -> float:
        """混合相似度：route + category + 标题词重叠。"""
        # 1. 技术路线相同 → 高相似
        if a.route and b.route and a.route == b.route:
            route_sim = 1.0
        else:
            route_sim = 0.0

        # 2. 分类标签相同 → 中相似
        if a.category and b.category and a.category == b.category:
            cat_sim = 0.6
        else:
            cat_sim = 0.0

        # 3. 标题词重叠（Jaccard）
        text_sim = self._title_jaccard(a, b)

        return max(route_sim, cat_sim, text_sim)

    def _title_jaccard(self, a: ScoredPaper, b: ScoredPaper) -> float:
        """标题词集合的 Jaccard 相似度。"""
        words_a = self._tokenize(a.paper.title)
        words_b = self._tokenize(b.paper.title)

        if not words_a or not words_b:
            return 0.0

        inter = len(words_a & words_b)
        union = len(words_a | words_b)
        return inter / union if union else 0.0

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        """分词 + 去停用词 + 词干化（简单去尾）。"""
        if not text:
            return set()
        tokens = re.findall(r"[a-zA-Z0-9-]+", text.lower())
        result = set()
        for t in tokens:
            if t in _STOPWORDS or len(t) < 3:
                continue
            # 简单词干化：去掉常见后缀
            for suffix in ("s", "es", "ing", "ed", "tion", "ers", "ies"):
                if t.endswith(suffix) and len(t) - len(suffix) >= 3:
                    t = t[:-len(suffix)]
                    break
            result.add(t)
        return result
