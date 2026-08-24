"""MechanismNormalizer — 把 raw mechanism 归一化成 canonical mechanism。

解决 Route × Mechanism Matrix 全是 ✗ 的假阴性问题：
ontology 的 "stress relaxation" 与论文抽取的 "stress relaxation via
network reconfiguration" 字符串不同，导致严格匹配失败。

类似 route_normalizer：raw → canonical。

使用方式:
    normalizer = MechanismNormalizer()
    canonical = normalizer.normalize("stress relaxation via network reconfiguration")
    # → "stress relaxation"
"""

import logging

logger = logging.getLogger(__name__)

# 固定的 canonical mechanism → 别名（人工维护，类似 route ontology）
MECHANISM_ONTOLOGY: dict[str, list[str]] = {
    "stress relaxation": ["stress relaxation", "network reconfiguration", "stress release", "viscoelastic relaxation", "relaxation"],
    "delayed gelation": ["delayed gelation", "delayed gel point", "late gelation", "gel delay", "delayed gel"],
    "volumetric expansion": ["volumetric expansion", "volume expansion", "expansion"],
    "ring strain relief": ["ring strain relief", "ring strain release", "ring strain"],
    "reversible bond exchange": ["reversible bond exchange", "bond exchange", "dynamic bond exchange", "reversible exchange"],
    "network rearrangement": ["network rearrangement", "topology rearrangement", "network reconfiguration"],
    "chain transfer": ["chain transfer", "addition fragmentation", "addition-fragmentation", "aft"],
    "reduced shrinkage": ["reduced shrinkage", "shrinkage reduction", "low shrinkage", "shrinkage compensation", "compensation of shrinkage"],
    "reduced polymerizable fraction": ["reduced polymerizable fraction", "reduced resin fraction", "lower resin content", "filler volume"],
    "modulus increase": ["modulus increase", "increased modulus", "stiffness increase", "rigidity"],
    "stress transfer": ["stress transfer", "load transfer", "particle reinforcement", "filler reinforcement"],
    "ring strain relief": ["ring strain relief", "ring strain release", "ring strain", "strain relief"],
    "cationic polymerization": ["cationic polymerization", "cationic ring-opening", "cationic"],
    "step-growth polymerization": ["step-growth", "step growth", "stepwise polymerization"],
    "oxygen tolerance": ["oxygen tolerance", "oxygen inhibition resistance", "oxygen insensitive"],
    "uniform network formation": ["uniform network", "homogeneous network", "regular network"],
    "low shrinkage monomer": ["low shrinkage monomer", "low-shrinkage", "shrinkage-resistant monomer"],
    "reduced double bond density": ["reduced double bond density", "lower double bond", "reduced methacrylate content"],
    "molecular weight control": ["molecular weight control", "molar mass control", "chain length control"],
    "two-stage cure": ["two-stage cure", "two stage", "dual cure", "dual-cure"],
    "stress relief": ["stress relief", "stress release", "residual stress reduction"],
    "delayed network formation": ["delayed network formation", "late network formation", "postponed crosslinking"],
}


class MechanismNormalizer:
    """raw mechanism → canonical mechanism。"""

    def __init__(self, ontology: dict[str, list[str]] | None = None):
        self.ontology = ontology or MECHANISM_ONTOLOGY

    def normalize(self, raw_mechanism: str) -> str:
        """把 raw mechanism 映射到 canonical（子串匹配别名）。"""
        rl = raw_mechanism.lower().strip()
        if not rl:
            return ""

        for canonical, aliases in self.ontology.items():
            for alias in aliases:
                if alias in rl or rl in alias:
                    return canonical
        return raw_mechanism  # 没匹配到，保留原样

    def normalize_many(self, raw_mechanisms: list[str]) -> list[str]:
        """批量归一化，去重。"""
        seen = set()
        result = []
        for raw in raw_mechanisms:
            canonical = self.normalize(raw)
            if canonical and canonical.lower() not in seen:
                seen.add(canonical.lower())
                result.append(canonical)
        return result
