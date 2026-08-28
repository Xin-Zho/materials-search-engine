"""query_family.py — v2.0 Query Family 数据结构（用户 2026-08-28 定稿）。

Family ≠ Query：一个 family 表示一个独立搜索方向，生成多条 lexical variants，
每条 variant 编译成一条 Scopus query。关键改变：不再有全局共享 anchor。

Family schema（registry 持久化格式）：
  {
    "family_id": "FAM_P_001",
    "family_type": "PROBLEM_ONLY",
    "concepts": {"problem": ["polymerization shrinkage"]},
    "generated_queries": ["TITLE-ABS-KEY(\"polymerization shrinkage\")", ...],
    "query_variants": [{"term": ..., "leakage": false, "source": ...}],
    "provenance": {"source": "RESEARCH_QUESTION", "derived_from_qgs_v1": false},
    "budget": 200,
    "stats": {"retrieved_unique": 0, "relevant_unique": 0, "new_venues": 0, "new_concepts": 0}
  }

6 类 family（v2.0 首批固定）：
  PROBLEM_ONLY       FAM-P      只给 problem 词，不加 material anchor（最关键）
  PROBLEM_MATERIAL   FAM-PM     problem × material
  PROBLEM_REACTION   FAM-PR     problem × reaction/mechanism
  PROBLEM_CONTEXT    FAM-PC     problem × context/community
  STRESS_SPECIFIC    FAM-STRESS stress 术语独立 family（shrinkage stress ≠ setting stress）
  VOLUME_FAMILY      FAM-VOLUME volume/contraction 术语独立 family
"""
from dataclasses import dataclass, field

FAMILY_TYPES = (
    "PROBLEM_ONLY",
    "PROBLEM_MATERIAL",
    "PROBLEM_REACTION",
    "PROBLEM_CONTEXT",
    "STRESS_SPECIFIC",
    "VOLUME_FAMILY",
)


@dataclass
class QueryVariant:
    term: str                  # lexical variant（如 "polymerisation shrinkage"）
    leakage: bool = False      # QGS-learned 变体标记
    source: str = "CLEAN"      # CLEAN / QGS_V1_LEARNED
    note: str = ""


@dataclass
class Family:
    family_id: str
    family_type: str
    concepts: dict[str, list[str]]          # slot -> term list
    budget: int = 200                       # 每 family 相同 K（第一版不做动态预算）
    provenance_source: str = "RESEARCH_QUESTION"
    derived_from_qgs_v1: bool = False
    variants: list[QueryVariant] = field(default_factory=list)
    generated_queries: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=lambda: {
        "retrieved_unique": 0, "relevant_unique": 0,
        "new_venues": 0, "new_concepts": 0})

    def to_dict(self) -> dict:
        return {
            "family_id": self.family_id,
            "family_type": self.family_type,
            "concepts": self.concepts,
            "query_variants": [v.__dict__ for v in self.variants],
            "generated_queries": self.generated_queries,
            "provenance": {"source": self.provenance_source,
                           "derived_from_qgs_v1": self.derived_from_qgs_v1},
            "budget": self.budget,
            "stats": self.stats,
        }

    @property
    def leakage_variants(self) -> list[QueryVariant]:
        return [v for v in self.variants if v.leakage]
