"""tools/build_term_families.py — v3.0 Terminology Repair 第一层（2026-08-29 用户定稿）。

TermFamily（保守归并） + EvidenceSupport（三正交量，不压 score）+ candidate gate。

输入：data/exports/audit_miss_diagnostics.json（95 篇 miss × term_candidates/historical_terms/
      community_evidence）
输出：data/exports/terminology/
      term_families.json         全部 TermFamily（FULL，审计用）
      term_family_members.json   surface form → family 映射（审计用）
      term_evidence_support.csv  核心排序表（rank/miss_support/miss_support_ratio/citation/
                                 historical/surface/source_candidates/miss_ids）
      repair_term_candidates.json  eligible（miss_support>=2 OR citation_positive）

归并规则（用户冻结，禁止语义扩张、禁止 Porter stemming）：
  Unicode NFC + lowercase
  → hyphen/whitespace 变体集合（hyphen→space / hyphen→none 都生成，保守覆盖
    "polymerization-induced stress"~"polymerization induced stress" 与
    "photo-polymerization"~"photopolymerization"）
  → trivial punctuation（去撇号等）
  → singular/plural（-ies→y；-es 去（保护 -ss/-is/-us）；-s 去（词长>=4 且前字符非 s）；
    结果长度>=3）
  → 拼写变体（-isation→-ization）
  → exact acronym family（surface 含 "ACR (expansion)" / "expansion (ACR)" 模式时，
    acronym 与 expansion 进同一 family——只在有明确 expansion 证据时）

禁止归并（用户例子）：methacrylate / methyl methacrylate / dimethacrylate 不得合并。

防泄漏（写死）：
  development_source = AUDIT_R01
  role = DEVELOPMENT_DATA
  eligible_for_r01_evaluation = false（S1 不得用 R01 证明 recall；必须新独立 Audit R02）

EvidenceSupport 三正交量（不压 score，排序用）：
  MissSupport(t)     = |{m: m 支持 term family t}|（distinct miss 数，非出现次数）
  CitationSupport(t) = positive(0/1) + source_miss_ids（第一版用 community_evidence.
                       known_bridge_node；数据覆盖不足时如实为 0）
  HistoricalSupport  = distinct miss 中 historical_terms 命中该 family 的数量
"""
import argparse
import csv
import json
import os
import re
import unicodedata
from collections import Counter, defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_IN = os.path.join(BASE, "data", "exports", "audit_miss_diagnostics.json")
OUT_DIR = os.path.join(BASE, "data", "exports", "terminology")

SOURCE_AUDIT = "AUDIT_R01"
DEVELOPMENT_ROLE = "DEVELOPMENT_DATA"
MISS_TOTAL = 95          # R01 封账后 miss 总数（MissSupportRatio 分母）


# ── 保守归并 ──────────────────────────────────────────────
def _nfc_lower(s: str) -> str:
    return unicodedata.normalize("NFC", s).strip().lower()


def _strip_punct(s: str) -> str:
    return re.sub(r"[\u2019']", "", s)      # 撇号等 trivial 标点


def _singular(w: str) -> str:
    """保守单数化（保护 -ss/-is/-us；长度>=3）。"""
    if len(w) < 4:
        return w
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith("ss") or w.endswith("is") or w.endswith("us"):
        return w
    if w.endswith("es") and len(w) > 4:
        return w[:-2]
    if w.endswith("s"):
        return w[:-1]
    return w


def _singularize_phrase(s: str) -> str:
    return " ".join(_singular(w) for w in s.split())


def _spelling(s: str) -> str:
    return s.replace("isation", "ization")   # polymerisation -> polymerization


def _acronym_variants(s: str) -> list[str]:
    """检测 'acr (expansion)' / 'expansion (acr)' 模式 → (acr, expansion) 对。"""
    m = re.match(r"^([a-z0-9][a-z0-9-]*)\s*\((.+)\)$", s)
    if not m:
        return []
    acr, exp = m.group(1), m.group(2).strip()
    return [acr, exp]


def family_keys(surface: str) -> set[str]:
    """一个 surface 的候选 family key 集合（保守变体）。同族判定 = key 集合交集非空。"""
    s = _nfc_lower(surface)
    s = _strip_punct(s)
    keys = set()
    # hyphen→space 与 hyphen→none 两个主变体
    s_h2s = re.sub(r"-+", " ", s)
    s_none = re.sub(r"-+", "", s)
    for v in {s, s_h2s, s_none}:
        v = re.sub(r"\s+", " ", v).strip()
        v = _spelling(_singularize_phrase(v))
        if v:
            keys.add(v)
    # acronym expansion 证据：acronym 与 expansion 各自归一后都入 key 集
    for v in _acronym_variants(s):
        vv = _spelling(_singularize_phrase(re.sub(r"-+", " ", v)))
        if vv:
            keys.add(vv)
    return keys


class UnionFind:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


# ── 主流程 ────────────────────────────────────────────────
def load_misses(path: str) -> list[dict]:
    d = json.load(open(path, encoding="utf-8"))
    return d["misses"]


def collect_terms(misses: list[dict]) -> list[dict]:
    """展平：(miss_id, surface, classes, historical, bridge)."""
    rows = []
    for m in misses:
        pid = m["paper_id"]
        exp = m.get("expansion") or {}
        bridge = (exp.get("community_evidence") or {}).get("known_bridge_node", False)
        hts = exp.get("historical_terms") or []
        hist_surfs = {h.get("surface") for h in hts if h.get("surface")}
        hist_canons = {h.get("canonical") for h in hts if h.get("canonical")}
        for tc in exp.get("term_candidates", []):
            surface = tc.get("surface") or ""
            if not surface:
                continue
            classes = tc.get("classes") or [tc.get("primary")] or ["UNKNOWN"]
            hist = (surface in hist_surfs) or (tc.get("canonical") in hist_canons)
            rows.append({"miss_id": pid, "surface": surface,
                         "classes": classes, "historical": hist,
                         "bridge": bridge})
    return rows


def build_families(rows: list[dict]) -> dict:
    """归并 → TermFamily 聚合。返回 {fid: family}。"""
    uf = UnionFind()
    term_keys: dict[str, set[str]] = {}       # surface -> keys
    for r in rows:
        keys = family_keys(r["surface"])
        term_keys[r["surface"]] = keys
        ks = list(keys)
        for i in range(1, len(ks)):
            uf.union(ks[0], ks[i])
    # 跨 surface：任意 key 交集 → 同族
    reps = {}
    for surf, keys in term_keys.items():
        if not keys:
            continue
        root = min(uf.find(k) for k in keys)
        reps.setdefault(root, []).append(surf)

    families: dict[str, dict] = {}
    for fid, (root, surfs) in enumerate(reps.items(), 1):
        fam = {
            "family_id": f"TF_{fid:06d}",
            "canonical_term": None,
            "surface_forms": [],
            "term_types": [],
            "miss_ids": [],
            "miss_support": 0,
            "miss_support_ratio": 0.0,
            "citation_support": {"positive": 0, "source_miss_ids": []},
            "historical_support": {"count": 0, "source_miss_ids": []},
            "source_candidates": 0,
            "development_source": SOURCE_AUDIT,
            "role": DEVELOPMENT_ROLE,
            "eligible_for_r01_evaluation": False,
        }
        families[fid] = fam

    # 逐条填充（每个 surface 只属于一个归并组；组序号 = family_id 序号）
    surf2fid: dict[str, int] = {}
    for idx, (root, surfs) in enumerate(reps.items(), 1):
        for s in surfs:
            surf2fid[s] = idx

    for r in rows:
        fid = surf2fid.get(r["surface"])
        if fid is None:
            continue
        f = families[fid]
        if r["surface"] not in f["surface_forms"]:
            f["surface_forms"].append(r["surface"])
        for c in r["classes"]:
            if c not in f["term_types"]:
                f["term_types"].append(c)
        if r["miss_id"] not in f["miss_ids"]:
            f["miss_ids"].append(r["miss_id"])
        f["source_candidates"] += 1
        if r["historical"]:
            if r["miss_id"] not in f["historical_support"]["source_miss_ids"]:
                f["historical_support"]["source_miss_ids"].append(r["miss_id"])
        if r["bridge"]:
            f["citation_support"]["positive"] = 1
            if r["miss_id"] not in f["citation_support"]["source_miss_ids"]:
                f["citation_support"]["source_miss_ids"].append(r["miss_id"])

    # 收尾：canonical_term = 覆盖 miss 最多的 surface（tie 最短）；排序字段
    for f in families.values():
        f["miss_support"] = len(f["miss_ids"])
        f["miss_support_ratio"] = round(f["miss_support"] / MISS_TOTAL, 4)
        f["historical_support"]["count"] = len(f["historical_support"]["source_miss_ids"])
        f["citation_support"]["source_miss_ids"] = sorted(
            f["citation_support"]["source_miss_ids"])
        f["historical_support"]["source_miss_ids"] = sorted(
            f["historical_support"]["source_miss_ids"])
        f["miss_ids"] = sorted(f["miss_ids"])
        # canonical：miss 覆盖最多的 surface（tie 取最短；再 tie 字母序）
        cnt = Counter()
        for r in rows:
            if r["surface"] in f["surface_forms"]:
                cnt[r["surface"]] += 1
        best = min(f["surface_forms"],
                   key=lambda s: (-cnt[s], len(s), s))
        f["canonical_term"] = best
        f["eligible"] = (f["miss_support"] >= 2 or f["citation_support"]["positive"] == 1)

    # 排序：MissSupport DESC → Citation DESC → Historical DESC → source DESC
    ordered = sorted(families.values(),
                     key=lambda f: (-f["miss_support"],
                                    -f["citation_support"]["positive"],
                                    -f["historical_support"]["count"],
                                    -f["source_candidates"],
                                    f["canonical_term"]))
    return ordered


def write_outputs(families: list[dict], out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    # term_families.json（全部，FULL）
    with open(os.path.join(out_dir, "term_families.json"), "w", encoding="utf-8") as f:
        json.dump({"development_source": SOURCE_AUDIT, "role": DEVELOPMENT_ROLE,
                   "eligible_for_r01_evaluation": False,
                   "note": "TermFamily 保守归并（禁语义扩张/禁 Porter stemming）；"
                           "miss_support=distinct miss 数",
                   "miss_total": MISS_TOTAL, "families": families},
                  f, ensure_ascii=False, indent=1)
    # term_family_members.json（surface → family 映射）
    members = {}
    for fam in families:
        for s in fam["surface_forms"]:
            members[s] = fam["family_id"]
    with open(os.path.join(out_dir, "term_family_members.json"), "w", encoding="utf-8") as f:
        json.dump(members, f, ensure_ascii=False, indent=1)
    # term_evidence_support.csv（核心排序表）
    csv_path = os.path.join(out_dir, "term_evidence_support.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rank", "family_id", "canonical_term", "term_type",
                    "miss_support", "miss_support_ratio", "citation_positive",
                    "citation_support_count", "historical_support_count",
                    "surface_form_count", "source_candidate_count", "eligible",
                    "miss_ids"])
        for i, fam in enumerate(families, 1):
            w.writerow([i, fam["family_id"], fam["canonical_term"],
                        ";".join(fam["term_types"]), fam["miss_support"],
                        fam["miss_support_ratio"],
                        fam["citation_support"]["positive"],
                        len(fam["citation_support"]["source_miss_ids"]),
                        fam["historical_support"]["count"],
                        len(fam["surface_forms"]), fam["source_candidates"],
                        fam["eligible"], ";".join(fam["miss_ids"])])
    # repair_term_candidates.json（eligible 子集）
    elig = [f for f in families if f["eligible"]]
    with open(os.path.join(out_dir, "repair_term_candidates.json"), "w", encoding="utf-8") as f:
        json.dump({"development_source": SOURCE_AUDIT, "role": DEVELOPMENT_ROLE,
                   "eligible_for_r01_evaluation": False,
                   "gate": "miss_support >= 2 OR citation_positive == 1",
                   "note": "historical 只记录不作为进入条件（老词 != 有检索价值，"
                           "Queryability 阶段再体现）",
                   "candidates": elig}, f, ensure_ascii=False, indent=1)
    return csv_path


def main():
    ap = argparse.ArgumentParser(description="TermFamily + EvidenceSupport（v3.0 第一层）")
    ap.add_argument("--input", default=DEFAULT_IN)
    ap.add_argument("--out-dir", default=OUT_DIR)
    ap.add_argument("--top", type=int, default=40, help="控制台只打印前 N（默认 40）")
    args = ap.parse_args()

    misses = load_misses(args.input)
    rows = collect_terms(misses)
    print(f"misses={len(misses)} | term rows={len(rows)}")
    families = build_families(rows)
    csv_path = write_outputs(families, args.out_dir)

    print(f"\nTermFamily 总数（FULL）= {len(families)}")
    elig = [f for f in families if f["eligible"]]
    print(f"REPAIR candidates（miss_support>=2 或 citation_positive）= {len(elig)}")
    print(f"其中 historical 标记 = {sum(1 for f in families if f['historical_support']['count'])}")
    print(f"\n{'rank':>4} {'miss':>4} {'cita':>4} {'hist':>4} {'type':<20} canonical_term")
    for i, f in enumerate(families[:args.top], 1):
        print(f"{i:>4} {f['miss_support']:>4} {f['citation_support']['positive']:>4} "
              f"{f['historical_support']['count']:>4} "
              f"{';'.join(f['term_types'])[:20]:<20} {f['canonical_term'][:46]}")
    print(f"\n[OK] CSV: {csv_path}")
    print(f"[OK] 输出目录: {args.out_dir}")


if __name__ == "__main__":
    main()
