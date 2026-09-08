"""Standard Topic Run 论文表导出（普通模式收尾，topic 无关）。

输入:
  --records <pilot_query_records.json>   默认 topics/<topic>/runs/pilot_query_records.json
  --labels  <qa_labels.json>             QA 输出（{key: RELEVANT|UNCERTAIN|IRRELEVANT} 或 {"labels":{...}}）
                                         labels 缺失 → 全标 PENDING（先出检索全表）
输出（默认 data/exports/promoted/<topic>/ 或 --out-dir）:
  <topic>_all_promoted.csv    检索去重全表（含 label 列，未判为 PENDING）
  <topic>_relevant_only.csv   RELEVANT 子集
  <topic>_uncertain_only.csv  UNCERTAIN 子集（R1 缓收池）
字段对齐 pc001 promoted CSV 前序约定: title,doi,topic,label,label_source,source_stage,...,
普通模式 source_stage='S6_PILOT_R1'（首轮 live），label_source 记 QA labels 文件 + 判定说明。

用法:
  python tools/export_standard_run_papers.py --topic thermochromic_materials \
      --labels topics/thermochromic_materials/runs/s6_qa_labels.json
"""
import argparse
import csv
import json
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser(description="Standard Topic Run 论文表导出")
    ap.add_argument("--topic", required=True)
    ap.add_argument("--records", default=None)
    ap.add_argument("--labels", default=None, help="QA labels json（可缺省 → 全 PENDING）")
    ap.add_argument("--corpus", default=None,
                    help="qa_corpus.json（可选）：pilot rows 不存 abstract，"
                         "从 corpus 按 key 补 abstract（corpus 含 122/150 篇）")
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()

    runs = REPO / "topics" / args.topic / "runs"
    rec_path = Path(args.records) if args.records else (runs / "pilot_query_records.json")
    rec = _load(rec_path)
    rbq = rec["records_by_query"]

    # ── corpus abstract 补充表（key → abstract）──
    corpus_abs = {}
    if args.corpus:
        corp = _load(args.corpus)
        items = corp.get("candidates") or corp.get("papers") or []
        for it in items:
            k = it.get("key") or it.get("id")
            if k and it.get("abstract"):
                corpus_abs[k] = it["abstract"]
        print(f"corpus abstract 表: {len(corpus_abs)} 篇")

    # ── 检索 union（key 去重，含来源 query/family/abstract）──
    papers = {}
    for aid, w in rbq.items():
        for r in w.get("rows", []):
            k = r.get("key")
            if not k:
                continue
            p = papers.setdefault(k, {"key": k, "doi": r.get("doi") or "",
                                      "eid": r.get("eid") or "",
                                      "title": r.get("title") or "",
                                      "abstract": r.get("abstract") or "",
                                      "sources": [], "families": set()})
            if aid not in p["sources"]:
                p["sources"].append(aid)
            p["families"].add(w.get("family", "?"))
    # corpus 补 abstract（pilot rows 不存 abstract；遍历 union 全量补）
    for p in papers.values():
        if not p["abstract"] and p["key"] in corpus_abs:
            p["abstract"] = corpus_abs[p["key"]]
    for p in papers.values():
        p["families"] = sorted(p["families"])
    print(f"检索去重 union = {len(papers)} 篇（{len(rbq)} queries）")

    # ── labels 合并 ──
    labels = {}
    if args.labels:
        lab = _load(args.labels)
        if "labels" in lab and isinstance(lab["labels"], dict):
            labels = lab["labels"]
        else:
            labels = lab
        n_lab = sum(1 for v in labels.values() if v in ("RELEVANT", "UNCERTAIN", "IRRELEVANT"))
        print(f"QA labels = {len(labels)}（有效 {n_lab}）")

    out_dir = Path(args.out_dir) if args.out_dir else (
        REPO / "data" / "exports" / "promoted")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir = out_dir / args.topic
    out_dir.mkdir(parents=True, exist_ok=True)

    cols = ["title", "doi", "topic", "label", "label_source", "source_stage",
            "source_queries", "family_mix", "has_abstract", "abstract"]
    label_src = (os.path.basename(args.labels) if args.labels
                 else "PENDING_NO_QA")

    def row(p):
        lbl = labels.get(p["key"], "PENDING")
        return [p["title"], p["doi"], args.topic, lbl, label_src,
                "S6_PILOT_R1", ";".join(p["sources"]), "+".join(p["families"]),
                "Y" if p["abstract"] else "N", p["abstract"]]

    all_rows = [row(p) for p in papers.values()]
    rel = [r for r in all_rows if r[3] == "RELEVANT"]
    unc = [r for r in all_rows if r[3] == "UNCERTAIN"]

    def write(name, rows):
        path = out_dir / name
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows(rows)
        print(f"[out] {path} ({len(rows)})")
        return path

    p_all = write(f"{args.topic}_all_promoted.csv", all_rows)
    p_rel = write(f"{args.topic}_relevant_only.csv", rel)
    p_unc = write(f"{args.topic}_uncertain_only.csv", unc)

    # 汇总 json（yield 快照：label 分布）
    from collections import Counter
    cnt = Counter(r[3] for r in all_rows)
    summary = {
        "topic": args.topic, "source_stage": "S6_PILOT_R1",
        "n_queries": len(rbq), "n_papers_union": len(papers),
        "label_distribution": dict(cnt),
        "files": [str(p_all), str(p_rel), str(p_unc)],
    }
    (out_dir / f"{args.topic}_standard_run_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[summary] {summary['label_distribution']}")


if __name__ == "__main__":
    main()
