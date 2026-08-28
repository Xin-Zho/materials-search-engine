"""人工核实后补一条 edge（EXTRACTION_MISS 的收尾）。

边界（用户定）：evidence 已存在但 extractor 没结构化 = EXTRACTION_MISS，不是搜索失败、
不是真 gap。targeted re-extract 仍失败时，人工核实原文证据后补 edge：
  - relation_type = "human_verified"（默认）—— 人工确认论文明确说
      provenance = "manual_audit"；coverage 视为 DIRECT_HUMAN ✓，但评估 extractor 时
      必须单独统计，不能用人工修复后的 coverage 说 extractor 自己做到了。
  - relation_type = "domain_verified"（--relation-type domain_verified）——
      领域知识确认但论文表述弱（如 "implying"），不伪装 DIRECT；coverage 视为
      DOMAIN_VERIFIED（C_domain 口径），完备性证明用双口径 C_strict / C_domain。

用法:
    python tools/add_human_edge.py <paper_id> <route> <mechanism> "<evidence>" [--conf 1.0]
    python tools/add_human_edge.py <paper_id> <route> <mechanism> "<evidence>" --relation-type domain_verified

例（filler × reduced polymerizable fraction，W7170061635）:
    python tools/add_human_edge.py openalex:https://openalex.org/W7170061635 \
        filler "reduced polymerizable fraction" \
        "Incorporating inorganic nanoparticles as fillers reduces the volume fraction of polymerizable material"

注意：evidence 必须来自该论文原文（abstract/正文），且 route→mechanism 因果主体
必须是论文明确陈述的——人工核实是最后一道防线，宁缺毋滥。
"""

import argparse
import sys

from search_engine.knowledge_base import KnowledgeBase
from search_engine.route_mechanism_ontology import build_edge

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def main():
    ap = argparse.ArgumentParser(description="人工核实补 edge（EXTRACTION_MISS 收尾）")
    ap.add_argument("paper_id", help="论文 paper_id（openalex:https://openalex.org/W...）")
    ap.add_argument("route", help="canonical route（如 filler / AFCT）")
    ap.add_argument("mechanism", help="canonical mechanism（如 reduced polymerizable fraction）")
    ap.add_argument("evidence", help="来自论文原文的证据（quote/paraphrase）")
    ap.add_argument("--conf", type=float, default=1.0, help="confidence（默认 1.0）")
    ap.add_argument("--relation-type", default="human_verified",
                    choices=["human_verified", "domain_verified"],
                    help="human_verified=人工确认论文明确说（DIRECT_HUMAN，默认）；"
                         "domain_verified=领域知识确认但论文表述弱（DOMAIN_VERIFIED，不伪装 DIRECT）")
    ap.add_argument("--dry-run", action="store_true", help="只打印不写库")
    args = ap.parse_args()

    kb = KnowledgeBase()
    rec = kb.get(args.paper_id)
    if rec is None:
        # 兼容：edge.paper_id 与 record.paper_id 不一致的遗留（re_extract 修复前），
        # 按 edge.paper_id 反查所属 record
        for cand in kb.get_all():
            if any(e.paper_id == args.paper_id for e in cand.route_mechanism_edges):
                print(f"ℹ 该 paper_id 不是 row key，它属于 record: {cand.paper_id}")
                print(f"   将写入 {cand.paper_id}（edge.paper_id 保持 {args.paper_id}）")
                rec = cand
                break
    if rec is None:
        print(f"✗ 找不到 paper: {args.paper_id}")
        kb.close()
        return

    edge = build_edge(
        paper_id=args.paper_id,
        raw_route=args.route,
        raw_mechanism=args.mechanism,
        evidence=args.evidence,
        confidence=args.conf,
        relation_type=args.relation_type,
    )
    edge.provenance = "manual_audit"
    print(f"将添加 edge（{('dry-run' if args.dry_run else '写入')}）:")
    print(f"  {edge.canonical_route or edge.raw_route} → "
          f"{edge.canonical_mechanism or edge.raw_mechanism}")
    print(f"  relation_type = {edge.relation_type}   provenance = {edge.provenance}")
    print(f"  confidence    = {edge.confidence}")
    print(f"  evidence      = {edge.evidence[:100]}")

    if not args.dry_run:
        # 防重复：同 paper 已有同 route+mechanism 的同类型 human/domain edge 则跳过
        for e in rec.route_mechanism_edges:
            er = e.canonical_route or e.raw_route
            em = e.canonical_mechanism or e.raw_mechanism
            if (er == edge.canonical_route or er == edge.raw_route) and \
               (em == edge.canonical_mechanism or em == edge.raw_mechanism) and \
               e.relation_type == args.relation_type:
                print(f"⏭  该 paper 已有相同 {args.relation_type} edge，跳过。")
                kb.close()
                return
        rec.route_mechanism_edges.append(edge)
        kb.store(rec)
        print("✓ 已写入。重跑验证:")
        print("  python tools/print_coverage_matrix.py")
        print("  python tools/audit_open_gaps.py")
    kb.close()


if __name__ == "__main__":
    main()
