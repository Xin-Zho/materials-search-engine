"""RouteNormalizer — 把 extractor 输出的 raw route 映射到标准技术路线名。

解决"gold route 覆盖 0/6 假象"：extractor 输出 raw 表达
（"network reconfiguration via AFT"），benchmark gold route 是高层分类
（"AFCT 网络重排"），两者粒度不一致。

三层 route 层级：
  Level 0: 原文表达（raw，evidence）
  Level 1: 标准技术路线（canonical，如 "addition-fragmentation chain transfer"）
  Level 2: 研究问题路线（research route，如 "AFCT 网络重排降低收缩应力"）

本模块做 Level 0 → Level 1 的归一化（不依赖 benchmark，避免背答案）。

使用方式:
    normalizer = RouteNormalizer(backend)
    canonical_map = await normalizer.normalize(raw_routes)
    # → {"network reconfiguration via AFT": "addition-fragmentation chain transfer", ...}
"""

import json
import logging
from .llm import LLMBackend

logger = logging.getLogger(__name__)

NORMALIZE_PROMPT = """Normalize these raw research routes into CANONICAL technical route names.
Merge synonyms / hyponyms under a short canonical name (2-5 words).

Rules:
- Canonical name must be the STABLE core concept (e.g. "addition-fragmentation chain transfer", not "network reconfiguration via AFT during photopolymerization")
- Merge phrasings that describe the SAME route
- Do NOT merge genuinely different routes
- Output ONLY valid JSON array

## Raw routes
{raw_routes}

## Output Format
[{{"raw": "network reconfiguration via AFT during photopolymerization", "canonical": "addition-fragmentation chain transfer"}}, ...]"""


class RouteNormalizer:
    """raw route → canonical 技术路线名。"""

    def __init__(self, backend: LLMBackend):
        self.backend = backend

    async def normalize(self, raw_routes: list[str]) -> dict[str, str]:
        """把 raw routes 归一化成 canonical 名，返回 {raw: canonical} 映射。"""
        if not raw_routes:
            return {}

        unique_raw = list(dict.fromkeys(raw_routes))  # 去重保序
        prompt = NORMALIZE_PROMPT.format(raw_routes="\n".join(f"- {r}" for r in unique_raw))

        try:
            response = await self.backend.chat(
                system_prompt="You are a materials science route taxonomist. Output only valid JSON.",
                user_message=prompt,
                temperature=0,
                max_tokens=2000,
            )
        except Exception as e:
            logger.warning("route 归一化失败: %s", e)
            return {r: r for r in unique_raw}

        mapping = self._parse(response, unique_raw)
        logger.info("route 归一化: %d raw → %d canonical",
                     len(unique_raw), len(set(mapping.values())))
        return mapping

    @staticmethod
    def _parse(response: str, raw_routes: list[str]) -> dict[str, str]:
        text = response.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:])
            if text.endswith("```"):
                text = text[:-3]
        try:
            start = text.index("[")
            end = text.rindex("]") + 1
            items = json.loads(text[start:end])
        except (json.JSONDecodeError, ValueError):
            items = []

        mapping = {}
        for it in items:
            if isinstance(it, dict) and it.get("raw") and it.get("canonical"):
                mapping[it["raw"]] = it["canonical"]

        # 兜底：没映射到的 raw 用自身
        for r in raw_routes:
            if r not in mapping:
                mapping[r] = r
        return mapping
