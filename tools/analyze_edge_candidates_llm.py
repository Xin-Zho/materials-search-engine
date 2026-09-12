# -*- coding: utf-8 -*-
"""边层候选的 LLM 解释层（**不是预测层**）。

用户 2026-09-13 裁定：「会不会连上已经够了，剩下可以让 llm 来分析」。
本工具不预测任何东西，只回答一个问题：

    根据 <= 2015 的语料，这两个端点**已经共享什么结构**、这条潜在连接**机制上意味着什么**。

配套与边界：
  * 候选由 `tools/evaluate_edge_event.py` 产生（唯一目标 = 新边形成事件）；
  * 真假由 2021-2025 的未来文献判定（P4-3），**不由语言模型判定**；
  * 输入**结构性**排除未来信息（未来标签、joint_fut、eval_* 一律不许进 payload），
    并断言所有证据年份 <= 2015 —— 不是靠"提醒模型别用未来"；
  * 解释层的主要失效模式是「自说自话」，所以**必须度量接地度**。

用法：
    python tools/analyze_edge_candidates_llm.py                 # dry-run：只打印待分析项
    python tools/analyze_edge_candidates_llm.py --freeze        # 冻结协议（prompt/tool/候选 三 hash）
    python tools/analyze_edge_candidates_llm.py --live --k 8    # 真实调用（需 DEEPSEEK_API_KEY）
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import hashlib
import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tools"))

from search_engine.llm import TruncatedResponse, create_backend  # noqa: E402

DATASET_DIR = Path(BASE) / "datasets" / "photopolymerization_v1"
CANDIDATES_PATH = DATASET_DIR / "edge_new_link_candidates_v2.json"
CONCEPTS_FULL = DATASET_DIR / "concepts_full_v2.jsonl"
CONCEPTS_V1 = DATASET_DIR / "concepts_v1.jsonl"
PROMPT_PATH = Path(BASE) / "prompts" / "p4_2v2_edge_analysis_v1.md"
SPEC_PATH = DATASET_DIR / "edge_analyst_spec.yaml"
CACHE_DIR = DATASET_DIR / "edge_analyst_cache"
OUT_PATH = DATASET_DIR / "edge_candidate_analysis_v1.json"

SPEC_VERSION = "p4_2_edge_analyst_v1"
MODEL = "deepseek-chat"
TEMPERATURE = 0.0
MAX_TOKENS = 1600
CUT = 2015                      # 特征窗口右端：证据只许来自 <= 2015
MAX_RETRIES = 3
RETRY_BACKOFF_S = 2.0
RATE_LIMIT_BACKOFF_S = 8.0
CONCURRENCY = 5

PRICE_IN = 0.27                 # deepseek-chat 公开价（USD/1M tokens），仅用于成本估算
PRICE_OUT = 1.10

ST_OK = "OK"
ST_JSON_INVALID = "JSON_INVALID"
ST_SCHEMA_INVALID = "SCHEMA_INVALID"
ST_API_ERROR = "API_ERROR"
ST_REFUSED = "REFUSED"

BRIDGE_QUALITY = ("strong", "weak", "unclear")

# ── payload 白名单与黑名单 ────────────────────────────────────────────────
# 白名单：只把这些键交给模型（结构上就不可能夹带未来信息）
PAYLOAD_PAIR_KEYS = ("a", "b")
PAYLOAD_STRUCT_KEYS = ("adamic_adar", "common_neighbors", "jaccard",
                       "deg_min", "deg_max")
EVIDENCE_KEYS = ("paper_uid", "year", "excerpt", "mentions")

# 黑名单：出现即拒绝（未来标签 / 评估期信息）。这是结构性断言，不是提醒。
FORBIDDEN_KEY_RE = re.compile(
    r"eval|joint_fut|joint_hist|formed|rate_ratio|future|citation|cited|impact|fwci",
    re.I)


def _rel(p):
    try:
        return os.path.relpath(p)
    except ValueError:                       # 跨盘符
        return str(p)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _safe(name):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(name))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(BASE, "tools", filename))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_prompt(path=PROMPT_PATH):
    """解析 prompt：`## SYSTEM` 段 + `## USER` 段里的裸围栏模板。

    ⚠️ 围栏必须是**裸 ```**（后面直接换行）—— 带语言标注会让正则找不到模板。
    """
    text = Path(path).read_text(encoding="utf-8")
    if "## SYSTEM" not in text or "## USER" not in text:
        raise ValueError("prompt 缺 ## SYSTEM / ## USER 段: %s" % path)
    sys_part, user_part = text.split("## SYSTEM", 1)[1].split("## USER", 1)
    m = re.search(r"```+\s*\n(.*?)```+", user_part, re.S)
    if not m:
        raise ValueError("prompt 的 ## USER 段找不到围栏模板: %s" % path)
    return sys_part.strip(), m.group(1)


# ══ 早期图 + 证据（全部 <= CUT）══════════════════════════════════════════
def load_context():
    """载入早期图与文本索引，供证据构造使用。"""
    ee = _load("ee_for_analyst", "evaluate_edge_event.py")
    val = _load("val_for_analyst", "validate_emergence_p4_3.py")
    dec = _load("dec_for_analyst", "discover_emergence_candidates.py")
    concepts = str(CONCEPTS_FULL if CONCEPTS_FULL.exists() else CONCEPTS_V1)
    ok_rows, meta = dec.load_inputs(dec.DB_PATH, concepts)
    g = ee.build_early_graph(ok_rows, meta, CUT, ee.K_ENDPOINTS)
    text_rows = val.load_text_rows(dec.DB_PATH)
    idx = val.LexIndex(text_rows, (CUT + 1, 2020), (2021, 2025))
    early = {i for i, (_u, y, _t) in enumerate(idx.docs)
             if y is not None and y <= CUT}
    return {"ee": ee, "val": val, "g": g, "text": text_rows, "idx": idx,
            "early": early, "concepts_file": concepts}


def shared_neighbors(g, a, b, limit=8):
    """两端共享的邻居概念，按 Adamic-Adar 贡献排序（桥的质量看这里）。"""
    na, nb = g["adj"].get(a) or set(), g["adj"].get(b) or set()
    shared = sorted(na & nb)
    out = []
    for s in shared:
        deg = len(g["adj"].get(s) or ()) or 1
        out.append({"name": s, "type": g["type"].get(s),
                    "home_topic": g["home"].get(s),
                    "aa_contribution": round(1.0 / __import__("math").log(max(deg, 2)), 4)})
    out.sort(key=lambda r: -r["aa_contribution"])
    return out[:limit]


def _snippet(text, names, width=170):
    """在论文文本里取包含概念词的一段（找不到就取开头）。"""
    toks = []
    for n in names:
        toks.extend(re.findall(r"[a-z0-9\-]+", n.lower()))
    low = (text or "").lower()
    hit = None
    for t in toks:
        if len(t) < 4:
            continue
        i = low.find(t)
        if i >= 0 and (hit is None or i < hit):
            hit = i
    if hit is None:
        return (text or "")[:width].strip()
    lo, hi = max(0, hit - 55), min(len(text), hit + width)
    return ("…" if lo else "") + text[lo:hi].strip() + ("…" if hi < len(text) else "")


def collect_evidence(ctx, a, b, shared, max_per_side=3):
    """构造证据：优先"与某个共享邻居出现在同一篇"的早期论文（那才是桥的证据）。

    ⚠️ 这里必须是**并集**（提到任一个共享邻居），不能取交集 —— 共享邻居常有
    几十个，取交集必然为空集，于是"桥证据"永远找不到，静默退化成普通论文。
    """
    idx, early, text = ctx["idx"], ctx["early"], ctx["text"]
    names = [s["name"] for s in shared]
    bridge_docs = set()
    for nm in names:
        ds = idx.docset(nm)
        if ds:
            bridge_docs |= (ds & early)
    ev, seen = [], set()
    for side in (a, b):
        ds = idx.docset(side) or set()
        pool = sorted((ds & early) & bridge_docs) or sorted(ds & early)
        rest = sorted((ds & early) - bridge_docs)
        for i in (list(pool[:max_per_side - 1]) + list(rest[:1])):
            uid = text[i][0]
            if uid in seen:
                continue
            seen.add(uid)
            present = [n for n in [a, b] + names
                       if idx.docset(n) and i in idx.docset(n)]
            ev.append({"paper_uid": uid, "year": text[i][1],
                       "excerpt": _snippet(text[i][2], [side] + names),
                       "mentions": sorted(set(present))[:8]})
    return ev[:6]


def build_payload(ctx, row, cand_id):
    """白名单构造模型输入 —— **并且断言证据年份不越界**。

    未来信息不是靠"提醒模型别说"排除的，而是**根本不进 payload**：
    `joint_fut` / `rate_ratio` / `eval_formed` 这些字段只在上游产物里，此处不取。
    """
    a, b = row["a"], row["b"]
    shared = shared_neighbors(ctx["g"], a, b)
    ev = collect_evidence(ctx, a, b, shared)
    for e in ev:
        y = e.get("year")
        if y is not None and y > CUT:
            raise SystemExit("[refused] 证据年份越界：%s 的 %s year=%s > %d"
                             % (cand_id, e.get("paper_uid"), y, CUT))
    body = {
        "cand_id": cand_id,
        "pair": {
            "a": {"concept": a, "type": row.get("type_a"),
                  "home_topic": row.get("home_a")},
            "b": {"concept": b, "type": row.get("type_b"),
                  "home_topic": row.get("home_b")},
        },
        "structure": {k: row.get(k) for k in PAYLOAD_STRUCT_KEYS},
        "shared_neighbors": shared,
        "evidence_papers": ev,
        "evidence_count": len(ev),
        "boundary_note": ("两端在 <= %d 的抽取语料中**从未共同出现**；"
                          "它们各自与上述 shared_neighbors 一起出现过。" % CUT),
    }
    bad = [k for k in _walk_keys(body) if FORBIDDEN_KEY_RE.search(str(k))]
    if bad:
        raise SystemExit("[refused] 输入含未来/影响力字段：%s" % bad)
    return body


def _walk_keys(obj, prefix=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            for x in _walk_keys(v, prefix):
                yield x
    elif isinstance(obj, list):
        for v in obj:
            for x in _walk_keys(v, prefix):
                yield x


def render_user_message(template, body):
    return template.format(cutoff=CUT,
                           payload=json.dumps(body, ensure_ascii=False, indent=1))


# ══ 输出校验 + 接地度 ════════════════════════════════════════════════════
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


def validate_payload(obj, provided_uids, provided_names):
    """字段校验 + **接地度**。

    接地度 = evidence 条目里引用了「本次提供的 paper_uid / 概念名 / <= CUT 的年份」
    的比例。它衡量模型是在引用给定证据，还是在自说自话。
    """
    if not isinstance(obj, dict):
        return None, ["payload 非对象"]
    issues, clean = [], {}
    for f in ("candidate", "shared_structure", "why_they_may_connect"):
        v = obj.get(f)
        clean[f] = str(v).strip() if v is not None else ""
        if not clean[f]:
            issues.append("缺字段 %s" % f)

    ev = _as_str_list(obj.get("evidence"))
    clean["evidence"] = ev
    if not ev:
        issues.append("evidence 为空")
    grounded = 0
    for line in ev:
        low = line.lower()
        hit = any(u.lower() in low for u in provided_uids if u)
        hit = hit or any(n.lower() in low for n in provided_names if len(n) > 3)
        hit = hit or bool(re.search(r"(?:19|20)\d{2}", line))   # 引用了某一年
        grounded += 1 if hit else 0
    clean["groundedness"] = round(grounded / len(ev), 3) if ev else 0.0
    if ev and clean["groundedness"] < 0.5:
        issues.append("接地度 < 0.5（多数 evidence 未引用给定材料）")

    bq = str(obj.get("bridge_quality", "")).strip()
    clean["bridge_quality"] = bq
    if bq not in BRIDGE_QUALITY:
        issues.append("bridge_quality 不在枚举内: %r" % bq)

    alt = _as_str_list(obj.get("alternative_explanations"))
    clean["alternative_explanations"] = alt
    if not alt:
        issues.append("alternative_explanations 为空")

    chk = _as_str_list(obj.get("falsifiable_checks"))
    clean["falsifiable_checks"] = chk
    if not chk:
        issues.append("falsifiable_checks 为空")

    u = obj.get("uncertainty")
    clean["uncertainty"] = None
    if isinstance(u, (int, float)) and not isinstance(u, bool):
        clean["uncertainty"] = float(u)
        if not (0.0 <= clean["uncertainty"] <= 1.0):
            issues.append("uncertainty 越界")
    else:
        issues.append("uncertainty 非数字（模型常写成字符串）")
    return clean, issues


# ══ 候选选择 ════════════════════════════════════════════════════════════
def select_candidates(rep, k):
    rows = list(rep.get("candidates") or [])
    rows.sort(key=lambda r: (-r.get("adamic_adar", 0), -r.get("common_neighbors", 0)))
    out = []
    for i, r in enumerate(rows[:k], 1):
        row = dict(r)
        row["cand_id"] = "E%04d" % i
        out.append(row)
    return out


def _statement(row):
    return "[EDGE] %s（%s） <-> %s（%s）  AA=%.3f 共享邻居=%d" % (
        row["a"], row.get("type_a"), row["b"], row.get("type_b"),
        row.get("adamic_adar") or 0, int(row.get("common_neighbors") or 0))


# ══ 协议冻结 ════════════════════════════════════════════════════════════
def freeze_spec(sample_candidates_path=CANDIDATES_PATH):
    import yaml
    spec = {
        "spec_version": SPEC_VERSION,
        "frozen_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "prompt_file": _rel(PROMPT_PATH), "prompt_sha256": _sha256(PROMPT_PATH),
        "tool_file": _rel(Path(__file__)), "tool_sha256": _sha256(Path(__file__)),
        "candidates_file": _rel(sample_candidates_path),
        "candidates_sha256": _sha256(sample_candidates_path),
        "model": MODEL, "temperature": TEMPERATURE, "max_tokens": MAX_TOKENS,
        "evidence_window": [2011, CUT],
        "policy": {
            "no_future_labels_in_payload": True,
            "evidence_year_asserted": True,
            "analyst_not_predictor": True,
            "prompt_edit_requires_refreeze": True,
        },
        "note": "改 prompt / 改工具 / 换候选文件后必须重跑 --freeze，否则 --live 拒绝运行",
    }
    SPEC_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SPEC_PATH, "w", encoding="utf-8", newline="\n") as f:
        yaml.safe_dump(spec, f, allow_unicode=True, sort_keys=False, width=100)
    return spec


def assert_spec_fresh(path=SPEC_PATH):
    import yaml
    if not Path(path).exists():
        raise SystemExit("[refused] 未冻结协议。先跑 --freeze（%s）" % _rel(path))
    spec = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    for field, p in (("prompt_sha256", PROMPT_PATH),
                     ("tool_sha256", Path(__file__)),
                     ("candidates_sha256", CANDIDATES_PATH)):
        cur = _sha256(p)
        if spec.get(field) != cur:
            raise SystemExit(
                "[refused] %s 已变更但协议未重冻结：冻结 %s… != 当前 %s…\n"
                "  改了 prompt/工具/候选就必须重跑 --freeze（否则产物与协议脱钩）"
                % (field.split("_")[0], str(spec.get(field))[:16], cur[:16]))
    return spec


# ══ 调用 ════════════════════════════════════════════════════════════════
async def analyze_one(backend, system_prompt, template, ctx, row, sem, spec):
    cid = row["cand_id"]
    t0 = time.time()
    body = build_payload(ctx, row, cid)
    user_msg = render_user_message(template, body) 
    provided_uids = [e["paper_uid"] for e in body["evidence_papers"]]
    provided_names = [body["pair"]["a"]["concept"], body["pair"]["b"]["concept"]] + \
        [s["name"] for s in body["shared_neighbors"]]
    usage, last_err = None, None
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
                except Exception as e:                   # noqa: BLE001
                    return _row(row, ST_JSON_INVALID, t0, usage, body,
                                error="%s: %s" % (type(e).__name__, e),
                                raw=content[:600])
                clean, issues = validate_payload(obj, provided_uids, provided_names)
                if clean is None:
                    return _row(row, ST_SCHEMA_INVALID, t0, usage, body,
                                error="; ".join(issues), raw=content[:600])
                status = ST_OK if not issues else ST_SCHEMA_INVALID
                return _row(row, status, t0, usage, body, clean=clean, issues=issues,
                            raw=(content[:600] if issues else None))
            except TruncatedResponse as e:
                last_err = "TruncatedResponse: %s" % e
                await asyncio.sleep(RETRY_BACKOFF_S * (attempt + 1))
            except Exception as e:                       # noqa: BLE001
                last_err = "%s: %s" % (type(e).__name__, e)
                wait = (RATE_LIMIT_BACKOFF_S if "429" in str(e)
                        else RETRY_BACKOFF_S * (attempt + 1))
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(wait)
    return _row(row, ST_API_ERROR, t0, usage, body, error=last_err)


def _row(row, status, t0, usage, body, clean=None, issues=None, error=None, raw=None):
    return {
        "cand_id": row["cand_id"],
        "status": status,
        "statement": _statement(row),
        "duration_s": round(time.time() - t0, 1),
        "usage": usage,
        "payload_ref": {"a": row["a"], "b": row["b"],
                        "shared_neighbors": [s["name"] for s in body["shared_neighbors"]],
                        "evidence_uids": [e["paper_uid"] for e in body["evidence_papers"]]},
        "analysis": clean,
        "issues": issues or [],
        "error": error,
        "raw_excerpt": raw,
    }


async def run(rows, spec, concurrency, ctx):
    system_prompt, template = load_prompt()
    if _sha256(PROMPT_PATH) != spec["prompt_sha256"]:
        raise SystemExit("[refused] prompt 与冻结协议不一致（并发中被改？）")
    backend = create_backend(provider="deepseek",
                             api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
                             model=spec["model"])
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(concurrency)
    cached, todo = {}, []
    for row in rows:
        p = CACHE_DIR / (_safe(row["cand_id"]) + ".json")
        if p.exists():
            try:
                cached[row["cand_id"]] = json.loads(p.read_text(encoding="utf-8"))
                continue
            except Exception:                            # noqa: BLE001
                pass
        todo.append(row)
    print("[run] 命中缓存 %d | 待分析 %d | 并发 %d" % (len(cached), len(todo), concurrency))
    out = list(cached.values())
    if todo:
        tasks = [analyze_one(backend, system_prompt, template, ctx, r, sem, spec)
                 for r in todo]
        done = 0
        for coro in asyncio.as_completed(tasks):
            row = await coro
            out.append(row)
            (CACHE_DIR / (_safe(row["cand_id"]) + ".json")).write_text(
                json.dumps(row, ensure_ascii=False, indent=1), encoding="utf-8")
            done += 1
            if done % 2 == 0 or done == len(todo):
                ok = sum(1 for r in out if r["status"] == ST_OK)
                print("  [%d/%d] 累计 OK %d/%d" % (done, len(todo), ok, len(out)),
                      flush=True)
    order = {r["cand_id"]: i for i, r in enumerate(rows)}
    out.sort(key=lambda r: order.get(r["cand_id"], 10 ** 9))
    return out


def summarize(rows, spec):
    ok = [r for r in rows if r["status"] == ST_OK]
    tin = sum((r["usage"] or {}).get("prompt_tokens", 0) for r in rows)
    tout = sum((r["usage"] or {}).get("completion_tokens", 0) for r in rows)
    unc = [r["analysis"]["uncertainty"] for r in ok
           if r["analysis"] and r["analysis"].get("uncertainty") is not None]
    grd = [r["analysis"]["groundedness"] for r in ok if r["analysis"]]
    return {
        "n_candidates": len(rows), "n_ok": len(ok),
        "status_counts": dict(__import__("collections").Counter(r["status"] for r in rows)),
        "tokens_in": tin, "tokens_out": tout,
        "cost_estimate_usd": round(tin / 1e6 * PRICE_IN + tout / 1e6 * PRICE_OUT, 4),
        "uncertainty_mean": round(sum(unc) / len(unc), 3) if unc else None,
        "uncertainty_high_gt_0_6": sum(1 for u in unc if u > 0.6),
        "groundedness_mean": round(sum(grd) / len(grd), 3) if grd else None,
        "bridge_quality": dict(__import__("collections").Counter(
            r["analysis"]["bridge_quality"] for r in ok if r["analysis"])),
        "issues_total": sum(len(r["issues"]) for r in rows),
        "spec": spec["spec_version"],
    }


def print_summary(s, rows):
    print("=" * 78)
    print("  边层 LLM Analyst（解释层，非预测层）  spec=%s" % s["spec"])
    print("  候选 %d | OK %d | %s" % (s["n_candidates"], s["n_ok"], s["status_counts"]))
    print("  token in/out %d/%d | 估算 $%s" % (s["tokens_in"], s["tokens_out"],
                                              s["cost_estimate_usd"]))
    print("  uncertainty 均值 %s | 高(>0.6) %d | 接地度均值 %s | 校验问题 %d 条"
          % (s["uncertainty_mean"], s["uncertainty_high_gt_0_6"],
             s["groundedness_mean"], s["issues_total"]))
    print("  桥质量分布 %s" % s["bridge_quality"])
    for r in rows:
        a = r.get("analysis")
        if not a:
            continue
        print("  - %s" % r["statement"])
        print("      桥 %s | u=%s 接地=%s" % (a["bridge_quality"], a["uncertainty"],
                                            a["groundedness"]))
        if a.get("shared_structure"):
            print("      %s" % a["shared_structure"][:150])
    print("=" * 78)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--freeze", action="store_true", help="冻结协议")
    ap.add_argument("--live", action="store_true", help="真实调用 LLM（需 DEEPSEEK_API_KEY）")
    ap.add_argument("--k", type=int, default=8, help="取 AA 排名前 k 条候选")
    ap.add_argument("--concurrency", type=int, default=CONCURRENCY)
    ap.add_argument("--candidates", default=str(CANDIDATES_PATH))
    ap.add_argument("--out", default=str(OUT_PATH))
    args = ap.parse_args(argv)

    if args.freeze:
        spec = freeze_spec(Path(args.candidates))
        print("[frozen] %s" % _rel(SPEC_PATH))
        print("  prompt_sha256=%s…" % spec["prompt_sha256"][:16])
        print("  tool_sha256=%s…" % spec["tool_sha256"][:16])
        print("  candidates_sha256=%s…" % spec["candidates_sha256"][:16])
        return 0

    rep = json.loads(Path(args.candidates).read_text(encoding="utf-8"))
    rows = select_candidates(rep, args.k)
    print("[in] %s | 候选 %d | 取前 %d" % (_rel(args.candidates),
                                           len(rep.get("candidates") or []), len(rows)))
    for r in rows:
        print("   %s  %s" % (r["cand_id"], _statement(r)))
    if not args.live:
        print("\n[dry-run] 未调用 LLM。加 --live 执行（需先 --freeze）")
        return 0

    spec = assert_spec_fresh()
    ctx = load_context()
    print("[ctx] 早期图 端点 %d | 早期论文 %d（<= %d）"
          % (len(ctx["g"]["nodes"]), len(ctx["early"]), CUT))
    results = asyncio.run(run(rows, spec, args.concurrency, ctx))
    s = summarize(results, spec)
    print_summary(s, results)

    payload = {
        "analyst_version": SPEC_VERSION,
        "finished_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "inputs": {
            "candidates_file": _rel(args.candidates),
            "candidates_sha256": _sha256(args.candidates),
            "concepts_file": _rel(ctx["concepts_file"]),
            "concepts_sha256": _sha256(ctx["concepts_file"]),
            "spec_file": _rel(SPEC_PATH), "spec_sha256": _sha256(SPEC_PATH),
            "tool_file": _rel(Path(__file__)), "tool_sha256": _sha256(Path(__file__)),
        },
        "spec": {k: spec[k] for k in ("spec_version", "frozen_at", "prompt_sha256",
                                      "model", "temperature")},
        "summary": s,
        "analyses": results,
        "note": ("解释层产物：**不作为预测结论**。候选是否成真由 2021-2025 文献独立判定；"
                 "本文件只提供机制解释、桥质量与可证伪项。输入已结构性排除未来标签。"),
    }
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    print("\n[ok] 分析 -> %s" % _rel(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
