#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/freeze_s6_trajectory.py — S6 pilot 运行结果冻结（2026-09-07 用户拍板 P0）。

冻结语义（轻量 freeze，不跑 R06）：
  把 S6 pilot 从"development 探索"提升为可引用版本，产出后续 S7 Query Planner /
  RL 的训练轨迹数据。不改变任何 query 层文件（s6_freeze_manifest.json 锚定文件
  只读校验，禁止修改）。

产物（data/exports/terminology/）：
  1. s6_seen_set.json           S6_SEEN = S5_SEEN(20417) ∪ pilot union new(1365)
  2. s6_candidate_labels.json   1365 new 论文 label（RUBRIC_V1 / DeepSeek blind）
                                + 多标签 source families/queries
  3. s6_trajectory.json         per-query 训练轨迹：raw features + 标签派生列
                                （reward 公式草案写入 note，参数 α/β/γ/δ/λ 留给
                                S7 v1 定参，不在 freeze 固化——防开发集过拟合）
  4. s6_trajectory_freeze_manifest.json   sha256 锚定以上产物 + 输入文件 + 校验
                                原 s6_freeze_manifest.json（query 层未漂移证明）

trajectory reward 公式（草案，参数待 S7 v1）：
  reward = α·(R + λ·U) + β·novelty − γ·redundancy − δ·query_cost
  novelty = 1 − Jaccard(query_i, existing)  ；此处以 pairwise 结果 overlap 度量
  query_cost ≈ Scopus total_hits（下载量代理）
"""
import argparse
import hashlib
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T = os.path.join(BASE, "data", "exports", "terminology")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load(name):
    return json.load(open(os.path.join(T, name), encoding="utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=T)
    ap.add_argument("--plan-only", action="store_true")
    args = ap.parse_args()
    out_dir = args.out_dir

    # ── 输入文件 ──
    f_actions = "s6_bridge_queries.json"          # 17 query actions（已 freeze）
    f_records = "s6_pilot_query_records.json"     # pilot 运行记录（hits/new/rows）
    f_labels = "s6_qa_labels.json"                # 1365 blind labels
    f_corpus = "s6_qa_corpus.json"                # 论文元数据 + 多标签
    f_s5seen = "s5_seen_set.json"                 # base seen
    f_freeze1 = "s6_freeze_manifest.json"         # 原三文件锚（词源/query/assembler）

    actions = load(f_actions)
    records = load(f_records)
    labels_doc = load(f_labels)
    labels = labels_doc.get("labels", labels_doc)
    corpus = load(f_corpus)
    corpus_rows = corpus.get("papers") or corpus.get("rows")
    s5 = load(f_s5seen)
    freeze1 = load(f_freeze1)

    # ── 校验 1：原 query 层 manifest 未漂移（files 为 list of {path, sha256}）──
    print("[check] 原 s6_freeze_manifest.json 锚定文件校验…")
    for item in freeze1.get("files", []):
        rel = item["path"].replace("\\", "/")
        # path 形如 data/exports/... 或 tools/...
        if rel.startswith("data/"):
            p = os.path.join(BASE, rel)
        elif rel.startswith("tools/"):
            p = os.path.join(BASE, rel)
        else:
            p = os.path.join(out_dir, rel)
        if not os.path.exists(p):
            print(f"  [FAIL] {rel} 缺失"); sys.exit(1)
        ok = sha256(p) == item.get("sha256")
        print(f"  {'[OK]' if ok else '[FAIL]'} {rel}")
        if not ok:
            print(f"    expect {item.get('sha256')}\n    actual {sha256(p)}")
            sys.exit(1)

    # ── 校验 2：数据一致性 ──
    acts = actions["actions"]
    rbq = records["records_by_query"]
    assert len(acts) == len(rbq) == 17, f"actions/records 数量不一致"
    for a in acts:
        assert a["action_id"] in rbq, f"{a['action_id']} 缺 pilot records"
    corpus_keys = {r["key"] for r in corpus_rows}
    label_keys = set(labels)
    assert label_keys == corpus_keys, \
        f"labels({len(label_keys)}) ≠ corpus({len(corpus_keys)})"
    s5_keys = set(s5["keys"])
    # corpus = pilot 相对 S5 的 union new（builder 已按 new_vs_S5 过滤，1365）
    # records rows 含 S5 已 seen 论文（1501≠1365），不可直接当 new 用。
    new_keys = set(corpus_keys)
    # per-query new：corpus 多标签反查（rows 无 new 标志，用 queries 字段归属）
    q2keys = {a["action_id"]: {r["key"] for r in corpus_rows
                               if a["action_id"] in r.get("queries", [])}
              for a in acts}
    ov = new_keys & s5_keys
    if ov:
        print(f"  [WARN] corpus new 与 S5 重叠 {len(ov)}（应为 0）")
    seen = s5_keys | new_keys
    print(f"[ok] S5={len(s5_keys)} pilot_new_union={len(new_keys)} "
          f"S6_SEEN={len(seen)}")

    # ── 派生列（per-query）──
    def ru_stats(keys_sub):
        r = sum(1 for k in keys_sub if labels.get(k) == "RELEVANT")
        u = sum(1 for k in keys_sub if labels.get(k) == "UNCERTAIN")
        return r, u

    traj = []
    for a in acts:
        aid = a["action_id"]
        rec = rbq[aid]
        nkeys = q2keys[aid]
        r_cnt, u_cnt = ru_stats(nkeys)
        traj.append({
            "action_id": aid,
            "family": a["family"],
            "domain": a["domain"],
            "strategy": a["strategy"],
            "exploratory": a.get("exploratory", False),
            "contains_shrinkage_anchor": a["contains_shrinkage_anchor"],
            "query_string": a["query_string"],
            "terms": a.get("terms", []),
            "term_source": a.get("term_source"),
            "context_source": a.get("context_source"),
            "features": {
                "scopus_total_hits": rec.get("total_hits", 0),
                "unique_returned": rec.get("unique_returned", 0),
                "usable_returned": rec.get("usable_returned", 0),
                "new_vs_S5": len(nkeys),
                "identity_unknown": rec.get("identity_unknown", 0),
                "depth_saturated": rec.get("depth_saturated", False),
            },
            "labels": {
                "RELEVANT": r_cnt,
                "UNCERTAIN": u_cnt,
                "IRRELEVANT": len(nkeys) - r_cnt - u_cnt,
                "R_plus_U": r_cnt + u_cnt,
            },
            "new_keys": sorted(nkeys),
        })
    # novelty / redundancy 特征：两两 canonical overlap（结果层，非 token）
    total = len(traj)
    for i in range(total):
        ki = set(traj[i]["new_keys"])
        if not ki:
            traj[i]["novelty"] = None
            continue
        ovs = []
        for j in range(total):
            if i == j:
                continue
            kj = set(traj[j]["new_keys"])
            if not kj:
                continue
            jac = len(ki & kj) / len(ki | kj)
            ovs.append(jac)
        traj[i]["novelty"] = {
            "max_pairwise_overlap": round(max(ovs), 4) if ovs else 0.0,
            "mean_pairwise_overlap": round(sum(ovs) / len(ovs), 4) if ovs else 0.0,
        }

    global_stats = {
        "n_actions": len(acts),
        "pilot_union_new": len(new_keys),
        "seen_base_S5": len(s5_keys),
        "seen_S6": len(seen),
        "labels_global": {v: sum(1 for x in labels.values() if x == v)
                          for v in ("RELEVANT", "UNCERTAIN", "IRRELEVANT")},
        "labels_global_R_plus_U": sum(1 for v in labels.values()
                                      if v in ("RELEVANT", "UNCERTAIN")),
    }

    # ── 产物 ──
    import datetime
    now = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    snapshot = f"S6_{now.replace('-', '').replace(':', '').replace('T', '_')[:15]}"

    out_seen = {
        "definition": "SEARCH_S6_SEEN_UNION = S5_SEEN(20417) ∪ S6 pilot union new "
                      "（17 actions, depth=1000, skip_cache；key 为 identity-reconciled "
                      "canonical EID）",
        "frozen_at": now,
        "search_snapshot_id": snapshot,
        "size": len(seen),
        "keys": sorted(seen),
    }
    out_labels = {
        "version": "s6_candidate_labels_v1",
        "frozen_at": now,
        "rubric_version": labels_doc.get("rubric_version"),
        "qa_engine": "DeepSeek blind (RUBRIC_V1), GPT-5.6 Sol calibration gold 70 篇",
        "note": "1365 = S6 pilot 相对 S5 的 union new。多标签 source。R1(operational)="
                "RELEVANT+UNCERTAIN∈FinalKB；R2(strict)=仅 RELEVANT。",
        "by_key": {r["key"]: {
            "label": labels[r["key"]],
            "families": r.get("families", []),
            "queries": r.get("queries", []),
            "title": (r.get("title") or "")[:200],
            "doi": r.get("doi"),
        } for r in corpus_rows},
    }
    out_traj = {
        "version": "s6_trajectory_v1",
        "frozen_at": now,
        "round": records.get("round"),
        "depth": records.get("depth"),
        "status": "FROZEN_DEVELOPMENT_PILOT",
        "reward_formula_draft": {
            "formula": "reward = a*(R + l*U) + b*novelty - g*redundancy - d*cost",
            "components": {"discovery": "R + l*U  (U 系数待定，UNCERTAIN=未来知识)",
                           "novelty": "1 - Jaccard(query, existing)，此处代理=结果层 "
                                      "pairwise overlap（存 max/mean 两列）",
                           "redundancy": "与既有 query 的 canonical 重复（可用 "
                                         "mean_pairwise_overlap 代理）",
                           "cost": "Scopus total_hits / QA 成本代理"},
            "parameters": "alpha/beta/gamma/delta/lambda 未固化——S7 v1 定参训练，"
                          "防在开发集过拟合",
        },
        "actions": traj,
        "global_stats": global_stats,
    }

    if args.plan_only:
        print("\n[plan-only] 产物预览：")
        print(f"  s6_seen_set.json               {len(seen)} keys")
        print(f"  s6_candidate_labels.json       {len(out_labels['by_key'])} keys")
        print(f"  s6_trajectory.json             {len(traj)} actions")
        print(f"  labels_global                  {global_stats['labels_global']}")
        return

    paths = {}
    for name, obj in (("s6_seen_set.json", out_seen),
                      ("s6_candidate_labels.json", out_labels),
                      ("s6_trajectory.json", out_traj)):
        p = os.path.join(out_dir, name)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        paths[name] = sha256(p)
        print(f"[ok] {name}  {paths[name][:16]}…")

    # manifest：锚定产物 + 输入（防后续任何一步被改而不自知）
    manifest = {
        "version": "s6_trajectory_freeze_manifest_v1",
        "frozen_at": now,
        "snapshot_id": snapshot,
        "purpose": "S6 pilot 运行结果冻结（轻量 freeze，R06 推迟至 S7 v1 后合并审计）",
        "reward_formula": out_traj["reward_formula_draft"],
        "input_files": {rel: {"sha256": sha256(os.path.join(out_dir, rel))}
                        for rel in (f_actions, f_records, f_labels, f_corpus,
                                    f_s5seen, f_freeze1)},
        "input_tool": {"freeze_s6_trajectory.py": sha256(
            os.path.join(BASE, "tools", "freeze_s6_trajectory.py"))},
        "products": paths,
    }
    mp = os.path.join(out_dir, "s6_trajectory_freeze_manifest.json")
    with open(mp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    print(f"[ok] s6_trajectory_freeze_manifest.json  {sha256(mp)[:16]}…")


if __name__ == "__main__":
    main()
