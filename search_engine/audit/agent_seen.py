"""search_engine/audit/agent_seen.py — Search A 检索完备性判定（2026-08-29 用户定稿）。

三态 agent_seen（用户定，不再用二值）：
  TRUE    = Search A 检索结果里确实出现（DOI 主判，EID/归一化 title 兜底）
  FALSE   = identity 已解析（DOI 存在）且不在检索结果 → 高置信 miss
  UNKNOWN = identity 未解析（无 DOI 且 title 无法匹配）→ 需 identity repair

identity 解析优先级（用户定）：WID ↔ DOI ↔ EID ↔ exact normalized title（最后兜底）。

Search A 检索结果集合 = depth run ∪ Round1 ∪ Round3（eid/doi/title 三通道）。
"""
import json
import os
import re

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXPORTS = os.path.join(BASE, "data", "exports")
CACHE_PATH = os.path.join(BASE, "data", "cache", "openalex_cache.json")

RETRIEVAL_SOURCES = [
    "query_family_runs_depth.json",
    "community_round1_retrieval.json",
    "round3_depth500_retrieval.json",
]


def _norm_doi(d) -> str:
    return (d or "").strip().lower().replace("https://doi.org/", "").replace("http://doi.org/", "")


def _norm_title(t) -> str:
    """归一化 title：小写 + 去非字母数字 + 压缩空白（精确匹配兜底）。"""
    t = (t or "").strip().lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def build_found_sets(data_dir: str | None = None) -> dict:
    """Search A 检索结果集合（eids/dois/titles 三通道）。"""
    exports = data_dir and os.path.join(data_dir, "exports") or EXPORTS
    found = {"eids": set(), "dois": set(), "titles": set()}
    for fn in RETRIEVAL_SOURCES:
        p = os.path.join(exports, fn)
        if not os.path.exists(p):
            continue
        d = json.load(open(p, encoding="utf-8"))
        for rs in (d.get("records", {}).values()
                   if "records" in d else []):
            for r in rs:
                if r.get("eid"):
                    found["eids"].add(str(r["eid"]).strip())
                if r.get("doi"):
                    found["dois"].add(_norm_doi(r["doi"]))
                if r.get("title"):
                    found["titles"].add(_norm_title(r["title"]))
        for cid, spec in d.get("communities", {}).items():
            for q in spec.get("queries", []):
                for rec in q.get("records", []):
                    if rec.get("eid"):
                        found["eids"].add(str(rec["eid"]).strip())
                    if rec.get("doi"):
                        found["dois"].add(_norm_doi(rec["doi"]))
                    if rec.get("title"):
                        found["titles"].add(_norm_title(rec["title"]))
    return found


def build_wid_meta(cache_path: str = CACHE_PATH) -> dict:
    """openalex_cache → {wid: {doi, title, year}}。"""
    meta = {}
    if not os.path.exists(cache_path):
        return meta
    cache = json.load(open(cache_path, encoding="utf-8"))
    for q, resp in cache.items():
        for w in resp.get("results", []):
            wid = (w.get("id") or "").replace("https://openalex.org/", "")
            if wid and wid not in meta:
                meta[wid] = {
                    "doi": _norm_doi(w.get("doi")),
                    "title": _norm_title(w.get("title")),
                    "year": w.get("publication_year"),
                }
    return meta


def resolve_agent_seen(paper_ids: list[str],
                       data_dir: str | None = None,
                       cache_path: str = CACHE_PATH) -> dict:
    """对 paper_ids 逐个判定 agent_seen 三态。

    返回 {pid: {"agent_seen": "TRUE"|"FALSE"|"UNKNOWN",
                "match": "doi"|"title"|"unmatched"|"no_identity"}}
    """
    found = build_found_sets(data_dir)
    meta = build_wid_meta(cache_path)
    out = {}
    for pid in paper_ids:
        m = meta.get(pid)
        if not m:
            out[pid] = {"agent_seen": "UNKNOWN", "match": "no_identity"}
            continue
        if m["doi"] and m["doi"] in found["dois"]:
            out[pid] = {"agent_seen": "TRUE", "match": "doi"}
            continue
        if m["title"] and m["title"] in found["titles"]:
            out[pid] = {"agent_seen": "TRUE", "match": "title"}
            continue
        if m["doi"]:
            out[pid] = {"agent_seen": "FALSE", "match": "unmatched"}
        else:
            # 无 DOI 且 title 未命中——identity 无法闭合
            out[pid] = {"agent_seen": "UNKNOWN", "match": "no_doi_unmatched_title"}
    return out
