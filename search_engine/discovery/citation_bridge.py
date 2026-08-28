"""search_engine/discovery/citation_bridge.py — v2.1 Citation Bridge v1（用户 2026-08-28 定稿）。

MVP 第一轮 = 纯离线 backward Citation Bridge：
  INPUT   40 RELEVANT seeds（staging，W id）
  DATA    existing OpenAlex cache only（不联网、不补数据）
  STEP    seed W-id → referenced_works → 1-hop backward neighborhood
          → canonicalize by W-id → 数每个 neighbor 被多少 seed 引用
  OUTPUT  bridge candidates + provenance

第一版只做「发现 + 计数 + provenance」——不做 community clustering、不生成 query。
先看 bridge 候选长什么样，再决定是否接 term_community.py。

桥强度（用户定）：
  Bridge(p) = #{ relevant seeds citing p }

分类（用户定，不丢弃已检索论文——已检索且被多 seed 共同引用的节点本身可能是强桥）：
  NEW_NEIGHBOR      不在系统已知论文集
  ALREADY_RETRIEVED 已在 Candidate DB / 系统已知（但非 relevant seed）
  ALREADY_RELEVANT  本身是 relevant seed

用法：
  python -m search_engine.discovery.citation_bridge        # 打印报告
  python -m search_engine.discovery.citation_bridge --json  # 输出 data/exports/citation_bridge_v1.json
"""
import argparse
import json
import os
import re
import sys
from collections import Counter

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE)

CACHE_PATH = os.path.join(BASE, "data", "cache", "openalex_cache.json")
STAGING_PATH = os.path.join(BASE, "data", "exports", "discovery_staging.json")
PROVENANCE_PATH = os.path.join(BASE, "data", "exports", "discovery_paper_provenance.json")
PHASE2_PATH = os.path.join(BASE, "data", "exports", "phase2_candidates.json")
DEPTH_RUN_PATH = os.path.join(BASE, "data", "exports", "query_family_runs_depth.json")
KB_DB = os.path.join(BASE, "data", "cache", "knowledge_base.db")
OUT_PATH = os.path.join(BASE, "data", "exports", "citation_bridge_v1.json")


def wid_of(pid: str) -> str:
    m = re.search(r"(W\d+)", pid or "")
    return m.group(1) if m else ""


def norm_doi(d: str) -> str:
    return (d or "").strip().lower().replace("https://doi.org/", "").replace("http://doi.org/", "")


class CitationBridge:
    def __init__(self):
        self.works: dict[str, dict] = {}      # wid -> {doi, title, year, refs}
        self.seeds: list[str] = []            # relevant seed wids
        self.known_wids: set[str] = set()     # 系统已知论文集（candidate/接触过）

    # ── 数据加载（全部只读现有缓存）──
    def load_openalex(self) -> None:
        cache = json.load(open(CACHE_PATH, encoding="utf-8"))
        for entry in cache.values():
            for w in (entry.get("results") or []):
                wid = (w.get("id") or "").rsplit("/", 1)[-1]
                if not wid:
                    continue
                rec = self.works.setdefault(wid, {"doi": "", "title": "", "year": None,
                                                  "refs": []})
                rec["title"] = rec["title"] or (w.get("display_name") or "")[:120]
                if rec["year"] is None:
                    rec["year"] = w.get("publication_year")
                doi = norm_doi(w.get("doi", ""))
                if doi and not rec["doi"]:
                    rec["doi"] = doi
                refs = w.get("referenced_works") or []
                if refs:
                    rec["refs"] = sorted({str(x).rsplit("/", 1)[-1] for x in refs})
        print(f"OpenAlex 缓存 works = {len(self.works)}")

    def load_seeds(self) -> None:
        staging = json.load(open(STAGING_PATH, encoding="utf-8"))
        items = staging if isinstance(staging, list) else staging.get("papers", [])
        self.seeds = [wid_of(p.get("paper_id", "")) for p in items
                      if p.get("relevance_status") == "RELEVANT" and wid_of(p.get("paper_id", ""))]
        print(f"RELEVANT seeds = {len(self.seeds)}")

    def load_known_wids(self) -> None:
        """系统已知论文集（ALREADY_RETRIEVED 判定）——多来源并集。"""
        known: set[str] = set()
        # staging + provenance
        for path in (STAGING_PATH, PROVENANCE_PATH):
            if not os.path.exists(path):
                continue
            d = json.load(open(path, encoding="utf-8"))
            items = d if isinstance(d, list) else d.get("papers", [])
            for it in items:
                w = wid_of(it.get("paper_id", ""))
                if w:
                    known.add(w)
        # phase2 candidates source_papers
        if os.path.exists(PHASE2_PATH):
            for c in json.load(open(PHASE2_PATH, encoding="utf-8")).get("candidates", []):
                for sp in (c.get("source_papers") or []):
                    w = wid_of(sp)
                    if w:
                        known.add(w)
        # KB
        if os.path.exists(KB_DB):
            import sqlite3
            con = sqlite3.connect(f"file:{KB_DB}?mode=ro", uri=True)
            for (pid,) in con.execute("SELECT paper_id FROM knowledge_records"):
                w = wid_of(pid)
                if w:
                    known.add(w)
            con.close()
        # depth run candidates：EID → DOI → OpenAlex wid 桥接
        doi2wid = {rec["doi"]: wid for wid, rec in self.works.items() if rec["doi"]}
        if os.path.exists(DEPTH_RUN_PATH):
            records = json.load(open(DEPTH_RUN_PATH, encoding="utf-8"))["records"]
            for recs in records.values():
                for r in recs:
                    doi = norm_doi(r.get("doi", ""))
                    if doi and doi in doi2wid:
                        known.add(doi2wid[doi])
        self.known_wids = known
        print(f"系统已知 W id（candidate/接触过）= {len(known)}")

    # ── 核心：backward 1-hop + bridge 计数 ──
    def build(self) -> dict:
        seed_with_refs = 0
        neighbor: dict[str, dict] = {}   # neighbor_wid -> {count, citing_seeds}
        for s in self.seeds:
            rec = self.works.get(s)
            if not rec or not rec["refs"]:
                continue
            seed_with_refs += 1
            for n in rec["refs"]:
                nb = neighbor.setdefault(n, {"count": 0, "citing_seeds": []})
                nb["count"] += 1
                nb["citing_seeds"].append(s)
        # 分类 + 元数据
        for n, nb in neighbor.items():
            w = self.works.get(n, {})
            nb["title"] = (w.get("title") or "")[:120]
            nb["year"] = w.get("year")
            nb["doi"] = w.get("doi") or ""
            if n in set(self.seeds):
                nb["class"] = "ALREADY_RELEVANT"
            elif n in self.known_wids:
                nb["class"] = "ALREADY_RETRIEVED"
            else:
                nb["class"] = "NEW_NEIGHBOR"
        return {"seed_with_refs": seed_with_refs, "neighbors": neighbor}

    # ── 报告 ──
    def report(self, result: dict, top_n: int = 30) -> None:
        nb = result["neighbors"]
        dist = Counter(v["count"] for v in nb.values())
        cls = Counter(v["class"] for v in nb.values())
        print("\n" + "=" * 70)
        print("Citation Bridge v1（offline backward, OpenAlex cache only）")
        print("=" * 70)
        print(f"seeds = {len(self.seeds)}（with references = {result['seed_with_refs']}）")
        print(f"\n四个关键数字：")
        print(f"  unique referenced neighbors  = {len(nb)}")
        print(f"  bridge_count >= 2            = {sum(1 for v in nb.values() if v['count'] >= 2)}")
        print(f"  bridge_count >= 3            = {sum(1 for v in nb.values() if v['count'] >= 3)}")
        print(f"  NEW_NEIGHBOR（不在系统已知） = {cls.get('NEW_NEIGHBOR', 0)}"
              f"（ALREADY_RETRIEVED={cls.get('ALREADY_RETRIEVED', 0)}, "
              f"ALREADY_RELEVANT={cls.get('ALREADY_RELEVANT', 0)}）")
        print(f"\nbridge_count 分布: {dict(sorted(dist.items()))}")
        print(f"\nTop {top_n} bridge candidates（bridge_count desc）:")
        print(f"  {'count':>5} {'class':<18} {'year':>5} {'wid':<14} {'title':<52}")
        for n, v in sorted(nb.items(), key=lambda x: -x[1]["count"])[:top_n]:
            print(f"  {v['count']:>5} {v['class']:<18} {str(v['year']):>5} "
                  f"{n:<14} {v['title'][:52]}")

    def save(self, result: dict) -> None:
        out = {
            "version": "v1_offline_backward",
            "created_at": "2026-08-28",
            "data_source": "openalex_cache.json only（no network）",
            "seeds": {"n": len(self.seeds), "with_refs": result["seed_with_refs"]},
            "summary": {
                "unique_referenced_neighbors": len(result["neighbors"]),
                "bridge_ge2": sum(1 for v in result["neighbors"].values() if v["count"] >= 2),
                "bridge_ge3": sum(1 for v in result["neighbors"].values() if v["count"] >= 3),
                "class": dict(Counter(v["class"] for v in result["neighbors"].values())),
                "bridge_distribution": dict(Counter(v["count"] for v in result["neighbors"].values())),
            },
            "candidates": [
                {"wid": n, "title": v["title"], "year": v["year"], "doi": v["doi"],
                 "bridge_count": v["count"], "citing_seed_ids": v["citing_seeds"],
                 "class": v["class"]}
                for n, v in result["neighbors"].items()],
        }
        with open(OUT_PATH, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print(f"\n✓ 已写: {OUT_PATH}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-json", action="store_true", help="只打印报告，不写 JSON")
    ap.add_argument("--top", type=int, default=30)
    args = ap.parse_args()

    bridge = CitationBridge()
    bridge.load_openalex()
    bridge.load_seeds()
    bridge.load_known_wids()
    result = bridge.build()
    bridge.report(result, top_n=args.top)
    if not args.no_json:
        bridge.save(result)


if __name__ == "__main__":
    main()
