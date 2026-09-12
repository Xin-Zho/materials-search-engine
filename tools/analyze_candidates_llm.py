"""P4-2 LLM Analyst：对**算法发现**的候选方向做解释（不参与预测）。

架构位置（用户 2026-09-12 裁定）：

    Emerging Candidates（算法产出，已冻结）  ->  LLM Analyst  ->  Explanation
                                                     |
                                        P4-3 Future Validation 判真伪（不依赖 LLM 判断）

本模块刻意**不**回答「哪些方向有潜力」——那个问题会引入语言模型的先验，
无法归因到这份语料，也无法被证伪。它只回答「这个已观测到的结构变化意味着什么」。

反泄漏（与 P4-1 抽取同一条纪律）：
  * 证据只允许 year <= CUTOFF_YEAR(2020) 的论文；
  * 发送给模型的内容**白名单构造**（只取候选记录里列出的字段），
    因此任何引用量/未来信息都进不去；
  * prompt / tool / 候选文件三者 sha256 一起冻结，改动即拒绝运行。

用法：
    python tools/analyze_candidates_llm.py --freeze      # 冻结协议
    python tools/analyze_candidates_llm.py               # dry-run：列出将分析的候选
    python tools/analyze_candidates_llm.py --live        # 真实调用（需 DEEPSEEK_API_KEY）
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import datetime
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from search_engine.llm import TruncatedResponse, create_backend  # noqa: E402

DATASET_DIR = Path(BASE) / "datasets" / "photopolymerization_v1"
CANDIDATES_PATH = DATASET_DIR / "emergence_candidates_v2.json"
PROMPT_PATH = Path(BASE) / "prompts" / "p4_2_candidate_analysis_v1.md"
SPEC_PATH = DATASET_DIR / "analyst_spec.yaml"
CACHE_DIR = DATASET_DIR / "analyst_cache"
OUT_PATH = DATASET_DIR / "candidate_analysis_v1.json"

SPEC_VERSION = "p4_2_analyst_v1"
MODEL = "deepseek-chat"
TEMPERATURE = 0.0
MAX_TOKENS = 1600
CUTOFF_YEAR = 2020
MAX_RETRIES = 3
RETRY_BACKOFF_S = 2.0
RATE_LIMIT_BACKOFF_S = 8.0
CONCURRENCY = 6

# deepseek-chat 公开价（USD / 1M tokens）：估算成本用，不影响任何科学判定
PRICE_IN = 0.27
PRICE_OUT = 1.10

ST_OK = "OK"
ST_JSON_INVALID = "JSON_INVALID"
ST_SCHEMA_INVALID = "SCHEMA_INVALID"
ST_API_ERROR = "API_ERROR"

REQUIRED_FIELDS = ("candidate", "structural_change", "evidence", "reasoning",
                   "structural_role", "alternative_explanations", "uncertainty",
                   "falsifiable_checks")
STRUCTURAL_ROLES = ("new_position", "bridge", "bottleneck_solver",
                    "new_combination", "new_connection", "gap_bridge", "none")

# 只允许这些字段进入模型输入（白名单构造 —— 任何泄漏都不可能"漏出去"）
PAYLOAD_FIELDS = ("kind", "a", "b", "concept", "type", "support", "first_seen",
                  "df_early", "df_late", "growth", "assoc_growth", "new_edge",
                  "cross_domain", "edge_grade", "typed_relations",
                  "topic_contrast", "home_topic_a", "home_topic_b",
                  "common_neighbors", "n_common_neighbors", "adamic_adar",
                  "scores", "evidence_papers", "topics",
                  # NODE 结构位置特征（用户 2026-09-12 纠正后的新口径）
                  "stratum", "degree", "new_edge_share", "bridge_raw",
                  "problem_raw", "problem_cohesion", "challengers",
                  "combination_raw", "n_mediated_pairs", "neighbors_by_module",
                  "method_like", "scores_basis")
EVIDENCE_FIELDS = ("paper_uid", "year", "title", "snippet", "via", "about")

_CITATION_RE = re.compile(r"cit(e|ed|ation)|impact\s*factor|被引|引用(量|次数)|影响因子",
                          re.I)


def _rel(path) -> str:
    try:
        return os.path.relpath(str(path), BASE).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe(name) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(name))[:120]


def load_prompt(path=PROMPT_PATH):
    text = Path(path).read_text(encoding="utf-8")
    if "## SYSTEM" not in text or "## USER" not in text:
        raise ValueError(f"prompt 缺 ## SYSTEM / ## USER 段: {path}")
    sys_part, user_part = text.split("## SYSTEM", 1)[1].split("## USER", 1)
    m = re.search(r"```+\s*\n(.*?)```+", user_part, re.S)
    if not m:
        raise ValueError(f"prompt 的 ## USER 段找不到围栏模板: {path}")
    return sys_part.strip(), m.group(1)


# ══ 候选 -> 模型输入 ═════════════════════════════════════════════════════
def _clean_obj(obj, fields):
    return {k: obj[k] for k in fields if k in obj}


def build_payload(cand):
    """白名单构造模型输入。**同时**断言证据年份不越界。"""
    body = _clean_obj(cand, PAYLOAD_FIELDS)
    ev = []
    for e in cand.get("evidence_papers") or []:
        y = e.get("year")
        if y is not None and y > CUTOFF_YEAR:
            raise SystemExit(
                f"[refused] 证据年份越界：{cand.get('cand_id')} 的 {e.get('paper_uid')} "
                f"year={y} > {CUTOFF_YEAR}")
        ev.append(_clean_obj(e, EVIDENCE_FIELDS))
    body["evidence_papers"] = ev
    body["cand_id"] = cand.get("cand_id")
    body["evidence_count"] = len(ev)
    return body


def render_user_message(template, cand):
    """渲染模型输入。

    防御性检查只针对**字段名**，不针对自由文本 —— 论文标题里可能出现
    "cited" 这类词（实测有），把词面扫描当门禁会产生误拒。
    真正要防的是「结构里夹带影响力指标字段」。
    """
    body = build_payload(cand)
    bad = [k for k in body if _CITATION_RE.search(str(k))]
    if bad:
        raise SystemExit(f"[refused] 候选 {cand.get('cand_id')} 的输入含影响力字段: {bad}")
    return template.format(cutoff=CUTOFF_YEAR,
                           payload=json.dumps(body, ensure_ascii=False, indent=1))


# ══ 输出校验 ═════════════════════════════════════════════════════════════
def parse_json_content(content):
    s = (content or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"```\s*$", "", s).strip()
    return json.loads(s)


def _as_str_list(v, limit=None):
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return []
    out = [str(x).strip() for x in v if str(x).strip()]
    return out[:limit] if limit else out


def validate_payload(obj, provided_uids):
    """字段校验 + **接地度**计算。

    接地度 = evidence 条目里引用了「本次提供的 paper_uid 或某个 <= cutoff 的年份」
    的比例。它衡量模型是在引用给定证据，还是在自说自话 —— 后者是解释层的
    主要失效模式，必须可度量。
    """
    if not isinstance(obj, dict):
        return None, ["payload 非对象"]
    issues = []
    clean = {}
    for f in ("candidate", "structural_change", "reasoning"):
        v = obj.get(f)
        clean[f] = str(v).strip() if v is not None else ""
        if not clean[f]:
            issues.append(f"缺字段或为空: {f}")
    clean["evidence"] = _as_str_list(obj.get("evidence"), 12)
    role = str(obj.get("structural_role") or "").strip().lower()
    if role not in STRUCTURAL_ROLES:
        issues.append(f"structural_role 不在枚举内: {role!r}")
        clean["structural_role"] = None
    else:
        clean["structural_role"] = role
    clean["alternative_explanations"] = _as_str_list(
        obj.get("alternative_explanations"), 6)
    clean["falsifiable_checks"] = _as_str_list(obj.get("falsifiable_checks"), 8)
    if not clean["evidence"]:
        issues.append("evidence 为空")

    try:
        u = float(obj.get("uncertainty"))
        clean["uncertainty"] = round(min(max(u, 0.0), 1.0), 3)
    except (TypeError, ValueError):
        clean["uncertainty"] = None
        issues.append("uncertainty 不是 0-1 的数")
    if not clean["falsifiable_checks"]:
        issues.append("falsifiable_checks 为空（解释层必须给出可证伪项）")

    grounded = 0
    for e in clean["evidence"]:
        low = e.lower()
        if any(u and u.lower() in low for u in provided_uids):
            grounded += 1
        elif re.search(r"\b(19|20)\d{2}\b", e):
            grounded += 1
    clean["grounded_evidence_items"] = grounded
    clean["groundedness"] = (round(grounded / len(clean["evidence"]), 3)
                             if clean["evidence"] else 0.0)
    if clean["groundedness"] < 0.5:
        issues.append("接地度 < 0.5（多数 evidence 未引用给定材料）")
    return clean, issues


# ══ 协议冻结 ═════════════════════════════════════════════════════════════
def freeze_spec():
    import yaml
    if not CANDIDATES_PATH.exists():
        raise SystemExit(f"[refused] 未找到冻结候选：{_rel(CANDIDATES_PATH)}，"
                         f"先跑 tools/discover_emergence_candidates.py --apply")
    cand = json.loads(CANDIDATES_PATH.read_text(encoding="utf-8"))
    spec = {
        "spec_version": SPEC_VERSION,
        "frozen_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "prompt_file": _rel(PROMPT_PATH),
        "prompt_sha256": _sha256(PROMPT_PATH),
        "tool_file": _rel(Path(__file__)),
        "tool_sha256": _sha256(Path(__file__)),
        "candidates_file": _rel(CANDIDATES_PATH),
        "candidates_sha256": _sha256(CANDIDATES_PATH),
        "candidates_predictor_version": cand.get("predictor_version"),
        "model": MODEL,
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "cutoff_year": CUTOFF_YEAR,
        "required_output_fields": list(REQUIRED_FIELDS),
        "policy": {
            "analyst_explains_does_not_predict": True,
            "evidence_whitelist_construction": True,
            "no_influence_metrics_in_input": True,
            "no_external_knowledge": True,
            "temperature_0_for_reproducibility": True,
            "prompt_edit_requires_refreeze": True,
        },
        "note": "改 prompt 后必须重跑 --freeze，否则 --live 拒绝运行；"
                "候选文件的 sha256 一并冻结 —— 候选变了分析就失效",
    }
    SPEC_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SPEC_PATH, "w", encoding="utf-8", newline="\n") as f:
        yaml.safe_dump(spec, f, allow_unicode=True, sort_keys=False, width=100)
    return spec


def assert_spec_fresh():
    import yaml
    if not SPEC_PATH.exists():
        raise SystemExit(f"[refused] 未冻结协议。先跑 --freeze（{_rel(SPEC_PATH)}）")
    spec = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    cur_p, cur_c = _sha256(PROMPT_PATH), _sha256(CANDIDATES_PATH)
    if spec.get("prompt_sha256") != cur_p:
        raise SystemExit("[refused] prompt 已变更但未重新冻结："
                         f"{str(spec.get('prompt_sha256'))[:16]}… != {cur_p[:16]}…")
    if spec.get("candidates_sha256") != cur_c:
        raise SystemExit("[refused] 候选文件已变更但未重新冻结："
                         f"{str(spec.get('candidates_sha256'))[:16]}… != {cur_c[:16]}…")
    return spec


# ══ 分析主循环 ═══════════════════════════════════════════════════════════
def select_candidates(cand, k_node, k_pair, k_gap):
    sel = []
    # NODE 取产物的**主榜**（small+mid 层且非表征手段）—— 与 P4-3 验证同一个集合
    for r in (cand.get("prediction_set_node_rows")
              or [x for x in cand["node_candidates"]
                  if x["type"] in cand["thresholds"]["prediction_types"]])[:k_node]:
        sel.append(dict(r, _group="NODE"))
    sel += [dict(r, _group="PAIR_PRESENT") for r in cand["pair_candidates"][:k_pair]]
    sel += [dict(r, _group="PAIR_GAP") for r in cand["gap_candidates"][:k_gap]]
    return sel


async def analyze_one(backend, system_prompt, template, cand, sem, spec):
    cid = cand["cand_id"]
    t0 = time.time()
    try:
        user_msg = render_user_message(template, cand)
    except SystemExit as e:
        return {"cand_id": cid, "group": cand["_group"], "status": "REFUSED",
                "error": str(e)}
    provided = [e.get("paper_uid") for e in
                (cand.get("evidence_papers") or []) if e.get("paper_uid")]
    usage = None
    last_err = None
    async with sem:
        for attempt in range(MAX_RETRIES + 1):
            try:
                content = await backend.chat(
                    system_prompt, user_msg,
                    temperature=spec["temperature"], max_tokens=spec["max_tokens"],
                    raise_on_truncation=True)
                usage = getattr(backend, "last_usage", None)
                try:
                    obj = parse_json_content(content)
                except Exception as e:                       # noqa: BLE001
                    return _row(cand, ST_JSON_INVALID, t0, usage,
                                error=f"{type(e).__name__}: {e}",
                                raw=content[:600])
                clean, issues = validate_payload(obj, provided)
                if clean is None:
                    return _row(cand, ST_SCHEMA_INVALID, t0, usage,
                                error="; ".join(issues), raw=content[:600])
                status = ST_OK if not issues else ST_SCHEMA_INVALID
                return _row(cand, status, t0, usage, clean=clean,
                            issues=issues, raw=(content[:600] if issues else None))
            except TruncatedResponse as e:
                last_err = f"TruncatedResponse: {e}"
                await asyncio.sleep(RETRY_BACKOFF_S * (attempt + 1))
            except Exception as e:                           # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"
                wait = (RATE_LIMIT_BACKOFF_S if "429" in str(e)
                        else RETRY_BACKOFF_S * (attempt + 1))
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(wait)
    return _row(cand, ST_API_ERROR, t0, usage, error=last_err,
                raw=user_msg[:0] or None)


def _row(cand, status, t0, usage, clean=None, issues=None, error=None, raw=None):
    return {
        "cand_id": cand["cand_id"],
        "group": cand["_group"],
        "kind": cand["kind"],
        "statement": _statement(cand),
        "status": status,
        "duration_s": round(time.time() - t0, 1),
        "usage": usage,
        "analysis": clean,
        "issues": issues or [],
        "error": error,
        "raw_excerpt": raw,
    }


def _statement(cand):
    """候选的一句话表述（人读用，也便于 P4-3 报告对齐）。"""
    if cand["kind"] == "NODE":
        return f"[NODE] {cand['concept']}（{cand['type']}）"
    a, b = cand["a"]["name"], cand["b"]["name"]
    if cand["kind"] == "PAIR_GAP":
        return f"[GAP] {a} <-> {b}（尚未共现，单侧证据各若干）"
    if cand.get("typed_relations"):
        rel = " --" + "/".join(r["relation"] for r in cand["typed_relations"][:2]) + "--> "
    else:
        rel = " + "          # 无抽取关系时用并列符，避免两个概念名黏在一起
    return f"[PAIR] {a}{rel}{b}"


def _count(it):
    return collections.Counter(it).items()


def summarize(rows, spec):
    ok = [r for r in rows if r["status"] == ST_OK]
    tin = sum((r["usage"] or {}).get("prompt_tokens", 0) for r in rows)
    tout = sum((r["usage"] or {}).get("completion_tokens", 0) for r in rows)
    unc = [r["analysis"]["uncertainty"] for r in ok
           if r["analysis"] and r["analysis"].get("uncertainty") is not None]
    grd = [r["analysis"]["groundedness"] for r in ok if r["analysis"]]
    return {
        "n_candidates": len(rows),
        "n_ok": len(ok),
        "status_counts": dict(_count(r["status"] for r in rows)),
        "by_group": dict(_count(r["group"] for r in rows if r["status"] == ST_OK)),
        "tokens_in": tin, "tokens_out": tout,
        "cost_estimate_usd": round(tin / 1e6 * PRICE_IN + tout / 1e6 * PRICE_OUT, 4),
        "uncertainty": {"mean": round(sum(unc) / len(unc), 3) if unc else None,
                        "max": max(unc) if unc else None,
                        "high_gt_0_6": sum(1 for u in unc if u > 0.6)},
        "groundedness_mean": (round(sum(grd) / len(grd), 3) if grd else None),
        "structural_roles": dict(collections.Counter(
            r["analysis"]["structural_role"] for r in ok if r["analysis"])),
        "issues_total": sum(len(r["issues"]) for r in rows),
        "spec": spec["spec_version"],
    }


def print_summary(s, rows):
    print("=" * 78)
    print(f"  P4-2 LLM Analyst（解释层，非预测层）  spec={s['spec']}")
    print(f"  候选 {s['n_candidates']} | OK {s['n_ok']} | {s['status_counts']}")
    print(f"  token in/out {s['tokens_in']}/{s['tokens_out']} "
          f"| 估算 ${s['cost_estimate_usd']}")
    print(f"  uncertainty 均值 {s['uncertainty']['mean']} "
          f"最大 {s['uncertainty']['max']} "
          f"高(>0.6) {s['uncertainty']['high_gt_0_6']}")
    print(f"  接地度均值 {s['groundedness_mean']} | 校验问题 {s['issues_total']} 条")
    print(f"  结构角色分布 {s['structural_roles']}")
    print("-" * 78)
    for r in rows:
        a = r["analysis"] or {}
        print(f"  [{r['status']:<13}] {r['statement'][:74]}")
        if a:
            print(f"      u={a.get('uncertainty')} 接地={a.get('groundedness')} "
                  f"| {a.get('structural_change','')[:96]}")
            if a.get("falsifiable_checks"):
                print(f"      证伪项: {a['falsifiable_checks'][0][:96]}")
    print("=" * 78)


async def run(rows, spec, concurrency):
    system_prompt, template = load_prompt()
    if _sha256(PROMPT_PATH) != spec["prompt_sha256"]:
        raise SystemExit("[refused] prompt 与冻结协议不一致（并发中被改？）")
    backend = create_backend(provider="deepseek",
                             api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
                             model=spec["model"])
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(concurrency)

    cached, todo = {}, []
    for cand in rows:
        p = CACHE_DIR / (_safe(cand["cand_id"]) + ".json")
        if p.exists():
            try:
                cached[cand["cand_id"]] = json.loads(p.read_text(encoding="utf-8"))
                continue
            except Exception:                                # noqa: BLE001
                pass
        todo.append(cand)
    print(f"[run] 命中缓存 {len(cached)} | 待分析 {len(todo)} | 并发 {concurrency}")

    out = list(cached.values())
    if todo:
        tasks = [analyze_one(backend, system_prompt, template, c, sem, spec)
                 for c in todo]
        done = 0
        for coro in asyncio.as_completed(tasks):
            row = await coro
            out.append(row)
            (CACHE_DIR / (_safe(row["cand_id"]) + ".json")).write_text(
                json.dumps(row, ensure_ascii=False, indent=1), encoding="utf-8")
            done += 1
            if done % 5 == 0 or done == len(todo):
                ok = sum(1 for r in out if r["status"] == ST_OK)
                print(f"  [{done}/{len(todo)}] 累计 OK {ok}/{len(out)}", flush=True)
    order = {c["cand_id"]: i for i, c in enumerate(rows)}
    out.sort(key=lambda r: order.get(r["cand_id"], 10 ** 9))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--freeze", action="store_true", help="冻结协议")
    ap.add_argument("--live", action="store_true", help="真实调用 LLM（需 API key）")
    ap.add_argument("--k-node", type=int, default=10)
    ap.add_argument("--k-pair", type=int, default=15)
    ap.add_argument("--k-gap", type=int, default=10)
    ap.add_argument("--concurrency", type=int, default=CONCURRENCY)
    ap.add_argument("--candidates", default=str(CANDIDATES_PATH))
    ap.add_argument("--out", default=str(OUT_PATH))
    args = ap.parse_args(argv)

    if args.freeze:
        spec = freeze_spec()
        print(f"[ok] 协议 -> {_rel(SPEC_PATH)}")
        print(f"     prompt_sha256={spec['prompt_sha256'][:16]}… "
              f"tool_sha256={spec['tool_sha256'][:16]}… "
              f"candidates_sha256={spec['candidates_sha256'][:16]}…")
        return 0

    cand = json.loads(Path(args.candidates).read_text(encoding="utf-8"))
    rows = select_candidates(cand, args.k_node, args.k_pair, args.k_gap)
    print(f"[input] 候选文件 {_rel(args.candidates)} "
          f"({cand.get('predictor_version')}, 冻结于 {cand.get('frozen_at')})")
    print(f"[select] NODE {args.k_node} + PAIR {args.k_pair} + GAP {args.k_gap} "
          f"-> 实际 {len(rows)}")

    if not args.live:
        for r in rows:
            print(f"  - {_statement(r)}")
        print("\n[dry-run] 未调用 LLM。加 --live 执行（需先 --freeze）")
        return 0

    spec = assert_spec_fresh()
    results = asyncio.run(run(rows, spec, args.concurrency))
    s = summarize(results, spec)
    print_summary(s, results)
    payload = {
        "analyst_version": SPEC_VERSION,
        "finished_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "spec": {k: spec[k] for k in ("spec_version", "frozen_at", "prompt_sha256",
                                      "tool_sha256", "candidates_sha256", "model",
                                      "temperature")},
        "summary": s,
        "analyses": results,
        "note": ("解释层产物：**不作为预测结论**。P4-3 用 2021-2025 文献独立判定候选真伪；"
                 "本文件只提供机制解释与可证伪项，供 P4-3 报告引用。"),
    }
    Path(args.out).write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n[ok] 分析 -> {_rel(args.out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
