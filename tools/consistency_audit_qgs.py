"""tools/consistency_audit_qgs.py — B4 假阴性离线 consistency audit（用户 2026-08-28）。

背景：sign-off v2 的 59 项筛选框（标题含危险词）太窄，漏掉"标题不危险但摘要
直接研究 shrinkage"的假阴性。用户手动扫 520 IRRELEVANT 抓到至少 12 篇应判
RELEVANT + 7 篇 REVIEW。本工具对全部 520 IRRELEVANT 做规则扫描，把摘要出现
direct shrinkage measurement / quantified contraction / suppression / causal
experiment / shrinkage simulation 的全拉进人工 sign-off。

规则（abstract+title 联合，大小写不敏感，命中即候选）：
  R1 QUANTIFIED_SHRINKAGE  数字 + (shrinkage|contraction|volume change) 共现
  R2 SUPPRESSION_REPORTED  shrinkage/contraction 与 suppress/reduc/lower/minimiz/
                           elimina/prevent 共现（主动降低收缩）
  R3 CAUSAL_STUDY          shrinkage/contraction 与 influence/effect/impact/role/
                           depend/affect 共现（研究 X 对收缩的影响）
  R4 SIMULATION            shrinkage/contraction 与 simul/model/predict 共现
  R5 MECHANISM             shrinkage/contraction 与 mechanism 共现
  R6 MEASUREMENT           shrinkage/contraction 与 measure/determin/quantif/
                           characteriz/monitor 共现（作为实验表征量）

输出：data/exports/qgs_consistency_audit.json（候选 + 证据句 + 命中规则），
供人工 sign-off 判定。无 abstract 的 98 篇单独列出（标题审）。
⚠️ 规则是 recall 工具不是判据——命中≠RELEVANT（如背景提到 shrinkage 的机械
性能研究也会命中 R2/R3），最终标签由领域专家按 B3 判定。
"""
import argparse
import json
import os
import re
import sys
from collections import Counter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = r"C:/Users/Administrator/Downloads/qgs_screening_manifest_completed.json"
OUT_PATH = os.path.join(BASE, "data", "exports", "qgs_consistency_audit.json")

SHRINK_WORDS = re.compile(
    r"\b(shrink(age|ing)?|contraction|volumetric change|volume change|volume contraction)\b", re.I)
NUM = re.compile(r"\b(\d+(?:\.\d+)?\s*(?:%|vol\.?%|vol%/|μm|um|micron|µm))\b")
SUPPRESS = re.compile(r"\b(suppress\w*|reduc\w*|lower\w*|minimi\w*|eliminat\w*|prevent\w*|inhib\w*|offset\w*|compensat\w*|decreas\w*|diminish\w*|mitigat\w*)\b", re.I)
CAUSAL = re.compile(r"\b(influen\w*|effect\w*|impact\w*|role\w*|depend\w*|affect\w*|relation\w*|correlat\w*)\b", re.I)
SIMUL = re.compile(r"\b(simulat\w*|model\w*|predict\w*|comput\w*)\b", re.I)
MECH = re.compile(r"\bmechanism\w*\b", re.I)
MEASURE = re.compile(r"\b(measur\w*|determin\w*|quantif\w*|characteriz\w*|monitor\w*|evaluat\w*)\b", re.I)


def sentence_hits(text: str) -> list[str]:
    """提取包含 shrinkage/contraction 的句子片段（证据句）。"""
    if not text:
        return []
    out = []
    for m in re.finditer(r"[^.;!?]{15,300}", text):
        seg = m.group(0)
        if SHRINK_WORDS.search(seg):
            out.append(seg.strip())
    return out


def audit(p: dict) -> dict:
    text = f"{p.get('title', '')}. {p.get('abstract') or ''}"
    hits = {}
    if NUM.search(text) and SHRINK_WORDS.search(text):
        hits["R1_QUANTIFIED"] = True
    if SUPPRESS.search(text) and SHRINK_WORDS.search(text):
        hits["R2_SUPPRESSION"] = True
    if CAUSAL.search(text) and SHRINK_WORDS.search(text):
        hits["R3_CAUSAL"] = True
    if SIMUL.search(text) and SHRINK_WORDS.search(text):
        hits["R4_SIMULATION"] = True
    if MECH.search(text) and SHRINK_WORDS.search(text):
        hits["R5_MECHANISM"] = True
    if MEASURE.search(text) and SHRINK_WORDS.search(text):
        hits["R6_MEASUREMENT"] = True
    return {
        "idx": p["idx"], "title": p.get("title", ""), "year": p.get("year"),
        "venue": p.get("venue", ""), "doi": p.get("doi", ""),
        "reason_code": p.get("reason_code", ""),
        "sources_from": p.get("sources_from", []),
        "has_abstract": bool(p.get("abstract")),
        "abstract": (p.get("abstract") or "")[:900],
        "rules_hit": sorted(hits.keys()),
        "evidence_sentences": sentence_hits(text)[:3],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-rules", type=int, default=1, help="最少命中规则数才进候选（默认 1）")
    args = ap.parse_args()

    manifest = json.load(open(MANIFEST, encoding="utf-8"))
    irr = [p for p in manifest["papers"] if p.get("decision") == "IRRELEVANT"]
    print(f"IRRELEVANT 总数: {len(irr)}（有 abstract {sum(1 for p in irr if p.get('abstract'))}，"
          f"无 abstract {sum(1 for p in irr if not p.get('abstract'))}）")

    audited = [audit(p) for p in irr]
    candidates = [a for a in audited if a["rules_hit"]]
    # 命中数排序（多规则命中 = 更强信号）
    candidates.sort(key=lambda a: (-len(a["rules_hit"]), str(a["year"])))
    no_ab = [a for a in audited if not a["has_abstract"]]

    print(f"\n命中规则的 IRRELEVANT（假阴性候选）: {len(candidates)}/{len(irr)}")
    print(f"无 abstract（标题审，需另列）: {len(no_ab)}")
    print("\n规则命中分布:")
    rc = Counter(r for a in candidates for r in a["rules_hit"])
    for r in sorted(rc, key=lambda x: -rc[x]):
        print(f"  {r:<18} {rc[r]:>4}")

    print("\n=== 假阴性候选（按命中规则数排序，前 40）===")
    for a in candidates[:40]:
        print(f"[{a['idx']}] ({a['year']}) {a['title'][:52]} | "
              f"{','.join(r.replace('R1_','').replace('R2_','').replace('R3_','').replace('R4_','').replace('R5_','').replace('R6_','') for r in a['rules_hit'])}")

    out = {
        "audit_id": "pc_001_consistency_audit_v1",
        "created_at": "2026-08-28",
        "scope": "全部 520 IRRELEVANT（B4 假阴性封口）",
        "rules": {
            "R1_QUANTIFIED": "数字+shrinkage/contraction 共现（量化收缩值）",
            "R2_SUPPRESSION": "shrinkage 与 suppress/reduce/lower/minimize 共现",
            "R3_CAUSAL": "shrinkage 与 influence/effect/impact 共现",
            "R4_SIMULATION": "shrinkage 与 simulate/model 共现",
            "R5_MECHANISM": "shrinkage 与 mechanism 共现",
            "R6_MEASUREMENT": "shrinkage 与 measure/determine/quantify 共现",
        },
        "note": "命中≠RELEVANT（背景提到也会命中）；最终标签由领域专家按 B3 判。",
        "summary": {
            "irrelevant_total": len(irr),
            "candidates": len(candidates),
            "no_abstract_title_only": len(no_ab),
            "rule_distribution": dict(rc),
        },
        "candidates": candidates,
        "no_abstract_papers": [{"idx": a["idx"], "title": a["title"], "year": a["year"],
                                "reason_code": a["reason_code"]} for a in no_ab],
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n✓ 已导出: {OUT_PATH}")


if __name__ == "__main__":
    main()
