"""固定的 route → mechanism ontology（7 个核心 route，人工固定）。

这是 Phase 1 的"标准答案"层：每个核心 route 固定一组 mechanism，
coverage 用这个固定集合做 route × mechanism matrix，不随 LLM 输出波动。

使用方式:
    from .route_mechanism_ontology import CORE_ROUTE_MECHANISMS
    mechs = CORE_ROUTE_MECHANISMS["AFCT"]["mechanisms"]
"""

# 7 个核心 route，每个固定 4-6 个 core mechanism + 别名
CORE_ROUTE_MECHANISMS: dict[str, dict] = {
    "AFCT": {
        "aliases": ["addition-fragmentation chain transfer", "AFT", "chain transfer monomer"],
        "mechanisms": [
            "reversible bond exchange",
            "network rearrangement",
            "stress relaxation",
            "delayed gelation",
            "chain transfer",
        ],
    },
    "ring-opening": {
        "aliases": ["ring-opening polymerization", "silorane", "spiro orthocarbonate", "expanding monomer"],
        "mechanisms": [
            "volumetric expansion",
            "ring strain relief",
            "reduced shrinkage",
            "cationic polymerization",
        ],
    },
    "thiol-ene": {
        "aliases": ["thiol-ene polymerization", "thiol-ene addition"],
        "mechanisms": [
            "delayed gelation",
            "step-growth polymerization",
            "oxygen tolerance",
            "uniform network formation",
        ],
    },
    "step-growth": {
        "aliases": ["step-growth polymerization"],
        "mechanisms": [
            "delayed gel point",
            "stress relaxation",
            "late gelation",
        ],
    },
    "filler": {
        "aliases": ["filler loading", "silica", "nanoparticle", "inorganic filler"],
        "mechanisms": [
            "reduced polymerizable fraction",
            "stress transfer",
            "reduced shrinkage strain",
            "modulus increase",
        ],
    },
    "monomer-design": {
        "aliases": ["low-shrinkage monomer", "monomer modification", "oligomer design"],
        "mechanisms": [
            "low shrinkage monomer",
            "reduced double bond density",
            "molecular weight control",
        ],
    },
    "dual-curing": {
        "aliases": ["dual-cure", "two-stage curing", "gradient polymerization"],
        "mechanisms": [
            "two-stage cure",
            "stress relief",
            "delayed network formation",
        ],
    },
}


def match_route(canonical_route: str) -> str | None:
    """把 canonical route 映射到 7 个核心 route 之一（模糊匹配别名）。"""
    rl = canonical_route.lower()
    for core, info in CORE_ROUTE_MECHANISMS.items():
        if core.lower() in rl:
            return core
        for alias in info["aliases"]:
            if alias.lower() in rl or rl in alias.lower():
                return core
    return None


def get_mechanisms(core_route: str) -> list[str]:
    """返回核心 route 的固定 mechanism 集合。"""
    return CORE_ROUTE_MECHANISMS.get(core_route, {}).get("mechanisms", [])
