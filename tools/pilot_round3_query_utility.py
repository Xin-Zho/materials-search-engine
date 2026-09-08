"""tools/pilot_round3_query_utility.py — v2.1.4 Level 3 Query-level Retrieval Utility Pilot。

问题（用户 2026-08-28/29 定稿）：anchored_hits>0 只证明"这种语言存在"，不证明
"这种语言值得成为 query"。

对每个 ANCHOR_QUERYABLE 候选做小深度检索（默认 depth=100），测量实际搜索边际收益：

  R_q(q)                 = anchored query 检索结果集合（Scopus, depth=pilot_depth）
  R_old                  = 旧 Candidate DB 论文集合（depth run ∩ cache 有文本）
  QueryMCG(q)            = |R_q ∖ R_old|（observed，depth 截断）
  QueryRetrievalNovelty  = |R_q ∖ R_old| / |R_q|
  pilot_depth_censored   = len(R_q) == depth

## canonical identity（2026-08-29 用户定稿 + evaluator bug 修复）

- 论文 identity：EID 优先（scopus_url：eid=2-s2.0-XXX / 2-s2.0-XXX /
  pages/publications/XXX 三种格式统一为 2-s2.0-<digits>），DOI fallback
- **EID↔DOI bridge（union-find）**：同 EID → same paper；同 normalized DOI →
  same paper；一条记录同时有 EID+DOI → 建立 bridge，解析为同一 canonical paper
- canonical key = 组件代表，优先 EID
- **identity 为空（无 EID 且无 DOI）→ unknown，不计入 MCG**
  （修复：旧版 identity="" 论文混入 new_papers——intensity 39 里 30 个是空 identity，
   导致 condMCG 虚高而 UnionMCG 被污染，Σcond=266 ≠ UnionMCG=86 的根因）

## selection 规则（QGS-blind，冻结）

  1. QueryMCG >= --min-mcg（默认 2）
  2. per-community cap = --max-per-community（默认 3）
  3. greedy coverage merge：每轮选 ConditionalMCG 最大的 query
     ConditionalMCG(q|S) = |R_q ∖ (R_old ∪ R_selected)|
     停止：best ConditionalMCG < min_mcg
  4. **硬断言：Σ ConditionalMCG == UnionMCG（数学不变量，非经验指标）**
     不满足 → 报错并拒绝 freeze（写 audit 报告，不写 frozen JSON）

## 运行模式

  普通模式：走 ScopusSearchEngine（默认 skip_cache=False 命中 api_cache 缓存，
  --skip-cache 强制真实检索）
  离线模式：--offline-cache 直接从 scopus_cache.api_cache 读 pilot 检索缓存
  （不启动浏览器；pilot 结果已缓存，EID 修复后离线重算即可）

用法：
  python tools/pilot_round3_query_utility.py --json <round3结果> --depth 100 \
      --min-mcg 2 --max-per-community 3 \
      --freeze-out "%TEMP%\\round3_queries_frozen.json"
  python tools/pilot_round3_query_utility.py --json ... --offline-cache   # 不启动浏览器
  python tools/pilot_round3_query_utility.py --json ... --plan-only
"""
import argparse
import asyncio
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter, defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

DEPTH_RUN_PATH = os.path.join(BASE, "data", "exports", "query_family_runs_depth.json")
SCOPUS_CACHE = os.path.join(BASE, "data", "cache", "scopus_cache.db")
ANCHOR = "polymerization shrinkage"

DEFAULT_OUT = os.path.join(tempfile.gettempdir(), "round3_query_utility_pilot.json")
DEFAULT_FREEZE = os.path.join(tempfile.gettempdir(), "round3_queries_frozen.json")

_EID_RE = re.compile(r"eid=2-s2\.0-(\d+)")
_EID_PATH_RE = re.compile(r"2-s2\.0-(\d+)")
_PUBLICATIONS_RE = re.compile(r"pages/publications/(\d+)")


def extract_eid(scopus_url: str | None) -> str | None:
    """从 Scopus URL 提取 EID，统一为 2-s2.0-<digits>。

    支持三种格式（2026-08-29 修复：CSV 导出 "链接" 列是新版
    pages/publications/<pub_id> 格式，pub_id 即 EID 的数字部分）：
      https://www.scopus.com/record/display.uri?eid=2-s2.0-85012345678
      https://www.scopus.com/pages/publications/84998538630?origin=resultslist
    """
    if not scopus_url:
        return None
    m = (_EID_RE.search(scopus_url) or _EID_PATH_RE.search(scopus_url)
         or _PUBLICATIONS_RE.search(scopus_url))
    return f"2-s2.0-{m.group(1)}" if m else None


def normalize_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    d = doi.strip().lower()
    return d or None


class IdentityResolver:
    """EID↔DOI bridge（union-find）。

    同 EID → same paper；同 normalized DOI → same paper；
    一条记录同时有 EID+DOI → 建立 bridge。
    canonical key = 组件代表，优先选 EID（2-s2.0-...）。
    """

    def __init__(self):
        self.parent: dict[str, str] = {}
        self.kind: dict[str, str] = {}      # key -> 'eid' | 'doi'

    def _register(self, key: str, kind: str):
        if key not in self.parent:
            self.parent[key] = key
            self.kind[key] = kind

    def _find(self, x: str) -> str:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def add(self, eid: str | None, doi: str | None):
        keys = []
        if eid:
            self._register(eid, "eid")
            keys.append(eid)
        if doi:
            self._register(doi, "doi")
            keys.append(doi)
        for a, b in zip(keys, keys[1:]):
            ra, rb = self._find(a), self._find(b)
            if ra != rb:
                self.parent[ra] = rb

    def canonical(self, eid: str | None, doi: str | None) -> str | None:
        keys = [k for k in (eid, doi) if k]
        if not keys:
            return None
        root = self._find(keys[0])
        members = [k for k in self.parent if self._find(k) == root]
        eids = [m for m in members if self.kind[m] == "eid"]
        return eids[0] if eids else members[0]

    def component_size(self, key: str) -> int:
        root = self._find(key)
        return sum(1 for k in self.parent if self._find(k) == root)

    def component_keys(self, key: str) -> list[str]:
        root = self._find(key)
        return sorted(k for k in self.parent if self._find(k) == root)


def connect_cache_ro(path: str) -> sqlite3.Connection:
    """复制 cache 到系统 Temp 后以 immutable 只读连接。"""
    tmp = os.path.join(tempfile.gettempdir(), "scopus_cache_ro_pilot.db")
    if not os.path.exists(tmp) or os.path.getmtime(tmp) < os.path.getmtime(path):
        shutil.copy2(path, tmp)
    return sqlite3.connect(f"file:{tmp}?mode=ro&immutable=1", uri=True)


def build_r_old() -> tuple[IdentityResolver, set[str]]:
    """旧 Candidate DB：resolver（含 bridge）+ r_old canonical keys。

    与 load_background 同一 join 语义：depth run records（有 eid）∩ scopus_cache
    （有 title+abstract 文本）→ 该论文属于旧库。不含 hop2 graph neighbors。
    """
    records = json.load(open(DEPTH_RUN_PATH, encoding="utf-8"))["records"]
    con = connect_cache_ro(SCOPUS_CACHE)
    doi_text: dict[str, str] = {}
    for pid, j in con.execute("SELECT paper_id, normalized_json FROM papers"):
        nj = json.loads(j)
        doi = normalize_doi(nj.get("doi"))
        t = (nj.get("title") or "") + " " + (nj.get("abstract") or "")
        if doi and t.strip():
            doi_text[doi] = t
    con.close()
    resolver = IdentityResolver()
    r_old_keys: set[str] = set()
    for recs in records.values():
        for r in recs:
            if not r.get("eid"):
                continue
            doi = normalize_doi(r.get("doi"))
            if doi and doi in doi_text:
                eid = r["eid"].strip()
                resolver.add(eid, doi)
                key = resolver.canonical(eid, doi)
                if key:
                    r_old_keys.add(key)
    return resolver, r_old_keys


def load_round3(path: str) -> list[dict]:
    d = json.load(open(path, encoding="utf-8"))
    reps = d["representative_terms"]
    return [r for r in reps
            if r.get("queryability", {}).get("status") == "ANCHOR_QUERYABLE"]


def paper_key_and_info(p, resolver: IdentityResolver,
                       r_old_keys: set[str]) -> tuple[str | None, dict]:
    """论文 → (canonical key, info)。key=None 表示无 identity（unknown）。"""
    eid = extract_eid(getattr(p, "scopus_url", None))
    doi = normalize_doi(getattr(p, "doi", None))
    resolver.add(eid, doi)
    key = resolver.canonical(eid, doi)
    info = {"eid": eid, "doi": doi,
            "identity_source": "eid" if eid else ("doi" if doi else "none")}
    info["in_old"] = bool(key and key in r_old_keys)
    return key, info


def diversity_capped(terms: list[dict], max_per_community: int) -> list[dict]:
    """每个 source community 最多保留 MCG top-N 条（diversity constraint）。"""
    by_comm: dict[str, list[dict]] = {}
    for r in terms:
        by_comm.setdefault(r["source_community"], []).append(r)
    picked: list[dict] = []
    for cid, items in sorted(by_comm.items()):
        items.sort(key=lambda r: (-r["query_utility"]["QueryMCG"],
                                  -r["query_utility"]["QueryRetrievalNovelty"]))
        picked.extend(items[:max_per_community])
    picked.sort(key=lambda r: -r["query_utility"]["QueryMCG"])
    return picked


def greedy_coverage_merge(pool: list[dict], min_mcg: int) -> tuple[list[dict], int, list[dict]]:
    """greedy coverage merge（canonical key 集合运算）。

    covered 初始为空（new 论文均已排除 R_old）。
    ConditionalMCG(q|S) = |R_q_keys ∖ covered|，每轮选最大，更新 covered。
    停止：best < min_mcg。
    返回 (selected, union_mcg, cond_log)；恒有 Σ cond == union（硬不变量）。
    """
    covered: set[str] = set()
    remaining = list(pool)
    selected: list[dict] = []
    cond_log: list[dict] = []
    while remaining:
        best, best_cond = None, 0
        for r in remaining:
            keys = r["query_utility"]["canonical_keys"]
            cond = len(keys - covered)
            if cond > best_cond:
                best, best_cond = r, cond
        if best is None or best_cond < min_mcg:
            break
        selected.append(best)
        remaining.remove(best)
        covered |= best["query_utility"]["canonical_keys"]
        cond_log.append({"term": best["preferred_surface"],
                         "conditional_mcg": best_cond,
                         "observed_mcg": best["query_utility"]["QueryMCG"]})
    return selected, len(covered), cond_log


def build_audit(candidates: list[dict], resolver: IdentityResolver,
                r_old_keys: set[str]) -> dict:
    """Identity audit（用户 2026-08-29 要求）。"""
    eids: set[str] = set()
    dois: set[str] = set()
    keys: set[str] = set()
    breakdown = Counter()
    key_queries: dict[str, set[str]] = defaultdict(set)
    raw_rows = 0
    for r in candidates:
        for p in r["query_utility"].get("raw_papers", []):
            raw_rows += 1
            eid, doi = p["eid"], p["doi"]
            if eid:
                eids.add(eid)
            if doi:
                dois.add(doi)
            key = p["canonical_key"]
            if key:
                keys.add(key)
                key_queries[key].add(r["preferred_surface"])
            if eid and doi:
                breakdown["both"] += 1
            elif eid:
                breakdown["eid_only"] += 1
            elif doi:
                breakdown["doi_only"] += 1
            else:
                breakdown["neither"] += 1
    cross_query = {k for k, qs in key_queries.items() if len(qs) >= 2}
    # bridge 统计：组件含 ≥2 个不同 kind 的 key（EID↔DOI 桥）
    bridged = set()
    for key in keys:
        comp = resolver.component_keys(key)
        kinds = {resolver.kind[k] for k in comp}
        if len(kinds) >= 2:
            bridged.add(key)
    return {
        "raw_result_rows": raw_rows,
        "unique_eid": len(eids),
        "unique_doi": len(dois),
        "canonical_unique_new_papers": len(keys),
        "identity_breakdown": dict(breakdown),
        "cross_query_duplicate_papers": len(cross_query),
        "bridged_components": len(bridged),
        "bridged_note": "canonical key 的组件同时含 EID 与 DOI 节点（EID<->DOI bridge 生效）",
    }


def _jsonable(d):
    """递归把 set 转成 list（frozen/summary JSON 序列化用）。"""
    if isinstance(d, dict):
        return {k: _jsonable(v) for k, v in d.items()}
    if isinstance(d, set):
        return sorted(d)
    if isinstance(d, list):
        return [_jsonable(x) for x in d]
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", required=True,
                    help="round3 结果 JSON（须已跑 --check-anchored，含 ANCHOR_QUERYABLE）")
    ap.add_argument("--top", type=int, default=None,
                    help="只 pilot score 前 N 个 ANCHOR_QUERYABLE（默认全部）")
    ap.add_argument("--depth", type=int, default=100,
                    help="pilot 检索深度（默认 100；正式 Round3 才 depth=500）")
    ap.add_argument("--min-mcg", type=int, default=2,
                    help="selection 门槛：QueryMCG >= N 才进候选池（默认 2，冻结规则）")
    ap.add_argument("--max-per-community", type=int, default=3,
                    help="diversity cap：每个 source community 最多贡献 N 条 query（默认 3）")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--freeze-out", default=DEFAULT_FREEZE,
                    help="冻结 Round3 queries JSON 路径（QGS-BLIND）")
    ap.add_argument("--offline-cache", action="store_true",
                    help="不启动浏览器，直接从 scopus_cache.api_cache 读 pilot 检索缓存"
                         "（pilot 已跑过且 EID 修复后离线重算）")
    ap.add_argument("--skip-cache", action="store_true",
                    help="普通模式下强制真实 Scopus 检索（默认命中 api_cache 缓存）")
    ap.add_argument("--engine-data-dir", default="data",
                    help="ScopusSearchEngine data_dir（默认 data）")
    ap.add_argument("--plan-only", action="store_true",
                    help="只读：打印将检索的候选清单与 R_old 规模，不发请求")
    args = ap.parse_args()

    candidates = load_round3(args.json)
    if args.top:
        candidates = candidates[:args.top]
    print(f"ANCHOR_QUERYABLE 候选 = {len(candidates)}"
          f"{'（top ' + str(args.top) + '）' if args.top else ''}")

    resolver, r_old_keys = build_r_old()
    print(f"R_old（旧 Candidate DB，canonical keys, EID 优先）= {len(r_old_keys)}")
    print(f"anchor = '{ANCHOR}' | pilot depth = {args.depth} | "
          f"min_mcg = {args.min_mcg} | max/community = {args.max_per_community}"
          f"{' | offline-cache' if args.offline_cache else ''}")

    if args.plan_only:
        print("\n[plan-only] 将执行以下 anchored 检索（不发送）：")
        for i, r in enumerate(candidates, 1):
            print(f"  [{i:>2}] {r['preferred_surface'][:55]}"
                  f"（src={r['source_community']}）")
        print(f"\n[plan-only] 结束：共 {len(candidates)} 条 × depth={args.depth}。")
        return

    # ── 取结果：离线缓存 或 Scopus 引擎 ──
    def run_offline() -> None:
        from search_engine.cache import SearchCache
        con = connect_cache_ro(SCOPUS_CACHE)
        rows = {q: j for q, j in con.execute("SELECT query_string, result_json FROM api_cache")}
        con.close()
        n_miss = 0
        for i, r in enumerate(candidates, 1):
            term = r["preferred_surface"]
            q = f'TITLE-ABS-KEY("{ANCHOR}" AND "{term}")'
            cached = rows.get(q)
            if cached is None:
                n_miss += 1
                print(f"  [{i:>2}] MISSING-CACHE {term[:40]}"
                      f"（pilot 缓存缺失，需普通模式跑）")
                r["query_utility"] = {"error": "NO_CACHE", "query_hits": -1,
                                      "QueryMCG": -1, "QueryRetrievalNovelty": None,
                                      "pilot_depth_censored": False}
                continue
            res = SearchCache._deserialize_result(cached)
            _measure(r, res.papers, args, resolver, r_old_keys, i, len(candidates))
        if n_miss:
            print(f"\n[WARN] {n_miss} 条缓存缺失——请用普通模式（含浏览器）跑一次补全。")

    async def run_engine() -> None:
        from search_engine.engine import ScopusSearchEngine
        engine = ScopusSearchEngine(data_dir=args.engine_data_dir)
        await engine.start()
        try:
            for i, r in enumerate(candidates, 1):
                term = r["preferred_surface"]
                q = f'TITLE-ABS-KEY("{ANCHOR}" AND "{term}")'
                try:
                    res = await engine.search(q, limit=args.depth,
                                              skip_cache=args.skip_cache)
                except Exception as e:
                    print(f"    [WARN] {term[:30]}: pilot 检索失败 {e}")
                    r["query_utility"] = {"error": str(e), "query_hits": -1,
                                          "QueryMCG": -1,
                                          "QueryRetrievalNovelty": None,
                                          "pilot_depth_censored": False}
                    await asyncio.sleep(1)
                    continue
                _measure(r, res.papers, args, resolver, r_old_keys, i, len(candidates))
                await asyncio.sleep(1)
        finally:
            await engine.close()

    def _measure(r, papers, args, resolver, r_old_keys, i, n_total) -> None:
        """测量单个 query 的 MCG/Novelty 并保存 raw rows（audit 用）。"""
        r_q = len(papers)
        raw_papers = []
        new_papers = []
        unknown = 0
        for p in papers:
            key, info = paper_key_and_info(p, resolver, r_old_keys)
            raw_papers.append({"eid": info["eid"], "doi": info["doi"],
                               "canonical_key": key,
                               "in_old": info["in_old"],
                               "identity_source": info["identity_source"]})
            if key is None:
                unknown += 1
            elif not info["in_old"]:
                new_papers.append({"identity": key, "eid": info["eid"],
                                   "doi": info["doi"]})
        mcg = len(new_papers)
        novelty = round(mcg / r_q, 4) if r_q else 0.0
        r["query_utility"] = {
            "query_hits": r_q,
            "QueryMCG": mcg,
            "QueryRetrievalNovelty": novelty,
            "in_old_count": r_q - mcg - unknown,
            "identity_unknown_count": unknown,
            "pilot_depth_censored": (r_q >= args.depth),
            "new_papers": new_papers,
            "canonical_keys": {np["identity"] for np in new_papers},
            "raw_papers": raw_papers,
        }
        cen = "C" if r["query_utility"]["pilot_depth_censored"] else " "
        print(f"  [{i:>2}/{n_total}]{cen} hits={r_q:>3} "
              f"MCG={mcg:>3} novel={novelty:>6.1%} unk={unknown:>2} "
              f"src={r['source_community']:>8} {r['preferred_surface'][:38]}")

    if args.offline_cache:
        run_offline()
    else:
        asyncio.run(run_engine())

    # ── selection（QGS-blind 冻结规则）──
    ok = [r for r in candidates if r.get("query_utility", {}).get("QueryMCG", -1) >= 0]
    pool = [r for r in ok if r["query_utility"]["QueryMCG"] >= args.min_mcg]
    capped = diversity_capped(pool, args.max_per_community)
    selected, union_mcg, cond_log = greedy_coverage_merge(capped, args.min_mcg)

    ok.sort(key=lambda r: -r["query_utility"]["QueryMCG"])

    print(f"\n{'='*92}")
    print(f"Query Utility Pilot（depth={args.depth}，R_old={len(r_old_keys)}，"
          f"min_mcg={args.min_mcg}，EID 优先 + DOI bridge）")
    print(f"{'='*92}")
    print(f"{'MCG':>4} {'hits':>4} {'novel':>7} {'cens':>4} {'src':>8} {'score':>6}  term")
    for r in ok:
        u = r["query_utility"]
        print(f"{u['QueryMCG']:>4} {u['query_hits']:>4} "
              f"{u['QueryRetrievalNovelty']:>6.1%} "
              f"{'C' if u.get('pilot_depth_censored') else '-':>4} "
              f"{r['source_community']:>8} {r['score']:>6.2f}  "
              f"{r['preferred_surface'][:46]}")

    comm_cnt = Counter(r["source_community"] for r in ok)
    print(f"\nper-community（cap 前）：{dict(comm_cnt.most_common())}")

    print(f"\ngreedy coverage merge（min_mcg={args.min_mcg}，cap={args.max_per_community}/community）")
    print(f"{'order':>5} {'cond':>5} {'obs':>5} {'src':>8}  term")
    sel_by_term = {r["preferred_surface"]: r for r in selected}
    for i, c in enumerate(cond_log, 1):
        src = sel_by_term[c["term"]]["source_community"]
        print(f"{i:>5} {c['conditional_mcg']:>5} {c['observed_mcg']:>5} {src:>8}  "
              f"{c['term'][:46]}")
    print(f"\nUnionMCG = {union_mcg} | selected = {len(selected)} 条 | "
          f"Σ ConditionalMCG = {sum(c['conditional_mcg'] for c in cond_log)}")

    # ── 硬断言：Σ ConditionalMCG == UnionMCG ──
    sum_cond = sum(c["conditional_mcg"] for c in cond_log)
    invariant_ok = (sum_cond == union_mcg)
    audit = build_audit(candidates, resolver, r_old_keys)
    print(f"\nidentity audit: {json.dumps(audit, ensure_ascii=False)}")
    print(f"\n硬断言 Σ ConditionalMCG == UnionMCG: "
          f"{sum_cond} == {union_mcg} → {'PASS' if invariant_ok else 'FAIL'}")

    if not invariant_ok:
        err = (f"EVALUATOR_INVARIANT_FAILED: Σ ConditionalMCG={sum_cond} != "
               f"UnionMCG={union_mcg}。禁止 freeze。")
        print(f"\n[ERROR] {err}")
        audit_report = {
            "status": "INVALIDATED_BY_EVALUATOR_BUG",
            "error": err,
            "identity_audit": audit,
            "cond_log": cond_log,
            "union_mcg": union_mcg,
            "note": "冻结被拒绝；先修 identity/merge 口径再重跑",
        }
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(audit_report, f, ensure_ascii=False, indent=1)
        print(f"audit 报告已写: {args.out}")
        sys.exit(1)

    # ── 冻结 JSON（QGS-BLIND：无 QGS 引用）──
    frozen = {
        "version": "round3_queries_frozen_v215",
        "frozen_at": "2026-08-29",
        "selection_mode": "QGS_BLIND",
        "anchor": ANCHOR,
        "identity": "EID 优先（scopus_url 三种格式统一 2-s2.0-<digits>），DOI fallback，"
                    "EID<->DOI union-find bridge；identity 为空不计 MCG",
        "selection_rules": {
            "QueryMCG_min": args.min_mcg,
            "max_per_community": args.max_per_community,
            "merge": "greedy coverage merge（ConditionalMCG 最大优先）",
            "invariant": "Σ ConditionalMCG == UnionMCG（硬断言，已 PASS）",
        },
        "union_mcg": union_mcg,
        "queries": [{
            "term": r["preferred_surface"],
            "query": f'TITLE-ABS-KEY("{ANCHOR}" AND "{r["preferred_surface"]}")',
            "source_community": r["source_community"],
            "expansion_score": r["score"],
            "pilot": _jsonable(r["query_utility"]),
        } for r in selected],
        "conditional_mcg_log": cond_log,
        "identity_audit": audit,
        "next": "depth=500 正式 Scopus retrieval → 全结果进 Candidate DB → 最后打开 QGS 算 G2R",
    }
    with open(args.freeze_out, "w", encoding="utf-8") as f:
        json.dump(frozen, f, ensure_ascii=False, indent=1)
    print(f"\n[OK] 冻结 Round3 queries: {args.freeze_out}"
          f"（{len(selected)} 条，QGS-BLIND，Σcond==UnionMCG={union_mcg}）")

    summary = {
        "version": "round3_query_utility_pilot_v215",
        "anchor": ANCHOR,
        "depth": args.depth,
        "r_old_size": len(r_old_keys),
        "identity_note": "EID 优先（pages/publications 修复）+ DOI fallback + union-find bridge",
        "formulas": {
            "QueryMCG": "|R_q ∖ R_old|（observed，depth 截断）",
            "QueryRetrievalNovelty": "|R_q ∖ R_old| / |R_q|",
            "ConditionalMCG": "|R_q ∖ (R_old ∪ R_selected)|",
            "UnionMCG": "|∪_q R_q ∖ R_old|",
            "invariant": "Σ ConditionalMCG == UnionMCG（硬断言）",
        },
        "candidates_checked": len(ok),
        "censored_count": sum(1 for r in ok
                              if r["query_utility"].get("pilot_depth_censored")),
        "total_observed_new": sum(r["query_utility"]["QueryMCG"] for r in ok),
        "identity_audit": audit,
        "per_community_before_cap": dict(comm_cnt),
        "selection": {
            "min_mcg": args.min_mcg,
            "pool_after_mcg": len(pool),
            "pool_after_cap": len(capped),
            "selected": len(selected),
            "union_mcg": union_mcg,
            "sum_conditional": sum_cond,
            "invariant_ok": invariant_ok,
            "conditional_log": cond_log,
        },
        "frozen_queries_file": args.freeze_out,
        "all_utilities": [{
            "term": r["preferred_surface"],
            "source_community": r["source_community"],
            "score": r["score"],
            "queryability": r.get("queryability"),
            "query_utility": _jsonable(r["query_utility"]),
        } for r in ok],
        "next": "正式 depth=500 retrieval → 全结果进 Candidate DB → 最后打开 QGS 算 G2R",
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(f"[OK] 已写: {args.out}")


if __name__ == "__main__":
    main()
