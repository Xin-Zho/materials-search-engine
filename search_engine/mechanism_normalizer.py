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
    "stress relaxation": ["stress relaxation", "relax stress", "stress relief"],
    "delayed gelation": ["delayed gelation", "delay gelation", "delayed gel point", "gelation delay"],
    "volumetric expansion": ["volumetric expansion", "volume expansion", "expansion"],
    "ring strain relief": ["ring strain relief", "ring strain release", "ring strain"],
    "reversible bond exchange": ["reversible bond exchange", "dynamic bond exchange", "bond exchange", "exchange reaction", "reversible exchange", "reversible chain transfer"],
    "network rearrangement": ["network rearrangement", "network reconfiguration", "network restructuring", "network relaxation", "network adaptation"],
    "chain transfer": ["chain transfer", "addition-fragmentation chain transfer", "AFCT"],
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

    def aliases_for(self, mechanism: str) -> list[str]:
        """返回 mechanism 的匹配别名集（含自身，小写），用于子串匹配。

        mechanism 可能是 canonical key，也可能是某个 key 的别名；
        返回它所属的完整别名列表。与 normalize() 的一对一不同，
        这里保留完整别名集，让 coverage 能对每个 checklist mechanism
        独立做子串匹配（一个关键词不会同时触发多个标签）。
        """
        m = mechanism.lower().strip()
        if not m:
            return []
        if m in self.ontology:
            out = [m]
            for alias in self.ontology[m]:
                a = alias.lower()
                if a not in out:
                    out.append(a)
            return out
        for canon, aliases in self.ontology.items():
            for alias in aliases:
                if alias.lower() == m:
                    out = [canon.lower()]
                    for a in aliases:
                        al = a.lower()
                        if al not in out:
                            out.append(al)
                    return out
        return [m]

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
