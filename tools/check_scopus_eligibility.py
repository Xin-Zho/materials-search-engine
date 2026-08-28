"""tools/check_scopus_eligibility.py — B5 Scopus eligibility 检查（用户定 2026-08-27）。

对已冻结的 pc_001_external_qgs_v1.json（RELEVANT）逐篇查 Scopus 收录：
  DOI 批量检索（每 50 个一批：DOI(d1 OR d2 ...)）→ 导出 CSV 拿 EID → 匹配
输出：
  B_total / B_scopus（含 EID）/ B_not_scopus（= database coverage limitation）
并写回 benchmark 文件（加 in_scopus + scopus_eid 字段）。

⚠️ 顺序固定：先 relevance（B4 已完）后 Scopus——B_not_scopus 仍需报告其相关
性（顺带量化 Scopus 对独立 QGS 的 source coverage）。

运行（需已登录的 Scopus 会话）：
  python tools/check_scopus_eligibility.py [--batch 50]
"""
import argparse
import asyncio
import csv
import io
import json
import os
import re
import sys

sys.path.insert(0, ".")
from search_engine.engine import ScopusSearchEngine

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH_PATH = os.path.join(BASE, "data", "exports", "pc_001_external_qgs_v1.json")


def norm_doi(d: str) -> str:
    return (d or "").strip().lower().replace("https://doi.org/", "").replace("http://doi.org/", "").strip()


def parse_rows(csv_text: str) -> list[dict]:
    if not csv_text.strip():
        return []
    out = []
    for row in csv.DictReader(io.StringIO(csv_text)):
        out.append({
            "doi": norm_doi(row.get("DOI", "")),
            "eid": (row.get("EID") or "").strip(),
            "title": (row.get("Title") or row.get("文献标题") or "").strip(),
        })
    return out


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=50, help="每批 DOI 数（Scopus OR 上限内）")
    args = ap.parse_args()

    bench = json.load(open(BENCH_PATH, encoding="utf-8"))
    papers = bench["papers"]
    print(f"benchmark RELEVANT: {len(papers)} 篇")

    # 增量模式（2026-08-27 定）：只查 PENDING / 无 eligibility 的论文。
    # 已确认的（IN_SCOPUS / NOT_IN_SCOPUS / NOT_CHECKABLE）不重查、不覆盖——
    # 合并 adjudication 新增 RELEVANT 后重跑本脚本，105 篇已有 EID 证据不动，
    # 只处理新增 16 篇（B_pending=16 → 查完归位）。
    pending = [p for p in papers
               if not p.get("scopus_eligibility") or p.get("scopus_eligibility") == "PENDING"]
    print(f"待查（PENDING/无状态）: {len(pending)} 篇；已有状态不重查: {len(papers) - len(pending)} 篇")
    if not pending:
        print("无待查论文，退出")
        return

    dois = [norm_doi(p.get("doi", "")) for p in pending]
    n_no_doi = sum(1 for d in dois if not d)
    print(f"待查中无 DOI（将标 NOT_CHECKABLE）: {n_no_doi}")

    engine = ScopusSearchEngine()
    await engine.start()
    try:
        eid_by_doi: dict[str, str] = {}
        for i in range(0, len(dois), args.batch):
            chunk = [d for d in dois[i:i + args.batch] if d]
            if not chunk:
                continue
            q = "DOI(" + " OR ".join(chunk) + ")"
            print(f"\n批 {i // args.batch + 1}: {len(chunk)} DOI → 检索...")
            # ⚠️ 必须先 search() 建立结果页上下文再 export（已踩坑 2026-08-27：
            # 直接 _export_via_api 时页面停在高级搜索表单页，下载 fetch 报
            # Failed to fetch / 会话过期误报）。skip_cache=True 强制真实导航，
            # 避免命中 scopus_cache 导致页面上下文未建立。
            result = await engine.search(q, limit=len(chunk), skip_cache=True)
            print(f"  页面 total_count={result.total_count}（仅参考：Scopus UI 文案"
                  f"变化时可能解析为 0，不影响 export 匹配）")
            # 第二次 export 拿 EID（正向证据）
            try:
                csv_text = await engine._export_via_api(
                    q, len(chunk), fields=["eid", "doi", "titles"])
            except Exception as e:
                print(f"  ⚠️ export 失败重试: {type(e).__name__}")
                await asyncio.sleep(3)
                csv_text = await engine._export_via_api(
                    q, len(chunk), fields=["eid", "doi", "titles"])
            rows = parse_rows(csv_text)
            for row in rows:
                if row["doi"] and row["eid"]:
                    eid_by_doi[row["doi"]] = row["eid"]
            print(f"  CSV 解析 {len(rows)} 行，累计 EID 命中 {len(eid_by_doi)}")

        # 三态判定（用户定：只有正向证据才进 B_scopus）——只对 pending 论文：
        #   IN_SCOPUS      —— Scopus EID 精确命中（正向证据）
        #   NOT_IN_SCOPUS  —— DOI 查询执行且无匹配（evidence: SCOPUS_DOI_QUERY_NO_MATCH）
        #   NOT_CHECKABLE  —— 无 DOI 或查询未执行（不能默认为在/不在）
        n_in = n_not = n_unk = 0
        for p in pending:
            doi = norm_doi(p.get("doi", ""))
            eid = eid_by_doi.get(doi, "") if doi else ""
            if eid:
                p["scopus_eligibility"] = "IN_SCOPUS"
                p["match_method"] = "DOI_EXACT"
                p["scopus_eid"] = eid
                n_in += 1
            elif doi:
                p["scopus_eligibility"] = "NOT_IN_SCOPUS"
                p["evidence"] = "SCOPUS_DOI_QUERY_NO_MATCH"
                p["scopus_eid"] = None
                n_not += 1
            else:
                p["scopus_eligibility"] = "NOT_CHECKABLE"
                p["evidence"] = "NO_DOI"
                p["scopus_eid"] = None
                n_unk += 1

        # 全量四态统计（含历史已确认论文）+ 硬 invariant
        n_total = len(papers)
        st = bench.setdefault("stats", {}).setdefault("scopus_eligibility", {})
        st["B_relevant_resolved"] = n_total
        st["B_scopus"] = sum(1 for x in papers if x.get("scopus_eligibility") == "IN_SCOPUS")
        st["B_not_scopus"] = sum(1 for x in papers if x.get("scopus_eligibility") == "NOT_IN_SCOPUS")
        st["B_pending"] = sum(1 for x in papers if x.get("scopus_eligibility") == "PENDING")
        st["B_not_checkable"] = sum(1 for x in papers if x.get("scopus_eligibility") == "NOT_CHECKABLE")
        total = st["B_scopus"] + st["B_not_scopus"] + st["B_pending"] + st["B_not_checkable"]
        assert total == n_total, f"scopus 状态账目不平: {total} != {n_total}"
        st["ScopusCoverage_QGS"] = round(st["B_scopus"] / n_total, 4) if n_total else 0.0
        with open(BENCH_PATH, "w", encoding="utf-8") as f:
            json.dump(bench, f, ensure_ascii=False, indent=1)

        print("\n" + "=" * 60)
        print(f"B_relevant_resolved = {st['B_relevant_resolved']}")
        print(f"B_scopus            = {st['B_scopus']}")
        print(f"B_not_scopus        = {st['B_not_scopus']}")
        print(f"B_pending           = {st['B_pending']}")
        print(f"B_not_checkable     = {st['B_not_checkable']}")
        print(f"ScopusCoverage_QGS  = {st['ScopusCoverage_QGS']}")
        print(f"✓ benchmark 已更新（四态 invariant 通过）: {BENCH_PATH}")
        if n_not:
            print("\n不在 Scopus（相关但未收录 = database coverage limitation）:")
            for p in papers:
                if p.get("scopus_eligibility") == "NOT_IN_SCOPUS":
                    print(f"  [{p['year']}] {p['title'][:65]} | {p.get('doi')}")
        print(f"\n✓ External QGS adjudication complete")
        print(f"✓ Final B_total = {st['B_relevant_resolved']}")
        print(f"✓ Confirmed B_scopus = {st['B_scopus']}")
        print("下一步：python tools/evaluate_relative_recall.py")
        print("  RR = |R_Agent ∩ B_scopus| / B_scopus")
    finally:
        await engine.close()


if __name__ == "__main__":
    asyncio.run(main())
