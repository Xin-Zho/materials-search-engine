"""S8 FinalKB 轻收录目录构建（2026-09-08 用户拍板：S7+S6 全收 + 轻收录先行）。

把 S6 R/U(120) + S7 R/U(129) 论文统一去重（vs KB 64 seed）→ 论文级收录目录。
收录口径 = R1 operational（RELEVANT+UNCERTAIN 均入 FinalKB，R2=仅 RELEVANT 可派生）。
抽取（2.0-edges）分批延后，本目录只做 membership + 溯源，extraction_status=PENDING。

输入（全部冻结产物）：
  s7_candidate_set.json / s7_keep_pool.json / s7_ex12_qa_corpus.json+s7_ex12_qa_labels.json
  s6_candidate_labels.json / s6_pilot_query_records.json
  data/cache/scopus_cache.db（abstract doi 桥接）/ knowledge_base.db（KB 64）/ openalex_cache.json（KB title）

输出：s8_finalkb_catalog.json
"""
import json
import os
import re
import sqlite3

T = "data/exports/terminology"
KB_DB = "data/cache/knowledge_base.db"
SCOPUS_DB = "data/cache/scopus_cache.db"
OPENALEX_CACHE = "data/cache/openalex_cache.json"
OUT = os.path.join(T, "s8_finalkb_catalog.json")


def norm_doi(d):
    if not d:
        return None
    d = str(d).strip().lower()
    d = re.sub(r"^doi:\s*", "", d)
    d = d.split("</div")[0].strip()
    if not d or d in ("none", "nan", "null", "-"):
        return None
    return d


def norm_title(t):
    """title 归一化（小写、去标点/空白，用于无 doi 时的兜底匹配）。"""
    if not t:
        return ""
    t = t.lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# ── 1. KB 侧（64 seed）────────────────────────────────────────────
conn = sqlite3.connect(KB_DB)
kb_records = []
for pid, rj in conn.execute("SELECT paper_id, record_json FROM knowledge_records"):
    d = json.loads(rj)
    kb_records.append({"paper_id": pid, "doi": norm_doi(d.get("doi")),
                       "openalex_id": d.get("openalex_id")})
kb_dois = {r["doi"] for r in kb_records if r["doi"]}
conn.close()

# KB title（openalex_cache 反查）
cache = json.load(open(OPENALEX_CACHE, encoding="utf-8"))
wid_title = {}
for url, resp in cache.items():
    for w in (resp.get("results") or []):
        wid = (w.get("id") or "").replace("https://openalex.org/", "")
        if wid and wid not in wid_title:
            wid_title[wid] = w.get("display_name") or w.get("title") or ""
kb_title_by_doi = {}
for r in kb_records:
    oid = r["openalex_id"]
    t = wid_title.get(oid or "", "")
    if r["doi"]:
        kb_title_by_doi[r["doi"]] = t
kb_titles = {norm_title(t) for t in kb_title_by_doi.values() if t}
kb_title_to_pid = {}
for r in kb_records:
    t = norm_title(wid_title.get(r["openalex_id"] or "", ""))
    if t:
        kb_title_to_pid[t] = r["paper_id"]

# ── 2. S7 候选（KEEP 池 750 label）────────────────────────────────
cs = json.load(open(os.path.join(T, "s7_candidate_set.json"), encoding="utf-8"))
pool = json.load(open(os.path.join(T, "s7_keep_pool.json"), encoding="utf-8"))
meta = {p["key"]: p for p in pool["papers"]}

def scopus_abstract(doi):
    """doi → abstract（scopus_cache.papers 表）。"""
    if not doi:
        return None
    try:
        sc = sqlite3.connect(SCOPUS_DB)
        row = sc.execute(
            "SELECT normalized_json FROM papers WHERE paper_id=?",
            ("scopus:" + doi,)).fetchone()
        sc.close()
        if row:
            try:
                ab = (json.loads(row[0]).get("abstract") or "").strip()
                return ab or None
            except Exception:
                return None
    except Exception:
        return None
    return None

papers = {}
for p in cs["papers"]:
    if p["label"] not in ("RELEVANT", "UNCERTAIN"):
        continue
    m = meta.get(p["key"], {})
    doi = norm_doi(m.get("doi"))
    key = p["key"]
    papers[key] = {
        "key": key, "source": "S7", "label": p["label"],
        "label_source": "s7_candidate_set.json（KEEP 池论文级 QA 终裁 R106/U22）",
        "title": m.get("title") or p.get("title") or "",
        "doi": doi,
        "abstract": m.get("abstract") or "",
        "abstract_src": "keep_pool" if (m.get("abstract") or "").strip() else None,
        "evidence": {
            "cluster_id": m.get("cluster_id"),
            "ex_sources": m.get("ex_sources") or p.get("ex_sources") or [],
        },
        "extraction_status": "PENDING",
    }

# EX-12 补跑 R（round4 新组，不在 KEEP 池）
ex12_c = json.load(open(os.path.join(T, "s7_ex12_qa_corpus.json"), encoding="utf-8"))
ex12_l = json.load(open(os.path.join(T, "s7_ex12_qa_labels.json"), encoding="utf-8"))
if "labels" in ex12_l:
    ex12_l = ex12_l["labels"]
ex12_meta = {p["key"]: p for p in ex12_c["papers"]}
for k, lab in ex12_l.items():
    if lab != "RELEVANT" or k in papers:
        continue
    m = ex12_meta.get(k, {})
    doi = norm_doi(m.get("doi"))
    papers[k] = {
        "key": k, "source": "S7_EX12", "label": "RELEVANT",
        "label_source": "s7_ex12_qa_labels.json（EX-12 coatings×warpage 36 篇论文级 QA）",
        "title": m.get("title") or "", "doi": doi,
        "abstract": m.get("abstract") or "",
        "abstract_src": "ex12_corpus" if (m.get("abstract") or "").strip() else None,
        "evidence": {"cluster_id": None, "ex_sources": ["EX-12"]},
        "extraction_status": "PENDING",
    }

# ── 3. S6 候选（freeze 全量 QA R63/U57）───────────────────────────
s6l = json.load(open(os.path.join(T, "s6_candidate_labels.json"), encoding="utf-8"))
s6r = json.load(open(os.path.join(T, "s6_pilot_query_records.json"), encoding="utf-8"))
doi_by_key = {}
for qid, w in s6r["records_by_query"].items():
    for r in w.get("rows", []):
        k = r.get("key")
        if k and k not in doi_by_key:
            doi_by_key[k] = norm_doi(r.get("doi")) or r.get("eid")
for k, v in s6l["by_key"].items():
    if v["label"] not in ("RELEVANT", "UNCERTAIN"):
        continue
    if k in papers:
        continue   # S6∩S7 理论上 ∅（S7 new_vs_S6 排除），防御
    doi = doi_by_key.get(k)
    ab = scopus_abstract(doi) if doi and not doi.startswith("2-s2.0") else None
    papers[k] = {
        "key": k, "source": "S6", "label": v["label"],
        "label_source": "s6_candidate_labels.json（S6 全量 QA R63/U57，freeze 2026-09-07）",
        "title": v.get("title") or "", "doi": doi,
        "abstract": ab or "", "abstract_src": "scopus_cache" if ab else None,
        "evidence": {"families": v.get("families") or [],
                     "queries": v.get("queries") or []},
        "extraction_status": "PENDING",
    }

# ── 4. 去重 vs KB ─────────────────────────────────────────────────
dedup = {"new_papers": [], "already_known": [], "conflict": []}
for key, p in papers.items():
    kb_status = "new"
    kb_match = None
    if p["doi"] and p["doi"] in kb_dois:
        kb_status = "already_known"
        kb_match = {"method": "doi", "kb_doi": p["doi"]}
    else:
        # title 兜底：仅当候选 doi 缺失时检查（doi 能对齐则 title 冗余）
        t = norm_title(p["title"])
        if not p["doi"] and t and t in kb_titles:
            kb_status = "already_known"
            kb_match = {"method": "title",
                        "kb_paper_id": kb_title_to_pid.get(t)}
    p["kb_status"] = kb_status
    p["kb_match"] = kb_match
    if kb_status == "new":
        dedup["new_papers"].append(key)
    else:
        dedup["already_known"].append(key)

# ── 5. 统计 ───────────────────────────────────────────────────────
by_source = {}
by_label = {}
abs_cov = {"n": 0, "with_abstract": 0}
for p in papers.values():
    by_source.setdefault(p["source"], {}).setdefault(p["label"], 0)
    by_source[p["source"]][p["label"]] += 1
    by_label[p["label"]] = by_label.get(p["label"], 0) + 1
    abs_cov["n"] += 1
    if p["abstract"]:
        abs_cov["with_abstract"] += 1

stats = {
    "n_papers": len(papers),
    "by_source": by_source,
    "by_label": by_label,
    "n_overlap_kb": len(dedup["already_known"]),
    "n_new": len(dedup["new_papers"]),
    "abstract": abs_cov,
    "kb_seed": {"n": len(kb_records), "n_with_doi": len(kb_dois)},
}

out = {
    "role": "S8 FinalKB 轻收录目录（论文级 membership，收录口径 R1=R+U）",
    "built_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
    "rubric": "S6_QA_RUBRIC_V1",
    "decisions": {
        "收录边界": "S7(129) + S6(120)，用户拍板 2026-09-08（R06=Search vN 整体审计，S6 不入会被算 screening miss）",
        "收录口径": "R1 operational = RELEVANT+UNCERTAIN 均入 FinalKB（North Star §4.1③ 冻结）；R2=仅 RELEVANT 可由 label 派生",
        "收录形态": "轻收录先行（本目录=membership+溯源），2.0-edges 抽取分批延后",
        "抽取": "extraction_status=PENDING，分批执行后回填",
    },
    "stats": stats,
    "dedup": dedup,
    "papers": list(papers.values()),
}

# R2 派生集合（仅 RELEVANT）
out["candidate_R2_keys"] = [p["key"] for p in papers.values()
                            if p["label"] == "RELEVANT"]

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)

print(f"[ok] {OUT}")
print(f"  收录 {stats['n_papers']} 篇 = S6+S7 R/U（R1 口径）")
for src, d in sorted(by_source.items()):
    print(f"    {src:<8} {d}")
print(f"  label: {by_label}")
print(f"  去重: new {len(dedup['new_papers'])} / already_known "
      f"{len(dedup['already_known'])} / conflict 0")
for k in dedup["already_known"]:
    p = papers[k]
    print(f"    [already_known] {k} {p['source']} {p['label']} "
          f"match={p['kb_match']}")
print(f"  abstract 覆盖: {abs_cov['with_abstract']}/{abs_cov['n']} "
      f"({abs_cov['with_abstract']/abs_cov['n']:.1%})")
print(f"  R2 keys: {len(out['candidate_R2_keys'])}")
