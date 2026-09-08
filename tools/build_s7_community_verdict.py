#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/build_s7_community_verdict.py — 簇级 KEEP/FAIL 裁决冻结 + EX 组贡献（2026-09-07）

用户裁决（22:30）：机械 keep_n=2 基础上人工修正——
  KEEP 6 簇：C-010/016/012/015（机械 KEEP 剔 C-017 假簇）+ C-005/011 人工上浮
    （语义直击 dim-accuracy/additive manufacturing，抽样 R>0 且估 R 5~6.6）
  C-017 剔除：no-abstract 论文集（15U 全 title-only missingness 假密度）→ LOW_CONFIDENCE
  UNCERTAIN 3 簇：C-008/014/018（R+U=1，待补抽/精筛）
  FAIL 14 簇：抽样 R+U=0（self-healing/corrosion/wear/barrier/concrete/perovskite…）

输出：
  s7_community_verdict.json —— 簇级裁决冻结（候选论文池分级）
    KEEP 池 6 簇 750 篇 → 候选池（进 KEEP 池后续 relevant 精筛/候选入池）
    FAIL 14 簇 1708 篇 → 整簇丢弃（EX 检索混入的非光固化噪声）
  EX 组贡献表（写盘）—— 每个 EX 组 rows 中落 KEEP 簇的 unique 论文数：
    relation-level retrieval gain（reward 的 coverage/retrieval 分量雏形）
用法：
  .venv\\Scripts\\python.exe tools\\build_s7_community_verdict.py
"""
import argparse
import datetime
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from search_engine.topic_config import DEFAULT_TOPIC, is_legacy_topic  # noqa: E402

T = os.path.join(BASE, "data", "exports", "terminology")
AGG = os.path.join(T, "s7_community_qa_aggregate.json")
MEM = os.path.join(T, "s7_community_memory.json")
REC = os.path.join(T, "s7_execute_query_records.json")
OUT = os.path.join(T, "s7_community_verdict.json")

# 用户人工裁决（2026-09-07 22:30，覆盖机械阈值输出）
HUMAN_OVERRIDES = {
    # cluster_id -> (decision, rationale)
    "C-017": ("LOW_CONFIDENCE",
              "no-abstract 会议论文集（22 篇 abs=0）；15/20 UNCERTAIN 全因 "
              "title-only missingness——RUBRIC missingness 不作 U 理由但 title 无法判定，"
              "75% 为假密度。不入池，登记待补 abstract 后重审"),
    "C-005": ("KEEP", "人工上浮：dental/3dp dimensional accuracy 语义直击用户点名 "
                        "relation（dim-accuracy 522 new）；抽样 1R、估 R5，密度低但"
                        "社区语义确定在 frame 内"),
    "C-011": ("KEEP", "人工上浮：additive manufacturing process 社区（估 R6.6），"
                      "与 EX-03/04 检索面（3dp delamination/dim-accuracy）直接对应"),
    # 以下为 22:30 用户冻结裁决的机械 KEEP（keep_n=2, n=20 R+U=2=10%≥S6 基准）——
    # 防 analyze 阈值参数变动（--topup --keep-n 3 等）回退冻结集
    "C-012": ("KEEP", "用户 22:30 冻结（机械 keep_n=2 KEEP）：coatings surface 社区"
                      "抽样 1R/1U（10% R+U），n=20 达机械阈值，冻结不随阈值参数漂移"),
    "C-015": ("KEEP", "用户 22:30 冻结（机械 keep_n=2 KEEP）：polymer films 社区"
                      "抽样 2U（10% R+U），n=20 达机械阈值，冻结不随阈值参数漂移"),
}

# 补抽二次裁决（2026-09-07 22:55：C-008/014/018 各 +20 → n=40 后收紧）
TOPUP_OVERRIDES = {
    "C-008": ("FAIL", "补抽 n=40：0R/1U（2.5% R+U）——embedded monitoring 社区"
                       "确认为非目标噪声，整簇弃"),
    "C-014": ("UNCERTAIN", "补抽 n=40：1R/3U（10% R+U 但 R 率 2.5%）——packaging "
                           "stress 有微弱信号，R 密度不足入池；109 篇留观待 "
                           "EX-10/11（packaging delamination/void）补跑后 packaging "
                           "专项复核"),
    "C-018": ("FAIL", "补抽 n=40：0R/1U（2.5%）——coatings hybrid 薄膜社区确认为"
                      "非目标噪声，整簇弃"),
}


def main():
    ap = argparse.ArgumentParser(description="S7 community 簇级 KEEP/FAIL 裁决冻结")
    ap.add_argument("--topic", default=None,
                    help="topic_id（默认 v1.0 legacy 主题；非 legacy 禁止——HUMAN_OVERRIDES/"
                         "TOPUP_OVERRIDES 是 pc001 人工裁决冻结，换主题须新裁决）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只裁决不写盘（防误覆盖冻结 verdict）")
    ap.add_argument("--out", default=None, help="输出路径覆盖")
    args = ap.parse_args()
    topic = args.topic or DEFAULT_TOPIC
    if not is_legacy_topic(topic):
        raise SystemExit(
            f"[topic] {topic}: 本脚本内嵌 pc001 人工裁决（HUMAN/TOPUP_OVERRIDES，"
            f"2026-09-07 22:30/22:55 冻结）；换主题须人工新裁决，勿复用。")
    out_path = args.out or OUT

    agg = json.load(open(AGG, encoding="utf-8"))
    mem = json.load(open(MEM, encoding="utf-8"))
    rec = json.load(open(REC, encoding="utf-8"))
    clusters = {c["cluster_id"]: c for c in mem["clusters"]}

    verdicts = {}
    for r in agg["clusters"]:
        cid = r["cluster_id"]
        if cid in TOPUP_OVERRIDES:
            dec, reason = TOPUP_OVERRIDES[cid]
        elif cid in HUMAN_OVERRIDES:
            dec, reason = HUMAN_OVERRIDES[cid]
        else:
            dec = r["decision"]
            reason = (f"QA 抽样 R+U≥2（keep_n=2）" if dec == "KEEP"
                      else ("QA 抽样 R+U=0" if dec == "FAIL"
                            else "QA 抽样 R+U=1，证据不足待补抽"))
        verdicts[cid] = {
            "cluster_id": cid, "decision": dec,
            "reason": reason,
            "size": clusters[cid]["size"],
            "qa": {k: r[k] for k in
                   ("R", "U", "I", "n_sampled", "R_plus_U_rate",
                    "R_plus_U_lo", "est_relevant")},
            "source_override": cid in HUMAN_OVERRIDES or cid in TOPUP_OVERRIDES,
            "override_round": ("topup_2255" if cid in TOPUP_OVERRIDES
                               else ("human_2230" if cid in HUMAN_OVERRIDES
                                      else None)),
        }

    # EX 组贡献：组内 rows → KEEP 簇论文 unique 数
    ex_contrib = {}
    key2cluster = {}
    for c in mem["clusters"]:
        for k in c["papers"]:
            key2cluster[k] = c["cluster_id"]
    for gid, w in rec.get("records_by_group", {}).items():
        keep_keys = set()
        all_keys = set()
        for r in w.get("rows", []):
            k = r.get("key")
            if not k:
                continue
            all_keys.add(k)
            if verdicts.get(key2cluster.get(k, ""), {}).get("decision") == "KEEP":
                keep_keys.add(k)
        ex_contrib[gid] = {
            "n_rows_unique": len(all_keys),
            "n_keep_pool": len(keep_keys),
            "keep_ratio": round(len(keep_keys) / len(all_keys), 3)
            if all_keys else None,
            "recall_layer": w.get("recall_layer"),
        }

    now = datetime.datetime.now().isoformat(timespec="seconds")
    out = {
        "role": "S7 community 簇级裁决冻结（candidate 论文池分级）",
        "decided_at": now,
        "rubric": "S6_QA_RUBRIC_V1",
        "decision_rule": "机械 keep_n=2 + 用户人工裁决 22:30"
                         "（KEEP 6 簇：C-010/016/012/015 机械 + C-005/011 上浮；"
                         "C-017 LOW_CONFIDENCE）"
                         " + 补抽二次裁决 22:55"
                         "（C-008/018 FAIL 确认、C-014 留观待 packaging 专项）",
        "summary": {},
        "clusters": verdicts,
        "ex_group_keep_contribution": ex_contrib,
        "pool_sizes": {},
    }
    for dec in ("KEEP", "UNCERTAIN", "FAIL", "LOW_CONFIDENCE"):
        ids = [c for c, v in verdicts.items() if v["decision"] == dec]
        n_papers = sum(verdicts[c]["size"] for c in ids)
        out["summary"][dec] = {"n_clusters": len(ids), "ids": ids,
                               "n_papers": n_papers}
        out["pool_sizes"][dec] = n_papers
    if args.dry_run:
        print("[dry-run] 未写盘；裁决概览见上。")
        return
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print("=" * 90)
    print("S7 community 簇级裁决（用户 22:30 拍板）")
    print("=" * 90)
    for dec in ("KEEP", "UNCERTAIN", "FAIL", "LOW_CONFIDENCE"):
        s = out["summary"][dec]
        print(f"  {dec:<14} {s['n_clusters']} 簇 / {s['n_papers']} 篇  "
              f"{s['ids']}")
    print("\nEX 组 → KEEP 池贡献（relation retrieval gain）:")
    for gid, v in sorted(ex_contrib.items()):
        ratio = (f"{v['keep_ratio']:.0%}" if v["keep_ratio"] is not None
                 else "  n/a")
        print(f"  {gid} [{v['recall_layer']}]  {v['n_keep_pool']:>4}/"
              f"{v['n_rows_unique']:<4} KEEP（{ratio}）")
    print(f"\n[OK] verdict: {out_path}")
    print("→ KEEP 池 6 簇论文 = S7 execute 候选池（~750 篇）")
    print("→ 回灌：EX community outcome 加 community_qa 字段（builder 读 verdict）")


if __name__ == "__main__":
    main()
