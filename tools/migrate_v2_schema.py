"""P0-1: knowledge_base.db 演进为 全局论文 + 主题关系 架构（additive，无损）。

设计（用户 2026-09-08 拍板，v1.0 frozen JSON/hash 一律只读当历史证据）:
    papers           全局唯一论文实体（doi 唯一索引；三身份 doi/openalex_id/scopus_eid）
    topics           研究主题注册
    topic_papers     论文×主题 关系（relevance_label topic-specific，PK(topic_id,paper_id)）
    search_runs      主题下检索 run 注册（v1.0 materialized 一条）
    search_queries   （结构就位；v1.0 逐 query 映射留待 P0-2 backfill）
    paper_retrievals （结构就位；同上）
    knowledge_claims mechanism/hypothesis 声明展开（record_json.physical_mechanisms 等）
    audits           R06 审计 materialized（audit 分主题独立 frame）

无损原则:
  - 旧表 knowledge_records / route_mechanism_edges 不增不减（v1.0 216 / 700 冻结态保留）
  - 新表只是 frozen 产物的 materialized representation，不重新判任何 label
  - 迁移前置备份由调用方完成（knowledge_base.preP0.db）

用法:
    python tools/migrate_v2_schema.py            # dry-run：计算+断言，不写库
    python tools/migrate_v2_schema.py --commit   # 落库 + 迁移报告
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "data/cache/knowledge_base.db")
CATALOG = os.path.join(BASE, "data/exports/terminology/s8_finalkb_catalog.json")
CACHE = os.path.join(BASE, "data/cache/openalex_cache.json")
R06_REPORT = os.path.join(BASE, "data/exports/terminology/r06_recall_report.json")
OUT_REPORT = os.path.join(BASE, "data/exports/schema/migration_v2_report.json")

TOPIC_ID = "photopolymerization_shrinkage"
TOPIC_NAME = "Photopolymerization Shrinkage & Shrinkage Stress"
TOPIC_QUESTION = "光固化聚合物降低聚合收缩与收缩应力的机制 (mechanisms of reducing polymerization shrinkage and shrinkage stress in photocurable polymers)"
RUBRIC_VERSION = "S6_QA_RUBRIC_V1"
SCHEMA_VERSION = "v2.0-topic-2026-09-08"

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def norm_doi(d):
    if not d:
        return None
    d = str(d).strip().lower()
    d = re.sub(r"^doi:\s*", "", d)
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", d)
    d = d.split("</div")[0].strip()
    return d if d and d not in ("none", "nan", "null") else None


def extract_w(w):
    if not w:
        return None
    w = str(w).strip()
    if w.startswith("https://openalex.org/"):
        w = w[len("https://openalex.org/"):]
    if w.startswith("openalex:"):
        w = w[len("openalex:"):]
    w = w.replace("https://openalex.org/", "")
    return w if w.startswith("W") else None


def extract_eid_from_paper_id(pid):
    # 'scopus:2-s2.0-xxx' -> '2-s2.0-xxx'
    pid = str(pid or "")
    if pid.startswith("scopus:"):
        pid = pid[len("scopus:"):]
    return pid if pid.startswith("2-s2.0-") else None


SCHEMA_SQL = """
-- 全局唯一论文实体 ---------------------------------------------------
CREATE TABLE IF NOT EXISTS papers (
    paper_id    TEXT PRIMARY KEY,   -- canonical: doi:<norm> | openalex:<W> | scopus:<EID>
    doi         TEXT,
    openalex_id TEXT,               -- Wxxxxxxxx（去 URL 前缀）
    scopus_eid  TEXT,               -- 2-s2.0-xxxx
    title       TEXT,
    abstract    TEXT,
    year        INTEGER,
    source_json TEXT,               -- 溯源：kb_record / s8_catalog / cache_backfill
    created_at  REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_papers_doi ON papers(doi)
    WHERE doi IS NOT NULL AND doi != '';

-- 研究主题 -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS topics (
    topic_id          TEXT PRIMARY KEY,
    name              TEXT,
    research_question TEXT,
    rubric_version    TEXT,
    config_path       TEXT,
    created_at        REAL
);

-- 论文×主题关系（relevance 是 topic-specific 的） ---------------------
CREATE TABLE IF NOT EXISTS topic_papers (
    topic_id         TEXT NOT NULL,
    paper_id         TEXT NOT NULL,
    relevance_label  TEXT NOT NULL CHECK (relevance_label IN ('RELEVANT','UNCERTAIN','IRRELEVANT')),
    label_source     TEXT,           -- QA 出处（blind paper-level QA / community verdict ...）
    promotion_status TEXT,           -- promoted / pending / seed
    first_seen_run   TEXT,           -- v1.0: S6 / S7 / S7_EX12
    evidence_json    TEXT,
    created_at       REAL,
    PRIMARY KEY (topic_id, paper_id)
);

-- 检索 run / query / 命中 -------------------------------------------------
CREATE TABLE IF NOT EXISTS search_runs (
    run_id      TEXT PRIMARY KEY,
    topic_id    TEXT,
    stage       TEXT,      -- S5/S6/S7/S8 / autonomous
    description TEXT,
    config_json TEXT,
    created_at  REAL
);
CREATE TABLE IF NOT EXISTS search_queries (
    query_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT,
    topic_id   TEXT,
    relation   TEXT,
    query_text TEXT,
    backend    TEXT
);
CREATE TABLE IF NOT EXISTS paper_retrievals (
    retrieval_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT,
    topic_id     TEXT,
    paper_id     TEXT,
    backend      TEXT,
    query_id     INTEGER
);

-- 知识声明（mechanism / hypothesis，从 record_json 展开） ---------------
CREATE TABLE IF NOT EXISTS knowledge_claims (
    claim_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id      TEXT,
    topic_id      TEXT,
    claim_type    TEXT,   -- mechanism | hypothesis
    material      TEXT,   -- mechanism claim: cause
    mechanism     TEXT,
    property      TEXT,   -- mechanism claim: effect
    evidence      TEXT,
    confidence    REAL,
    payload_json  TEXT,   -- hypothesis 等非 mechanism 声明的完整负载
    source_record TEXT    -- 展开来源 record_json 的 paper_id
);
CREATE INDEX IF NOT EXISTS ix_claims_paper ON knowledge_claims(paper_id);
CREATE INDEX IF NOT EXISTS ix_claims_type ON knowledge_claims(claim_type);

-- 审计（audit/recall 分主题独立 frame） ----------------------------------
CREATE TABLE IF NOT EXISTS audits (
    audit_id         TEXT PRIMARY KEY,
    topic_id         TEXT,
    frame_id         TEXT,
    frame_desc       TEXT,
    retrieval_recall REAL,
    sensitivity      REAL,
    e2e_recall       REAL,
    resolve_scope    TEXT,   -- R1_operational / R2_strict
    labels_n         INTEGER,
    report_path      TEXT,
    audited_at       TEXT
);
"""


def build_cache_meta():
    """openalex_cache -> {wid: (title, year)}, {doi: (title, year)}（首非空优先）"""
    cache = json.load(open(CACHE, encoding="utf-8"))
    wm, dm = {}, {}
    for url, resp in cache.items():
        for w in resp.get("results") or []:
            wid = extract_w(w.get("id"))
            doi = norm_doi(w.get("doi"))
            title = (w.get("title") or "").strip()
            yr = w.get("publication_year")
            if wid and wid not in wm and (title or yr):
                wm[wid] = (title or "", yr)
            if doi and doi not in dm and (title or yr):
                dm[doi] = (title or "", yr)
    return wm, dm


def load_sources():
    kb = []
    con = sqlite3.connect(f"file:{DB}?mode=ro&immutable=1", uri=True)
    for pid, rj, ver, conf in con.execute(
            "SELECT paper_id, record_json, extractor_version, confidence FROM knowledge_records"):
        kb.append({"paper_id": pid, "record_json": json.loads(rj),
                   "extractor_version": ver, "confidence": conf})
    con.close()

    cat = json.load(open(CATALOG, encoding="utf-8"))
    catalog = cat["papers"]
    r06 = json.load(open(R06_REPORT, encoding="utf-8")) if os.path.exists(R06_REPORT) else None
    return kb, catalog, r06


def build_entities(kb, catalog, wm, dm):
    """合并 KB 216 + catalog 249 -> 全局实体（按 doi 归并；KB 优先作知识主源）。"""
    ents = {}          # key=(doi 或 wid 或 eid) -> entity
    order = []

    def ent_key(doi, wid, eid):
        if doi:
            return ("doi", doi)
        if wid:
            return ("wid", wid)
        return ("eid", eid)

    def get_ent(k):
        if k not in ents:
            ents[k] = {
                "doi": None, "openalex_id": None, "scopus_eid": None,
                "title": "", "abstract": "", "year": None,
                "sources": [], "kb_records": [],
                "catalog": None, "catalog_src": "",
            }
            order.append(k)
        return ents[k]

    # KB records（知识主源）
    for r in kb:
        rj = r["record_json"]
        doi = norm_doi(rj.get("doi"))
        wid = extract_w(rj.get("openalex_id")) or extract_w(r.get("paper_id"))
        eid = extract_eid_from_paper_id(r.get("paper_id"))
        # paper_id 直接为 doi 形态的旧记录
        if not doi and re.match(r"^10\.\d{4,9}/", str(r.get("paper_id"))):
            doi = norm_doi(r.get("paper_id"))
        if not eid and str(r.get("paper_id")).startswith("2-s2.0-"):
            eid = str(r.get("paper_id"))
        e = get_ent(ent_key(doi, wid, eid))
        if doi and not e["doi"]:
            e["doi"] = doi
        if wid and not e["openalex_id"]:
            e["openalex_id"] = wid
        if eid and not e["scopus_eid"]:
            e["scopus_eid"] = eid
        e["sources"].append("kb_record:" + r["paper_id"])
        e["kb_records"].append({"paper_id": r["paper_id"], "record_json": rj,
                                "extractor_version": r["extractor_version"]})
        # title 补全（record_json 无 title；仅能来自 cache 或 catalog）
        t, y = None, None
        if wid:
            t, y = wm.get(wid, (None, None))
        if not t and doi:
            t, y = dm.get(doi, (None, None))
        if t and not e["title"]:
            e["title"] = t
        if y and not e["year"]:
            e["year"] = y

    # catalog 249（topic_papers 源 + title/abstract）
    for p in catalog:
        doi = norm_doi(p.get("doi"))
        eid = str(p.get("key") or "")
        if eid.startswith("2-s2.0-"):
            eid = eid
        elif not eid and doi:
            eid = None
        else:
            eid = extract_eid_from_paper_id(eid) or eid or None
        k = ent_key(doi, None, eid)
        e = get_ent(k)
        if doi and not e["doi"]:
            e["doi"] = doi
        if eid and not e["scopus_eid"]:
            e["scopus_eid"] = eid
        if (p.get("title") or "").strip() and not e["title"]:
            e["title"] = p["title"].strip()
        if (p.get("abstract") or "").strip() and not e["abstract"]:
            e["abstract"] = p["abstract"].strip()
        e["catalog"] = p
        e["catalog_src"] = p.get("source") or ""
        e["sources"].append("s8_catalog")

    return ents, order


def canonical_paper_id(e):
    if e["doi"]:
        return "doi:" + e["doi"]
    if e["openalex_id"]:
        return "openalex:" + e["openalex_id"]
    return "scopus:" + e["scopus_eid"]


def collect_claims(e, pid):
    """从 entity 的 KB records 展开 mechanism / hypothesis claims。"""
    claims = []
    for rec in e["kb_records"]:
        rj = rec["record_json"]
        for m in rj.get("physical_mechanisms") or []:
            claims.append({
                "paper_id": pid, "topic_id": TOPIC_ID, "claim_type": "mechanism",
                "material": (m.get("cause") or "").strip(),
                "mechanism": (m.get("canonical") or m.get("mechanism") or "").strip(),
                "property": (m.get("effect") or "").strip(),
                "evidence": (m.get("evidence") or "").strip(),
                "confidence": m.get("confidence"),
                "payload_json": None,
                "source_record": rec["paper_id"],
            })
        for h in rj.get("search_hypotheses") or []:
            claims.append({
                "paper_id": pid, "topic_id": TOPIC_ID, "claim_type": "hypothesis",
                "material": None, "mechanism": None, "property": None,
                "evidence": (h.get("rationale") or "").strip(),
                "confidence": None,
                "payload_json": json.dumps(h, ensure_ascii=False),
                "source_record": rec["paper_id"],
            })
    return claims


def migrate(dry_run=True):
    print(f"[migrate] schema={SCHEMA_VERSION} topic={TOPIC_ID} dry_run={dry_run}")
    kb, catalog, r06 = load_sources()
    wm, dm = build_cache_meta()
    print(f"[load] kb_records={len(kb)} catalog={len(catalog)} cache_w={len(wm)} cache_doi={len(dm)}")

    ents, order = build_entities(kb, catalog, wm, dm)
    print(f"[entities] merged global papers = {len(order)}  (216 KB + 249 catalog 按 doi 归并)")

    # duplicate-doi 在 KB 侧的 resolve（addma 案例：两条 KB record 同 doi -> 已并入同一 entity）
    n_dup_doi_entities = sum(
        1 for k in order if ents[k]["doi"] and len(ents[k]["kb_records"]) > 1)
    print(f"[resolve] entities with >1 KB record (doi merge) = {n_dup_doi_entities}")

    # papers 行
    rows_papers = []
    for k in order:
        e = ents[k]
        pid = canonical_paper_id(e)
        rows_papers.append({
            "paper_id": pid, "doi": e["doi"], "openalex_id": e["openalex_id"],
            "scopus_eid": e["scopus_eid"], "title": e["title"] or None,
            "abstract": e["abstract"] or None, "year": e["year"],
            "source_json": json.dumps({"sources": e["sources"],
                                       "n_kb_records": len(e["kb_records"])}, ensure_ascii=False),
        })

    # topic_papers：仅 catalog 249（有裁决的），label 原样
    rows_tp = []
    for k in order:
        e = ents[k]
        if not e["catalog"]:
            continue
        p = e["catalog"]
        ev = p.get("evidence") or {}
        pid = canonical_paper_id(e)
        rows_tp.append({
            "topic_id": TOPIC_ID, "paper_id": pid,
            "relevance_label": p.get("label") or "",
            "label_source": p.get("label_source") or "",
            "promotion_status": "promoted",
            "first_seen_run": p.get("source") or "",
            "evidence_json": json.dumps({**ev, "kb_status": p.get("kb_status")},
                                        ensure_ascii=False),
        })

    # knowledge_claims 展开
    rows_claims = []
    for k in order:
        e = ents[k]
        if not e["kb_records"]:
            continue
        pid = canonical_paper_id(e)
        rows_claims.extend(collect_claims(e, pid))

    stats = {
        "papers": len(rows_papers),
        "topic_papers": len(rows_tp),
        "topic_papers_by_label": {},
        "claims": len(rows_claims),
        "claims_by_type": {},
        "identity": {"with_doi": 0, "with_wid": 0, "with_eid": 0, "no_doi_no_wid_no_eid": 0},
        "title_fill": 0, "year_fill": 0,
        "kb_only_papers": 0, "catalog_only_papers": 0,
    }
    from collections import Counter
    stats["topic_papers_by_label"] = dict(Counter(r["relevance_label"] for r in rows_tp))
    stats["claims_by_type"] = dict(Counter(r["claim_type"] for r in rows_claims))
    for r in rows_papers:
        if r["doi"]:
            stats["identity"]["with_doi"] += 1
        if r["openalex_id"]:
            stats["identity"]["with_wid"] += 1
        if r["scopus_eid"]:
            stats["identity"]["with_eid"] += 1
        if not (r["doi"] or r["openalex_id"] or r["scopus_eid"]):
            stats["identity"]["no_doi_no_wid_no_eid"] += 1
        if r["title"]:
            stats["title_fill"] += 1
        if r["year"]:
            stats["year_fill"] += 1
        src = json.loads(r["source_json"])["sources"]
        if "s8_catalog" not in src:
            stats["kb_only_papers"] += 1
        if "s8_catalog" in src and not any(s.startswith("kb_record:") for s in src):
            stats["catalog_only_papers"] += 1

    # ── 验收断言 ───────────────────────────────────────────────
    checks = {}
    checks["tp_total_249"] = len(rows_tp) == 249
    checks["tp_R170_U79"] = stats["topic_papers_by_label"].get("RELEVANT") == 170 and \
        stats["topic_papers_by_label"].get("UNCERTAIN") == 79
    checks["papers_doi_unique"] = len({r["doi"] for r in rows_papers if r["doi"]}) == \
        sum(1 for r in rows_papers if r["doi"])
    checks["no_identityless"] = stats["identity"]["no_doi_no_wid_no_eid"] == 0
    for name, ok in checks.items():
        print(f"  [check] {name}: {'PASS' if ok else 'FAIL'}")

    if dry_run:
        print("\n[dry-run] 未写库。stats:")
        print(json.dumps(stats, ensure_ascii=False, indent=1))
        print("\n[dry-run] 通过即执行 --commit")
        return

    # ── commit：写库（事务；旧表绝不触碰） ─────────────────────
    con = sqlite3.connect(DB)
    try:
        con.executescript(SCHEMA_SQL)
        now = time.time()
        # 防重入：表非空则拒绝（保幂等安全；清空需显式 --force）
        n_papers = con.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        if n_papers:
            print(f"[ABORT] papers 表已有 {n_papers} 行；拒绝重入。如需重建请手动清空 v2 表。")
            sys.exit(2)
        con.execute("BEGIN")
        con.executemany(
            "INSERT INTO papers (paper_id, doi, openalex_id, scopus_eid, title, abstract, "
            "year, source_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            [(r["paper_id"], r["doi"], r["openalex_id"], r["scopus_eid"], r["title"],
              r["abstract"], r["year"], r["source_json"], now) for r in rows_papers])
        con.execute(
            "INSERT INTO topics (topic_id, name, research_question, rubric_version, "
            "config_path, created_at) VALUES (?,?,?,?,?,?)",
            (TOPIC_ID, TOPIC_NAME, TOPIC_QUESTION, RUBRIC_VERSION,
             "topics/photopolymerization_shrinkage/topic.yaml", now))
        con.executemany(
            "INSERT INTO topic_papers (topic_id, paper_id, relevance_label, label_source, "
            "promotion_status, first_seen_run, evidence_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
            [(r["topic_id"], r["paper_id"], r["relevance_label"], r["label_source"],
              r["promotion_status"], r["first_seen_run"], r["evidence_json"], now)
             for r in rows_tp])
        con.executemany(
            "INSERT INTO knowledge_claims (paper_id, topic_id, claim_type, material, "
            "mechanism, property, evidence, confidence, payload_json, source_record) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(r["paper_id"], r["topic_id"], r["claim_type"], r["material"], r["mechanism"],
              r["property"], r["evidence"], r["confidence"], r["payload_json"],
              r["source_record"]) for r in rows_claims])
        # search_runs：v1.0 materialized 一条
        con.execute(
            "INSERT INTO search_runs (run_id, topic_id, stage, description, config_json, "
            "created_at) VALUES (?,?,?,?,?,?)",
            (f"{TOPIC_ID}::v1.0", TOPIC_ID, "S5-S8",
             "v1.0 release materialized from frozen artifacts (no re-run)",
             json.dumps({"source": "data/exports/releases/v1.0_manifest.json",
                         "note": "S6/S7 逐 query 映射留待 P0-2 backfill"}, ensure_ascii=False),
             now))
        # audits：R06 materialized（R2 strict 主口径 + R1 存 frame_desc）
        if r06:
            r2 = r06["R2_strict"]
            r1 = r06["R1_operational"]
            con.execute(
                "INSERT INTO audits (audit_id, topic_id, frame_id, frame_desc, "
                "retrieval_recall, sensitivity, e2e_recall, resolve_scope, labels_n, "
                "report_path, audited_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (r06["audit_id"], TOPIC_ID, r06["universe_id"],
                 json.dumps({"frame": "FRAME_V2 OpenAlex wide (external)",
                             "R1_operational": r1, "frozen_note":
                             "external-frame, 不代表材料领域绝对召回率"}, ensure_ascii=False),
                 r2["retrieval"]["recall"], r2["sensitivity"]["sens"],
                 r2["e2e"]["recall"], "R2_strict", r06["n_labels"],
                 "docs/2026-09-08-r06-recall-audit-report.md", r06["built_at"]))
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

    # 落盘迁移报告
    os.makedirs(os.path.dirname(OUT_REPORT), exist_ok=True)
    report = {
        "schema_version": SCHEMA_VERSION, "migrated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "topic": TOPIC_ID, "dry_run": dry_run,
        "stats": stats, "checks": checks,
        "v1_frozen_untouched": {
            "knowledge_records": 216, "route_mechanism_edges": 700,
            "note": "旧表未增删；v1.0 manifest/frozen JSON 只读历史证据",
        },
        "known_gaps": [
            "papers.title 由 cache/catalog 尽力补全；KB-only 且 cache 缺失者 title=NULL",
            "papers.year v1.0 产物未存，仅 cache 命中 ~44 篇有值，其余 NULL（v2 运行时补）",
            "search_queries / paper_retrievals 表已建但未填充（v1.0 无 run 级映射，P0-2 backfill）",
        ],
        "checks_explained": {
            "tp_total_249": "topic_papers 必须恰好 249",
            "tp_R170_U79": "label 分布 170 R + 79 U 不变",
            "papers_doi_unique": "全局无重复 DOI（唯一索引约束生效前提）",
            "no_identityless": "每篇至少一个身份通道",
        },
    }
    tmp = OUT_REPORT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT_REPORT)
    print(f"[commit] 迁移完成 -> {OUT_REPORT}")
    print(json.dumps(stats, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="P0-1 schema v2 无损迁移")
    ap.add_argument("--commit", action="store_true", help="落库（默认 dry-run）")
    args = ap.parse_args()
    migrate(dry_run=not args.commit)
