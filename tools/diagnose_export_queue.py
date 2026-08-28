"""tools/diagnose_export_queue.py — F0b 根因隔离实验（用户 2026-08-28 定稿）。

目标：证实/证伪「bulk-jobs 队列污染导致 AND query 导出 RETRY」假设。
方法：同一 AND query × {limit=1000, limit=2000}，dirty queue vs clean queue 的 A/B。
原则：不改 search_engine/engine.py、不改 tools/verify_export_depth.py——独立实验脚本，
      F0b 根因钉死后才决定怎么修主链。

判断矩阵（用户冻结）：
  dirty 1000/2000 都失败, clean 都成功        → QUEUE_CONTAMINATION_CONFIRMED
  dirty 1000 成功 2000 失败, clean 仍 2000 失败 → BULK_SIZE_LIMIT（export-service 对 2000 的问题）
  AND 一直失败但单 query 成功                 → SESSION_OR_SYNTAX（query/export session、encoding）
  干净队列仍随机 RETRY                        → EXPORT_JOB_UNSTABLE（需要 fallback）

⚠️ 队列清理策略（用户定）：inspect → 等待 active job 全部 terminal（COMPLETED/FAILED）→
   未发现官方 cancel 接口时不强制删除内部 job；clean = 无 active job。

用法：
  python tools/diagnose_export_queue.py                     # queue A/B 主实验
  python tools/diagnose_export_queue.py --query 'TITLE-ABS-KEY("polymerization shrinkage" AND "photopolymer")' --limit1 1000 --limit2 2000
  python tools/diagnose_export_queue.py --empty-audit       # 17 空 query total_hits 审计（真 0 vs F0）
"""
import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

EXPORT_BASE = "https://www.scopus.com/gateway/export-service-reactive/export"
EXPORT_ENDPOINT = EXPORT_BASE + "/bulk-job/initiate"
OUT_PATH = os.path.join(BASE, "data", "exports", "export_queue_diagnosis.json")
DEFAULT_QUERY = 'TITLE-ABS-KEY("polymerization shrinkage" AND "composite")'
EMPTY_AUDIT_PATH = os.path.join(BASE, "data", "exports", "execution_completeness_audit.json")
DEPTH_RUN_PATH = os.path.join(BASE, "data", "exports", "query_family_runs_depth.json")
# depth run 里同 query 曾 SUCCESS（limit=1000）的 AND query——历史证据（来自 FAM_PM_001003）
# 选 "polymerization shrinkage" AND "composite"：depth run FAM_PM_001003::24/25 = 1000 条
TERMINAL = ("COMPLETED", "FAILED")
ACTIVE = ("PENDING", "PROCESSING", "RETRY", "SUBMITTED")
SORT_SPEC = [{"fieldName": "datesort", "order": "desc"},
             {"fieldName": "relevance", "order": "desc"}]


def request_fingerprint(query: str, limit: int, offset: int, fields: list[str]) -> dict:
    """export 请求指纹（与 verify_export_depth.py 同构，用于两条路径对比）。"""
    payload = {
        "query": query,
        "limit": limit,
        "offset": offset,
        "sort": SORT_SPEC,
        "field_groups": fields,
        "locale": "zh-CN",
        "endpoint": EXPORT_ENDPOINT,
        "document_classification": "PRIMARY",
        "export_type": "PUBLICATION",
        "file_type": "CSV",
    }
    h = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    return {"payload": payload, "sha256": h}


def build_body(query: str, limit: int, offset: int = 0) -> dict:
    """与 engine._export_via_api 相同的导出请求体（复制自 engine.py，实验专用）。"""
    return {
        "searchRequest": {
            "query": query,
            "documentClassification": "PRIMARY",
            "sortBy": SORT_SPEC,
            "resultSet": {"offset": offset, "itemCount": limit},
        },
        "fileType": "CSV",
        "exportType": "PUBLICATION",
        "fieldGroupIdentifiers": ["eid", "doi"],
        "locale": "zh-CN",
        "userQuery": query,
    }


async def dump_queue(engine) -> dict:
    """dump 当前 bulk-jobs 队列（只读）。"""
    jobs = await engine._page.evaluate(f"""
        async () => {{
            const res = await fetch('{EXPORT_BASE}/bulk-jobs');
            if (!res.ok) return {{ jobs: [] }};
            return await res.json();
        }}
    """)
    all_jobs = jobs.get("jobs", [])
    st = Counter(j.get("status") for j in all_jobs)
    return {"total": len(all_jobs), "status": dict(st),
            "active": sum(st.get(s, 0) for s in ACTIVE),
            "jobs": all_jobs}


async def initiate_export(engine, query: str, limit: int) -> dict:
    """发起导出，返回 {job_id} 或 {error}。发起前打印 request fingerprint。"""
    fp = request_fingerprint(query, limit, 0, ["eid", "doi"])
    print(f"    [fingerprint] sha256={fp['sha256']} limit={limit} offset=0 "
          f"fields={fp['payload']['field_groups']} (无 search 预热)")
    result = await engine._page.evaluate(f"""
        async () => {{
            try {{
                const res = await fetch('{EXPORT_BASE}/bulk-job/initiate', {{
                    method: 'POST',
                    headers: {{ 'content-type': 'application/json' }},
                    body: JSON.stringify({json.dumps(build_body(query, limit))}),
                    credentials: 'include',
                }});
                if (!res.ok) return {{ error: 'HTTP ' + res.status }};
                return await res.json();
            }} catch (e) {{ return {{ error: e.message }}; }}
        }}
    """)
    if not result or result.get("error"):
        return {"error": result or "no response"}
    jid = result.get("bulkExportId")
    if not jid:
        return {"error": "no bulkExportId in response: " + str(result)[:200]}
    return {"job_id": jid}


async def poll_job(engine, job_id: str, timeout: int = 90) -> dict:
    """轮询单 job，记录状态流转（压缩：只记变化点），COMPLETED 时尝试下载确认。"""
    flow: list[str] = []
    last = None
    t0 = time.time()
    final = None
    while time.time() - t0 < timeout:
        await asyncio.sleep(1)
        jobs = await engine._page.evaluate(f"""
            async () => {{
                const res = await fetch('{EXPORT_BASE}/bulk-jobs');
                if (!res.ok) return {{ jobs: [] }};
                return await res.json();
            }}
        """)
        job = next((j for j in jobs.get("jobs", []) if j.get("jobId") == job_id), None)
        st = job.get("status") if job else "NOT_IN_LIST"
        if st != last:
            flow.append(st)
            last = st
        if st in TERMINAL:
            final = st
            break
    return {"job_id": job_id, "final": final,
            "flow": flow, "seconds": round(time.time() - t0, 1),
            "timed_out": final is None}


async def wait_queue_settle(engine, max_wait: int = 180) -> dict:
    """等队列中 active job 全部 terminal；返回 settle 前后 dump。"""
    t0 = time.time()
    last = None
    while time.time() - t0 < max_wait:
        await asyncio.sleep(3)
        q = await dump_queue(engine)
        if q["active"] == 0:
            return {"settled_after_s": round(time.time() - t0, 1), "queue": q}
        if q["active"] != last:
            last = q["active"]
            print(f"    …等待队列稳定: active={q['active']} "
                  f"({', '.join(f'{k}:{v}' for k, v in q['status'].items())})")
    q = await dump_queue(engine)
    return {"settled_after_s": None, "queue": q, "warning": "等待超时，仍有 active job"}


def diagnose(dirty: dict, clean: dict) -> str:
    """判定 F0b 根因（用户 2026-08-28 定稿矩阵 + 实测修正）。

    实测（queue active=0）：dirty 1000/2000 都 COMPLETED，clean 也 COMPLETED
    → TRANSIENT_EXPORT_INSTABILITY_NOT_REPRODUCED（排除 queue 污染/bulk size/syntax）。
    """
    d1, d2 = dirty.get("limit_1000", "?")[:8], dirty.get("limit_2000", "?")[:8]
    c1, c2 = clean.get("limit_1000", "?")[:8], clean.get("limit_2000", "?")[:8]
    d1ok, d2ok = d1 == "COMPLETED", d2 == "COMPLETED"
    c1ok, c2ok = c1 == "COMPLETED", c2 == "COMPLETED"
    if d1ok and d2ok and c1ok and c2ok:
        return "TRANSIENT_EXPORT_INSTABILITY_NOT_REPRODUCED"
    if not d1ok and not d2ok and c1ok and c2ok:
        return "QUEUE_CONTAMINATION_CONFIRMED"
    if d1ok and not d2ok and c1ok and not c2ok:
        return "BULK_SIZE_LIMIT"
    if not d1ok and not d2ok and not c1ok and not c2ok:
        return "EXPORT_JOB_UNSTABLE"   # 全败：稳定复现的失败（syntax/session/不稳定）
    return "MIXED_SIGNAL"


async def queue_ab(engine, query: str, limit1: int, limit2: int) -> dict:
    print("=" * 72)
    print("F0b queue A/B（query =", query[:60], "）")
    print("=" * 72)

    # STEP 1: dump 当前队列（脏基线）
    before = await dump_queue(engine)
    print(f"\n[STEP1] 当前队列: total={before['total']}, active={before['active']}, "
          f"status={before['status']}")
    if before["total"] > 20:
        print(f"  ⚠️ 队列堆积 {before['total']} 个 job（FAILED 大量）——脏队列基线")

    # STEP 2: 脏队列下测试 {limit1, limit2}
    print(f"\n[STEP2] 脏队列测试（不清队列）:")
    dirty = {}
    for lim in (limit1, limit2):
        init = await initiate_export(engine, query, lim)
        if "error" in init:
            dirty[f"limit_{lim}"] = "INITIATE_FAILED"
            print(f"  limit={lim}: initiate 失败 {init['error']}")
            continue
        r = await poll_job(engine, init["job_id"], timeout=90)
        dirty[f"limit_{lim}"] = r["final"] or ("RETRY/" + "→".join(r["flow"])[:40])
        print(f"  limit={lim}: final={r['final']} flow={'→'.join(r['flow'])} "
              f"({r['seconds']}s)")

    # STEP 3: 等待队列稳定（inspect→wait；无官方 cancel 不强制删）
    print("\n[STEP3] 等待队列稳定（inspect→wait，不强制删 job）:")
    settle = await wait_queue_settle(engine, max_wait=180)
    if settle.get("warning"):
        print("  " + settle["warning"])
    after = settle["queue"]
    print(f"  队列: total={after['total']}, active={after['active']}, status={after['status']}")

    # STEP 4: 干净队列下测试
    print(f"\n[STEP4] 干净队列测试:")
    clean = {}
    for lim in (limit1, limit2):
        init = await initiate_export(engine, query, lim)
        if "error" in init:
            clean[f"limit_{lim}"] = "INITIATE_FAILED"
            print(f"  limit={lim}: initiate 失败 {init['error']}")
            continue
        r = await poll_job(engine, init["job_id"], timeout=120)
        clean[f"limit_{lim}"] = r["final"] or ("RETRY/" + "→".join(r["flow"])[:40])
        print(f"  limit={lim}: final={r['final']} flow={'→'.join(r['flow'])} "
              f"({r['seconds']}s)")

    verdict = diagnose(dirty, clean)
    print(f"\n诊断: {verdict}")
    return {"query": query, "limits": [limit1, limit2], "before_queue": {
                "total": before["total"], "active": before["active"],
                "status": before["status"]},
            "dirty_queue_test": dirty,
            "after_queue": {"total": after["total"], "active": after["active"],
                            "status": after["status"]},
            "clean_queue_test": clean,
            "diagnosis": verdict}


async def download_csv(engine, job_id: str) -> str:
    """下载已 COMPLETED job 的 CSV（复制自 engine._export_via_api 的下载段）。"""
    try:
        return await engine._page.evaluate(f"""
            async () => {{
                const genRes = await fetch(
                    '{EXPORT_BASE}/bulk-job/{job_id}/generate-url',
                    {{ method: 'POST' }}
                );
                const genData = await genRes.json();
                if (!genData.presignedUrl) return '';
                const csvRes = await fetch(genData.presignedUrl);
                return await csvRes.text();
            }}
        """)
    except Exception as e:
        return f"__DOWNLOAD_ERROR__{e}"


async def probe_query(engine, query: str, timeout: int = 90) -> dict:
    """limit=1 execution probe：真正发起一次小导出，明确得到状态。

    SUCCESS_WITH_RECORDS（job COMPLETED 且 CSV ≥1 行）→ query 有效
    COMPLETED_EMPTY（job COMPLETED 且 CSV 0 行）→ 显式空（近似 total=0）
    RETRY / FAILED / TIMEOUT / INITIATE_FAILED → 执行失败
    """
    init = await initiate_export(engine, query, 1)
    if "error" in init:
        return {"status": "INITIATE_FAILED", "error": init["error"]}
    r = await poll_job(engine, init["job_id"], timeout=timeout)
    if r["final"] != "COMPLETED":
        return {"status": r["final"] or "TIMEOUT", "flow": r["flow"],
                "seconds": r["seconds"]}
    csv = await download_csv(engine, init["job_id"])
    if csv.startswith("__DOWNLOAD_ERROR__"):
        return {"status": "DOWNLOAD_FAILED", "detail": csv[:80]}
    rows = [ln for ln in csv.splitlines() if ln.strip()][1:]  # 去表头
    return {"status": "COMPLETED", "rows": len(rows), "job_id": init["job_id"]}


async def empty_audit(engine) -> dict:
    """17 个空 query 的 limit=1 execution probe 审计（用户 2026-08-28 定稿）。

    判定（用户冻结）：
      SUCCESS + ≥1 record   → F0_EXECUTION_GAP（原 depth run 空 = 执行缺口）
      SUCCESS + explicit 0  → TRUE_ZERO_CANDIDATE（job COMPLETED 且 CSV 空）
      RETRY/FAILED/timeout  → EXECUTION_FAILED
      无法确认              → UNKNOWN（没有显式 zero 证据不当 TRUE_ZERO）
    """
    records = json.load(open(DEPTH_RUN_PATH, encoding="utf-8"))["records"]
    empty_qs = sorted(k for k, v in records.items() if not v)
    print(f"\n=== 17 empty queries limit=1 execution probe ===")
    out = []
    for i, key in enumerate(empty_qs, 1):
        query = key.split("::", 2)[-1]
        r = await probe_query(engine, query)
        st = r["status"]
        if st == "COMPLETED":
            if r["rows"] > 0:
                cls = "F0_EXECUTION_GAP"
            else:
                cls = "TRUE_ZERO_CANDIDATE"   # job COMPLETED + CSV 0 行
        elif st in ("RETRY", "FAILED", "TIMEOUT", "INITIATE_FAILED",
                    "DOWNLOAD_FAILED"):
            cls = "EXECUTION_FAILED"
        else:
            cls = "UNKNOWN"
        out.append({"query": key, "probe": r, "class": cls})
        print(f"  [{i:>2}] {cls:<22} probe={st} rows={r.get('rows', '-')} "
              f"flow={r.get('flow', '-')} | {query[:48]}")
        await asyncio.sleep(1)
    c = Counter(o["class"] for o in out)
    f0 = c.get("F0_EXECUTION_GAP", 0)
    tz = c.get("TRUE_ZERO_CANDIDATE", 0)
    print(f"\n审计结果: {dict(c)}")
    print(f"QER 修正预览: (130 有记录 + {tz} 真0候选) / 147 = "
          f"{(130 + tz) / 147 * 100:.1f}%（真0候选待用户拍板）")
    return {"audited": len(out), "classes": dict(c), "items": out}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default=DEFAULT_QUERY)
    ap.add_argument("--limit1", type=int, default=1000)
    ap.add_argument("--limit2", type=int, default=2000)
    ap.add_argument("--empty-audit", action="store_true",
                    help="17 空 query total_hits 审计（不跑 queue A/B）")
    args = ap.parse_args()

    from search_engine.engine import ScopusSearchEngine
    engine = ScopusSearchEngine()
    await engine.start()   # 必须先启动（已踩坑：漏 start → 'NoneType' url）
    result = {}
    try:
        if args.empty_audit:
            result = {"mode": "empty_audit",
                      "empty_audit": await empty_audit(engine)}
        else:
            result = await queue_ab(engine, args.query, args.limit1, args.limit2)
            result["mode"] = "queue_ab"
    finally:
        await engine.close()

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print(f"\n✓ 已写: {OUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
