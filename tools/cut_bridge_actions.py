"""P1-B1 Round-1 action 裁剪工具（deterministic，零 API/LLM）。

输入:   --asset  <topics/<id>/p1b1_round1_cut.json>   裁剪决策资产（簇归并表，人工审）
        --actions <bridge_queries.json>              可选；默认按 topic 路由 runs/bridge_queries.json
输出:   topics/<topic_id>/runs/p1b1_round1_actions.json   保留 actions（S6 pilot live 输入）
        topics/<topic_id>/runs/p1b1_round1_cut_report.json 裁剪报告（removed 清单 + 理由 + 统计）

判定树（与资产 rules_summary 对应，可审计）:
  L0  全部 actions 读入（135）
  L1  结构去重: query_string 唯一（源应已唯一；若有重复取首个并记录）
  L2  D family → 全保留（keep_all_D）
  L3  C family → 按簇归并:
        A-term 属某簇 representative → 保留其 B∈keep_bs 的 action（≤2）
        A-term 是 member 非 rep      → removed[cluster_dup]
        A-term 属 rep=null 的簇       → removed[cluster_deprioritized]
        rep 但 B∉keep_bs             → removed[b_capped]（进半保留池）
  L4  A(lex) family → 仅 lex_keeps 的 term 且 B∈keep_bs 保留；其余 removed[lex_dropped]/[b_capped]
  校验: 保留数 == asset.expected_counts；保留 query 互异；保留均源于源 actions

用法:
  python tools/cut_bridge_actions.py --asset topics/thermochromic_materials/p1b1_round1_cut.json
"""
import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def resolve_runs_dir(topic_id: str) -> Path:
    tcfg = REPO / "topics" / topic_id / "topic.yaml"
    # 轻量解析（不引 topic_config 依赖，读 asset 所在 topic 的约定 runs/）
    runs = REPO / "topics" / topic_id / "runs"
    if not runs.exists():
        raise SystemExit(f"[err] runs 目录不存在: {runs}")
    return runs


def main():
    ap = argparse.ArgumentParser(description="P1-B1 Round-1 action 裁剪（deterministic）")
    ap.add_argument("--asset", required=True, help="裁剪决策资产 JSON（topics/<id>/p1b1_round1_cut.json）")
    ap.add_argument("--actions", default=None, help="bridge_queries.json 路径（默认按 topic 路由）")
    ap.add_argument("--out-dir", default=None, help="输出目录（默认 topics/<id>/runs/）")
    args = ap.parse_args()

    asset_path = Path(args.asset)
    if not asset_path.is_absolute():
        asset_path = REPO / asset_path
    asset = json.load(open(asset_path, encoding="utf-8"))
    topic_id = asset["topic_id"]
    runs = Path(args.out_dir) if args.out_dir else resolve_runs_dir(topic_id)

    actions_path = Path(args.actions) if args.actions else (runs / "bridge_queries.json")
    bq = json.load(open(actions_path, encoding="utf-8"))
    actions = bq["actions"]
    src_n = len(actions)
    print(f"[info] topic={topic_id} source_actions={src_n} asset={asset['version']}")

    # ── L1 结构去重 ──
    seen_qs, dedup = set(), []
    dup_records = []
    for a in actions:
        if a["query_string"] in seen_qs:
            dup_records.append(a["action_id"])
            continue
        seen_qs.add(a["query_string"])
        dedup.append(a)
    if dup_records:
        print(f"[warn] L1 结构去重移除重复 query: {dup_records}")

    # 索引: (family, A-term) -> actions
    def a_of(a):
        return a["terms"][0]

    # 簇查找表: A-term -> cluster 元数据（rep 自身也计入；null 簇 rep_of=None）
    rep_of, keep_bs_of_rep = {}, {}
    for cl in asset["clusters"]:
        rep = cl["representative"]
        keep_bs_of_rep[rep] = set(cl["keep_bs"]) if rep else None
        for m in cl["members"]:
            rep_of[m] = rep
    lex_keep = {lk["term"]: set(lk["keep_bs"]) for lk in asset["lex_keeps"]}

    keep, removed = [], []  # removed: {action_id, query_string, reason, pool}

    for a in dedup:
        fam, term = a["family"], a_of(a)
        bs = a["terms"][1]
        if fam == "D" and asset["keep_all_D"]:
            keep.append(a)
        elif fam == "C":
            if term in keep_bs_of_rep and keep_bs_of_rep[term] is not None:
                # representative 词
                if bs in keep_bs_of_rep[term]:
                    keep.append(a)
                else:
                    removed.append({**a, "cut_reason": "b_capped",
                                    "cut_detail": "B 不在 keep_bs（半保留池）"})
            elif term in rep_of:
                rep = rep_of[term]
                if rep is None:
                    removed.append({**a, "cut_reason": "cluster_deprioritized",
                                    "cut_detail": "整簇延后（precision 风险），Round 2 视 yield 再补"})
                elif term == rep:
                    # rep 词但 keep_bs_of_rep[rep] 为空（rep 无 B 应只在 null 簇；防御）
                    removed.append({**a, "cut_reason": "unknown_term",
                                    "cut_detail": "rep 但无 keep_bs（检查资产）"})
                else:
                    removed.append({**a, "cut_reason": "cluster_dup",
                                    "cut_detail": f"同义簇 member，代表={rep}"})
            else:
                removed.append({**a, "cut_reason": "unknown_term", "cut_detail": "不在簇表（需检查）"})
        elif fam == "A":
            if term in lex_keep:
                if bs in lex_keep[term]:
                    keep.append(a)
                else:
                    removed.append({**a, "cut_reason": "b_capped",
                                    "cut_detail": "lex 锚 B 不在 keep_bs（半保留池）"})
            else:
                removed.append({**a, "cut_reason": "lex_dropped",
                                "cut_detail": "体系锚未保留（检索面被保留锚覆盖）"})
        else:
            removed.append({**a, "cut_reason": "unknown_family", "cut_detail": fam})

    # 校验
    from collections import Counter
    fam_cnt = Counter(k["family"] for k in keep)
    exp = asset["expected_counts"]
    assert len(keep) == exp["total"], \
        f"保留数 != {exp['total']}（实测 {len(keep)}）：C={fam_cnt['C']} A={fam_cnt['A']} D={fam_cnt['D']}"
    assert fam_cnt["C"] == exp["C"] and fam_cnt["A"] == exp["A_lex"] and fam_cnt["D"] == exp["D"], \
        f"family 分布漂移: {dict(fam_cnt)} != {exp}"
    qs = [k["query_string"] for k in keep]
    assert len(set(qs)) == len(qs), "保留 query 存在重复"
    src_ids = {a["action_id"] for a in actions}
    assert all(k["action_id"] in src_ids for k in keep), "保留 action 不在源 actions 中"

    # 输出
    out_acts = {
        "version": "p1b1_round1_actions_v1",
        "topic_id": topic_id,
        "cut_asset": asset["version"],
        "source_actions": src_n,
        "created_at": "2026-09-08",   # deterministic（非时间戳）
        "n": len(keep),
        "family_counts": dict(fam_cnt),
        "actions": keep,
    }
    removed.sort(key=lambda x: (x["family"], x["action_id"]))
    from collections import Counter as C2
    reason_cnt = C2(r["cut_reason"] for r in removed)
    report = {
        "version": "p1b1_round1_cut_report_v1",
        "topic_id": topic_id,
        "cut_asset": asset["version"],
        "source_actions": src_n,
        "n_keep": len(keep),
        "n_removed": len(removed),
        "removed_by_reason": dict(reason_cnt),
        "keep_by_family": dict(fam_cnt),
        "half_pool_note": "removed 全部可追溯（round1 不删除源，仅不进 live 输入）；"
                          "Round 2 按 yield 选择性启用",
        "removed": removed,
    }
    (runs / "p1b1_round1_actions.json").write_text(
        json.dumps(out_acts, ensure_ascii=False, indent=1), encoding="utf-8")
    (runs / "p1b1_round1_cut_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[ok] keep={len(keep)} C={fam_cnt['C']} A={fam_cnt['A']} D={fam_cnt['D']} | "
          f"removed={len(removed)} {dict(reason_cnt)}")
    print(f"[out] {(runs / 'p1b1_round1_actions.json')}")
    print(f"[out] {(runs / 'p1b1_round1_cut_report.json')}")


if __name__ == "__main__":
    main()
