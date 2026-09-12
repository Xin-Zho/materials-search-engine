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
R8 权限分离    身份（:func:`resolve_or_create_paper`）与内容（:func:`backfill_paper_fields`）
               是两个入口；``doi`` / ``openalex_id`` / ``scopus_eid`` / ``paper_id``
               **永远不可**被补字段入口触碰。
R9 关系后置    ``topic_papers`` 只能由 :func:`register_topic_paper` 写入，且要求论文已存在
               （关系不得先于实体，否则等于开了第二个身份生产者）。

uid 拼接字面量（``<prefix>:<value>``）全仓**只剩** :mod:`search_engine.identity` 一处，
由 ``tests/test_p0b1b_static_guards.py`` 静态守卫持续证明。

背景：P0-A 在真库中发现 30 条 ``doi:2-s2.0-*``——Scopus EID 被写入者放进了 DOI 列。
根因链是**两级**的：
  生产侧 ``tools/build_s8_finalkb_catalog.py`` 把 EID 兜底塞进名为 ``doi`` 的字段；
  消费侧 ``tools/migrate_v2_schema.py::norm_doi`` 不校验形态即接受。
因此本入口的设计目标不是「修那 30 条」，而是**让这一类错误在写入时不可能发生**。

详见 ``docs/2026-09-11-p0b1-identity-write-entry-design.md``。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field as dc_field

from search_engine.identity import (
    COLUMN_SOURCES,
    ID_TYPES,
    LOCAL_UID_PREFIX,
    UID_PREFIX_BY_TYPE,
    IdentityConflict,
    IdentifierClaim,
    assign_primary,
    detect_actual_type,
    normalize_identifier,
    normalize_title,
)
from search_engine.identity import make_paper_uid as identity_make_paper_uid

RESOLVER_NAME = "search_engine.paper_writer"
RESOLVER_VERSION = "p0b1_v1"

# ── 状态 ────────────────────────────────────────────────
STATUS_CREATED = "CREATED"                 # 新建 uid + 写入
STATUS_REUSED = "REUSED"                   # 命中既有 uid，未新建
STATUS_CONFLICT_REVIEW = "CONFLICT_REVIEW"  # 一个标识被多个 uid 认领 -> 只记录不合并
STATUS_DRY_RUN = "DRY_RUN"                 # 计划已产出，零写入

# uid 前缀表的权威定义在 identity.UID_PREFIX_BY_TYPE（唯一出口）。
# 此处保留别名仅为向后兼容既有引用；**不得**在此新增/覆盖前缀。
_PREFIX_BY_TYPE = UID_PREFIX_BY_TYPE
LOCAL_PREFIX = LOCAL_UID_PREFIX

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

# ── uid 生成 ────────────────────────────────────────────
# 实现已收口到纯函数层 search_engine.identity.make_paper_uid（P0-B1b）。
# 本模块只做 re-export，保持既有调用方（W1/W2/测试）接口不变。
# 之所以移动：拼接字面量必须**只剩一处**，静态守卫才能证明没有第二个自造命名空间。
make_paper_uid = identity_make_paper_uid


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

def classify_metadata(metadata):
    """把 metadata 拆成 claims / anomalies / rejects（纯计算，零写入）。

    R2/R3 的落点：每个声明列的值先按声明类型校验；失败则**按真实类型重新归属**，
    并把 (声明列, 原值, 真实类型) 记成一条 ``MISPLACED_IDENTIFIER`` 冲突。

    公开原因（P0-B1b）：迁移脚本需要**在无数据库连接时预演**同一套判定
    （``--dry-run`` 必须先给出与 commit 完全相同的 uid 规划，否则 dry-run 就是谎言）。
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

    claims, anomalies = classify_metadata(metadata or {})

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
        # 溯源：调用方可经 metadata["source_json"] 透传自己的来源描述
        # （W1 需要携带 sources / n_kb_records）。入口自身的字段**优先**，
        # 调用方无法借透传伪造 writer / source。
        src_obj = {"writer": RESOLVER_NAME, "source": str(source),
                   "resolver_version": resolver_version}
        extra = metadata.get("source_json")
        if extra:
            if isinstance(extra, str):
                try:
                    extra = json.loads(extra)
                except Exception:
                    extra = {"raw_source_json": extra}
            if isinstance(extra, dict):
                src_obj = {**extra, **src_obj}
        conn.execute(
            "INSERT INTO papers (paper_id, doi, openalex_id, scopus_eid, title, "
            "abstract, year, source_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (paper_uid, cols["doi"], cols["openalex_id"], cols["scopus_eid"],
             metadata.get("title") or "", metadata.get("abstract") or "",
             metadata.get("year"),
             json.dumps(src_obj, ensure_ascii=False), ts))
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


# ══════════════════════════════════════════════════════════
# 受限补字段入口（P0-B1b：权限分离）
# ══════════════════════════════════════════════════════════

# 允许被补写的**非身份**列。
BACKFILLABLE_PAPER_FIELDS = ("title", "abstract", "year")
# 显式列出受保护列，而不是「不在白名单里就拒」——
# 这样报错信息能点出**越权意图**，而不是让人猜哪个名字打错了。
PROTECTED_PAPER_FIELDS = ("paper_id", "doi", "openalex_id", "scopus_eid",
                          "source_json", "created_at")

BACKFILL_APPLIED = "APPLIED"
BACKFILL_DRY_RUN = "DRY_RUN"
BACKFILL_NOT_EMPTY = "SKIPPED_NOT_EMPTY"
BACKFILL_SAME = "SKIPPED_SAME"
BACKFILL_MISSING = "SKIPPED_MISSING_PAPER"


@dataclass
class BackfillOutcome:
    """单列补写结果（可审计：谁、对哪一行的哪一列、从什么变成了什么）。"""

    paper_uid: str
    field: str
    status: str
    old_value: object = None
    new_value: object = None

    @property
    def applied(self):
        return self.status == BACKFILL_APPLIED

    def as_dict(self):
        return {"paper_uid": self.paper_uid, "field": self.field, "status": self.status,
                "old_value": self.old_value, "new_value": self.new_value}


def backfill_paper_fields(conn, paper_uid, fields, source, *, only_if_empty=True,
                          dry_run=False, now=None):
    """**受限**的内容补写入口：只允许 ``title`` / ``abstract`` / ``year``。

    为什么必须存在第二个入口（而不是让调用方直接 UPDATE）
    ------------------------------------------------------
    ``tools/disposition_r06_funnel.py`` 的真实职责是「给**已存在**的论文补摘要」。
    它因此需要一个写 papers 的能力，但**不该**、也**不能**因此获得改写身份的权限。
    把「身份」与「内容」拆成两个入口就是权限分离：

      ``resolve_or_create_paper``  身份 —— 新建 uid / 复用既有 uid
      ``backfill_paper_fields``    内容 —— 补字段，**永不触碰身份列**

    硬约束
    ------
    * 传入 ``PROTECTED_PAPER_FIELDS`` 中任一列 -> ``ValueError``。
      这里**显式失败而非静默忽略**：静默忽略会把「越权意图」藏起来，
      而「静默接受一个看起来像 DOI 的 EID」正是那 30 条得以发生的方式。
    * ``only_if_empty=True``（默认）绝不覆盖既有非空值 —— 与「冲突只记录不覆盖」同源。
      确实需要覆盖时显式传 ``only_if_empty=False``。
    * 论文不存在 -> 返回 ``SKIPPED_MISSING_PAPER``，**不新建**：
      身份只能由 ``resolve_or_create_paper`` 产生（R1）。
    """
    if not source or not str(source).strip():
        raise ValueError("source 必填：无溯源的写入不允许进入 KB")

    fields = dict(fields or {})
    illegal = sorted(set(fields) & set(PROTECTED_PAPER_FIELDS))
    if illegal:
        raise ValueError(
            f"backfill_paper_fields 拒绝改写身份/溯源列 {illegal}；"
            f"允许的列仅 {list(BACKFILLABLE_PAPER_FIELDS)}。"
            "身份变更必须走 resolve_or_create_paper，且永不就地改写既有 uid（R5）。")
    unknown = sorted(set(fields) - set(BACKFILLABLE_PAPER_FIELDS))
    if unknown:
        raise ValueError(f"backfill_paper_fields 不认识列 {unknown}；"
                         f"允许的列仅 {list(BACKFILLABLE_PAPER_FIELDS)}")

    row = conn.execute(
        "SELECT title, abstract, year FROM papers WHERE paper_id = ?",
        (paper_uid,)).fetchone()
    outcomes = []
    if row is None:
        return [BackfillOutcome(paper_uid, k, BACKFILL_MISSING) for k in sorted(fields)]

    current = {"title": row[0], "abstract": row[1], "year": row[2]}
    ts = float(now if now is not None else time.time())
    for k in sorted(fields):
        new = fields[k]
        old = current.get(k)
        if new is None:
            outcomes.append(BackfillOutcome(paper_uid, k, BACKFILL_SAME, old, old))
            continue
        if not only_if_empty and str(new) == str(old or ""):
            outcomes.append(BackfillOutcome(paper_uid, k, BACKFILL_SAME, old, new))
            continue
        if only_if_empty and str(old or "").strip():
            outcomes.append(BackfillOutcome(paper_uid, k, BACKFILL_NOT_EMPTY, old, new))
            continue
        if dry_run:
            outcomes.append(BackfillOutcome(paper_uid, k, BACKFILL_DRY_RUN, old, new))
            continue
        conn.execute(f"UPDATE papers SET {k} = ? WHERE paper_id = ?", (new, paper_uid))
        outcomes.append(BackfillOutcome(paper_uid, k, BACKFILL_APPLIED, old, new))
    return outcomes


# ══════════════════════════════════════════════════════════
# topic_papers 唯一写入入口（P0-B1b）
# ══════════════════════════════════════════════════════════

TOPIC_PAPER_CREATED = "CREATED"
TOPIC_PAPER_REUSED = "REUSED"
TOPIC_PAPER_DRY_RUN = "DRY_RUN"
TOPIC_PAPER_REFRESHED = "REFRESHED"


@dataclass
class TopicPaperOutcome:
    status: str
    topic_id: str
    paper_uid: str
    relevance_label: str
    evidence_refreshed: bool = False
    note: str = ""

    def as_dict(self):
        return {"status": self.status, "topic_id": self.topic_id,
                "paper_uid": self.paper_uid, "relevance_label": self.relevance_label,
                "evidence_refreshed": self.evidence_refreshed, "note": self.note}


def register_topic_paper(conn, topic_id, paper_uid, relevance_label, *, source,
                         label_source=None, promotion_status=None,
                         first_seen_run=None, evidence=None, refresh_evidence=False,
                         dry_run=False, now=None):
    """``topic_papers``（论文 × 主题 关系）的**唯一**写入入口。

    语义
    ----
    * ``paper_uid`` **必须已存在**于 ``papers`` -> 否则 ``ValueError``。
      关系不能先于实体：若这里允许自动建论文，就等于在 ``resolve_or_create_paper``
      之外开了第二个身份生产者（R1 立刻失守）。
    * 幂等：PK ``(topic_id, paper_uid)`` 已存在 -> ``REUSED``，**不覆盖既有判定**。
      ``relevance_label`` 是外部审计的结论，覆盖它会静默吞掉诊断信息
      （与 ``identity_conflicts`` 只记录不覆盖同源纪律）。
    * ``refresh_evidence=True`` 且 ``evidence`` 非空 -> 允许刷新 ``evidence_json``
      （这是「摘要补全后同步更新依据」的受限通道），但**绝不改 label**。
    """
    if not source or not str(source).strip():
        raise ValueError("source 必填：无溯源的写入不允许进入 KB")
    if relevance_label not in ("RELEVANT", "UNCERTAIN", "IRRELEVANT"):
        raise ValueError(f"relevance_label 非法: {relevance_label!r}")

    if not paper_exists(conn, paper_uid):
        raise ValueError(
            f"paper_uid={paper_uid!r} 不在 papers 中。关系不得先于实体："
            "请先经 resolve_or_create_paper() 建立论文身份。")

    ev_json = evidence if isinstance(evidence, str) else (
        json.dumps(evidence, ensure_ascii=False) if evidence is not None else None)

    existing = conn.execute(
        "SELECT evidence_json FROM topic_papers WHERE topic_id = ? AND paper_id = ?",
        (topic_id, paper_uid)).fetchone()

    if existing is None:
        if dry_run:
            return TopicPaperOutcome(TOPIC_PAPER_DRY_RUN, topic_id, paper_uid,
                                     relevance_label, note="dry-run: zero writes")
        conn.execute(
            "INSERT INTO topic_papers (topic_id, paper_id, relevance_label, label_source, "
            "promotion_status, first_seen_run, evidence_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (topic_id, paper_uid, relevance_label, label_source, promotion_status,
             first_seen_run, ev_json, float(now if now is not None else time.time())))
        return TopicPaperOutcome(TOPIC_PAPER_CREATED, topic_id, paper_uid, relevance_label)

    if refresh_evidence and ev_json and ev_json != existing[0]:
        if dry_run:
            return TopicPaperOutcome(TOPIC_PAPER_DRY_RUN, topic_id, paper_uid,
                                     relevance_label, note="dry-run: evidence refresh only")
        conn.execute(
            "UPDATE topic_papers SET evidence_json = ? WHERE topic_id = ? AND paper_id = ?",
            (ev_json, topic_id, paper_uid))
        return TopicPaperOutcome(TOPIC_PAPER_REFRESHED, topic_id, paper_uid,
                                 relevance_label, evidence_refreshed=True,
                                 note=f"source={source}")

    return TopicPaperOutcome(TOPIC_PAPER_REUSED, topic_id, paper_uid, relevance_label,
                             note="existing label preserved (no overwrite)")


def _iso(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _conflict_exists(conn, cf):
    """幂等去重（R6）：同 (type, id_type, norm, incoming_uid) 只记一次。"""
    row = conn.execute(
        "SELECT 1 FROM identity_conflicts WHERE conflict_type = ? AND id_type = ? "
        "AND normalized_value = ? AND incoming_paper_uid = ?",
        cf.dedup_key).fetchone()
    return row is not None
