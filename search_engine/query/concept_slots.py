"""concept_slots.py — v2.0 Query-Family 概念槽位（P/M/R/C）。

把 research question 拆成 4 类 concept slot：
  P = Problem / phenomenon   （聚合收缩、收缩应力等）
  M = Material / system      （光聚合物、树脂、复合材料等）
  R = Reaction / mechanism   （自由基/阳离子/开环聚合等）
  C = Context / application  （牙科、涂料、增材制造、全息等）

设计原则（用户 2026-08-28 定稿）：
  1. 初始概念只来自 research question 能明确提供的少量概念 + KB 已有概念
     —— 不人工穷举，不写 QGS 单篇对应词。
  2. 每个概念带 provenance + leakage 标记：
       CLEAN           —— research question / 通用领域 / KB 已有（leakage=False）
       QGS_V1_LEARNED  —— 从 QGS v1 failure analysis 学到的（leakage=True，
                          必须进 leakage ledger）
  3. 后续由 Knowledge Extractor / community discovery 动态扩展。
"""
from dataclasses import dataclass, field

SLOT_NAMES = ("problem", "material", "reaction", "context")

# 槽位中文说明（调试/文档用）
SLOT_DESC = {
    "problem": "Problem / phenomenon（收缩、应力、体积变化）",
    "material": "Material / system（光聚合物、树脂、复合材料）",
    "reaction": "Reaction / mechanism（自由基、阳离子、开环）",
    "context": "Context / application（牙科、涂料、AM、全息）",
}


@dataclass
class Concept:
    term: str
    slot: str                 # problem / material / reaction / context
    provenance: str           # RESEARCH_QUESTION / KB / QGS_V1_LEARNED
    leakage: bool = False     # True 表示来自 QGS v1（开发可用但进 ledger）
    note: str = ""

    def to_dict(self) -> dict:
        return {"term": self.term, "slot": self.slot,
                "provenance": self.provenance, "leakage": self.leakage,
                "note": self.note}


# ── 初始概念（v2.0 首批，2026-08-28）──────────────────────────────
# research question：光固化聚合物降低聚合收缩与收缩应力的机制
#   → P 核心词（shrinkage/stress）与 M 核心词（光固化）由 question 直接提供（CLEAN）
#   → R 机制词来自 KB 已有 route/mechanism（CLEAN）
#   → C 社区词：KB 已知 AM/3D 打印方向（CLEAN）；dental/holography/coating 等
#     来自 P3-E failure analysis 的 COMMUNITY_ISOLATION 证据（QGS_V1_LEARNED，leakage）
#   → 历史术语（contraction/setting stress/hardening stress）来自 P3-E 的
#     HISTORICAL_TERMINOLOGY 证据（QGS_V1_LEARNED，leakage）

INITIAL_CONCEPTS: list[Concept] = [
    # P — research question 直接提供（CLEAN）
    Concept("polymerization shrinkage", "problem", "RESEARCH_QUESTION"),
    Concept("shrinkage stress", "problem", "RESEARCH_QUESTION"),
    Concept("volumetric shrinkage", "problem", "RESEARCH_QUESTION"),
    # P — QGS v1 学的历史/应力词汇（leakage，进 ledger）
    Concept("contraction stress", "problem", "QGS_V1_LEARNED", True,
            "P3-E HISTORICAL_TERMINOLOGY 证据（dental 早期文献常用 contraction）"),
    Concept("setting stress", "problem", "QGS_V1_LEARNED", True,
            "P3-E HISTORICAL_TERMINOLOGY：早期 dental 文献术语"),
    Concept("hardening stress", "problem", "QGS_V1_LEARNED", True,
            "P3-E HISTORICAL_TERMINOLOGY：早期 dental 文献术语"),
    Concept("polymerization contraction", "problem", "QGS_V1_LEARNED", True,
            "P3-E：contraction 术语体系（HISTORICAL_TERMINOLOGY）"),
    Concept("cure-induced stress", "problem", "QGS_V1_LEARNED", True,
            "P3-E：curing stress 术语体系"),
    # M — research question 直接提供（CLEAN）
    Concept("photopolymer", "material", "RESEARCH_QUESTION"),
    Concept("photocurable resin", "material", "RESEARCH_QUESTION"),
    Concept("composite", "material", "RESEARCH_QUESTION"),
    # R — KB 已有机制概念（CLEAN：开发资产，非 QGS 泄漏）
    Concept("radical polymerization", "reaction", "KB"),
    Concept("cationic polymerization", "reaction", "KB"),
    Concept("ring-opening polymerization", "reaction", "KB"),
    Concept("thiol-ene", "reaction", "KB"),
    Concept("step-growth polymerization", "reaction", "KB"),
    # C — KB 已知方向（CLEAN）
    Concept("additive manufacturing", "context", "KB"),
    Concept("stereolithography", "context", "KB"),
    Concept("vat photopolymerization", "context", "KB"),
    Concept("3D printing", "context", "KB"),
    # C — QGS v1 学的社区（leakage，进 ledger）
    Concept("dental", "context", "QGS_V1_LEARNED", True,
            "P3-E COMMUNITY_ISOLATION：dental 社区是 v1 最大盲区（SR_07 0/40）"),
    Concept("holography", "context", "QGS_V1_LEARNED", True,
            "P3-E COMMUNITY_ISOLATION：全息记录社区（SR_05）"),
    Concept("coating", "context", "QGS_V1_LEARNED", True,
            "P3-E：涂料/涂层社区（UV curing coatings）"),
]


def concepts_by_slot(concepts: list[Concept] | None = None) -> dict[str, list[Concept]]:
    """按槽位分组。"""
    src = concepts if concepts is not None else INITIAL_CONCEPTS
    out: dict[str, list[Concept]] = {s: [] for s in SLOT_NAMES}
    for c in src:
        out[c.slot].append(c)
    return out


def qgs_learned_concepts(concepts: list[Concept] | None = None) -> list[Concept]:
    """QGS-learned（leakage=True）概念——用于登记 leakage ledger。"""
    src = concepts if concepts is not None else INITIAL_CONCEPTS
    return [c for c in src if c.leakage]
