"""P0-B1: 统一身份写入入口 —— ``resolve_or_create_paper``。

铁律（Iron Rules）
------------------
R1 唯一生产者  本模块是全仓唯一执行 ``INSERT INTO papers`` 的生产代码路径。
R2 识别优先    标识的**真实类型由值形态决定**；字段名只是声明，不是事实。
R3 错位不写入  DOI 字段收到 EID 时，**绝不写入 doi 列**；按 SCOPUS_EID 处理并记冲突。
R4 冲突不覆盖  任何冲突只落 ``identity_conflicts``；不合并、不改写既有 uid。
R5 旧 uid 不变 已存在的 uid **永不重写**（30 条 ``doi:2-s2.0-*`` 保持原样）；
               新 uid 由 :func:`make_paper_uid` 的稳定规则生成。
R6 幂等        同一 metadata 重复调用不产生新行、不产生新冲突。
R7 统一查询    身份查询一律经 :func:`resolve_identifier` / :func:`lookup_owners` /
               :func:`find_paper_uid`；**禁止外部自行拼接** ``doi:`` / ``openalex:`` / ``scopus:``。

背景：P0-A 在真库中发现 30 条 ``doi:2-s2.0-*``——Scopus EID 被写入者放进了 DOI 列。
根因链是**两级**的：
  生产侧 ``tools/build_s8_finalkb_catalog.py`` 把 EID 兜底塞进名为 ``doi`` 的字段；
  消费侧 ``tools/migrate_v2_schema.py::norm_doi`` 不校验形态即接受。
因此本入口的设计目标不是「修那 30 条」，而是**让这一类错误在写入时不可能发生**。

详见 ``docs/2026-09-11-p0b1-identity-write-entry-design.md``。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field as dc_field

from search_engine.identity import (
    COLUMN_SOURCES,
    ID_TYPES,
    PRIMARY_PRIORITY,
    IdentityConflict,
    IdentifierClaim,
    assign_primary,
    detect_actual_type,
    normalize_identifier,
    normalize_title,
)

RESOLVER_NAME = "search_engine.paper_writer"
RESOLVER_VERSION = "p0b1_v1"

# ── 状态 ────────────────────────────────────────────────
STATUS_CREATED = "CREATED"                 # 新建 uid + 写入
STATUS_REUSED = "REUSED"                   # 命中既有 uid，未新建
STATUS_CONFLICT_REVIEW = "CONFLICT_REVIEW"  # 一个标识被多个 uid 认领 -> 只记录不合并
STATUS_DRY_RUN = "DRY_RUN"                 # 计划已产出，零写入

# uid 前缀的**唯一出口**。任何模块需要 uid 字符串都必须经 make_paper_uid()。
_PREFIX_BY_TYPE = {
    "DOI": "doi",
    "OPENALEX": "openalex",
    "SCOPUS_EID": "scopus",
    "PUBMED": "pubmed",
    "ARXIV": "arxiv",
    "ISBN": "isbn",
    "URL": "url",
}
# 无任何可识别标识时的本地命名空间（绝不借用外部命名空间，避免类型混淆）
LOCAL_PREFIX = "local"

_TITLE_HASH_LEN = 16


# ══════════════════════════════════════════════════════════
# 查询层（R7：身份查询的唯一入口）
# ══════════════════════════════════════════════════════════

def resolve_identifier(raw, declared_type=None):
    """把一个原始值解析为 ``(id_type, normalized_value)``；无法识别返回 ``None``。

    这是「这个值**实际上**是什么」的唯一判定入口。``declared_type`` 只是提示：
    一旦声明类型校验失败，会退化为**按值形态识别**（R2）。
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if declared_type:
        norm = normalize_identifier(declared_type, s)
        if norm is not None:
            return (declared_type, norm)
    for t in ID_TYPES:
        if t == declared_type:
            continue
        norm = normalize_identifier(t, s)
        if norm is not None:
            return (t, norm)
    return None


def lookup_owners(conn, id_type, normalized_value):
    """列出认领该标识的全部 uid（应为 0 或 1；>1 即需人工裁决）。"""
    rows = conn.execute(
        "SELECT paper_uid FROM paper_identifiers WHERE id_type = ? AND normalized_value = ?",
        (id_type, normalized_value),
    ).fetchall()
    return sorted({r[0] for r in rows})


def find_paper_uid(conn, raw, declared_type=None):
    """由任意原始标识值反查 uid。调用方**不应**自行拼接 ``doi:`` 前缀。"""
    hit = resolve_identifier(raw, declared_type)
    if hit is None:
        return None
    owners = lookup_owners(conn, hit[0], hit[1])
    return owners[0] if len(owners) == 1 else None


def paper_exists(conn, paper_uid):
    return conn.execute("SELECT 1 FROM papers WHERE paper_id = ?", (paper_uid,)).fetchone() is not None


# ══════════════════════════════════════════════════════════
# uid 生成（R5：稳定规则）
# ══════════════════════════════════════════════════════════

def make_paper_uid(*, claims=None, title=None, year=None):
    """由标识集合生成稳定 uid。

    规则（deterministic，与调用顺序无关）：
      1. 取 ``PRIMARY_PRIORITY`` 最高的可用标识 -> ``<prefix>:<normalized_value>``
      2. 无任何标识（title-only）-> ``local:<sha256(norm_title|year)[:16]``
         **绝不**把 title 塞进 ``openalex:`` / ``scopus:`` 命名空间。

    注意：本函数只生成**新** uid。既有 uid 的解析走 ``lookup_owners``，永不重算。
    """
    if claims:
        best = min(claims, key=lambda c: PRIMARY_PRIORITY.get(c.id_type, 99))
        prefix = _PREFIX_BY_TYPE.get(best.id_type)
        if prefix is None:
            raise ValueError(f"no uid prefix for id_type={best.id_type!r}")
        return f"{prefix}:{best.normalized_value}"
    key = "|".join([normalize_title(title) or "", str(year or "")])
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:_TITLE_HASH_LEN]
    return f"{LOCAL_PREFIX}:{digest}"


def uid_type_matches_value(paper_uid):
    """校验 uid 的**前缀声明**与**值形态**是否一致（用于 UID_PREFIX_MISMATCH 计量）。

    返回 ``(ok, declared_type, actual_type)``。``local:`` 前缀视为一致。
    """
    p = str(paper_uid or "")
    prefix, _, value = p.partition(":")
    if prefix == LOCAL_PREFIX:
        return (True, None, None)
    for id_type, pref in _PREFIX_BY_TYPE.items():
        if pref == prefix:
            declared = id_type
            break
    else:
        return (False, None, None)
    actual = detect_actual_type(value, exclude=None)
    return (actual == declared, declared, actual)


# ══════════════════════════════════════════════════════════
# 结果对象
# ══════════════════════════════════════════════════════════

@dataclass
class ResolutionOutcome:
    """``resolve_or_create_paper`` 的完整结果（可审计：输入 -> 判定 -> 写入）。"""

    status: str
    paper_uid: str | None
    identifier_hits: list = dc_field(default_factory=list)    # [(id_type, norm, source)]
    misplaced: list = dc_field(default_factory=list)           # [Anomaly-like dict]
    conflicts: list = dc_field(default_factory=list)           # 待写/已存的冲突行
    created_rows: dict = dc_field(default_factory=dict)        # {"papers":1,"identifiers":2,...}
    reused_from: str | None = None
    primary_type: str | None = None
    note: str = ""

    def as_dict(self):
        return {
            "status": self.status,
            "paper_uid": self.paper_uid,
            "primary_type": self.primary_type,
            "reused_from": self.reused_from,
            "identifier_hits": [list(x) for x in self.identifier_hits],
            "misplaced": self.misplaced,
            "n_conflicts": len(self.conflicts),
            "created_rows": self.created_rows,
            "note": self.note,
        }


# ══════════════════════════════════════════════════════════
# 唯一写入入口
# ══════════════════════════════════════════════════════════

def _classify_metadata(metadata):
    """把 metadata 拆成 claims / anomalies / rejects（纯计算，零写入）。

    R2/R3 的落点：每个声明列的值先按声明类型校验；失败则**按真实类型重新归属**，
    并把 (声明列, 原值, 真实类型) 记成一条 ``MISPLACED_IDENTIFIER`` 冲突。
    """
    claims, anomalies = [], []
    seen = set()

    def _add(id_type, norm, id_value, source, confidence=1.0):
        if (id_type, norm) in seen:
            return False
        seen.add((id_type, norm))
        claims.append(IdentifierClaim(id_type, norm, id_value, None, source,
                                      confidence=confidence))
        return True

    # 1) 三个声明列（正确列优先：先收合法值）
    for col, declared_type, source_label in COLUMN_SOURCES:
        raw = metadata.get(col)
        if raw is None:
            continue
        s = str(raw).strip()
        if not s:
            continue
        norm = normalize_identifier(declared_type, s)
        if norm is not None:
            _add(declared_type, norm, s, source_label)
            continue
        # 声明类型不成立 -> 识别真实类型（R2）
        actual = detect_actual_type(s, exclude=declared_type)
        actual_norm = normalize_identifier(actual, s) if actual else None
        anomalies.append({
            "declared_type": declared_type,
            "declared_column": col,
            "raw_value": s,
            "actual_type": actual,
            "actual_normalized": actual_norm,
            "kind": "MISPLACED" if actual_norm else "INVALID",
            "recovered": False,
        })
        if actual_norm is not None:
            # 归属到真实类型，而非丢弃（R3）
            if _add(actual, actual_norm, s, f"recovered_from:{col}", confidence=0.9):
                anomalies[-1]["recovered"] = True

    # 2) 自由标识列表（无字段名可信任，纯按值识别）
    for raw in metadata.get("identifiers") or []:
        hit = resolve_identifier(raw)
        if hit is None:
            anomalies.append({"declared_type": None, "declared_column": "identifiers",
                              "raw_value": str(raw), "actual_type": None,
                              "actual_normalized": None, "kind": "INVALID",
                              "recovered": False})
            continue
        _add(hit[0], hit[1], str(raw).strip(), "metadata.identifiers")

    return claims, anomalies


def resolve_or_create_paper(conn, metadata, source, *, dry_run=False,
                            first_seen_run=None, resolver_version=RESOLVER_VERSION,
                            now=None):
    """**唯一**的论文写入入口。

    参数
    ----
    conn      sqlite3 连接（调用方负责事务边界）
    metadata  论文元数据 dict。可含 ``doi`` / ``openalex_id`` / ``scopus_eid`` /
              ``title`` / ``abstract`` / ``year`` / ``identifiers``(list)。
              **字段名是声明；值形态才是事实。**
    source    写入者标识（必填，用于溯源；空则拒绝）
    dry_run   True 时只做判定，零写入

    返回 :class:`ResolutionOutcome`。

    语义
    ----
    * 命中唯一既有 uid -> ``REUSED``（**不修改 papers 行**，R5）
    * 未命中 -> 新建 uid + 写 papers / paper_identifiers -> ``CREATED``
    * 一个标识被 ≥2 个既有 uid 认领 -> ``CONFLICT_REVIEW``（**不合并、不新建**，R4）
    * 错位值一律：按真实类型归属 + 记 ``MISPLACED_IDENTIFIER``；**绝不写入声明列**（R3）
    """
    if not source or not str(source).strip():
        raise ValueError("source 必填：无溯源的写入不允许进入 KB")

    claims, anomalies = _classify_metadata(metadata or {})

    # ── 解析：查既有 owner（R7 + R5）────────────────────
    owner_map = {}          # uid -> [claim]
    conflicts = []
    for c in claims:
        owners = lookup_owners(conn, c.id_type, c.normalized_value)
        if len(owners) > 1:
            # 同一标识被多个 uid 认领：只记录，绝不自动合并
            for o in owners:
                conflicts.append(IdentityConflict(
                    c.id_type, c.normalized_value, incoming_paper_uid=o,
                    conflict_type="IDENTIFIER_ALREADY_OWNED",
                    incoming_value=c.id_value, source=source,
                    provenance_json=json.dumps(
                        {"owners": owners, "note": "multi-owner; no auto-merge"},
                        ensure_ascii=False),
                    resolution_status="REVIEW_REQUIRED",
                    resolver=RESOLVER_NAME, resolver_version=resolver_version))
            return ResolutionOutcome(
                status=STATUS_CONFLICT_REVIEW, paper_uid=None,
                identifier_hits=[(c.id_type, c.normalized_value, c.source) for c in claims],
                misplaced=anomalies, conflicts=conflicts,
                note=f"identifier claimed by {len(owners)} uids; merged=0")
        if len(owners) == 1:
            owner_map.setdefault(owners[0], []).append(c)

    # ── 判定 uid ───────────────────────────────────────
    if owner_map:
        if len(owner_map) > 1:
            # 输入的不同标识指向不同既有论文：这是**合并请求**，必须人工裁决
            owners = sorted(owner_map)
            for c in claims:
                owners_c = lookup_owners(conn, c.id_type, c.normalized_value)
                if len(owners_c) == 1:
                    conflicts.append(IdentityConflict(
                        c.id_type, c.normalized_value, incoming_paper_uid=owners_c[0],
                        conflict_type="IDENTIFIER_ALREADY_OWNED",
                        incoming_value=c.id_value, source=source,
                        provenance_json=json.dumps(
                            {"would_merge": owners,
                             "note": "metadata spans multiple uids; no auto-merge"},
                            ensure_ascii=False),
                        resolution_status="REVIEW_REQUIRED",
                        resolver=RESOLVER_NAME, resolver_version=resolver_version))
            return ResolutionOutcome(
                status=STATUS_CONFLICT_REVIEW, paper_uid=None,
                identifier_hits=[(c.id_type, c.normalized_value, c.source) for c in claims],
                misplaced=anomalies, conflicts=conflicts,
                note=f"metadata spans uids {owners}; merged=0")
        paper_uid = next(iter(owner_map))
        status = STATUS_REUSED
    else:
        paper_uid = make_paper_uid(claims=claims, title=metadata.get("title"),
                                   year=metadata.get("year"))
        status = STATUS_CREATED
        # 防御：uid 已存在但无 identifier 承载（历史遗留）-> 视为复用，不重复插入
        if paper_exists(conn, paper_uid):
            status = STATUS_REUSED

    # ── 主身份裁决 ─────────────────────────────────────
    for c in claims:
        c.paper_uid = paper_uid
        c.first_seen_run = first_seen_run
    mismatch = assign_primary(claims, paper_uid) if claims else None
    reused_prefix_mismatch = None

    if not claims and status == STATUS_CREATED:
        conflicts.append(IdentityConflict(
            None or "TITLE", normalize_title(metadata.get("title")) or "",
            incoming_paper_uid=paper_uid, conflict_type="NO_IDENTIFIER",
            incoming_value=metadata.get("title"), source=source,
            provenance_json=json.dumps({"uid_form": f"{LOCAL_PREFIX}:<hash>"},
                                       ensure_ascii=False),
            resolution_status="REVIEW_REQUIRED",
            resolver=RESOLVER_NAME, resolver_version=resolver_version))
    if mismatch:
        # 结构性不变式：**CREATE 路径下前缀错配不可能发生** —— make_paper_uid 与
        # assign_primary 都按 PRIMARY_PRIORITY 取最优，二者必然一致。
        # 因此「新写入 UID_PREFIX_MISMATCH=0」是由构造保证的，而非事后检查。
        # REUSE 一条**既有**前缀错标 uid（如 P0-A 的 30 条 doi:2-s2.0-*）时，
        # 该事实已由 P0-A 以 MISPLACED_IDENTIFIER 记录，此处不重复计一次。
        if status == STATUS_CREATED:
            conflicts.append(IdentityConflict(
                mismatch["effective"], "", incoming_paper_uid=paper_uid,
                conflict_type="MISPLACED_IDENTIFIER", incoming_value=None,
                source=source,
                provenance_json=json.dumps({"kind": "UID_PREFIX_MISMATCH", **mismatch},
                                           ensure_ascii=False),
                resolution_status="REVIEW_REQUIRED",
                resolver=RESOLVER_NAME, resolver_version=resolver_version))
        else:
            reused_prefix_mismatch = dict(mismatch)

    # 错位/非法值 -> 冲突（R3/R4：只记录，不改写）
    #
    # ⚠️ 口径约定（必须与 P0-A 冻结口径一致，不可各自为政）：
    #   `identity_conflicts.id_type` = **声明类型 / 声明列**（问题被观察到的地方），
    #   **不是**值的真实类型。真实类型放 `provenance.looks_like` / `routed_as`。
    #   P0-A 对那 30 条正是这样写的：id_type='DOI'、normalized_value=<EID>、
    #   provenance={"looks_like":"SCOPUS_EID", ...}。
    #   若两个写入者各用一套口径，`dedup_key`（含 id_type）永远匹配不上 ——
    #   同一事实被重复记录，30 条会在审计里变成 60 条。故此处**必须**与 P0-A 同口径。
    for a in anomalies:
        if a["kind"] == "MISPLACED":
            conflicts.append(IdentityConflict(
                a["declared_type"] or a["actual_type"], a["raw_value"],
                incoming_paper_uid=paper_uid, conflict_type="MISPLACED_IDENTIFIER",
                incoming_value=a["raw_value"], source=source,
                provenance_json=json.dumps(
                    {"declared_column": a["declared_column"],
                     "declared_type": a["declared_type"],
                     "looks_like": a["actual_type"],
                     "routed_as": a["actual_type"],
                     "routed_to": a["actual_normalized"] or "",
                     "written_to_declared_column": False,
                     "raw_input_preserved": True,
                     "writer": RESOLVER_NAME,
                     "resolver_version": resolver_version},
                    ensure_ascii=False),
                resolution_status="REVIEW_REQUIRED",
                resolver=RESOLVER_NAME, resolver_version=resolver_version))
        else:
            conflicts.append(IdentityConflict(
                a["declared_type"] or "TITLE", a["raw_value"] or "",
                incoming_paper_uid=paper_uid, conflict_type="INVALID_IDENTIFIER",
                incoming_value=a["raw_value"], source=source,
                provenance_json=json.dumps({"declared_column": a["declared_column"],
                                            "raw_input_preserved": True,
                                            "writer": RESOLVER_NAME},
                                           ensure_ascii=False),
                resolution_status="REVIEW_REQUIRED",
                resolver=RESOLVER_NAME, resolver_version=resolver_version))

    outcome = ResolutionOutcome(
        status=STATUS_DRY_RUN if dry_run else status,
        paper_uid=paper_uid,
        identifier_hits=[(c.id_type, c.normalized_value, c.source) for c in claims],
        misplaced=anomalies,
        conflicts=conflicts,
        reused_from=paper_uid if status == STATUS_REUSED else None,
        primary_type=next((c.id_type for c in claims if c.is_primary), None),
        note=("dry-run: zero writes" if dry_run else
              (f"reused uid with legacy prefix mismatch {reused_prefix_mismatch}"
               if reused_prefix_mismatch else "")),
    )
    if dry_run:
        return outcome

    # ── 写入（R1：本模块是全仓唯一 papers 写入者）────────
    ts = float(now if now is not None else time.time())
    created = {"papers": 0, "identifiers": 0, "conflicts": 0}

    if status == STATUS_CREATED:
        # R3 的硬约束：只有**按真实类型**归属的列才写值
        cols = {"doi": None, "openalex_id": None, "scopus_eid": None}
        for c in claims:
            col = {"DOI": "doi", "OPENALEX": "openalex_id",
                   "SCOPUS_EID": "scopus_eid"}.get(c.id_type)
            if col and cols[col] is None:
                cols[col] = c.id_value
        conn.execute(
            "INSERT INTO papers (paper_id, doi, openalex_id, scopus_eid, title, "
            "abstract, year, source_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (paper_uid, cols["doi"], cols["openalex_id"], cols["scopus_eid"],
             metadata.get("title") or "", metadata.get("abstract") or "",
             metadata.get("year"),
             json.dumps({"writer": RESOLVER_NAME, "source": str(source),
                         "resolver_version": resolver_version}, ensure_ascii=False), ts))
        created["papers"] = 1

    for c in claims:
        cur = conn.execute(
            "INSERT OR IGNORE INTO paper_identifiers (id_type, normalized_value, paper_uid, "
            "id_value, source, confidence, is_primary, first_seen_run, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (*c.as_row(), _iso(ts)))
        created["identifiers"] += cur.rowcount

    for cf in conflicts:
        if _conflict_exists(conn, cf):
            continue
        conn.execute(
            "INSERT INTO identity_conflicts (id_type, normalized_value, incoming_paper_uid, "
            "existing_paper_uid, incoming_value, existing_value, conflict_type, source, "
            "provenance_json, detected_at, resolution_status, resolution_decision, "
            "resolver, resolver_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            cf.as_row(_iso(ts)))
        created["conflicts"] += 1

    outcome.created_rows = created
    return outcome


def _iso(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _conflict_exists(conn, cf):
    """幂等去重（R6）：同 (type, id_type, norm, incoming_uid) 只记一次。"""
    row = conn.execute(
        "SELECT 1 FROM identity_conflicts WHERE conflict_type = ? AND id_type = ? "
        "AND normalized_value = ? AND incoming_paper_uid = ?",
        cf.dedup_key).fetchone()
    return row is not None
