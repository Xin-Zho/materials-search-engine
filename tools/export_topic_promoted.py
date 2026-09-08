"""导出主题论文表（promoted papers）为 CSV，供直接查看/Excel。

全局 KB 多主题视图的第一步：把一个主题 catalog（如 s8_finalkb_catalog.json）
导出为两个可读文件：

    {out_dir}/{topic}_all_promoted.csv      R + U（全部进入收录流程的论文）
    {out_dir}/{topic}_relevant_only.csv     R（strict relevant，不含 UNCERTAIN）

字段以 catalog 内真实存在的数据为准，不伪造缺失列：
  - year / source_query 在 v1.0 冻结产物中未落盘 → 表头保留但留空，
    追溯路径在 CSV 同级 _README.txt 中说明。

用法:
    python tools/export_topic_promoted.py \
        --catalog data/exports/terminology/s8_finalkb_catalog.json \
        --topic photopolymerization_shrinkage
"""

import argparse
import csv
import json
import os
import re
import sqlite3
import sys

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


def load_kb_dois(db_path):
    """KB 中已完成抽取的论文 doi 集合（record_json.doi 通道）。"""
    if not db_path or not os.path.exists(db_path):
        return set()
    con = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    out = set()
    try:
        for (rj,) in con.execute("SELECT record_json FROM knowledge_records"):
            try:
                r = json.loads(rj)
            except Exception:
                continue
            d = norm_doi(r.get("doi"))
            if d:
                out.add(d)
    finally:
        con.close()
    return out

# 输出列（顺序即展示顺序）。缺失字段值一律空字符串。
COLUMNS = [
    "title", "doi", "topic", "label", "label_source",
    "source_stage", "community_cluster", "ex_sources",
    "has_abstract", "abstract_src", "extraction_status", "kb_status",
    "abstract",
]

TRACE_NOTES = """字段说明（v1.0 冻结产物真实字段，缺失列不伪造）:
- label:        RELEVANT(R) / UNCERTAIN(U)。relevant_only.csv 只含 R。
- label_source: QA 出处（blind paper-level QA / community verdict 等）。
- source_stage: 收录来源阶段（S6 / S7 / S7_EX12 等，见 s8 catalog）。
- community_cluster / ex_sources: S7 community 簇与 EX 组（evidence 字段）。
- has_abstract / abstract_src: 摘要可用性与来源（keep_pool / openalex 等）。
- extraction_status: EXTRACTED=已入 KB 并完成 2.0-edges 抽取；PENDING=待抽取。
- kb_status: new=本轮新收录；already_known=先前已在 KB。

v1.0 未落盘、故此处留空的列:
- year:             publish year 未存入 v1.0 冻结数据（catalog/keep_pool/
                    candidate_set 均无）。v2 全局 papers schema 将收录。
- source_query:     具体检索 query 文本未随 catalog 落盘。追溯:
                    S6 -> data/exports/terminology/s6_bridge_queries.json
                    S7 -> data/exports/terminology/s7_execute_query_records.json
                    （按 key/EID join）
"""


def main():
    ap = argparse.ArgumentParser(description="导出主题 promoted 论文表 CSV")
    ap.add_argument("--catalog", required=True,
                    help="主题 catalog JSON（如 data/exports/terminology/s8_finalkb_catalog.json）")
    ap.add_argument("--topic", required=True, help="topic_id（如 photopolymerization_shrinkage）")
    ap.add_argument("--out-dir", default="data/exports/promoted",
                    help="输出目录（默认 data/exports/promoted）")
    ap.add_argument("--kb", default="data/cache/knowledge_base.db",
                    help="KB 数据库路径（只读；用于以 DB 真值回填 extraction_status，"
                         "缺省则用 catalog 原值）")
    args = ap.parse_args()

    d = json.load(open(args.catalog, encoding="utf-8"))
    papers = d.get("papers") or d.get("records") or d.get("items") or []
    if not papers:
        print(f"[ERROR] catalog 无 papers 数组: {args.catalog}")
        sys.exit(1)

    kb_dois = load_kb_dois(args.kb) if args.kb else set()
    if kb_dois:
        print(f"[KB] {len(kb_dois)} papers in KB (extraction truth source)")
    else:
        print("[KB] 未读到 DB 或 DB 为空 —— extraction_status 用 catalog 原值")

    os.makedirs(args.out_dir, exist_ok=True)
    all_path = os.path.join(args.out_dir, f"{args.topic}_all_promoted.csv")
    rel_path = os.path.join(args.out_dir, f"{args.topic}_relevant_only.csv")

    def row(p):
        ev = p.get("evidence") or {}
        cluster = ev.get("cluster_id") or ""
        exs = ev.get("ex_sources") or []
        doi = (p.get("doi") or "").strip()
        # extraction 真值回填：KB 中有该 doi 的抽取记录 = EXTRACTED
        est = p.get("extraction_status") or ""
        if kb_dois and norm_doi(doi) in kb_dois:
            est = "EXTRACTED"
        elif kb_dois:
            est = "PENDING"
        return {
            "title": (p.get("title") or "").strip(),
            "doi": doi,
            "topic": args.topic,
            "label": p.get("label") or "",
            "label_source": p.get("label_source") or "",
            "source_stage": p.get("source") or "",
            "community_cluster": cluster,
            "ex_sources": ",".join(exs) if isinstance(exs, list) else str(exs or ""),
            "has_abstract": "Y" if (p.get("abstract") or "").strip() else "N",
            "abstract_src": p.get("abstract_src") or "",
            "extraction_status": est,
            "kb_status": p.get("kb_status") or "",
            "abstract": (p.get("abstract") or "").strip(),
        }

    all_rows = [row(p) for p in papers]
    rel_rows = [r for r in all_rows if r["label"] == "RELEVANT"]

    def write_csv(path, rows):
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)
        print(f"[OK] {path}  ({len(rows)} rows)")

    write_csv(all_path, all_rows)
    write_csv(rel_path, rel_rows)

    # 同目录说明文件（列语义 + 追溯路径）
    readme = os.path.join(args.out_dir, f"{args.topic}_README.txt")
    with open(readme, "w", encoding="utf-8") as f:
        f.write(f"topic: {args.topic}\n")
        f.write(f"catalog: {args.catalog}\n")
        f.write(f"all_promoted: {len(all_rows)}  (R {sum(1 for r in all_rows if r['label']=='RELEVANT')} + U {sum(1 for r in all_rows if r['label']=='UNCERTAIN')})\n")
        f.write(f"relevant_only: {len(rel_rows)}\n\n")
        f.write(TRACE_NOTES)
    print(f"[OK] {readme}")

    # 汇总
    from collections import Counter
    print(f"\ntopic={args.topic}  all={len(all_rows)}  relevant={len(rel_rows)}")
    print("label:", dict(Counter(r["label"] for r in all_rows)))
    print("stage:", dict(Counter(r["source_stage"] for r in all_rows)))
    print("extraction:", dict(Counter(r["extraction_status"] for r in all_rows)))
    print("has_abstract:", dict(Counter(r["has_abstract"] for r in all_rows)))


if __name__ == "__main__":
    main()
