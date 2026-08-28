"""variant_generator.py — v2.0 Lexical Variant 自动生成。

两个来源（用户 2026-08-28 定稿，必须分清）：
  1. CLEAN —— 通用规则变体，无 leakage：
       - 英/美拼写（polymerization ↔ polymerisation）
       - 复数/单数（shrinkage 不可数，但 composite/composites）
       - 连字符/空格变体（ring-opening ↔ ring opening；photo-polymer ↔ photopolymer）
       - 缩写/全称（SLA ↔ stereolithography 不自动做——缩写需领域知识，留给 KB）
  2. QGS_V1_LEARNED —— 从 QGS v1 failure analysis 学到的术语
       （setting stress / hardening stress / contraction 系 / dental 等社区词）
       标记 leakage=True，必须进 leakage ledger。

variant 生成后，family 编译成 Scopus query 时用 TITLE-ABS-KEY("term") 形式。
"""
import re

# ── 英/美拼写规则 ─────────────────────────────────
BRITISH_TO_AMERICAN = {
    "polymerisation": "polymerization",
    "photopolymerisation": "photopolymerization",
    "copolymerisation": "copolymerization",
    "colour": "color",
}

# 常见词缀：美式 -ize / 英式 -ise（-ization ↔ -isation）
_ISE_TO_IZE = re.compile(r"isation\b")


def _british_variants(term: str) -> list[str]:
    """英式拼写变体（-ization → -isation）。"""
    out = []
    if "ization" in term:
        out.append(term.replace("ization", "isation"))
    return out


def _hyphen_variants(term: str) -> list[str]:
    """连字符/空格/粘连变体：ring-opening ↔ ring opening；photo-polymer ↔ photopolymer。"""
    out = []
    if "-" in term:
        out.append(term.replace("-", " "))      # ring-opening → ring opening
        out.append(term.replace("-", ""))       # ring-opening → ringopening（罕见，Scopus 兼容）
    else:
        # 粘连复合词拆连字符（如 photopolymer → photo-polymer）
        for m in re.finditer(r"(photo|photo|curing|cure)(?=[a-z])", term):
            if m.start() > 0:
                out.append(term[:m.start()] + m.group(1) + "-" + term[m.start() + len(m.group(1)):])
    return [v for v in out if v != term]


def _plural_variants(term: str) -> list[str]:
    """复数变体（仅规则名词；不适用于 shrinkage 等不可数）。"""
    out = []
    if term.endswith("s"):
        return out
    for suffix in ("composite", "monomer", "resin", "polymer", "filler"):
        if term.endswith(suffix):
            out.append(term + "s")
    return out


def generate_clean_variants(term: str) -> list[str]:
    """CLEAN 变体（通用规则，leakage=False）。返回去重、不含自身的变体列表。"""
    variants = []
    variants += _british_variants(term)
    variants += _hyphen_variants(term)
    variants += _plural_variants(term)
    # 去重 + 去空 + 去自身
    seen = {term}
    out = []
    for v in variants:
        v = re.sub(r"\s+", " ", v.strip())
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def generate_variants(term: str, source: str = "CLEAN", leakage: bool = False) -> list[str]:
    """统一入口：按来源生成变体。
    source=CLEAN → 通用规则；source=QGS_V1_LEARNED → 原词 + 通用规则（leakage=True）。"""
    variants = generate_clean_variants(term)
    if source == "QGS_V1_LEARNED":
        variants = [term] + variants   # QGS-learned 词本身是核心，先保留
    # 去重保序
    seen, out = set(), []
    for v in variants:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out
