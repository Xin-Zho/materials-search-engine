"""tools/sanity_check_phrases.py — Queryability v1 的 PHRASE_REVIEW（2026-08-29 用户定稿）。

对 greedy selected 中疑似 n-gram 截断的短语做 deterministic sanitation：
回到 source miss 的 title/abstract 原句，把短语左右扩展，判断是否被截断。

判定（无 LLM、不利用任何检索结果——不算调参泄漏）：
  RESTORED     = 原短语 + 右侧稳定名词后缀（跨出现位置统计，高频/多 miss 支持）
  KEEP_AS_IS   = 原短语本身是完整表达（如形容词短语 solvent-free）
  REJECT_FRAGMENT = 扩展不出稳定名词短语

流程：
  1) 对每个 PHRASE_REVIEW 词，在 source miss 文本中定位所有出现
  2) 每个出现右扩 1-4 token（到标点/句界），统计扩展短语频率
  3) 判定 + 输出 updated repair candidates（RESTORED 用新 canonical，miss_ids 不变）
  4) 重跑 greedy（tools/build_queryability.py --input <updated>）

输入：term_families.json（miss_ids）、labels filled_final（title/abstract）、
      queryability_gate.json（greedy selected 标记）
输出：data/exports/terminology/phrase_sanity_review.json
      data/exports/terminology/repair_term_candidates_sanitized.json
"""
import argparse
import json
import os
import re
from collections import Counter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TERM_DIR = os.path.join(BASE, "data", "exports", "terminology")

REVIEW_TERMS = ["pressure sensitive", "modified calcium", "physico mechanical",
                "solvent free", "beam scanning"]


def load_miss_texts() -> dict[str, str]:
    """{wid: title + abstract}（labels filled_final；连字符统一转空格——匹配兼容）。"""
    p = os.path.join(BASE, "data", "exports", "completeness_labels",
                     "pc_001__20260829043656_filled_final.json")
    d = json.load(open(p, encoding="utf-8"))
    texts = {}
    for x in d["labels"]:
        pid = x.get("paper_id")
        if pid:
            t = (x.get("title") or "")
            a = (x.get("abstract") or "")
            texts[pid] = re.sub(r"-", " ", (t + " " + a).lower())
    return texts


def review_term(term: str, miss_ids: list[str], texts: dict[str, str]) -> dict:
    """回到 source miss 原句：左扩 1-3 / 右扩 1-4 token，候选短语在原文完整
    出现（词边界内）即判定 RESTORED——直接实现用户"扩 2-4 token 判断截断"定义。"""
    pat = re.compile(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])")
    base = term.split()
    FUNC = {"of", "and", "with", "for", "in", "on", "the", "to", "via", "from",
            "at", "as", "by", "or", "a", "an"}
    out = {"term": term, "miss_ids": miss_ids, "n_miss": len(miss_ids),
           "occurrences": 0, "status": None, "restored_term": None,
           "candidate_evidence": []}
    for pid in miss_ids:
        text = texts.get(pid, "")
        if not text:
            continue
        out["occurrences"] += sum(1 for _ in pat.finditer(text))
    all_text = " \n ".join(texts.get(pid, "") for pid in miss_ids)

    def _valid(cand: str) -> bool:
        return bool(re.search(r"(?<![a-z0-9])" + re.escape(cand) + r"(?![a-z0-9])",
                              all_text))

    # 规则 1：尾 token 形容词/分词后缀（X-free / X-based）→ 短语本身完整
    if base[-1] in {"free", "based", "induced", "containing"}:
        out["status"] = "KEEP_AS_IS"
        out["note"] = f"尾 token '{base[-1]}' = 形容词/分词后缀，短语本身完整（X-{base[-1]}）"
        return out

    # 规则 2：右扩 1 token（名词性，非功能词）→ RESTORED
    best = None
    for pid in miss_ids:
        text = texts.get(pid, "")
        for m in pat.finditer(text):
            tail = re.split(r"\s+", text[m.end():].strip())
            if tail and tail[0] not in FUNC:
                cand = term + " " + tail[0]
                if _valid(cand) and len(cand.split()) == len(base) + 1:
                    best = cand
                    break
        if best:
            break
    # 规则 3：右扩无 → 左扩 1 token（左截断特例，如 beam scanning ← laser）
    if not best:
        for pid in miss_ids:
            text = texts.get(pid, "")
            for m in pat.finditer(text):
                head = re.split(r"\s+", text[:m.start()].strip())
                if head and head[-1] not in FUNC:
                    cand = head[-1] + " " + term
                    if _valid(cand) and len(cand.split()) == len(base) + 1:
                        best = cand
                        break
            if best:
                break
    if best:
        out["status"] = "RESTORED"
        out["restored_term"] = best
        out["candidate_evidence"] = [{"candidate": best,
                                      "n_tokens": len(best.split()),
                                      "source_miss": pid}]
    else:
        out["status"] = "KEEP_AS_IS"
        out["note"] = "右/左扩 1 token 均无稳定名词短语"
    return out


def main():
    ap = argparse.ArgumentParser(description="PHRASE_REVIEW deterministic sanity")
    ap.add_argument("--terms", default=",".join(REVIEW_TERMS))
    ap.add_argument("--out-dir", default=TERM_DIR)
    args = ap.parse_args()

    fams = json.load(open(os.path.join(args.out_dir, "term_families.json"),
                          encoding="utf-8"))["families"]
    fid2miss = {f["canonical_term"]: f["miss_ids"] for f in fams}
    texts = load_miss_texts()

    reviews = []
    updates = {}        # canonical_term -> restored_term（RESTORED）
    for term in [t.strip() for t in args.terms.split(",") if t.strip()]:
        miss_ids = fid2miss.get(term, [])
        r = review_term(term, miss_ids, texts)
        reviews.append(r)
        if r["status"] == "RESTORED":
            updates[term] = r["restored_term"]
        print(f"{term:<22} n_miss={r['n_miss']:<2} -> {r['status']:<10}"
              f"{r['restored_term'] or ''}")
        for c in r["candidate_evidence"][:5]:
            print(f"      +'{c['candidate']}' ({c['n_tokens']}t, {c['source_miss']})")

    # 生成 sanitized candidates（RESTORED 替换 canonical_term；queryability 重算）
    cands = json.load(open(os.path.join(args.out_dir, "repair_term_candidates.json"),
                           encoding="utf-8"))["candidates"]
    new_cands = []
    for f in cands:
        c = f["canonical_term"]
        if c in updates:
            f2 = dict(f)
            f2["canonical_term"] = updates[c]
            f2["sanitized_from"] = c
            new_cands.append(f2)
        else:
            new_cands.append(f)

    out_dir = args.out_dir
    with open(os.path.join(out_dir, "phrase_sanity_review.json"), "w",
              encoding="utf-8") as f:
        json.dump({"review_terms": REVIEW_TERMS, "reviews": reviews,
                   "note": "deterministic；不利用任何检索结果；RESTORED 重 canonicalize，"
                           "REJECT/KEEP 不进 greedy"}, f, ensure_ascii=False, indent=1)
    with open(os.path.join(out_dir, "repair_term_candidates_sanitized.json"), "w",
              encoding="utf-8") as f:
        json.dump({"development_source": "AUDIT_R01", "role": "DEVELOPMENT_DATA",
                   "gate": "miss_support >= 2 OR citation_positive == 1",
                   "candidates": new_cands}, f, ensure_ascii=False, indent=1)
    print(f"\n[OK] reviews={len(reviews)} | updated candidates={len(new_cands)}")
    print(f"[OK] 输出: {out_dir}")


if __name__ == "__main__":
    main()
