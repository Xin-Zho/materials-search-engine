"""P1-A2: 通用 S6 semantic-bridge 编译器（纯 deterministic，不调 LLM）。

从 build_s6_bridge_queries.py 提取的通用编译引擎：specs（决策资产）→ Scopus
Boolean actions。所有 pc001 专属常量已迁至 topics/<id>/bridge_spec.json 资产，
本模块只消费 spec 字典，禁止 topic 特判与领域常量。

    Spec schema（bridge_spec.json / bridge_synth 产物同构）:
    {
      "process_clause": "(curing OR polymeriz* ...)",   # 可空（无 process 锚主题）
      "shrink_lex_clause": "(...)",                       # 可空（无 shrinkage 锚主题）
      "domain_context": {"dental": "(...)", ..., "general": None},
      "context_source": "S5_FROZEN_...",
      "source_labels": {"process": "...", "shrink_lex": "...", "context": "..."},
      "role_correction": {...},
      "query_form_map": {...},
      "specs": [ {family, domain, strategy, left?, obs[], mech[]?,
                  ctx?, add_process?, note?, exploratory?}, ... ]
    }
"""
from __future__ import annotations


def q(s: str) -> str:
    return f'"{s}"'


def group_or(items) -> str:
    """每个 AND 组一律括号化（Scopus Boolean：AND/OR 不括号会串优先级）。"""
    items = list(items)
    assert items, "empty group"
    return "(" + " OR ".join(q(t) for t in items) + ")"


def _norm_for_compare(t: str) -> str:
    """去引号/尾部通配（仅用于锚/process 判定；query 词面保留原文）。"""
    return t.strip().strip('"').rstrip("*")


def compile_query(spec: dict, dc: dict, process_clause: str,
                  shrink_lex_clause: str | None, query_form_map: dict,
                  process_words: list[str] | None = None) -> str:
    """把单条 spec 编译成 Scopus query_string。dc = domain_context 映射。

    通用槽位语义（顺序与 pc001 旧输出逐一兼容）：
      term_left   = spec["mech"] 或 spec["left_terms"]（词面 OR 组，C 型/体系锚）
      template_left = spec["left"] ∈ {process, shrink_lex, ctx:<domain>}（模板 OR 组）
      组合序: [term_left] → [template_left] → obs → [add_process] → [ctx 域]
    对 pc001 各 family：A=(proc,obs,ctx) B=(shrink,obs,ctx) C=(mech,obs,proc)
    D=(ctxX,obs)，与冻结输出完全一致。合成 spec 无 family 依赖，槽位自描述。
    """
    qfm = query_form_map or {}
    left = spec.get("left")

    def render(items):
        return group_or([qfm.get(t, t) for t in items])

    term_left = spec.get("mech") or spec.get("left_terms")
    template_left = None
    if left == "process":
        template_left = process_clause
    elif left == "shrink_lex":
        template_left = shrink_lex_clause
    elif left and left.startswith("ctx:"):
        template_left = dc.get(left.split(":", 1)[1])

    parts = []
    if term_left:
        parts.append(render(term_left))
    if template_left:
        parts.append(template_left)
    parts.append(render(spec.get("obs", [])))
    if spec.get("add_process") and process_clause:
        parts.append(process_clause)
    ctx = dc.get(spec["ctx"]) if spec.get("ctx") else None
    if ctx:
        parts.append(ctx)
    return "TITLE-ABS-KEY(" + " AND ".join(parts) + ")"


def contains_shrinkage_anchor(spec: dict) -> bool:
    if "shrink_lex" in (spec.get("left") or ""):
        return True
    return any(("shrink" in str(t).lower() or "contraction" in str(t).lower())
               for t in spec.get("obs", []))


def compile_action(spec: dict, idx: int, *, dc: dict, process_clause: str,
                   shrink_lex_clause: str | None, query_form_map: dict,
                   context_source: str, term_source_label: str,
                   source_labels: dict | None = None,
                   process_words: list[str] | None = None,
                   prefix: str = "S6") -> dict:
    """spec → action（字段与 pc001 s6_bridge_queries.json 一致，可 byte 对照）。"""
    qs = compile_query(spec, dc, process_clause, shrink_lex_clause,
                       query_form_map, process_words)
    family = spec["family"]
    terms = []
    for t in (spec.get("mech") or spec.get("left_terms") or []):
        terms.append(query_form_map.get(t, t))
    terms += [query_form_map.get(t, t) for t in spec.get("obs", [])]

    sl = source_labels or {}
    left = spec.get("left")
    if spec.get("ctx") or (left and left.startswith("ctx:")):
        csrc = context_source
    elif left == "process":
        csrc = sl.get("process", "PROCESS_TEMPLATE")
    elif left == "shrink_lex":
        csrc = sl.get("shrink_lex", "SHRINK_LEX")
    else:
        csrc = None

    return {
        "action_id": f"{prefix}-{family}-{idx:02d}",
        "type": "scopus_query",
        "query_string": qs,
        "bridge_type": f"{prefix}-{family}",
        "family": family,
        "domain": spec.get("domain"),
        "strategy": spec.get("strategy"),
        "terms": terms,
        "term_source": term_source_label,
        "context_source": csrc,
        "contains_shrinkage_anchor": contains_shrinkage_anchor(spec),
        "exploratory": bool(spec.get("exploratory", False)),
        "note": spec.get("note", ""),
    }


def compile_spec_asset(asset: dict, term_source_label: str,
                       prefix: str = "S6") -> list[dict]:
    """整个 spec 资产 → actions（与 pc001 旧输出逐字段一致）。"""
    dc = asset.get("domain_context") or {}
    pc = asset.get("process_clause") or ""
    slc = asset.get("shrink_lex_clause")
    qfm = asset.get("query_form_map") or {}
    csrc = asset.get("context_source")
    pwords = asset.get("process_words")
    return [compile_action(sp, i + 1, dc=dc, process_clause=pc,
                           shrink_lex_clause=slc, query_form_map=qfm,
                           context_source=csrc, term_source_label=term_source_label,
                           source_labels=asset.get("source_labels"),
                           process_words=pwords, prefix=prefix)
            for i, sp in enumerate(asset.get("specs", []))]
