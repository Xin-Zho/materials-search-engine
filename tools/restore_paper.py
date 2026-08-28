"""恢复被 STRONG 误删的 paper（Identity Hotfix）。

背景：STRONG（title+year 一致）曾把"正式论文 vs preprint / supplementary"误判为
重复删除（W4401422516 ssrn / W7111174545 .s001）。修复 IDENTITY_CONFLICT 后，
这两条需要恢复：从 OpenAlex 重新拉 work → 2.0-edges 重抽 → 插回 KB（不 merge）。

用法（需 DEEPSEEK_API_KEY）:
    python tools/restore_paper.py openalex:https://openalex.org/W4401422516 \
        openalex:https://openalex.org/W7111174545
"""

import argparse
import asyncio
import os
import sys

from search_engine.llm import DeepSeekBackend
from search_engine.knowledge_base import KnowledgeBase
from search_engine.knowledge_extractor import KnowledgeExtractor
from search_engine.backends import OpenAlexBackend
from search_engine.models import KnowledgeRecord

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def _strip_openalex_prefix(pid: str) -> str:
    if pid.startswith("openalex:"):
        pid = pid[len("openalex:"):]
    if pid.startswith("https://openalex.org/"):
        pid = pid[len("https://openalex.org/"):]
    return pid


def _build_minimal_record(paper, pid: str) -> KnowledgeRecord:
    """无 LLM 降级恢复：恢复 identity/metadata（canonical_paper_id/doi/openalex_id），
    edges 留空（extractor_version 诚实标记 1.1-restored，有 key 后 re-extract 覆盖）。"""
    doi = (paper.doi or "").strip()
    rec = KnowledgeRecord(
        paper_id=pid,
        canonical_paper_id=f"doi:{doi}" if doi else pid,
        doi=doi,
        openalex_id=_strip_openalex_prefix(paper.paper_id or ""),
        problem=paper.title or "",
        source_text=(paper.abstract or paper.title or "")[:500],
        extractor_version="1.1-restored",
        confidence=0.0,
    )
    rec.extraction_status = "insufficient_evidence" if not paper.abstract else ""
    return rec


async def main():
    ap = argparse.ArgumentParser(description="恢复被误删的 paper（重拉 + 2.0-edges 重抽 + 插回）")
    ap.add_argument("paper_ids", nargs="+", help="要恢复的 paper_id（openalex:https://openalex.org/W...）")
    ap.add_argument("--no-llm", action="store_true",
                    help="无 LLM 降级恢复：只恢复 identity/metadata（edges=[]，v1.1-restored），"
                         "有 DEEPSEEK_API_KEY 后重跑本脚本即覆盖为完整 2.0-edges")
    ap.add_argument("--mailto", default=os.environ.get("OPENALEX_MAILTO", ""), help="OpenAlex polite pool email")
    args = ap.parse_args()

    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key and not args.no_llm:
        print("⚠️  未设 DEEPSEEK_API_KEY —— 用 --no-llm 降级恢复（identity 完整，edges=[]），"
              "或设置 key 后完整重抽。", file=sys.stderr)
        return

    kb = KnowledgeBase()
    llm = DeepSeekBackend(api_key=key) if key else None
    extractor = KnowledgeExtractor(llm, extractor_version="2.0-edges") if llm else None
    mailto = args.mailto or None

    async with OpenAlexBackend(mailto=mailto) as oa:
        for pid in args.paper_ids:
            oid = _strip_openalex_prefix(pid)
            print(f"── 恢复 {pid}  (oid={oid})")
            papers = await oa._fetch_works_by_ids([oid])
            if not papers:
                print(f"  ✗ OpenAlex 拉不到 {oid}，跳过")
                continue
            paper = papers[0]
            if llm:
                rec = await extractor.extract(paper)
                if rec is None:
                    print(f"  ✗ 抽取失败 {oid}")
                    continue
                rec.paper_id = pid  # 保持原 row key
                for e in rec.route_mechanism_edges:
                    e.paper_id = pid  # edges 的 paper_id 同步为 row key
                rec.extraction_status = "insufficient_evidence" if not paper.abstract else "ok"
            else:
                rec = _build_minimal_record(paper, pid)
            kb.store(rec)
            print(f"  ✓ 已恢复: doi={rec.doi or '-'}  edges={len(rec.route_mechanism_edges)}  "
                  f"[{rec.extractor_version} / {rec.extraction_status or 'ok'}]")
            for e in rec.route_mechanism_edges:
                print(f"      edge: {(e.canonical_route or e.raw_route or '(unbound)')} → "
                      f"{e.canonical_mechanism or e.raw_mechanism}  [{e.relation_type}]")

    kb.close()
    print("\n恢复完成。确认不与 survivor merge（DOI 不同 → IDENTITY_CONFLICT 保护）：")
    print("  python tools/audit_paper_identity.py")
    if not key:
        print("\n当前为 --no-llm 降级恢复。设置 DEEPSEEK_API_KEY 后重跑本脚本可补全 edges：")
        print("  python tools/restore_paper.py " + " ".join(args.paper_ids))


if __name__ == "__main__":
    asyncio.run(main())
