#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
R05 blind adjudication, frozen R03/R04 policy.

Input:
  pc_001__20260831162811.json

Output:
  pc_001__20260831162811_filled.json

Discipline:
- title + abstract only
- never reads S4/S5 seen/join fields
- RELEVANT / UNCERTAIN / IRRELEVANT
- low-shrinkage as incidental advantage alone is NOT sufficient
- sintering / pyrolysis / thermal / biological shrinkage are NOT target shrinkage
"""

import argparse, json, re
from pathlib import Path

AUDIT_ID = "pc_001::20260831162811"
UNIVERSE_ID = "pc_001-2026-08-31T162811"

REL = "Title/abstract substantively measures, models, evaluates, reduces, or treats polymerization/curing shrinkage, shrinkage stress, or cure-induced deformation as a material/process outcome or mechanism."
IRR = "Available title/abstract does not show polymerization/curing shrinkage, shrinkage stress, or cure-induced deformation as a substantive study objective, mechanism, or measured outcome; any mention is incidental/background."
NONCURE = "Reported shrinkage/deformation is thermal, sintering, pyrolysis, drying, biological, swelling/deswelling, or another non-polymerization/non-curing process rather than target polymerization/curing shrinkage."
UNC = "Insufficient title/abstract evidence for reliable blind adjudication; curing/resin dimensional behavior is plausible, but target polymerization/curing shrinkage relevance cannot be established from the available record."

# Hand-adjudicated from the current R05 attachment only (title/abstract),
# never from S4/S5 seen status.
OVERRIDES = {
    # early R05
    "W1523365441": ("IRRELEVANT", IRR),
    "W1544972701": ("RELEVANT", REL),
    "W1561809837": ("IRRELEVANT", NONCURE),
    "W1564121295": ("IRRELEVANT", IRR),
    "W1578655814": ("IRRELEVANT", IRR),
    "W158355579": ("IRRELEVANT", IRR),
    "W1588266700": ("RELEVANT", REL),
    "W1596076176": ("RELEVANT", REL),
    "W1636190075": ("IRRELEVANT", NONCURE),
    "W1644266098": ("IRRELEVANT", NONCURE),
    "W1756411942": ("IRRELEVANT", IRR),
    "W180946843": ("IRRELEVANT", IRR),
    "W1835252136": ("IRRELEVANT", IRR),
    "W1844978943": ("IRRELEVANT", IRR),
    "W1874698966": ("IRRELEVANT", IRR),
    "W1881711038": ("IRRELEVANT", IRR),
    "W1902761323": ("IRRELEVANT", IRR),
    "W1936606446": ("IRRELEVANT", IRR),
    "W1946141443": ("IRRELEVANT", IRR),
    "W1963591839": ("IRRELEVANT", IRR),
    "W1967245131": ("RELEVANT", REL),
    "W1969010793": ("IRRELEVANT", IRR),
    "W1969642079": ("IRRELEVANT", IRR),
    "W1971085790": ("IRRELEVANT", IRR),
    "W1972895488": ("IRRELEVANT", IRR),
    "W1974619687": ("IRRELEVANT", IRR),
    "W1977360914": ("UNCERTAIN", UNC),
    "W1978080673": ("IRRELEVANT", IRR),
    "W1978261346": ("RELEVANT", REL),
    "W1978874507": ("IRRELEVANT", IRR),
    "W1981259231": ("IRRELEVANT", NONCURE),
    "W1982601040": ("RELEVANT", REL),
    "W198395475": ("IRRELEVANT", IRR),
    "W1986116254": ("IRRELEVANT", IRR),
    "W1986188974": ("IRRELEVANT", IRR),
    "W1987190535": ("IRRELEVANT", IRR),
    "W1987220425": ("RELEVANT", REL),
    "W1987497539": ("IRRELEVANT", IRR),
    "W1987579277": ("IRRELEVANT", IRR),
    "W1991753060": ("IRRELEVANT", IRR),
    "W1991756645": ("RELEVANT", REL),
    "W1991830829": ("IRRELEVANT", IRR),
    "W1992091990": ("IRRELEVANT", IRR),
    "W1995492056": ("IRRELEVANT", IRR),
    "W1997114067": ("IRRELEVANT", IRR),
    "W1998157807": ("IRRELEVANT", IRR),
    "W1998672678": ("IRRELEVANT", IRR),
    "W2000125827": ("RELEVANT", REL),
    "W2000187507": ("IRRELEVANT", IRR),
    "W2000384613": ("IRRELEVANT", IRR),
    "W2001389266": ("IRRELEVANT", IRR),
    "W2001638778": ("IRRELEVANT", IRR),
    "W2002470507": ("IRRELEVANT", IRR),
    "W2003492525": ("IRRELEVANT", IRR),
    "W2005136870": ("IRRELEVANT", IRR),
    "W2005406824": ("IRRELEVANT", IRR),
    "W2006567769": ("IRRELEVANT", IRR),
    "W2009093410": ("IRRELEVANT", IRR),
    "W2009540782": ("IRRELEVANT", IRR),
    "W2010064819": ("IRRELEVANT", IRR),
    "W2016474352": ("IRRELEVANT", IRR),
    "W2017102381": ("RELEVANT", REL),
    "W2019001195": ("RELEVANT", REL),
    "W2024240557": ("IRRELEVANT", IRR),
    "W2024556922": ("IRRELEVANT", IRR),
    "W2025596636": ("IRRELEVANT", IRR),
    "W2026812759": ("RELEVANT", REL),
    "W2027180499": ("IRRELEVANT", IRR),
    "W2027393703": ("IRRELEVANT", IRR),
    "W2028270534": ("IRRELEVANT", IRR),
    "W2029674766": ("RELEVANT", REL),
    "W2034019493": ("RELEVANT", REL),
    "W2036977385": ("IRRELEVANT", IRR),
    "W2039946352": ("IRRELEVANT", IRR),
    "W2041818718": ("IRRELEVANT", IRR),
    "W2043589711": ("IRRELEVANT", NONCURE),
    "W2044119475": ("IRRELEVANT", IRR),
    "W2044797387": ("RELEVANT", REL),
    "W2045692753": ("RELEVANT", REL),
    "W2046628225": ("IRRELEVANT", IRR),
    "W2047372376": ("IRRELEVANT", IRR),
    "W2047714662": ("IRRELEVANT", NONCURE),
    "W2048127533": ("IRRELEVANT", IRR),
    "W2049682286": ("IRRELEVANT", IRR),
    "W2049792607": ("IRRELEVANT", IRR),
    "W2050343055": ("IRRELEVANT", IRR),
    "W2052404682": ("IRRELEVANT", IRR),
    "W2053318605": ("IRRELEVANT", IRR),
    "W2053591216": ("IRRELEVANT", IRR),
    "W2054566455": ("IRRELEVANT", IRR),
    "W2054633300": ("IRRELEVANT", IRR),
    "W2057091737": ("IRRELEVANT", IRR),
    "W2057941122": ("IRRELEVANT", IRR),
    "W2060363268": ("IRRELEVANT", IRR),
    "W2060937267": ("IRRELEVANT", IRR),
    "W2061614172": ("IRRELEVANT", IRR),
    "W2062196300": ("UNCERTAIN", UNC),
    "W2062396779": ("IRRELEVANT", IRR),
    "W2063262205": ("IRRELEVANT", IRR),
    "W2064436548": ("IRRELEVANT", IRR),
    "W2064503011": ("IRRELEVANT", IRR),
    "W2066111547": ("IRRELEVANT", IRR),
    "W2067182412": ("IRRELEVANT", IRR),
    "W2068625540": ("IRRELEVANT", NONCURE),
    "W2071451193": ("IRRELEVANT", IRR),
    "W2071490891": ("IRRELEVANT", IRR),
    "W2072523688": ("IRRELEVANT", IRR),
    "W2072684154": ("IRRELEVANT", IRR),
    "W2078353141": ("RELEVANT", REL),
    "W2084409008": ("IRRELEVANT", IRR),
    "W2085495870": ("IRRELEVANT", IRR),
    "W2086004066": ("IRRELEVANT", IRR),
    "W2087915265": ("RELEVANT", REL),
    "W2090365972": ("IRRELEVANT", IRR),
    "W2090411296": ("IRRELEVANT", IRR),
    "W2090631074": ("IRRELEVANT", IRR),
    "W2092666179": ("IRRELEVANT", IRR),
    "W2092934158": ("IRRELEVANT", IRR),
    "W2094488509": ("IRRELEVANT", IRR),
    "W2095919085": ("IRRELEVANT", IRR),
    "W2096291298": ("IRRELEVANT", IRR),
    "W2096745277": ("RELEVANT", REL),
    "W2100835957": ("IRRELEVANT", IRR),
    "W2101314826": ("IRRELEVANT", NONCURE),
    "W2101757470": ("IRRELEVANT", IRR),
    "W2106331293": ("IRRELEVANT", NONCURE),
    "W2109143747": ("IRRELEVANT", IRR),
    "W2111256950": ("IRRELEVANT", NONCURE),
    "W2113072880": ("IRRELEVANT", IRR),
    "W2117818786": ("IRRELEVANT", NONCURE),
    "W2118369999": ("IRRELEVANT", IRR),
    "W2121728728": ("IRRELEVANT", IRR),
    "W2123945853": ("IRRELEVANT", IRR),
    "W2124221727": ("IRRELEVANT", IRR),
    "W2127439515": ("IRRELEVANT", IRR),
    "W2129659230": ("IRRELEVANT", IRR),
    "W2142320237": ("IRRELEVANT", IRR),
    "W2144939736": ("IRRELEVANT", IRR),
    "W2152578949": ("IRRELEVANT", IRR),
    "W2155045014": ("RELEVANT", REL),
    "W2163375261": ("IRRELEVANT", IRR),
    "W2163421751": ("IRRELEVANT", IRR),
    "W2165372530": ("IRRELEVANT", IRR),
    "W2167083027": ("IRRELEVANT", IRR),
    "W2169181964": ("IRRELEVANT", NONCURE),
    "W2171179610": ("IRRELEVANT", IRR),
    "W2171620142": ("IRRELEVANT", IRR),
    "W2172693518": ("IRRELEVANT", IRR),
    "W2176236098": ("IRRELEVANT", IRR),
    "W2182129876": ("IRRELEVANT", IRR),
    "W2211463889": ("RELEVANT", REL),
    "W2213414239": ("IRRELEVANT", IRR),
    "W2227728714": ("IRRELEVANT", IRR),
    "W2237667835": ("IRRELEVANT", IRR),
    "W2254657057": ("IRRELEVANT", IRR),
    "W2258347720": ("IRRELEVANT", IRR),
    "W2272470796": ("IRRELEVANT", IRR),
    "W2298192247": ("RELEVANT", REL),
    "W2303496208": ("RELEVANT", REL),
    "W2318546146": ("IRRELEVANT", NONCURE),
    "W2320566028": ("RELEVANT", REL),
    "W2325462504": ("IRRELEVANT", IRR),
    "W2328172359": ("IRRELEVANT", IRR),
    "W2329723089": ("IRRELEVANT", IRR),
    "W2330307975": ("IRRELEVANT", NONCURE),
    "W2337274022": ("RELEVANT", REL),
    "W2340105520": ("IRRELEVANT", NONCURE),
    "W2348683596": ("IRRELEVANT", IRR),
    "W2353700263": ("IRRELEVANT", IRR),
    "W2356623401": ("RELEVANT", REL),
    "W2363697769": ("RELEVANT", REL),
    "W2364905069": ("IRRELEVANT", IRR),
    "W2365745898": ("RELEVANT", REL),

    # later current R05 records surfaced by file search
    "W2901252583": ("RELEVANT", REL),
    "W2994847716": ("IRRELEVANT", IRR),
    "W3087038390": ("RELEVANT", REL),
    "W3088824593": ("RELEVANT", REL),
    "W3114702194": ("RELEVANT", REL),
    "W3121748514": ("IRRELEVANT", IRR),
    "W4292488431": ("RELEVANT", REL),
    "W7204142321": ("RELEVANT", REL),
}

def norm(s):
    s = s or ""
    s = (s.replace("‐","-").replace("‑","-").replace("–","-").replace("—","-")
           .replace("−","-").replace("×","x"))
    return re.sub(r"\s+", " ", s.lower()).strip()

def hit(text, pats):
    return any(re.search(p, text, flags=re.I) for p in pats)

DIRECT = [
    r"\bpolymeri[sz]ation shrink(?:age|ages|ing)?\b",
    r"\bpolymeri[sz]ation contraction\b",
    r"\bcur(?:e|ing)[ -]shrink(?:age|ages)?\b",
    r"\bchemical shrink(?:age)?\b",
    r"\bshrinkage stress(?:es)?\b",
    r"\bcontraction stress(?:es)?\b",
    r"\bpolymeri[sz]ation[- ]induced shrinkage stress\b",
    r"\bpolymeri[sz]ation stress(?:es)?\b",
    r"\bshrinkage strain(?:s)?\b",
    r"\bcure[- ]induced (?:stress|stresses|deformation|distortion|warpage)\b",
    r"\bcuring (?:deformation|stress|stresses|warpage)\b",
]
CURE_CTX = [
    r"\bphotopoly", r"\bphoto[- ]?cur", r"\buv[- ]?cur", r"\blight[- ]?cur",
    r"\bcuring\b", r"\bcure\b", r"\bpolymeri[sz]", r"\bcrosslink(?:ing)?\b",
    r"\bstereolith", r"\bvat photopoly", r"\bdlp\b",
]
SHRINKLIKE = [
    r"\bshrink(?:age|ages|ing)?\b", r"\bcontraction\b", r"\bwarpage\b",
    r"\bdistortion\b", r"\bdimensional (?:change|changes|deviation|deviations|error|errors|stability)\b",
    r"\bresidual stress(?:es)?\b", r"\bresidual deformation\b",
]
SUBSTANTIVE = [
    r"\bmeasure", r"\bmeasurement", r"\bmonitor", r"\bcharacteri[sz]",
    r"\banaly", r"\bmodel", r"\bpredict", r"\bquantif", r"\bevaluat",
    r"\breduc", r"\bmitigat", r"\bcontrol", r"\binfluence", r"\beffect",
    r"\bdepend", r"\binvestigat", r"\bdetermin", r"\boptimi[sz]",
    r"\bcomparison", r"\bcompare", r"\bcorrelat",
]
NONCURE = [
    r"\bsinter(?:ing|ed)?\b", r"\bpyroly", r"\bcarboni[sz]", r"\bthermal shrink",
    r"\bdebind", r"\bcalcination\b", r"\bdrying shrink", r"\bdry shrink",
    r"\bcrystallization shrink", r"\bcell shrink", r"\bnuclear shrink",
    r"\bmembrane shrink", r"\bdeswell", r"\bswelling\b", r"\bvapor-induced",
]
BACKGROUND_LOW = [
    r"\b(?:low|lower|reduced|minimal|negligible|small) shrinkage\b",
    r"\b(?:low|lower|reduced|minimal) curing shrinkage\b",
    r"\blow-shrinkage\b",
]
EXPANDING = [
    r"\bexpanding monomer", r"\bzero[- ]shrinkage\b", r"\bvolume expansion\b",
    r"\bspiro ortho(?:carbonate|ester)", r"\borthocarbonate\b",
]

def classify(item):
    pid = item.get("paper_id")
    if pid in OVERRIDES:
        return OVERRIDES[pid]

    title = norm(item.get("title"))
    abstract = norm(item.get("abstract"))
    text = (title + " " + abstract).strip()

    direct = hit(text, DIRECT)
    cure = hit(text, CURE_CTX)
    shrink = hit(text, SHRINKLIKE)
    substantive = hit(text, SUBSTANTIVE)
    noncure = hit(text, NONCURE)

    # Explicit non-target process dominates unless target cure shrinkage is independently explicit.
    if noncure and not direct:
        return "IRRELEVANT", NONCURE

    # Direct target phrase in title is strongest evidence.
    if hit(title, DIRECT):
        return "RELEVANT", REL

    # Strong explicit target phrase in abstract, but reject if it is just an incidental sentence
    # and no study/action verb co-occurs.
    if direct and (substantive or hit(title, SHRINKLIKE)):
        return "RELEVANT", REL

    # Expanding/zero-shrinkage chemistry is relevant only if polymerization/curing volume change is
    # explicitly an objective/result rather than a generic property claim.
    if hit(text, EXPANDING) and shrink and (substantive or "shrink" in title):
        return "RELEVANT", REL

    # Cure-related shrinkage/warpage/deformation explicitly studied.
    if cure and shrink and substantive:
        return "RELEVANT", REL

    # SLA/DLP shrinkage as a direct part-accuracy outcome.
    if ("shrink" in title) and hit(title, [r"\bstereolith", r"\bdlp\b", r"\bphotopoly", r"\buv[- ]?cur", r"\bresin\b", r"\bdental\b", r"\bcomposite\b"]):
        return "RELEVANT", REL

    # Warpage/distortion during curing or stereolithography with modelling/control.
    if hit(text, [r"\bwarpage\b", r"\bdistortion\b", r"\bresidual stress(?:es)?\b"]) and cure and substantive:
        return "RELEVANT", REL

    # Generic "low shrinkage" advantage is not enough.
    if hit(text, BACKGROUND_LOW) and not substantive and "shrink" not in title:
        return "IRRELEVANT", IRR

    # No abstract: preserve uncertainty only for genuinely suggestive target-dimensional titles.
    if not abstract:
        if hit(title, [r"\bpolymeri[sz]ation shrink", r"\bcure shrink", r"\bcuring shrink", r"\bshrinkage stress", r"\bcontraction stress"]):
            return "RELEVANT", REL
        if ("shrink" in title and hit(title, CURE_CTX)) or (
            hit(title, [r"\bwarpage\b", r"\bdistortion\b", r"\bresidual stress"]) and
            hit(title, [r"\bresin\b", r"\bepoxy\b", r"\bstereolith", r"\bphotopoly", r"\bcure\b"])
        ):
            return "UNCERTAIN", UNC

    return "IRRELEVANT", IRR

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    src = Path(args.input)
    data = json.loads(src.read_text(encoding="utf-8"))

    if data.get("audit_id") != AUDIT_ID:
        raise SystemExit(f"wrong audit_id: {data.get('audit_id')} != {AUDIT_ID}")
    if data.get("universe_id") != UNIVERSE_ID:
        raise SystemExit(f"wrong universe_id: {data.get('universe_id')} != {UNIVERSE_ID}")

    counts = {"RELEVANT":0, "UNCERTAIN":0, "IRRELEVANT":0}
    sources = {"override":0, "rule":0}
    for x in data["labels"]:
        pid = x.get("paper_id")
        label, reason = classify(x)
        x["label"] = label
        x["reviewer"] = "ChatGPT"
        x["reason"] = reason
        counts[label] += 1
        sources["override" if pid in OVERRIDES else "rule"] += 1

    unresolved = sum(x.get("label") == "UNRESOLVED" for x in data["labels"])
    if unresolved:
        raise SystemExit(f"ERROR: unresolved remain = {unresolved}")

    out = Path(args.out) if args.out else src.with_name("pc_001__20260831162811_filled.json")
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    print("[OK]", out)
    print("[audit]", data["audit_id"])
    print("[universe]", data["universe_id"])
    print("[labels]", counts, "total=", sum(counts.values()))
    print("[adjudication-source]", sources)
    print("[seen/join fields used] FALSE")

if __name__ == "__main__":
    main()
