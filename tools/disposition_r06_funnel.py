#!/usr/bin/env python3
"""R06 漏斗归因处置引擎：41 篇 = 22 队列（promotion_pending_queue）+ 19 外部判 R 未筛。

不是一键晋升：每篇按证据走规则，产出 promotion proposal + append-only 处置日志，
默认 dry-run，--apply 才写 KB（分批、可回滚：写前落 before-snapshot）。

规则（U_PROMOTION_RULE_V1）：
  R1 external_audit_R   19 篇 E_NOT_SCREENED：R06 外部盲评判 RELEVANT（权威判定）
                        → 提议收录（label_source=r06_external_audit，abstract 跨缓存补）
  R2 keep_cluster_u     U 且所在社区簇 verdict=KEEP（R+U 率抽样达标）→ 暂定收录（保留 U 标记）
  R3 no_abstract        判 R 无摘要：openalex_cache/scopus_cache 补摘要 → 收录
  R4 defer              证据不足（FAIL 簇的 U / 补不到摘要且无外部判定）→ 留队列等二轮 QA

KB 写入面（--apply）：papers（三通道身份）+ topic_papers（label/promotion_status/溯源）。
不做知识抽取（需 API）——本工具只修晋升，抽取归 knowledge extractor。

用法：
  .venv/Scripts/python tools/disposition_r06_funnel.py            # dry-run（默认）
  .venv/Scripts/python tools/disposition_r06_funnel.py --apply    # 执行 KB 写入
输出：
  data/exports/terminology/r06_disposition_log.json（append-only）
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TERM = ROOT / "data" / "exports" / "terminology"
QUEUE_PATH = TERM / "promotion_pending_queue.json"
REVIEW_PATH = TERM / "r06_funnel19_review.json"
LOG_PATH = TERM / "r06_disposition_log.json"
KB_DB = ROOT / "data" / "cache" / "knowledge_base.db"
DETAIL_PATH = TERM / "r06_retrieved_relevant_detail.json"
CANDIDATE_PATH = TERM / "s7_candidate_set.json"
VERDICT_PATH = TERM / "s7_community_verdict.json"
MEMORY_PATH = TERM / "s7_community_memory.json"  # 簇成员表（papers 字段），覆盖 QA 语料全集
OA_CACHE = ROOT / "data" / "cache" / "openalex_cache.json"
# 第三摘要源：R06 外部盲评 labels（500 篇全带 title/abstract/doi）
R06_LABELS = ROOT / "data" / "exports" / "completeness_labels" / "pc_001__20260908012830_filled.json"

TOPIC_ID = "pc_001"
RULES_VERSION = "U_PROMOTION_RULE_V1"

# ── P0-B1b：KB 写入全部经唯一身份入口（R1/R8/R9）────────────────────────
# 迁移前本脚本是**第二个 papers 直写点**，且沿用了同一套「信任字段名」的写法：
#   paper_id = f"scopus:{p['key']}"            <- key 若是 DOI 形态，前缀就错配
#   doi      = p.get("doi")                    <- 同一列可能装着 EID（与 W1 同源缺陷）
#   con.execute("insert into papers ...")      <- 绕过身份裁决
# 它的真实职责只有一半是「建论文」，另一半是「给已存在论文补摘要」——
# 后者正因如此才需要 backfill_paper_fields 这个**受限**的第二入口（权限分离）。
sys.path.insert(0, str(ROOT))
from search_engine import identity as idt  # noqa: E402
from search_engine import paper_writer as pw  # noqa: E402

SOURCE = "tools/disposition_r06_funnel"


def norm_doi(doi: str | None) -> str:
    return (doi or "").strip().lower()


def load_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_evidence() -> tuple[dict, dict, dict, dict]:
    """返回 (eid→cluster, cluster→decision, W/doi→外部R判定, eid→candidate 元数据)。

    簇映射主源 = community_memory.papers（QA 语料全集成员表），
    candidate_set（750 篇子集）作次源补充。
    """
    eid_cluster: dict[str, str] = {}
    for cl in load_json(MEMORY_PATH)["clusters"]:
        for k in cl.get("papers", []):
            eid_cluster[k] = cl["cluster_id"]
    cand = load_json(CANDIDATE_PATH)["papers"]
    eid_meta = {p["key"]: p for p in cand}
    for p in cand:
        eid_cluster.setdefault(p["key"], p.get("cluster_id"))
    verdict = load_json(VERDICT_PATH)["clusters"]
    cluster_decision = {cid: v.get("decision") for cid, v in
                        (verdict.items() if isinstance(verdict, dict)
                         else ((c["cluster_id"], c) for c in verdict))}
    ext_R: dict[str, dict] = {}
    for p in load_json(DETAIL_PATH):
        if p.get("doi"):
            ext_R[norm_doi(p["doi"])] = p
        ext_R[p["paper_id"]] = p
    return eid_cluster, cluster_decision, ext_R, eid_meta


_OA_CACHE_MEMO: dict | None = None


def _r06_label_abstract(doi: str, wid: str) -> str:
    if not R06_LABELS.exists():
        return ""
    data = load_json(R06_LABELS)
    items = data.get("labels") if isinstance(data, dict) else data
    for it in items or []:
        if not isinstance(it, dict):
            continue
        if (it.get("paper_id") == wid) or (doi and norm_doi(it.get("doi")) == norm_doi(doi)):
            return (it.get("abstract") or "").strip()
    return ""


def any_abstract(doi: str, wid: str) -> tuple[str, str]:
    """三源摘要兜底，返回 (abstract, source)。"""
    for source, fn in (
        ("openalex_cache", lambda: oa_abstract(doi, wid)),
        ("scopus_cache", lambda: scopus_abstract(doi)),
        ("r06_labels", lambda: _r06_label_abstract(doi, wid)),
    ):
        ab = fn()
        if ab:
            return ab, source
    return "", ""


def _oa_cache() -> dict:
    global _OA_CACHE_MEMO
    if _OA_CACHE_MEMO is None:
        _OA_CACHE_MEMO = load_json(OA_CACHE) if OA_CACHE.exists() else {}
    return _OA_CACHE_MEMO


def oa_abstract(doi: str, wid: str) -> str:
    if not (doi or wid):
        return ""
    for v in _oa_cache().values():
        if not isinstance(v, dict):
            continue
        if norm_doi(v.get("doi")) == norm_doi(doi) or str(v.get("id", "")).endswith(wid or "\0"):
            ab = v.get("abstract")
            if isinstance(ab, str) and ab.strip():
                return ab.strip()
    return ""


def scopus_abstract(doi: str) -> str:
    db = ROOT / "data" / "cache" / "scopus_cache.db"
    if not db.exists() or not doi:
        return ""
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        # 这是 **scopus_cache.db.papers** 的源记录标识（引擎缓存，同名不同库），
        # 不是 KB 的 paper_uid —— 故用 make_record_id 而非 make_paper_uid。
        row = con.execute(
            "select normalized_json from papers where paper_id = ?",
            (idt.make_record_id("scopus", doi),),
        ).fetchone()
        con.close()
        if row:
            return (json.loads(row[0]).get("abstract") or "").strip()
    except sqlite3.Error:
        pass
    return ""


def rule_evaluate(eid: str, entry: dict, ev: tuple) -> dict:
    """单条队列条目 → {rule, action, reason, evidence}。"""
    eid_cluster, cluster_decision, ext_R, eid_meta = ev
    doi = entry.get("doi") or norm_doi(eid_meta.get(eid, {}).get("doi"))
    ext = ext_R.get(doi) or ext_R.get(eid)
    if entry.get("reason_code") == "no_abstract" or (entry.get("label") == "RELEVANT"):
        ab = scopus_abstract(doi) or oa_abstract(doi, eid)
        if ab or ext:
            return {"rule": "R3_no_abstract_backfill" if ab else "R1_external_audit_R",
                    "action": "promote", "label": "RELEVANT",
                    "reason": "补到摘要" if ab else "外部审计判 R",
                    "evidence": {"abstract_source": "scopus_cache" if scopus_abstract(doi)
                                 else ("openalex_cache" if ab else "r06_external_audit")}}
        return {"rule": "R4_defer", "action": "defer", "reason": "判 R 无摘要且缓存补不到，待 API 补源"}
    # UNCERTAIN 分支
    if ext:
        return {"rule": "R1_external_audit_R", "action": "promote", "label": "RELEVANT",
                "reason": "R06 外部盲评判 RELEVANT（权威，覆盖内部 U）",
                "evidence": {"external": True}}
    cid = eid_cluster.get(eid)
    dec = cluster_decision.get(cid) if cid else None
    if dec == "KEEP":
        return {"rule": "R2_keep_cluster_u", "action": "promote_tentative", "label": "UNCERTAIN",
                "reason": f"U 且所在簇 {cid} verdict=KEEP（社区级 R+U 抽样达标）",
                "evidence": {"cluster_id": cid, "decision": dec}}
    return {"rule": "R4_defer", "action": "defer",
            "reason": f"U 且簇 {cid or '?'} verdict={dec or 'unknown'}，证据不足留队列",
            "evidence": {"cluster_id": cid, "decision": dec}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="执行 KB 写入（默认 dry-run）")
    args = ap.parse_args()

    ev = build_evidence()
    queue = load_json(QUEUE_PATH)
    proposals = []

    # 19 篇外部判 R（E_NOT_SCREENED）
    for p in load_json(DETAIL_PATH):
        if p["in_kb"]:
            continue
        ab = oa_abstract(p.get("doi", ""), p["paper_id"]) or scopus_abstract(p.get("doi", ""))
        proposals.append({
            "key": p["paper_id"], "kind": "external_R_unscreened",
            "rule": "R1_external_audit_R", "action": "promote", "label": "RELEVANT",
            "doi": p.get("doi"), "title": p.get("title", ""),
            "abstract_source": "openalex_cache" if oa_abstract(p.get("doi", ""), p["paper_id"])
            else ("scopus_cache" if scopus_abstract(p.get("doi", "")) else None),
            "reason": "R06 外部盲评 RELEVANT，系统从未筛选（检索覆盖≠筛选覆盖）",
        })
    # 22 条队列
    for eid, entry in sorted(queue["entries"].items()):
        verdict = rule_evaluate(eid, entry, ev)
        meta = ev[3].get(eid, {})
        proposals.append({
            "key": eid, "kind": "queue_entry", **verdict,
            "doi": entry.get("doi"), "title": meta.get("title", ""),
            "stages": entry.get("stages", {}),
        })

    from collections import Counter
    acts = Counter(p["action"] for p in proposals)
    print(f"处置建议 {len(proposals)} 条: {dict(acts)}")
    for p in proposals:
        print(f"  {p['key']:<16} {p['action']:<17} {p['rule']:<24} {p.get('reason', '')[:60]}")

    log = load_json(LOG_PATH) if LOG_PATH.exists() else {"entries": []}
    run_id = datetime.now().isoformat(timespec="seconds") + ("+apply" if args.apply else "+dryrun")
    log["entries"].append({
        "run_id": run_id, "rules_version": RULES_VERSION, "applied": args.apply,
        "summary": dict(acts), "proposals": proposals,
    })
    LOG_PATH.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[ok] 处置日志 {LOG_PATH}（{'APPLY' if args.apply else 'DRY-RUN'}）")

    if not args.apply:
        print("dry-run 未写 KB；确认后 --apply 执行")
        return 0

    # ── KB 写入（分批、写前快照）──────────────────────────────────────
    # P0-B1b：**全部**经唯一身份入口。本脚本不再有任何 papers 直写语句。
    #   身份 -> resolve_or_create_paper（唯一生产者，R1）
    #   内容 -> backfill_paper_fields（受限：只允许 title/abstract/year，R8）
    #   关系 -> register_topic_paper（关系后置于实体，R9）
    to_write = [p for p in proposals if p["action"].startswith("promote")]
    shutil.copy2(KB_DB, KB_DB.with_suffix(".preDisposition.db"))
    con = sqlite3.connect(KB_DB)
    status_map = {"promote": "promoted", "promote_tentative": "promoted_tentative_u"}
    n = n_upgraded = n_conflict = 0
    try:
        con.execute("BEGIN")
        for p in to_write:
            key = p["key"]
            is_w = key.startswith("W")
            doi = p.get("doi") or ""
            ab, ab_src = any_abstract(doi, key if is_w else "")
            # 声明列只是输入；入口按**值形态**决定归属与 uid。
            # key 为 W 形态 -> OPENALEX；否则按值识别（EID / DOI 各自归位）。
            meta = {"title": p.get("title") or "", "abstract": ab, "doi": doi or None}
            if is_w:
                meta["openalex_id"] = key
            else:
                meta["scopus_eid"] = key
            out = pw.resolve_or_create_paper(con, meta, SOURCE)
            if out.status == pw.STATUS_CONFLICT_REVIEW:
                # 该标识被多个 uid 认领 -> 只记录冲突，绝不自动合并（R4）
                print(f"  [conflict] {key}: {out.note}")
                n_conflict += 1
                continue
            if ab:
                res = pw.backfill_paper_fields(con, out.paper_uid, {"abstract": ab}, SOURCE)
                if any(r.applied for r in res):
                    n_upgraded += 1
            pw.register_topic_paper(
                con, TOPIC_ID, out.paper_uid, p["label"], source=SOURCE,
                label_source=("r06_external_audit" if p["rule"].startswith("R1")
                              else RULES_VERSION),
                promotion_status=status_map[p["action"]],
                first_seen_run="r06_disposition",
                evidence={"rule": p["rule"], "reason": p.get("reason"),
                          "abstract_backfilled": bool(ab), "title_level": not bool(ab),
                          **({"abstract_source": ab_src} if ab_src else {})},
                refresh_evidence=(out.status == pw.STATUS_REUSED))
            if out.status == pw.STATUS_CREATED:
                n += 1
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    print(f"[ok] KB 新建 {n} 篇、摘要升级 {n_upgraded} 篇、待人工裁决 {n_conflict} 篇"
          f"（快照 {KB_DB.with_suffix('.preDisposition.db').name}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
