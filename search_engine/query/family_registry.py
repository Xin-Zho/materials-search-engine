"""family_registry.py — query_registry_v2.json 读写 + leakage ledger 登记。

Registry（data/query_registry_v2.json）：
  {
    "registry_version": 2,
    "created_at": "...",
    "architecture": "query_family",
    "families": [ ...Family.to_dict()... ]
  }

Leakage ledger（data/leakage_ledger_v2.json）：
  登记所有 QGS-learned 术语/社区（用户 2026-08-28 定：可用于 v2 开发，
  但必须记录，因为 QGS v1 此后只作 regression，QGS v2 才是独立评估）。
  每条：{"term", "source": "QGS_V1_FAILURE_ANALYSIS", "evidence", "used_in_families"}
"""
import json
import os

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REGISTRY_PATH = os.path.join(BASE, "data", "query_registry_v2.json")
LEDGER_PATH = os.path.join(BASE, "data", "leakage_ledger_v2.json")


def load_registry(path: str = REGISTRY_PATH) -> dict:
    if os.path.exists(path):
        return json.load(open(path, encoding="utf-8"))
    return {"registry_version": 2, "architecture": "query_family",
            "created_at": "", "families": []}


def save_registry(registry: dict, path: str = REGISTRY_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=1)


def load_ledger(path: str = LEDGER_PATH) -> dict:
    if os.path.exists(path):
        return json.load(open(path, encoding="utf-8"))
    return {"ledger_version": 2, "note": "QGS-learned 术语登记（开发可用，正式评估需独立 QGS v2）",
            "entries": []}


def save_ledger(ledger: dict, path: str = LEDGER_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(ledger, f, ensure_ascii=False, indent=1)


def register_leakage_terms(terms: list[dict], path: str = LEDGER_PATH) -> int:
    """登记 QGS-learned 术语（幂等：同 term 不重复）。terms = [{term, evidence, used_in}...]"""
    ledger = load_ledger(path)
    existing = {e["term"] for e in ledger["entries"]}
    n = 0
    for t in terms:
        if t["term"] not in existing:
            ledger["entries"].append({
                "term": t["term"],
                "source": "QGS_V1_FAILURE_ANALYSIS",
                "leakage": True,
                "evidence": t.get("evidence", ""),
                "used_in_families": t.get("used_in", []),
            })
            existing.add(t["term"])
            n += 1
    save_ledger(ledger, path)
    return n
