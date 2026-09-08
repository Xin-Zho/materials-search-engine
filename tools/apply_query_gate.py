"""Query safety gate：generic A-term + topic umbrella → Precision 通道（topic 无关，资产驱动）。

输入:   --gate   <topics/<id>/query_gate.json>    gate 决策资产（specificity 表 + umbrella）
        --actions <p1b1_round1_actions.json>     可选；默认按 topic 路由 runs/p1b1_round1_actions.json
输出:   topics/<topic_id>/runs/p1b1_round1_gated_actions.json
        （36 条：discovery 保留原 query；precision 包 umbrella；每条带 gate_class/original_query_string）

判定树:
  对每 action: A = terms[0]
    A ∈ specificity[specific]  → gate_class=discovery（anchor-free，query 原样）
    A ∈ specificity[generic]   → gate_class=precision（query = umbrella_fragment AND (A AND B)）
    A 未标注                   → default_class=generic（安全侧）
  产物校验: n 不变、query 互异、precision 的 query 必含 umbrella fragment、discovery 与原 query 相等。
  pc001 无 gate 资产 → 工具不适用（frozen 17 不经此工具；run_s6_pilot 用原 config 行为等价）。

用法:
  python tools/apply_query_gate.py --gate topics/thermochromic_materials/query_gate.json
"""
import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def main():
    ap = argparse.ArgumentParser(description="Query safety gate（generic A + umbrella → precision）")
    ap.add_argument("--gate", required=True, help="query_gate 决策资产 JSON")
    ap.add_argument("--actions", default=None, help="cut 产物 actions（默认 runs/p1b1_round1_actions.json）")
    ap.add_argument("--out", default=None, help="输出路径（默认 runs/p1b1_round1_gated_actions.json）")
    args = ap.parse_args()

    gate_path = Path(args.gate)
    if not gate_path.is_absolute():
        gate_path = REPO / gate_path
    gate = json.load(open(gate_path, encoding="utf-8"))
    topic_id = gate["topic_id"]
    runs = REPO / "topics" / topic_id / "runs"
    acts_path = Path(args.actions) if args.actions else (runs / "p1b1_round1_actions.json")
    acts = json.load(open(acts_path, encoding="utf-8"))
    actions = acts["actions"]

    spec = {s["term"]: s["class"] for s in gate["specificity"]}
    umb = gate["umbrella"]["scopus_fragment"]
    default = gate["default_class"]
    # term class（资产语义）→ query 通道（产物语义）
    CHANNEL = {"specific": "discovery", "generic": "precision"}
    assert default in CHANNEL, f"default_class 未知: {default}"
    assert set(spec.values()) <= set(CHANNEL), f"specificity class 含未知值: {set(spec.values()) - set(CHANNEL)}"
    unknown = sorted({a["terms"][0] for a in actions} - set(spec))
    if unknown:
        print(f"[warn] A-term 未在 specificity 表（default={default}）: {unknown}")

    out_actions = []
    for a in actions:
        A = a["terms"][0]
        cls = spec.get(A, default)
        channel = CHANNEL[cls]
        rec = dict(a)
        rec["gate_class"] = channel
        rec["term_class"] = cls
        rec["original_query_string"] = a["query_string"]
        if channel == "precision":
            # TITLE-ABS-KEY((umbrella) AND (A) AND (B))
            inner = a["query_string"]
            assert inner.startswith("TITLE-ABS-KEY(") and inner.endswith(")")
            body = inner[len("TITLE-ABS-KEY("):-1]
            rec["query_string"] = f"TITLE-ABS-KEY({umb} AND {body})"
        else:
            rec["query_string"] = a["query_string"]
        out_actions.append(rec)

    # 校验
    from collections import Counter
    cc = Counter(r["gate_class"] for r in out_actions)
    assert len(out_actions) == len(actions), "action 数漂移"
    qs = [r["query_string"] for r in out_actions]
    assert len(set(qs)) == len(qs), "gated query 存在重复"
    for r in out_actions:
        if r["gate_class"] == "precision":
            assert umb.split()[0].strip('(').strip('"') in r["query_string"], \
                f"precision query 缺 umbrella: {r['action_id']}"
        else:
            assert r["query_string"] == r["original_query_string"], \
                f"discovery query 被改动: {r['action_id']}"

    # 顶层兼容字段（run_s6_pilot 读取：family_counts/anchor_free_actions/exploratory_actions）
    from collections import Counter as C2
    fam_cnt = C2(a["family"] for a in actions)
    out = {
        "version": "p1b1_round1_gated_actions_v1",
        "topic_id": topic_id,
        "gate_asset": gate["version"],
        "source": acts["version"],
        "n": len(out_actions),
        "family_counts": dict(fam_cnt),
        "anchor_free_actions": sum(1 for a in actions if not a.get("contains_shrinkage_anchor", False)),
        "exploratory_actions": sum(1 for a in actions if a.get("exploratory", False)),
        "gate_class_counts": dict(cc),
        "umbrella": gate["umbrella"]["or_terms"],
        "actions": out_actions,
    }
    out_path = Path(args.out) if args.out else (runs / "p1b1_round1_gated_actions.json")
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[ok] gated {len(out_actions)} | discovery={cc.get('discovery',0)} precision={cc.get('precision',0)}")
    print(f"[out] {out_path}")


if __name__ == "__main__":
    main()
