#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/analyze_s7_reward.py — S7 execute reward 实证校准（2026-09-07）

用户 reward 框架（09-07）：R = α·R_new + β·U_new + γ·C_community
                              − λ·Cost − μ·Redundancy
13 EX 组四维真实数据（hits/new/keep_pool/R/U）第一次实证。本工具：
  1) 分量分解表（每 EX 组的各 reward 分量）
  2) 三组候选权重方案（总量偏好 γ↑ / 密度偏好 γ↓ / 平衡）→ 排序对比
     —— reward 不固化：这是给用户审视的实证候选，非冻结公式
关键实证发现（写盘）：
  - 裸 new 是 anti-signal：EX-06 new821 R3 vs EX-05 new37 R14
  - R 与 keep_pool 高度共线（R 论文 ~全在 KEEP 簇）——γ·C_community 与 α·R_new 冗余
  - 密度（R/new）与总量（R）独立两维 → 排序取决于 γ/λ 相对权重
用法：
  .venv\\Scripts\\python.exe tools\\analyze_s7_reward.py
输出：
  data/exports/terminology/s7_reward_analysis.json
"""
import datetime
import json
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T = os.path.join(BASE, "data", "exports", "terminology")
MEM = os.path.join(T, "s7_relation_memory.json")
OUT = os.path.join(T, "s7_reward_analysis.json")


def main():
    mem = json.load(open(MEM, encoding="utf-8"))
    exs = [r for r in mem["relations"]
           if r.get("source") == "s7_execute"
           and r["outcome"].get("new") is not None]
    rows = []
    for c in sorted(exs, key=lambda x: x["id"]):
        o = c["outcome"]
        qa = c.get("community_qa") or {}
        new = o["new"] or 0
        r = o.get("relevant") or 0
        u = o.get("uncertain") or 0
        keep = qa.get("n_keep_pool") or 0
        hits = o.get("hits") or 0
        rows.append({
            "id": c["id"], "domain": c["domain"], "B": c["observables"][0],
            "new": new, "R": r, "U": u, "keep_pool": keep, "hits": hits,
            "density": round(r / new, 4) if new else 0.0,
            "recall_layer": c.get("recall_layer"),
            "status": c["outcome"]["status"],
        })
    # 归一参考：max 值
    mx = {k: max((x[k] for x in rows), default=1) for k in
          ("new", "R", "U", "keep_pool", "hits")}
    # 三组候选权重：α 固定 1（R 主分量）；β=0.5；三方案在 γ/λ
    plans = {
        "balanced_γ1.0": {"alpha": 1.0, "beta": 0.5, "gamma": 1.0,
                          "lambda": 0.15, "mu": 0.5,
                          "note": "γ=1 总量偏好；λ 惩罚 hits 检索成本；"
                                  "μ 惩罚密度（1-density 代理浪费）"},
        "density_pref_γ0.2": {"alpha": 1.0, "beta": 0.5, "gamma": 0.2,
                              "lambda": 0.3, "mu": 1.0,
                              "note": "γ 低 + μ 高 → 偏爱高密度窄 query（EX-05 型）"},
        "total_pref_γ2.0": {"alpha": 1.0, "beta": 0.5, "gamma": 2.0,
                            "lambda": 0.05, "mu": 0.0,
                            "note": "γ 高 + 无密度惩罚 → 偏爱大池（EX-04 型）"},
    }
    for pname, w in plans.items():
        for x in rows:
            score = (w["alpha"] * x["R"] / mx["R"]
                     + w["beta"] * x["U"] / mx["U"]
                     + w["gamma"] * x["keep_pool"] / mx["keep_pool"]
                     - w["lambda"] * x["hits"] / mx["hits"]
                     - w["mu"] * (1 - x["density"]))
            x.setdefault("_scores", {})[pname] = round(score, 3)
    ranked = {pname: sorted(rows, key=lambda x: -x["_scores"][pname])
              for pname in plans}

    now = datetime.datetime.now().isoformat(timespec="seconds")
    out = {
        "role": "S7 execute reward 实证校准（候选权重，非冻结）",
        "built_at": now,
        "empirical_findings": [
            "裸 new 是 anti-signal：EX-06 new=821 R=3 vs EX-05 new=37 R=14——"
            "检索量大的宽 query 不产生 reward，α 分量应锚 R 非 new",
            "R 论文 ~全在 KEEP 簇（EX-04 R84/keep366）：γ·C_community 与 α·R_new "
            "高度共线——γ 实际调节的是总量 vs 密度偏好而非独立维度",
            "密度（R/new）与总量（R）独立：EX-05 38% vs EX-04 16%——排序结果对 "
            "γ/λ/μ 权重敏感，超参选择 = 探索目标声明（扩总量 or 保密度）",
            "ZERO_HIT/NO_NEW/ABANDONED 组（EX-01/02/10/11/13）reward=0 且消耗成本——"
            "应作为硬 deny 而非 reward 负项（已在 memory 层落地）",
        ],
        "component_max": mx,
        "weight_plans": plans,
        "rows": rows,
        "rankings": ranked,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("=" * 100)
    print("S7 execute reward 实证校准（三组候选权重排序对比）")
    print("=" * 100)
    hdr = f"{'EX':<7}{'domain×B':<30}{'R':>4}{'U':>3}{'keep':>5}{'hits':>5}" + \
          "".join(f"{p.split('_')[0]:>14}" for p in plans)
    print(hdr)
    by_id = {x["id"]: x for x in rows}
    for xid in sorted(by_id):
        x = by_id[xid]
        sc = "".join(f"{x['_scores'][p]:>14.2f}" for p in plans)
        print(f"{x['id']:<7}{x['domain']+'×'+x['B'][:22]:<30}{x['R']:>4}"
              f"{x['U']:>3}{x['keep_pool']:>5}{x['hits']:>5}{sc}")
    print("\n排名（reward 高→低）:")
    for pname in plans:
        rk = ranked[pname]
        print(f"  {pname:<18} " + " > ".join(x["id"] for x in rk[:6])
              + (" ..." if len(rk) > 6 else ""))
    print("\n实证发现与权重说明见 s7_reward_analysis.json")
    print(f"[OK] {OUT}")


if __name__ == "__main__":
    main()
