"""审计修复：离线重建 query_id 关联（用户定 2026-08-27，不烧 OpenAlex）。

历史 bug（审计发现）：registry 无 query_id 字段 → execute_query 退化成
normalized_query[:16]（22 条 query 只有 4 个碰撞 id：'bulk composite f/c/d/e'）→
staging.query_ids 写入空串 → screen_staging 无法关联 query（87 篇退化全 UNCERTAIN
或 0/0/0）→ provenance join 断、trace 空。

修复（纯本地，不调用 OpenAlex / LLM）：
  1. registry：每条补 query_id = md5(query_text)[:12]（写盘）
  2. provenance：query_id 重算为 md5(query_text)[:12]（原截断碰撞）
  3. staging：query_ids 从 provenance 按 paper_id 重建（many-to-many）；
     relevance_status 重置 STAGED（下次 run_staging_pipeline 重新 screening）

用法: python tools/repair_staging_join.py
"""

import json
import os
import sys

from search_engine.discovery.query_registry import _query_id, REGISTRY_PATH

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

PROVENANCE_PATH = "data/exports/discovery_paper_provenance.json"
STAGING_PATH = "data/exports/discovery_staging.json"


def _load(path: str):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def repair_staging_join(registry_path=REGISTRY_PATH,
                        provenance_path=PROVENANCE_PATH,
                        staging_path=STAGING_PATH) -> dict:
    """离线重建 query_id 关联。返回统计。"""
    registry = _load(registry_path)
    provenance = _load(provenance_path)
    staging = _load(staging_path)
    if isinstance(staging, dict):
        staging = staging.get("papers", [])

    # 1. registry 补 query_id（md5(query_text)[:12]）
    reg_by_qid: dict[str, dict] = {}
    for r in registry:
        if not r.get("query_id"):
            r["query_id"] = _query_id(r.get("query_text", ""))
        reg_by_qid[r["query_id"]] = r

    # 2. provenance.query_id 重算（原来 normalized[:16] 截断碰撞）
    for p in provenance:
        p["query_id"] = _query_id(p.get("query_text", ""))

    # 3. staging.query_ids 从 provenance 重建 + relevance 重置 STAGED
    prov_by_paper: dict[str, list[str]] = {}
    for p in provenance:
        prov_by_paper.setdefault(p.get("paper_id", ""), []).append(p["query_id"])
    n_restaged = 0
    for s in staging:
        qids = sorted(set(prov_by_paper.get(s.get("paper_id", ""), [])))
        s["query_ids"] = qids
        if s.get("relevance_status") != "STAGED":
            s["relevance_status"] = "STAGED"     # 重新 screening（query 关联修好后）
            n_restaged += 1

    _save(registry_path, registry)
    _save(provenance_path, provenance)
    _save(staging_path, {"phase": "2.1b-staging", "papers": staging})
    return {
        "registry_queries": len(registry),
        "registry_unique_query_ids": len(reg_by_qid),
        "provenance_records": len(provenance),
        "staging_papers": len(staging),
        "staging_restaged": n_restaged,
        "staging_with_query_ids": sum(1 for s in staging if s.get("query_ids")),
    }


def main():
    stats = repair_staging_join()
    print("✓ query_id 关联已重建（离线，未调用 OpenAlex/LLM）:")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print("\n下一步：python tools/run_staging_pipeline.py --extractor llm"
          "（重新 screening → extract → diff → trace）")


if __name__ == "__main__":
    main()
