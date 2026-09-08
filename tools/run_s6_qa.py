#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/run_s6_qa.py — S6 pilot candidate QA：blind LLM 三态判定（2026-09-05 用户定方案 3→1）。

流程（用户拍板）：
  1) 分层校准集（~70）→ 人工 gold {key: RELEVANT|UNCERTAIN|IRRELEVANT}
  2) --mode run 对校准集跑 LLM（blind）→ --mode compare 出混淆矩阵/误判模式
     重点：FN_{R→I}（人工 gold RELEVANT 被 LLM 判 IRRELEVANT）与
     U_{rate|GoldR}（人工 RELEVANT 被 LLM 判 UNCERTAIN）；--meta 时按 abstract 有无分列。
  3) rubric 冻结后 --mode run 全量 1365（corpus）→ s6_qa_labels.json
     （analyze_s6_pilot.py --labels 直接消费 {key: label}）

blind 纪律（写死）：
  - prompt 只含 title + abstract + rubric；绝不注入 query/family/domain/source/stratum
    （防 LLM 知道"A 是新策略"后产生确认偏差）
  - 一篇一判（canonical key），全局 labels 文件 {key: label}

rubric 冻结：RUBRIC_V1 为模块常量；产物记录 rubric_version。校准改动 → RUBRIC_V2（改常量，
不改历史产物；防 R06 归因漂移）。

用法：
  python tools/run_s6_qa.py --mode run --set <calib_or_corpus.json> --llm-out <labels.json> \
        [--batch 5] [--provider deepseek] [--model deepseek-chat] [--api-key sk-...]
  python tools/run_s6_qa.py --mode compare --gold <gold.json> --llm-out <llm_labels.json> \
        [--out report.json]
"""
import argparse
import asyncio
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tools"))
sys.path.insert(0, os.path.join(BASE, "search_engine"))

from llm_term_expander import parse_llm_json  # noqa: E402  四级 JSON 回退复用

T = os.path.join(BASE, "data", "exports", "terminology")
DEFAULT_CALIB = os.path.join(T, "s6_qa_calibration_set.json")
DEFAULT_CORPUS = os.path.join(T, "s6_qa_corpus.json")
DEFAULT_GOLD = os.path.join(T, "s6_qa_calibration_gold.json")
DEFAULT_LLM_OUT = os.path.join(T, "s6_qa_llm_labels.json")

RUBRIC_VERSION = "S6_QA_RUBRIC_V1"
VALID = {"RELEVANT", "UNCERTAIN", "IRRELEVANT"}

# ⚠️ 冻结对象：盲评 rubric。校准只允许改这里 → 版本升 V2。禁止注入任何 query/family 来源。
RUBRIC_SYSTEM_PROMPT = f"""You are a careful literature relevance screener for a materials-science knowledge base. Rubric version {RUBRIC_VERSION}.

TOPIC (research frame): polymerization shrinkage / shrinkage stress / contraction stress and their observable consequences in photopolymerization and light-curing polymer systems, including how they are measured, simulated, or mitigated. Application contexts include but are not limited to: dental composites/restoratives, SLA/DLP/3D-printing resins, coatings, electronic packaging/molding compounds, adhesives, holographic/optical recording media.

HARD SCOPE REQUIREMENT (must hold for RELEVANT): the studied system must contain a photoinitiated / light-cured polymerization or crosslinking process of a monomer or resin (photopolymerization, photocuring, light-curing, UV curing, photoresin SLA/DLP printing), AND the paper must substantively study a consequence OF THAT curing process (volume change / shrinkage / contraction, stress, deformation, warpage, dimensional error, interface defects...). Two things are NOT sufficient on their own: (a) light merely being used somewhere in the material or its processing, or (b) a defect/outcome keyword merely appearing in the results without being attributed to the photopolymerization curing process.

A paper is RELEVANT if the hard scope holds AND, in that context of curing/polymerization of monomers or resins, it studies ANY of:
 1. volumetric/polymerization shrinkage, contraction, shrinkage stress, polymerization/curing stress, internal or residual stress developing during cure;
 2. OBSERVABLE CONSEQUENCES of such shrinkage/stress — deformation, warpage, curl, dimensional error/inaccuracy, deflection, cracking, marginal/internal gap or leakage, void formation, delamination, cusp deflection, poor fit/trueness, etc. The paper does NOT need to contain the word "shrinkage" (or any synonym); what matters is that the studied outcome is a known shrinkage/stress consequence in a curing system;
 3. mechanisms that generate or relieve these stresses (e.g. stress relaxation, delayed gel point, addition-fragmentation chain transfer, modulus development during cure, low-stress/low-shrink monomers or additives);
 4. measurement, simulation, or mitigation of any of the above.

A paper is IRRELEVANT if:
 - it applies a polymerized/3D-printed/cured product (antenna, waveguide, photonic device, dental restoration performance, packaging device...) without any shrinkage/stress dimension;
 - it concerns polymer properties unrelated to shrinkage/stress (e.g. only aesthetics, biocompatibility, adhesion strength to substrates, wear, color stability);
 - the studied curing/polymerization is NOT photoinitiated (e.g. purely thermal curing of epoxy/molding compounds, pure glass-ionomer acid-base cement, thermoplastic processing). Cross-domain physical similarity does NOT bring such papers into scope: even if a thermal-cure study explicitly links cure shrinkage to warpage/voids, it is still OUT OF SCOPE for this light-curing frame;
 - the studied system is not a curing/polymerizing resin at all.

EXPLICIT BOUNDARY RULINGS (user-frozen 2026-09-05, override any ambiguity):
 - glass-ionomer / non-resin dental cements: "light-cured" plus a defect keyword (e.g. marginal leakage) is NOT enough. RELEVANT only if a photoinitiated resin polymerization is clearly present AND the paper links the leakage/gap outcome to that polymerization shrinkage. If the evidence is insufficient to establish either, use UNCERTAIN.
 - thermal-cure packaging/molding compounds (TQFP, epoxy encapsulation, etc.): if the cure is purely thermal with no photopolymerization/photocuring, IRRELEVANT regardless of whether cure-shrinkage→warpage/void physics is studied. Such papers are reserved as future cross-domain discovery sources, not current-frame gold.
 - 3D-printing/polymerization/fabrication without a shrinkage/stress consequence dimension (SLA antennas, PEDOT:PSS conductive polymers, generic printed devices): IRRELEVANT.

Use UNCERTAIN only when the paper plausibly concerns the topic but the available evidence is genuinely insufficient to choose between RELEVANT and IRRELEVANT (e.g. unclear setting chemistry or unclear studied outcome). Missing abstract alone is NOT grounds for UNCERTAIN: decide on the balance of available evidence (title at minimum) — a title like "Polymerization shrinkage of light-cured dental composites" is RELEVANT without an abstract, and "SLA antenna fabrication" can be IRRELEVANT without one. Evidence sufficiency decides the label; missingness never does.

Output STRICT JSON only, no prose. For a single paper:
{{"label": "RELEVANT" | "UNCERTAIN" | "IRRELEVANT", "reason": "<one short English sentence>"}}
For a batch of papers (input is a JSON array of {{"id","title","abstract"}}):
{{"<id>": {{"label": "...", "reason": "..."}}, ...}} — one entry per input id, nothing else."""


def backend_of(args):
    from search_engine.llm import create_backend
    key = args.api_key or os.environ.get("DEEPSEEK_API_KEY") or ""
    return create_backend(args.provider, api_key=key, model=args.model)


def load_set(path):
    d = json.load(open(path, encoding="utf-8"))
    # corpus/calib 两种容器：{"papers":[...]} / {"rows":[...]}
    items = d.get("papers") or d.get("rows")
    if items is None:
        raise SystemExit(f"[ERR] {path}: 无 papers/rows 列表")
    return items


async def run_qa(items, args, llm_out_path):
    """blind 盲评；断点续跑（已有 label 跳过）。返回 {key: label}。"""
    existing = {}
    if os.path.exists(llm_out_path):
        try:
            existing = json.load(open(llm_out_path, encoding="utf-8"))
        except Exception:
            existing = {}
        if "labels" in existing and isinstance(existing["labels"], dict):
            existing = existing["labels"]

    backend = backend_of(args)
    todo = [it for it in items
            if existing.get(it["key"]) not in VALID]
    print(f"total={len(items)} 已判={len(items) - len(todo)} 待判={len(todo)}")

    def _abs(it):
        a = (it.get("abstract") or "").strip()
        return a if a else "<no abstract available>"

    for i in range(0, len(todo), args.batch):
        chunk = todo[i:i + args.batch]
        payload = [{"id": it["key"], "title": (it.get("title") or "")[:400],
                    "abstract": _abs(it)[:1500]} for it in chunk]
        # retry-missing：单篇 + 强格式约束（防 batch 解析失败的格式问题复发）
        user_msg = json.dumps(payload, ensure_ascii=False)
        if args.retry_missing:
            user_msg += "\n\nOnly output the JSON array. No explanation. No markdown fences."
        ok = False
        for attempt in range(3):
            try:
                resp = await backend.chat(
                    RUBRIC_SYSTEM_PROMPT,
                    user_msg,
                    temperature=0.0)
                parsed = parse_llm_json(resp)
                # parsed 可能整体 {id:{...}}，或 {labels: {...}}
                if isinstance(parsed, dict) and "labels" in parsed and \
                        isinstance(parsed["labels"], dict):
                    parsed = parsed["labels"]
                got = 0
                for it in chunk:
                    ent = parsed.get(it["key"])
                    if isinstance(ent, str) and ent in VALID:
                        existing[it["key"]] = ent
                        got += 1
                    elif isinstance(ent, dict) and ent.get("label") in VALID:
                        existing[it["key"]] = ent["label"]
                        got += 1
                ok = True
                if got < len(chunk):
                    miss = [it["key"] for it in chunk
                            if existing.get(it["key"]) not in VALID]
                    print(f"  batch@{i} 解析 {got}/{len(chunk)} 缺: {miss}")
                    for mk in miss:
                        it = next(x for x in chunk if x["key"] == mk)
                        fb_msg = (f'One paper only. {json.dumps({"id": it["key"], "title": it["title"], "abstract": _abs(it)}, ensure_ascii=False)}'
                                  + ("\n\nOnly output the JSON object. No explanation. No markdown fences."
                                     if args.retry_missing else ""))
                        fallback = await backend.chat(
                            RUBRIC_SYSTEM_PROMPT, fb_msg, temperature=0.0)
                        fb = parse_llm_json(fallback)
                        if isinstance(fb, dict) and "labels" in fb:
                            fb = fb["labels"]
                        ent = fb.get(it["key"]) if isinstance(fb, dict) else None
                        lab = ent if isinstance(ent, str) else (ent or {}).get("label") if isinstance(ent, dict) else None
                        if lab in VALID:
                            existing[it["key"]] = lab
                break
            except Exception as e:
                print(f"  [WARN] batch@{i} attempt{attempt + 1}: {str(e)[:120]}")
                await asyncio.sleep(3 * (attempt + 1))
        if not ok:
            print(f"  [SKIP] batch@{i}: 3 次失败，留待续跑")
        # 每批落盘（断点续跑安全）
        with open(llm_out_path, "w", encoding="utf-8") as f:
            json.dump({"version": "s6_qa_llm_labels",
                       "rubric_version": RUBRIC_VERSION,
                       "labels": existing}, f, ensure_ascii=False, indent=1)
        if (i // args.batch + 1) % 20 == 0:
            print(f"  progress: {min(i + args.batch, len(todo))}/{len(todo)}")
        await asyncio.sleep(0.2)
    return existing


def compare(gold_path, llm_path, out_path=None, meta_path=None):
    gold = json.load(open(gold_path, encoding="utf-8"))
    if "labels" in gold and isinstance(gold["labels"], dict):
        gold = gold["labels"]
    llm = json.load(open(llm_path, encoding="utf-8"))
    if "labels" in llm and isinstance(llm["labels"], dict):
        llm = llm["labels"]
    # 可选 meta（校准集 JSON）：key -> 是否有 abstract。仅用于报告分层，
    # 不进入 relevance 定义（missingness ≠ UNCERTAIN 理由）。
    has_abs = {}
    if meta_path:
        meta = json.load(open(meta_path, encoding="utf-8"))
        rows = meta.get("rows", meta if isinstance(meta, list) else [])
        for r in rows:
            k = r.get("key")
            if k:
                has_abs[k] = bool((r.get("abstract") or "").strip())
    keys = sorted(k for k in gold if gold[k] in VALID)
    cm = {}  # (gold, llm) -> n
    for k in keys:
        g, l = gold[k], llm.get(k)
        if l not in VALID:
            l = "NO_LABEL"
        cm[(g, l)] = cm.get((g, l), 0) + 1
    print(f"\ngold keys={len(keys)}（gold ∩ llm = "
          f"{sum(1 for k in keys if llm.get(k) in VALID)}）")
    print("\n=== confusion (gold rows × llm cols) ===")
    labs = ["RELEVANT", "UNCERTAIN", "IRRELEVANT", "NO_LABEL"]
    hdr = "gold\\llm    " + "".join(f"{x[:8]:>10}" for x in labs)
    print(hdr)
    for g in labs[:-1]:
        row = "".join(f"{cm.get((g, l), 0):>10}" for l in labs)
        print(f"{g[:8]:<12}{row}")
    # 核心错误率（用户冻结口径 2026-09-05）：
    #   FN_{R→I}   = 人工 gold RELEVANT 被 LLM 判 IRRELEVANT（最危险，杀 recall）
    #   U_{rate|GoldR} = 人工 gold RELEVANT 被 LLM 判 UNCERTAIN（recall-first 下
    #                    不致命但膨胀 FinalKB/人工复核成本）
    rel = [k for k in keys if gold[k] == "RELEVANT"]
    fn = [k for k in rel if llm.get(k) == "IRRELEVANT"]
    unc_on_rel = [k for k in rel if llm.get(k) == "UNCERTAIN"]
    fn_rate = len(fn) / len(rel) if rel else None
    unc_rate = len(unc_on_rel) / len(rel) if rel else None
    print(f"\n[核心] gold RELEVANT={len(rel)}")
    if fn_rate is not None:
        print(f"  FN_{{R→I}}  = {len(fn)} ({fn_rate:.1%})")
        print(f"  U_{{rate|GoldR}} = {len(unc_on_rel)} ({unc_rate:.1%})")
    else:
        print("  FN_{R→I} / U_{rate|GoldR} = n/a（gold 无 RELEVANT）")
    if meta_path and rel:
        for label, pool in (("has_abstract", [k for k in rel if has_abs.get(k, True)]),
                            ("no_abstract", [k for k in rel if not has_abs.get(k, True)])):
            if not pool:
                continue
            f2 = [k for k in pool if llm.get(k) == "IRRELEVANT"]
            u2 = [k for k in pool if llm.get(k) == "UNCERTAIN"]
            print(f"  [{label}] gold RELEVANT={len(pool)} | "
                  f"FN={len(f2)} ({len(f2) / len(pool):.1%}) | "
                  f"UNC={len(u2)} ({len(u2) / len(pool):.1%})")
    rep = {"confusion": {f"{g}->{l}": n for (g, l), n in cm.items()},
           "gold_relevant": len(rel), "llm_fn": len(fn),
           "llm_unc_on_rel": len(unc_on_rel),
           "fn_rate": round(fn_rate, 4) if fn_rate is not None else None,
           "unc_rate_on_rel": round(unc_rate, 4) if unc_rate is not None else None,
           "fn_keys": fn,
           "unc_on_rel_keys": unc_on_rel}
    if meta_path:
        rep["by_abstract"] = {}
        for label, pool in (("has_abstract", [k for k in rel if has_abs.get(k, True)]),
                            ("no_abstract", [k for k in rel if not has_abs.get(k, True)])):
            if pool:
                f2 = [k for k in pool if llm.get(k) == "IRRELEVANT"]
                u2 = [k for k in pool if llm.get(k) == "UNCERTAIN"]
                rep["by_abstract"][label] = {
                    "gold_relevant": len(pool),
                    "fn": len(f2), "fn_rate": round(len(f2) / len(pool), 4),
                    "unc": len(u2), "unc_rate": round(len(u2) / len(pool), 4)}
    return rep


def main():
    ap = argparse.ArgumentParser(description="S6 blind LLM relevance QA")
    ap.add_argument("--mode", choices=["run", "compare"], required=True)
    # run
    ap.add_argument("--set", help="校准集或 corpus json")
    ap.add_argument("--llm-out", default=DEFAULT_LLM_OUT)
    ap.add_argument("--batch", type=int, default=5)
    ap.add_argument("--provider", default="deepseek",
                    choices=["deepseek", "ollama", "tencent"])
    ap.add_argument("--model", default=None)
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--retry-missing", action="store_true",
                    help="只补 NO_LABEL/缺失 key（断点续跑语义），强制单篇调用 + "
                         "追加 'Only output JSON array. No explanation.' 强格式约束。"
                         "用于 batch 解析失败后的收尾补判，不重跑已判样本。")
    # compare
    ap.add_argument("--gold", default=DEFAULT_GOLD)
    ap.add_argument("--out", default=None)
    ap.add_argument("--meta", default=None,
                    help="compare 可选：校准集 JSON（s6_qa_calibration_set.json），"
                         "按 abstract 有无分列 FN/UNC 率。missingness 仅作报告分层，"
                         "不进入 relevance 定义。")
    args = ap.parse_args()

    if args.mode == "run":
        if not args.set:
            raise SystemExit("--mode run 需要 --set <corpus/calib json>")
        items = load_set(args.set)
        if args.retry_missing and args.batch > 1:
            args.batch = 1  # 补判单篇调用，降低再失败概率
        labels = asyncio.run(run_qa(items, args, args.llm_out))
        n = {v: sum(1 for x in labels.values() if x == v) for v in VALID}
        n_miss = len(items) - sum(n.values())
        print(f"\n[OK] 判定完成: RELEVANT {n['RELEVANT']} | UNCERTAIN "
              f"{n['UNCERTAIN']} | IRRELEVANT {n['IRRELEVANT']}")
        if n_miss:
            print(f"[WARN] 仍缺 {n_miss} 篇（未解析成功）——重跑同命令继续补判")
        else:
            print(f"[OK] {len(items)} 篇全部有有效 label，可冻结")
        print(f"[OK] labels: {args.llm_out}")
    else:
        rep = compare(args.gold, args.llm_out, args.out, args.meta)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                json.dump(rep, f, ensure_ascii=False, indent=1)
            print(f"[OK] report: {args.out}")


if __name__ == "__main__":
    main()
