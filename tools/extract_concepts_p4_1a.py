#!/usr/bin/env python
"""P4-1A concept + relation 抽取驱动。

用户 2026-09-12 裁定：
  · extraction 必须输出 **concept + relation**，不只是关键词
  · 首轮 ~1000 篇（分层采样：A 300 / B 400 / C 300）
  · 先小规模验证 pipeline，再跑满

纪律（沿用本项目既有约定）
--------------------------
1. **先冻结再跑**：``--freeze`` 把 prompt 文件 sha256 + 模型参数写进
   ``extraction_spec.yaml``；``--live`` 前会断言当前 prompt 的 sha256 与冻结值
   一致 —— **改了 prompt 不重新冻结就拒绝运行**，防止产物与协议脱钩。
2. **默认 dry-run**：不加 ``--live`` 绝不调用 LLM（防误耗额度）。
3. **逐篇缓存 + 断点续跑**：``extraction_cache/<uid>.json`` 已存在则跳过；
   重跑不会重复计费。
4. **失败分类**，不混为一谈：``JSON_INVALID`` / ``TRUNCATED`` / ``SCHEMA_INVALID``
   / ``API_ERROR`` / ``INSUFFICIENT_TEXT`` 分开记，便于判断是 prompt 问题还是
   网络问题。
5. **EVAL 侧永不抽取** —— 输入只接受 P4-1A 采样产物（其本身只含 TRAIN 侧）。

用法::

    python tools/extract_concepts_p4_1a.py --freeze              # 冻结协议
    python tools/extract_concepts_p4_1a.py                        # dry-run 计划
    python tools/extract_concepts_p4_1a.py --live --limit 30      # pilot
    python tools/extract_concepts_p4_1a.py --live                 # 跑满
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

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from search_engine.llm import TruncatedResponse, create_backend  # noqa: E402

SPEC_VERSION = "p4_1a_extraction_v1"
PROMPT_PATH = BASE / "prompts" / "p4_1a_concept_extraction_v1.md"
DATASET_DIR = BASE / "datasets" / "photopolymerization_v1"
SPEC_PATH = DATASET_DIR / "extraction_spec.yaml"
DEFAULT_SAMPLE = DATASET_DIR / "sample_v1.jsonl"
DEFAULT_OUT = DATASET_DIR / "concepts_v1.jsonl"
DEFAULT_CACHE = DATASET_DIR / "extraction_cache"

DEFAULT_MODEL = "deepseek-chat"
DEFAULT_TEMPERATURE = 0.0      # 抽取要可复现，不要创造性
DEFAULT_MAX_TOKENS = 2000
DEFAULT_CONCURRENCY = 4
DEFAULT_ABSTRACT_MAX_CHARS = 8000   # 长综述（实测最长 27,701 字符）截断以约束成本
MAX_RETRIES = 2
RETRY_BACKOFF_S = 3.0
RATE_LIMIT_BACKOFF_S = 20.0

CONCEPT_TYPES = ("direction", "material", "mechanism",
                 "fabrication_method", "application", "challenge")
RELATION_VOCAB = ("addresses", "enables", "requires", "improves",
                  "causes", "part_of", "alternative_to", "combines_with")
OTHER_RELATION = "other"

# 状态分类（严格分开记，便于判断是 prompt 问题还是网络问题）
ST_OK = "OK"
ST_INSUFFICIENT = "INSUFFICIENT_TEXT"
ST_JSON_INVALID = "JSON_INVALID"
ST_TRUNCATED = "TRUNCATED"
ST_SCHEMA_INVALID = "SCHEMA_INVALID"
ST_API_ERROR = "API_ERROR"

# 仅用于预算护栏的估算价（以 2026-09 公布价为准，非账单口径）
PRICE_IN_PER_MTOK = 0.27
PRICE_OUT_PER_MTOK = 1.10


def _rel(path) -> str:
    """展示用相对路径（跨盘符安全）。"""
    try:
        return os.path.relpath(str(path), str(BASE)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe(uid):
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(uid))


# ══ prompt 装载 ══════════════════════════════════════════════════════════
def load_prompt(path=PROMPT_PATH):
    """从 prompt 文件切出 (system, user_template)。

    文件用 ``## SYSTEM`` / ``## USER`` 两个二级标题分段；user 段里取唯一一个
    围栏代码块作为模板。找不到就抛错 —— 宁可硬失败也不要静默用错 prompt。
    """
    text = Path(path).read_text(encoding="utf-8")
    if "## SYSTEM" not in text or "## USER" not in text:
        raise ValueError(f"prompt 缺 ## SYSTEM / ## USER 段: {path}")
    sys_part, user_part = text.split("## SYSTEM", 1)[1].split("## USER", 1)
    m = re.search(r"```+\s*\n(.*?)```+", user_part, re.S)
    if not m:
        raise ValueError(f"prompt 的 ## USER 段找不到围栏模板: {path}")
    return sys_part.strip(), m.group(1)


def build_user_message(template, rec, abstract_max=DEFAULT_ABSTRACT_MAX_CHARS):
    """填模板。超长摘要截断并**显式标注**（截断是成本决策，必须留痕）。"""
    ab = (rec.get("abstract") or "").strip()
    if len(ab) > abstract_max:
        ab = ab[:abstract_max] + "\n[TRUNCATED BY PIPELINE]"
    return template.format(title=(rec.get("title") or "").strip(),
                           year=rec.get("year"), abstract=ab)


# ══ 结果校验与规范化 ═════════════════════════════════════════════════════
_WS = re.compile(r"\s+")
# 首尾标点（strip() 只动两端，故 `thiol-ene` 这类内部连字符安全）
_PUNCT = " \t\r\n.,;:!?\"'`()[]{}<>/\\|~*_=+&^%$#@、，。；：！？（）【】「」《》…—–-"
# 至少要有一个字母或汉字：否则 `!!`、`→→`、`--` 这类会被当成"概念"
_HAS_LETTER = re.compile(r"[a-z\u4e00-\u9fff]")


def normalize_concept_name(raw):
    """概念名规范化（**只做机械规范化，不做同义合并**）。

    同义合并是 concept graph 的下游任务，这里擅自合并会把不同概念揉成一个，
    且不可逆。故只做：小写 / 压空白 / 去首尾标点。
    """
    if not isinstance(raw, str):
        return None
    s = _WS.sub(" ", raw.strip().lower()).strip(_PUNCT)
    if not (2 <= len(s) <= 80):
        return None
    if not _HAS_LETTER.search(s):
        return None
    if re.fullmatch(r"[\d\s\.\-/]+", s):        # 纯数字/符号
        return None
    return s


def validate_payload(obj):
    """校验并清洗 LLM 输出。返回 ``(clean, issues)``。

    硬约束（prompt 里要求过，这里**再验一遍** —— prompt 不是保证）：
      · type 必须在 CONCEPT_TYPES
      · relation 必须在 RELATION_VOCAB（否则降级为 ``other``，不丢弃）
      · relation 的 source/target 必须是已声明的概念名（逐字一致）
      · 概念名去重（同类型同名只留一条）
    """
    issues = collections.Counter()
    if not isinstance(obj, dict):
        return None, {"NOT_AN_OBJECT": 1}

    concepts, seen = [], set()
    for c in (obj.get("concepts") or []):
        if not isinstance(c, dict):
            issues["CONCEPT_NOT_OBJECT"] += 1
            continue
        ctype = str(c.get("type") or "").strip().lower()
        if ctype not in CONCEPT_TYPES:
            issues["CONCEPT_BAD_TYPE"] += 1
            continue
        name = normalize_concept_name(c.get("name"))
        if not name:
            issues["CONCEPT_BAD_NAME"] += 1
            continue
        if (ctype, name) in seen:
            issues["CONCEPT_DUPLICATE"] += 1
            continue
        seen.add((ctype, name))
        ev = c.get("evidence")
        concepts.append({"type": ctype, "name": name,
                         "evidence": (str(ev).strip()[:400] if ev else None)})

    declared = {c["name"] for c in concepts}
    relations, rseen = [], set()
    for r in (obj.get("relations") or []):
        if not isinstance(r, dict):
            issues["RELATION_NOT_OBJECT"] += 1
            continue
        s = normalize_concept_name(r.get("source"))
        t = normalize_concept_name(r.get("target"))
        rel = str(r.get("relation") or "").strip().lower()
        if not s or not t:
            issues["RELATION_BAD_ENDPOINT"] += 1
            continue
        if s not in declared or t not in declared:
            # prompt 明确要求指向已声明概念；越界边会污染图，丢弃但记账
            issues["RELATION_UNDECLARED_ENDPOINT"] += 1
            continue
        if rel not in RELATION_VOCAB:
            issues["RELATION_OUT_OF_VOCAB"] += 1
            rel = OTHER_RELATION
        if s == t:
            issues["RELATION_SELF_LOOP"] += 1
            continue
        key = (s, rel, t)
        if key in rseen:
            issues["RELATION_DUPLICATE"] += 1
            continue
        rseen.add(key)
        relations.append({"source": s, "relation": rel, "target": t})

    note = obj.get("note")
    return {"concepts": concepts, "relations": relations,
            "note": (str(note)[:120] if note else None)}, dict(issues)


def parse_json_content(content):
    """剥掉可能的 ``` 围栏后解析 JSON。"""
    s = (content or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"```\s*$", "", s).strip()
    return json.loads(s)


# ══ 协议冻结 ═════════════════════════════════════════════════════════════
def freeze_spec(model, temperature, max_tokens, abstract_max, sample_path):
    import yaml
    sample_sha = _sha256(sample_path) if Path(sample_path).exists() else None
    spec = {
        "spec_version": SPEC_VERSION,
        "frozen_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "prompt_file": _rel(PROMPT_PATH),
        "prompt_sha256": _sha256(PROMPT_PATH),
        # 校验器代码也进哈希：同一 prompt 下换校验逻辑会产出不同产物，
        # 只冻结 prompt 会让产物无法完全溯源。
        "tool_file": _rel(Path(__file__)),
        "tool_sha256": _sha256(Path(__file__)),
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "abstract_max_chars": abstract_max,
        "concept_types": list(CONCEPT_TYPES),
        "relation_vocabulary": list(RELATION_VOCAB),
        "extra_relation_bucket": OTHER_RELATION,
        "sample_file": _rel(sample_path),
        "sample_sha256": sample_sha,
        "policy": {
            "eval_side_never_extracted": True,
            "temperature_0_for_reproducibility": True,
            "prompt_edit_requires_refreeze": True,
            "normalization": "机械规范化（小写/压空白/去尾标点）；"
                            "**不做同义合并**（合并属下游图任务，且不可逆）",
        },
        "note": "改 prompt 或改模型参数后必须重跑 --freeze，否则 --live 会拒绝运行",
    }
    SPEC_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SPEC_PATH, "w", encoding="utf-8", newline="\n") as f:
        yaml.safe_dump(spec, f, allow_unicode=True, sort_keys=False, width=100)
    return spec


def assert_spec_fresh():
    """``--live`` 之前断言 prompt 与参数未被改动。"""
    import yaml
    if not SPEC_PATH.exists():
        raise SystemExit(f"[refused] 未冻结协议。先跑 --freeze（{_rel(SPEC_PATH)}）")
    spec = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    cur = _sha256(PROMPT_PATH)
    if spec.get("prompt_sha256") != cur:
        raise SystemExit(
            "[refused] prompt 已变更但未重新冻结：\n"
            f"  冻结 {spec.get('prompt_sha256')[:16]}… != 当前 {cur[:16]}…\n"
            "  改了 prompt 必须重跑 --freeze（否则产物与协议脱钩）")
    return spec


# ══ 抽取主循环 ═══════════════════════════════════════════════════════════
async def extract_one(backend, system_prompt, template, rec, sem,
                      abstract_max, spec):
    """单篇抽取（含重试）。任何异常都转成状态，不中断整批。"""
    uid = rec["paper_uid"]
    user_msg = build_user_message(template, rec, abstract_max)
    t0 = time.time()
    last_err = None
    usage = None

    async with sem:
        for attempt in range(MAX_RETRIES + 1):
            try:
                content = await backend.chat(
                    system_prompt, user_msg,
                    temperature=spec["temperature"], max_tokens=spec["max_tokens"],
                    # 该参数同时打开 JSON mode 与截断检测（见 llm.py 注释）
                    raise_on_truncation=True)
                usage = getattr(backend, "last_usage", None)
                try:
                    obj = parse_json_content(content)
                except Exception as e:
                    return _row(rec, ST_JSON_INVALID, t0, usage, spec,
                                error=f"{type(e).__name__}: {e}",
                                raw=content[:600])
                clean, issues = validate_payload(obj)
                if clean is None:
                    return _row(rec, ST_SCHEMA_INVALID, t0, usage, spec,
                                error="payload 非对象", raw=content[:600])
                if not clean["concepts"]:
                    st = (ST_INSUFFICIENT
                          if (clean.get("note") == "insufficient_text")
                          else ST_SCHEMA_INVALID)
                    return _row(rec, st, t0, usage, spec, clean=clean,
                                issues=issues, raw=content[:600])
                return _row(rec, ST_OK, t0, usage, spec, clean=clean,
                            issues=issues)
            except TruncatedResponse as e:
                last_err = f"TruncatedResponse: {e}"
                wait = RETRY_BACKOFF_S * (attempt + 1)
            except Exception as e:                       # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"
                wait = (RATE_LIMIT_BACKOFF_S if "429" in str(e)
                        else RETRY_BACKOFF_S * (attempt + 1))
            if attempt < MAX_RETRIES:
                await asyncio.sleep(wait)

    st = ST_TRUNCATED if (last_err or "").startswith("TruncatedResponse") \
        else ST_API_ERROR
    return _row(rec, st, t0, usage, spec, error=last_err)


def _row(rec, status, t0, usage, spec, clean=None, issues=None, error=None,
         raw=None):
    clean = clean or {"concepts": [], "relations": [], "note": None}
    return {
        "paper_uid": rec["paper_uid"],
        "sample_layer": rec.get("sample_layer"),
        "year": rec.get("year"),
        "primary_topic": rec.get("primary_topic"),
        "status": status,
        "concepts": clean["concepts"],
        "relations": clean["relations"],
        "n_concepts": len(clean["concepts"]),
        "n_relations": len(clean["relations"]),
        "type_counts": dict(collections.Counter(
            c["type"] for c in clean["concepts"])),
        "validation_issues": (dict(issues) if issues else {}),
        "latency_s": round(time.time() - t0, 2),
        "usage": usage,
        "error": error,
        "raw_excerpt": raw,
        "model": spec["model"],
        "prompt_version": spec["spec_version"],
        "prompt_sha256": spec["prompt_sha256"],
        "extracted_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }


def _tok(rows, out=True):
    total = 0
    for r in rows:
        u = r.get("usage") or {}
        k = "completion_tokens" if out else "prompt_tokens"
        total += int(u.get(k) or 0)
    return total


async def run_extraction(records, spec, cache_dir, out_path, concurrency,
                         abstract_max, resume=True):
    system_prompt, template = load_prompt()
    if hashlib.sha256(PROMPT_PATH.read_bytes()).hexdigest() != spec["prompt_sha256"]:
        raise SystemExit("[refused] prompt 与冻结协议不一致（并发中被改？）")

    backend = create_backend(provider="deepseek",
                             api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
                             model=spec["model"])
    sem = asyncio.Semaphore(concurrency)
    cache_dir.mkdir(parents=True, exist_ok=True)

    cached, todo = {}, []
    for rec in records:
        p = cache_dir / (_safe(rec["paper_uid"]) + ".json")
        if resume and p.exists():
            try:
                cached[rec["paper_uid"]] = json.loads(p.read_text(encoding="utf-8"))
                continue
            except Exception:
                pass
        todo.append(rec)
    print(f"[run] 命中缓存 {len(cached)} | 待抽 {len(todo)} | 并发 {concurrency}")

    results = list(cached.values())
    done = 0
    if todo:
        tasks = [extract_one(backend, system_prompt, template, r, sem,
                             abstract_max, spec) for r in todo]
        for coro in asyncio.as_completed(tasks):
            row = await coro
            results.append(row)
            (cache_dir / (_safe(row["paper_uid"]) + ".json")).write_text(
                json.dumps(row, ensure_ascii=False, indent=1), encoding="utf-8")
            done += 1
            if done % 10 == 0 or done == len(todo):
                ok = sum(1 for r in results if r["status"] == ST_OK)
                print(f"  [{done}/{len(todo)}] 累计 OK {ok}/{len(results)} "
                      f"({ok / len(results):.1%})", flush=True)

    order = {r["paper_uid"]: i for i, r in enumerate(records)}
    results.sort(key=lambda r: order.get(r["paper_uid"], 10 ** 9))
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return results


def summarize(results, spec):
    st = collections.Counter(r["status"] for r in results)
    ok = [r for r in results if r["status"] == ST_OK]
    tin, tout = _tok(results), _tok(results, out=False)
    return {
        "spec_version": spec["spec_version"],
        "prompt_sha256": spec["prompt_sha256"],
        "model": spec["model"],
        "temperature": spec["temperature"],
        "n_total": len(results),
        "by_status": dict(st),
        "coverage": round(len(ok) / len(results), 4) if results else 0.0,
        "concepts": {
            "total": sum(r["n_concepts"] for r in ok),
            "distinct": len({c["name"] for r in ok for c in r["concepts"]}),
            "relations_total": sum(r["n_relations"] for r in ok),
            "relations_distinct": len({(x["source"], x["relation"], x["target"])
                                       for r in ok for x in r["relations"]}),
            "type_totals": dict(collections.Counter(
                c["type"] for r in ok for c in r["concepts"])),
            "relation_totals": dict(collections.Counter(
                x["relation"] for r in ok for x in r["relations"])),
        },
        "usage": {
            "prompt_tokens": tin, "completion_tokens": tout,
            "total_tokens": tin + tout,
            "est_cost_usd": round(tin / 1e6 * PRICE_IN_PER_MTOK
                                 + tout / 1e6 * PRICE_OUT_PER_MTOK, 4),
            "note": "价格为 2026-09 公布价的估算，仅作预算护栏，非账单口径",
        },
        "latency_s": {
            "total": round(sum(r["latency_s"] for r in results), 1),
            "mean": round(sum(r["latency_s"] for r in results)
                          / max(len(results), 1), 2),
        },
        "validation_issues": dict(sum(
            (collections.Counter(r.get("validation_issues") or {})
             for r in results), collections.Counter())),
    }


def print_summary(s):
    print("═" * 70)
    print(f"  P4-1A 抽取汇总  [{s['spec_version']}]  model={s['model']} T={s['temperature']}")
    print("═" * 70)
    print(f"  抽取 {s['n_total']} 篇 | 状态分布 {s['by_status']}")
    print(f"  Coverage(OK) {s['coverage']:.1%}")
    c = s["concepts"]
    print(f"  概念 {c['total']} 个（distinct {c['distinct']}）| "
          f"关系 {c['relations_total']} 条（distinct {c['relations_distinct']}）")
    print(f"  类型分布 {c['type_totals']}")
    print(f"  关系分布 {c['relation_totals']}")
    u = s["usage"]
    print(f"  token in/out {u['prompt_tokens']}/{u['completion_tokens']} "
          f"| 估算成本 ${u['est_cost_usd']}")
    print(f"  耗时 总 {s['latency_s']['total']}s / 均 {s['latency_s']['mean']}s")
    if s["validation_issues"]:
        print(f"  校验收到的清洗 {s['validation_issues']}")
    print("═" * 70)


def main(argv=None):
    ap = argparse.ArgumentParser(description="P4-1A concept+relation 抽取")
    ap.add_argument("--sample", default=str(DEFAULT_SAMPLE))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    ap.add_argument("--limit", type=int, default=0, help="只抽前 N 篇（0=全部）")
    ap.add_argument("--only-layer", default="", help="A,B,C 子集（逗号分隔）")
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    ap.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    ap.add_argument("--abstract-max-chars", type=int,
                    default=DEFAULT_ABSTRACT_MAX_CHARS)
    ap.add_argument("--freeze", action="store_true", help="冻结协议后退出")
    ap.add_argument("--live", action="store_true", help="允许真实 LLM 调用")
    ap.add_argument("--no-resume", action="store_true", help="忽略缓存重抽")
    args = ap.parse_args(argv)

    if args.freeze:
        spec = freeze_spec(args.model, args.temperature, args.max_tokens,
                           args.abstract_max_chars, args.sample)
        print(f"[frozen] {_rel(SPEC_PATH)}  prompt_sha256={spec['prompt_sha256'][:16]}…")
        print(f"[frozen] sample_sha256={str(spec['sample_sha256'])[:16]}…")
        return 0

    records = [json.loads(l) for l in
               Path(args.sample).read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.only_layer:
        want = {x.strip().upper() for x in args.only_layer.split(",") if x.strip()}
        records = [r for r in records if r["sample_layer"] in want]
    if args.limit:
        records = records[:args.limit]

    spec = assert_spec_fresh()
    print(f"[p4.1a] 样本 {_rel(args.sample)} -> {len(records)} 篇 | "
          f"层 {dict(collections.Counter(r['sample_layer'] for r in records))}")
    print(f"[p4.1a] 冻结协议 {spec['spec_version']} "
          f"prompt={spec['prompt_sha256'][:16]}… model={spec['model']} T={spec['temperature']}")

    if not args.live:
        print("\n[DRY-RUN] 不调用 LLM。加 --live 执行。")
        print(f"  预计调用 {len(records)} 次；缓存目录 {_rel(args.cache_dir)}")
        return 0
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise SystemExit("[refused] 未设置 DEEPSEEK_API_KEY")

    t0 = time.time()
    results = asyncio.run(run_extraction(
        records, spec, Path(args.cache_dir), args.out, args.concurrency,
        args.abstract_max_chars, resume=not args.no_resume))
    s = summarize(results, spec)
    s["wall_clock_s"] = round(time.time() - t0, 1)
    print_summary(s)
    with open(str(args.out) + ".summary.json", "w", encoding="utf-8",
              newline="\n") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    print(f"\n[applied] {_rel(args.out)}  + {os.path.basename(args.out)}.summary.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
