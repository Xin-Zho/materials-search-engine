# -*- coding: utf-8 -*-
"""统一论文身份层（P0-A）：标识符归一化 + 回填计划 + 冲突判定。

用户 2026-09-10 拍板的设计约束（docs/2026-09-10-identity-and-event-db-proposal.md §3 修订）:
  1. 唯一约束落在 ``UNIQUE(id_type, normalized_value)``；原始值记录在 ``id_value``。
     DOI 大小写 / ``https://doi.org/`` 前缀 / W-ID URL 形式必须在标准化层消除，
     否则仍会产生重复。这是护栏：同一外部标识不可能被两个实体认领。
  2. uid 不可变。本层不做任何 uid 改写（实测 30 条错位 uid 被 topic_papers 引用）。
  3. 冲突只记录不覆盖（overwrite 会让诊断信息被静默吞掉 = 多事实源复发路径）。
  4. 无 DOI 记录是一等公民：W-only / EID-only 直接以对应标识作 canonical uid。
  5. **版本关系不属于本层**（IDENTICAL/VERSION_OF/ERRATUM_OF/PART_OF 留给后续
     ``paper_relations``）。``identity_conflicts`` 只描述标识认领冲突。
  6. **title 同题不得自动合并**：同题只能作为冲突候选证据，产出 REVIEW_REQUIRED。
     SI 与正文近似同题、preprint 与 published 完全同题、不同论文也可能同题、
     机构库可能只是镜像 —— 这些都无法由 title 区分。自动合并只能依赖高置信身份规则。

口径术语（统一，禁混用）:
  W_PRIMARY              主键来源为 OpenAlex（paper_id 形如 ``openalex:W...``）
  W_TRUE_ONLY            只有 W-ID，无有效 DOI 且无有效 EID
  MULTI_ID_WITH_W        拥有 W-ID 以及其他有效身份（DOI 或 EID）
  NO_DOI                 **无有效 DOI**（注意：不是「doi 列为空」——列里可能是错位的 EID）
  EFFECTIVE_DOI_PRIMARY  前缀为 ``doi:`` 且 DOI 值确实有效
  UID_PREFIX_MISMATCH    uid 前缀声明的类型与实际可用身份不符（真实存在 30 条）

本模块为纯函数层：不打开数据库、不写盘。迁移工具与后续读写路径（P0-B/P1）共用它。
"""

import hashlib
import re

# id_type 白名单（与 DDL CHECK 一致）
ID_TYPES = ("DOI", "OPENALEX", "SCOPUS_EID", "PUBMED", "ARXIV", "ISBN", "URL")
# 冲突表额外允许 TITLE（同题候选证据，非标识符）
CONFLICT_ID_TYPES = ID_TYPES + ("TITLE",)

CONFLICT_TYPES = (
    "IDENTIFIER_ALREADY_OWNED",     # 该 (id_type, norm) 已被另一个 uid 认领
    "MISPLACED_IDENTIFIER",         # 值本身是合法标识，但出现在错误的列/前缀（可恢复，需人工确认）
    "INVALID_IDENTIFIER",           # 原值非空但无法标准化为任何已知标识（不可恢复）
    "TITLE_COLLISION_CANDIDATE",    # 同题候选（不得自动合并）
    "NO_IDENTIFIER",                # 该论文无任何有效外部标识
)

RESOLUTION_STATUSES = (
    "UNRESOLVED", "REVIEW_REQUIRED",
    "RESOLVED_MERGED", "RESOLVED_KEPT", "RESOLVED_INVALID",
)

# 主身份优先级（当 uid 前缀声明的类型不可用时，取优先级最高者作 primary）
PRIMARY_PRIORITY = {"DOI": 0, "OPENALEX": 1, "SCOPUS_EID": 2,
                    "PUBMED": 3, "ARXIV": 4, "ISBN": 5, "URL": 6}

# papers 列 -> (id_type, source 标签)
COLUMN_SOURCES = (
    ("doi", "DOI", "papers.doi"),
    ("openalex_id", "OPENALEX", "papers.openalex_id"),
    ("scopus_eid", "SCOPUS_EID", "papers.scopus_eid"),
)

# ── uid 前缀的**唯一**权威表（P0-B1b 收口）──────────────────────────────
# 命名空间即类型声明：``<prefix>:<value>`` 的 value 必须是该类型的**合法形态**。
# 全仓任何模块需要拼 uid 字符串，都必须经 make_paper_uid() / make_record_id()；
# 静态守卫（tests/test_p0b1b_static_guards.py）据此证明不存在第二个自造命名空间的地方。
UID_PREFIX_BY_TYPE = {
    "DOI": "doi",
    "OPENALEX": "openalex",
    "SCOPUS_EID": "scopus",
    "PUBMED": "pubmed",
    "ARXIV": "arxiv",
    "ISBN": "isbn",
    "URL": "url",
}
UID_TYPE_BY_PREFIX = {v: k for k, v in UID_PREFIX_BY_TYPE.items()}

# 无任何可识别标识时的本地命名空间。
# **绝不**把 title / hash 塞进 openalex: / scopus: —— 那正是 P0-A 那 30 条的病理。
LOCAL_UID_PREFIX = "local"

_PID_PREFIX_TYPE = tuple((UID_PREFIX_BY_TYPE[t] + ":", t)
                         for t in ("DOI", "OPENALEX", "SCOPUS_EID"))


def uid_prefix(id_type):
    """id_type -> uid 前缀。无对应前缀时抛错（不静默回退，避免类型混淆）。"""
    p = UID_PREFIX_BY_TYPE.get(id_type)
    if p is None:
        raise ValueError(f"no uid prefix for id_type={id_type!r}; "
                         f"known={sorted(UID_PREFIX_BY_TYPE)}")
    return p


def make_record_id(source, value):
    """**检索层源记录标识**的唯一构造出口：``<source>:<value>``。

    ⚠️ 这不是 KB 的 paper_uid —— 两者语义不同，切勿互换：

    ==================  ===========================  ==============================
                         KB 身份                      源记录 ID
    ==================  ===========================  ==============================
    构造函数            ``make_paper_uid()``         ``make_record_id()``
    落库位置            ``papers.paper_id``          ``Paper.paper_id``（内存传输）
    值的要求            **标准化后的合法标识**        外部源返回的任意串（可为标题片段）
    唯一性              全库唯一                     仅在同一 source 内可比
    ==================  ===========================  ==============================

    存在的意义是**消除字面量**：``f"scopus:{x}"`` 这类写法曾经散落 29 处，
    其中一部分（``knowledge_extractor`` 的 ``canonical_paper_id``）直接写进了 KB 事实层，
    另一部分在 uid 前缀语义过载时无法与真正的 KB uid 区分。
    收口到本函数后，静态守卫可以证明全仓不存在第二处自造命名空间。
    """
    return f"{source}:{value}"


def scopus_cache_key(value):
    """``scopus_cache.db.papers.paper_id`` 的构造出口。

    ⚠️ 与 KB uid **无关**：这是**引擎层缓存库**的记录键（同名 ``papers`` 表，不同库，
    9.8 万行 vs 331 行）。全仓曾有 9 处 ``"scopus:" + doi`` 手工拼接去查这张表 ——
    收口到本函数后，静态守卫能证明没有第二处在自造这个命名空间。
    """
    return f"{UID_PREFIX_BY_TYPE['SCOPUS_EID']}:{str(value or '').strip().lower()}"


def scopus_cache_key_value(paper_id):
    """``scopus_cache_key`` 的逆运算（读写必须对称）。

    ⚠️ 不能用 :func:`extract_from_paper_uid` 代替：后者做**形态校验**，
    而缓存键里的值是任意串（可能是 DOI、可能是空），形态校验会把合法键判为 None。
    """
    p = str(paper_id or "")
    pref = UID_PREFIX_BY_TYPE["SCOPUS_EID"] + ":"
    return p[len(pref):] if p.startswith(pref) else p


_TITLE_HASH_LEN = 16


def make_paper_uid(*, claims=None, title=None, year=None, priority=None):
    """由标识集合生成**稳定** uid（KB 身份的唯一生成规则）。

    规则（deterministic，与调用顺序无关）：
      1. 取**优先级**最高的可用标识 -> ``<prefix>:<normalized_value>``
      2. 无任何标识（title-only）-> ``local:<sha256(norm_title|year)[:16]``
         **绝不**把 title 塞进 ``openalex:`` / ``scopus:`` 命名空间。

    ``priority``
        可选的 ``{id_type: rank}`` 覆写，默认 ``PRIMARY_PRIORITY``（KB 口径：
        DOI > OPENALEX > SCOPUS_EID）。P4 时间基准数据集传 **OPENALEX 优先**，
        因为它的语料以 OpenAlex 为记录源。

        ⚠️ 这是**参数**，不是第二套实现 —— uid 命名空间的生成仍然只有本函数一处
        （static guard G2 守的就是这一点）。用户 2026-09-12 的裁定把
        ``entity_id`` 与 ``preferred_identifier`` 分成两个概念：改优先级只影响
        **新建实体**用哪个标识做 id，不影响既有 uid，也不影响「引用时哪个标识最优」。
        故数据集侧同时落 ``preferred_identifier``（DOI 优先）与 ``kb_paper_uid`` 作桥。

    注意：本函数只生成**新** uid。既有 uid 的解析走 ``lookup_owners``，永不重算。
    """
    ranks = priority or PRIMARY_PRIORITY
    if claims:
        best = min(claims, key=lambda c: ranks.get(c.id_type, 99))
        return f"{uid_prefix(best.id_type)}:{best.normalized_value}"
    key = "|".join([normalize_title(title) or "", str(year or "")])
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:_TITLE_HASH_LEN]
    return f"{LOCAL_UID_PREFIX}:{digest}"

_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")
_W_RE = re.compile(r"^[Ww]\d+$")
_EID_RE = re.compile(r"^2-s2\.0-\d+$")
_PMID_RE = re.compile(r"^\d+$")
_ARXIV_RE = re.compile(r"^(\d{4}\.\d{4,5}|[a-z\-]+(\.[a-z\-]+)?/\d{7})(v\d+)?$")
_ISBN_RE = re.compile(r"^(\d{9}[\dx]|\d{13})$")
_URL_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]*\.[a-z]{2,}(/|$)")

_NULLISH = ("none", "nan", "null", "-", "n/a", "na")


def normalize_identifier(id_type, raw):
    """把原始标识值标准化为唯一约束所用的键。返回 None 表示非法/空。

    标准化规则（PRIMARY KEY 落在返回值上）：
      DOI         小写；去 ``doi:`` / ``https://[dx.]doi.org/`` 前缀；去历史脏尾巴；需匹配 10.xxxx/yyy
      OPENALEX    去 ``openalex:`` / URL 前缀；大写 Wnnn
      SCOPUS_EID  去 ``scopus:`` 前缀；需匹配 2-s2.0-nnn
      PUBMED      去 ``pmid:`` 前缀；纯数字
      ARXIV       去 ``arxiv:`` / ``arxiv.org/abs/`` 前缀；小写
      ISBN        去连字符与空白；大写
      URL         去首尾空白（保守，不做语义归一）
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    low = s.lower()
    # 历史脏数据：HTML 残留 + 空值字面量
    for junk in ("</div", "</span", "<div"):
        if junk in low:
            s = s[:low.index(junk)].strip()
            low = s.lower()
    if low in _NULLISH:
        return None

    if id_type == "DOI":
        s = re.sub(r"^(doi:\s*|doi\s+)", "", low)
        s = re.sub(r"^https?://(dx\.)?doi\.org/", "", s)
        s = s.strip().strip(".")
        return s if _DOI_RE.match(s) else None

    if id_type == "OPENALEX":
        s = s.strip()
        # ⚠️ 必须**循环**剥离：KB 历史数据里存在叠加前缀形态
        # ``openalex:https://openalex.org/W...``（uid 前缀 + URL 前缀），
        # 单次剥离只去掉一层，随后 _W_RE 匹配失败 —— 于是这条记录会被判为
        # 「无任何标识」并落到 local:<hash>，与它真实拥有的 W-ID 脱钩。
        # （P0-B1b 实测：这正是 W1 重放差异中除那 30 条之外的**唯一**一条。）
        prefixes = ("https://openalex.org/", "http://openalex.org/",
                    "https://api.openalex.org/works/", "openalex:")
        changed = True
        while changed:
            changed = False
            for p in prefixes:
                if s.lower().startswith(p):
                    s = s[len(p):]
                    changed = True
                    break
        s = s.strip().strip("/")
        s = re.sub(r"^works/", "", s, flags=re.I)
        return s.upper() if _W_RE.match(s) else None

    if id_type == "SCOPUS_EID":
        s = s.strip()
        if s.lower().startswith("scopus:"):
            s = s[len("scopus:"):]
        s = s.strip()
        return s if _EID_RE.match(s) else None

    if id_type == "PUBMED":
        s = re.sub(r"^(pmid:\s*|pmid\s+)", "", s.lower()).strip()
        return s if _PMID_RE.match(s) else None

    if id_type == "ARXIV":
        s = re.sub(r"^(arxiv:\s*)", "", s.lower())
        s = re.sub(r"^https?://arxiv\.org/(abs|pdf)/", "", s)
        s = s.strip().strip("/")
        # 必须匹配 arXiv 编号（新式 2401.12345 / 旧式 math.GT/0701001）。过宽会让
        # detect_actual_type 把任意垃圾串判为 arXiv。
        return s if _ARXIV_RE.match(s) else None

    if id_type == "ISBN":
        s = re.sub(r"[-\s]", "", s).upper()
        return s if _ISBN_RE.match(s) else None

    if id_type == "URL":
        s = re.sub(r"^https?://", "", s.lower()).strip().rstrip("/")
        # 必须是 host[/path] 形态。若过宽，detect_actual_type 会把任意垃圾串判为 URL
        if not _URL_RE.match(s):
            return None
        return s or None

    raise ValueError(f"unknown id_type: {id_type!r}")


def detect_actual_type(raw, exclude=None):
    """识别一个值「实际上」是什么类型的标识（用于错位诊断，不能作为身份判定依据）。

    返回类型名或 None（无法识别）。``exclude`` 用于跳过声明类型本身。
    """
    if raw is None:
        return None
    for t in ID_TYPES:
        if t == exclude:
            continue
        if normalize_identifier(t, raw) is not None:
            return t
    return None


def normalize_title(raw):
    """标题标准化（仅用于同题候选分组，绝不用作身份判定）。"""
    if not raw:
        return None
    s = str(raw).strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^\w\s]", "", s, flags=re.UNICODE)
    return s or None


def uid_prefix_type(paper_uid):
    """paper_uid 的前缀身份类型（openalex:W.. -> OPENALEX）。无前缀返回 None。"""
    p = str(paper_uid or "")
    for pref, t in _PID_PREFIX_TYPE:
        if p.startswith(pref):
            return t
    return None


def extract_from_paper_uid(paper_uid):
    """从 canonical uid 反解标识（uid 不可变前提下的自证通道）。

    返回 [(id_type, normalized_value)]，可能为空。用于保证「每个已知身份可反向解析」。
    """
    p = str(paper_uid or "")
    out = []
    for pref, t in _PID_PREFIX_TYPE:
        if p.startswith(pref):
            v = normalize_identifier(t, p[len(pref):])
            if v:
                out.append((t, v))
            break
    return out


class IdentifierClaim:
    """一条待写入 paper_identifiers 的记录。"""

    __slots__ = ("id_type", "normalized_value", "id_value", "paper_uid",
                 "source", "confidence", "is_primary", "first_seen_run")

    def __init__(self, id_type, normalized_value, id_value, paper_uid,
                 source, confidence=1.0, is_primary=0, first_seen_run=None):
        self.id_type = id_type
        self.normalized_value = normalized_value
        self.id_value = id_value
        self.paper_uid = paper_uid
        self.source = source
        self.confidence = confidence
        self.is_primary = is_primary
        self.first_seen_run = first_seen_run

    @property
    def key(self):
        return (self.id_type, self.normalized_value)

    def as_row(self):
        return (self.id_type, self.normalized_value, self.paper_uid, self.id_value,
                self.source, self.confidence, self.is_primary, self.first_seen_run)


class IdentityConflict:
    """一条待写入 identity_conflicts 的记录（记录「为什么不能写入」，不覆盖）。"""

    __slots__ = ("id_type", "normalized_value", "incoming_paper_uid", "existing_paper_uid",
                 "incoming_value", "existing_value", "conflict_type", "source",
                 "provenance_json", "resolution_status", "resolution_decision",
                 "resolver", "resolver_version")

    def __init__(self, id_type, normalized_value, incoming_paper_uid, conflict_type,
                 existing_paper_uid=None, incoming_value=None, existing_value=None,
                 source=None, provenance_json=None,
                 resolution_status="UNRESOLVED", resolution_decision=None,
                 resolver=None, resolver_version=None):
        self.id_type = id_type
        self.normalized_value = normalized_value or ""
        self.incoming_paper_uid = incoming_paper_uid
        self.existing_paper_uid = existing_paper_uid
        self.incoming_value = incoming_value
        self.existing_value = existing_value
        self.conflict_type = conflict_type
        self.source = source
        self.provenance_json = provenance_json
        self.resolution_status = resolution_status
        self.resolution_decision = resolution_decision
        self.resolver = resolver
        self.resolver_version = resolver_version

    @property
    def dedup_key(self):
        return (self.conflict_type, self.id_type, self.normalized_value,
                self.incoming_paper_uid)

    def as_row(self, detected_at):
        return (self.id_type, self.normalized_value, self.incoming_paper_uid,
                self.existing_paper_uid, self.incoming_value, self.existing_value,
                self.conflict_type, self.source, self.provenance_json, detected_at,
                self.resolution_status, self.resolution_decision,
                self.resolver, self.resolver_version)


class Anomaly:
    """列值异常（回填过程中发现的问题，一律转冲突记录，不静默丢弃）。"""

    __slots__ = ("declared_type", "raw", "source", "kind", "actual_type", "recovered")

    def __init__(self, declared_type, raw, source, kind, actual_type=None, recovered=False):
        self.declared_type = declared_type
        self.raw = raw
        self.source = source
        self.kind = kind                # MISPLACED | INVALID
        self.actual_type = actual_type  # MISPLACED 时指向真实类型
        self.recovered = recovered      # 是否已按真实类型补回一条 identifier


def collect_row_claims(paper_uid, *, doi=None, openalex_id=None, scopus_eid=None):
    """从 papers 一行的三列 + uid 前缀收集标识候选。

    返回 ``(claims, anomalies)``:
      claims    去重后的 IdentifierClaim（同 (id_type, norm) 一条；列值优先于 uid 派生）
                ``is_primary`` 由 plan_backfill 统一裁决，此处恒为 0
      anomalies Anomaly 列表（错位 / 非法 / 从错列恢复）

    错位恢复策略：**正确列优先**。先采集全部列的合法值（source 反映真实列，confidence 1.0），
    再处理错位/非法值；仅当某值的真实类型**在本行无任何承载**时，才按真实类型补一条
    identifier（``confidence=0.9``、``source='recovered_from:<col>'``），
    确保「每个合法标识均回填」；同时仍产出冲突记录保留错位事实。
    """
    row = {"doi": doi, "openalex_id": openalex_id, "scopus_eid": scopus_eid}
    claims, seen, pending = [], set(), []

    # Pass 1: 各列的合法值（正确列优先，source 反映真实列）
    for col, id_type, source in COLUMN_SOURCES:
        raw = row.get(col)
        if raw is None:
            continue
        s = str(raw).strip()
        if not s:
            continue
        norm = normalize_identifier(id_type, s)
        if norm is None:
            pending.append((col, id_type, source, s))
            continue
        if (id_type, norm) in seen:
            continue
        seen.add((id_type, norm))
        claims.append(IdentifierClaim(
            id_type, norm, s, paper_uid, source, first_seen_run="p0a_backfill"))

    # Pass 2: 错位 / 非法值（仅无承载时恢复）
    anomalies = []
    for col, id_type, source, s in pending:
        actual = detect_actual_type(s, exclude=id_type)
        recovered = False
        if actual is not None:
            av = normalize_identifier(actual, s)
            if av and (actual, av) not in seen:
                seen.add((actual, av))
                claims.append(IdentifierClaim(
                    actual, av, s, paper_uid, f"recovered_from:{col}",
                    confidence=0.9, first_seen_run="p0a_backfill"))
                recovered = True
        anomalies.append(Anomaly(id_type, s, source,
                                 "MISPLACED" if actual else "INVALID",
                                 actual_type=actual, recovered=recovered))

    # Pass 3: uid 前缀自证（uid 不可变；列缺失时仍应能反解出身份）
    for id_type, norm in extract_from_paper_uid(paper_uid):
        if (id_type, norm) in seen:
            continue
        seen.add((id_type, norm))
        claims.append(IdentifierClaim(
            id_type, norm, str(paper_uid), paper_uid, "paper_id_prefix",
            first_seen_run="p0a_backfill"))

    return claims, anomalies


def assign_primary(claims, paper_uid):
    """裁决一个 uid 的 is_primary（每个有身份的 uid 恰好一个）。

    uid 前缀声明的类型可用 -> 它即主身份；否则取优先级最高的可用身份，
    并在返回值中报告前缀错配（UID_PREFIX_MISMATCH）。
    """
    if not claims:
        return None
    declared = uid_prefix_type(paper_uid)
    chosen = None
    for c in claims:
        if c.id_type == declared:
            chosen = c
            break
    mismatch = None
    if chosen is None:
        chosen = min(claims, key=lambda c: PRIMARY_PRIORITY.get(c.id_type, 99))
        mismatch = {"paper_uid": paper_uid, "declared": declared,
                    "effective": chosen.id_type}
    chosen.is_primary = 1
    return mismatch


def plan_backfill(rows, *, title_collision=True, resolver_version="p0a_v1"):
    """构建回填计划（纯计算，零写入）。

    rows: 可迭代 dict（``paper_uid`` 必填，可选 ``doi``/``openalex_id``/``scopus_eid``/``title``）
          或 5-tuple ``(uid, doi, w, eid, title)``。

    返回 ``identifiers`` / ``conflicts`` / ``reverse_map`` / ``terms`` / ``stats`` /
    ``uid_prefix_mismatches``。
    """
    rows = list(rows)
    identifiers, conflicts = [], []
    claimed = {}          # (id_type, norm) -> claim（首认领者胜，后续记冲突）
    reverse_map = {}
    title_groups = {}
    mismatches = []
    terms = empty_terms()   # 与 identifiers 同源累积（避免双口径）
    n_rows = n_no_id = n_misplaced = n_invalid = n_recovered = 0

    for idx, r in enumerate(rows):
        uid, doi, wid, eid, title = _unpack_row(r)
        n_rows += 1

        claims, anomalies = collect_row_claims(
            uid, doi=doi, openalex_id=wid, scopus_eid=eid)

        for a in anomalies:
            if a.kind == "MISPLACED":
                n_misplaced += 1
                if a.recovered:
                    n_recovered += 1
                conflicts.append(IdentityConflict(
                    a.declared_type, normalize_identifier(a.actual_type, a.raw) or "",
                    uid, "MISPLACED_IDENTIFIER",
                    incoming_value=a.raw, source=a.source,
                    provenance_json=(
                        '{"looks_like":"%s","recovered_as":"%s","recovered":%s,'
                        '"note":"value is a valid %s but sits in the %s column; '
                        'uid prefix left untouched (referenced by other tables)"}'
                        % (a.actual_type, a.actual_type, str(a.recovered).lower(),
                           a.actual_type, a.declared_type)),
                    resolution_status="REVIEW_REQUIRED",
                    resolution_decision="PENDING_HUMAN_REVIEW",
                    resolver=resolver_version, resolver_version=resolver_version))
            else:
                n_invalid += 1
                conflicts.append(IdentityConflict(
                    a.declared_type, "", uid, "INVALID_IDENTIFIER",
                    incoming_value=a.raw, source=a.source,
                    provenance_json=(
                        '{"reason":"raw non-empty but matches no known id_type"}'),
                    resolution_status="REVIEW_REQUIRED",
                    resolver=resolver_version, resolver_version=resolver_version))

        if not claims:
            n_no_id += 1
            conflicts.append(IdentityConflict(
                "DOI", "", uid, "NO_IDENTIFIER",
                provenance_json='{"reason":"no valid DOI/OPENALEX/SCOPUS_EID on row"}',
                resolution_status="REVIEW_REQUIRED",
                resolver=resolver_version, resolver_version=resolver_version))

        mm = assign_primary(claims, uid)
        if mm:
            mm.update({"doi": doi, "openalex_id": wid, "scopus_eid": eid})
            mismatches.append(mm)
        accumulate_terms(terms, uid, claims)

        for c in claims:
            owner = claimed.get(c.key)
            if owner is None:
                claimed[c.key] = c
                identifiers.append(c)
                reverse_map[f"{c.id_type}:{c.normalized_value}"] = uid
            elif owner.paper_uid != uid:
                conflicts.append(IdentityConflict(
                    c.id_type, c.normalized_value, uid, "IDENTIFIER_ALREADY_OWNED",
                    existing_paper_uid=owner.paper_uid,
                    incoming_value=c.id_value, existing_value=owner.id_value,
                    source=c.source,
                    provenance_json=(
                        '{"incoming_source":"%s","existing_source":"%s",'
                        '"note":"first-claim-wins; no overwrite, no auto-merge"}'
                        % (c.source, owner.source)),
                    resolution_status="UNRESOLVED",
                    resolver=resolver_version, resolver_version=resolver_version))

        if title_collision:
            tnorm = normalize_title(title)
            if tnorm:
                title_groups.setdefault(tnorm, []).append((idx, uid, str(title).strip()))

    # 同题候选：仅证据，REVIEW_REQUIRED，绝不自动合并
    n_title_groups = 0
    for tnorm, members in sorted(title_groups.items()):
        if len(members) < 2:
            continue
        n_title_groups += 1
        members = sorted(members)
        first_uid, first_raw = members[0][1], members[0][2]
        for _i, uid, raw in members[1:]:
            conflicts.append(IdentityConflict(
                "TITLE", tnorm, uid, "TITLE_COLLISION_CANDIDATE",
                existing_paper_uid=first_uid, incoming_value=raw, existing_value=first_raw,
                source="papers.title",
                provenance_json=(
                    '{"group_size":%d,"members":%s,'
                    '"note":"title equality is evidence only; cannot decide IDENTICAL -- '
                    'SI/preprint/mirror are indistinguishable by title"}'
                    % (len(members), _json_uids([m[1] for m in members]))),
                resolution_status="REVIEW_REQUIRED",
                resolution_decision="PENDING_HUMAN_REVIEW",
                resolver=resolver_version, resolver_version=resolver_version))

    identifiers.sort(key=lambda c: (c.paper_uid, -c.is_primary, c.id_type))
    conflicts.sort(key=lambda c: (c.conflict_type, c.id_type,
                                  c.normalized_value, c.incoming_paper_uid))

    by_type, conf_by_type = {}, {}
    for c in identifiers:
        by_type[c.id_type] = by_type.get(c.id_type, 0) + 1
    for c in conflicts:
        conf_by_type[c.conflict_type] = conf_by_type.get(c.conflict_type, 0) + 1

    stats = {
        "rows": n_rows,
        "identifiers": len(identifiers),
        "identifiers_by_type": by_type,
        "primary_marked": sum(1 for c in identifiers if c.is_primary),
        "uids_with_identity": len({c.paper_uid for c in identifiers}),
        "conflicts": len(conflicts),
        "conflicts_by_type": conf_by_type,
        "title_collision_groups": n_title_groups,
        "rows_without_identifier": n_no_id,
        "misplaced_values": n_misplaced,
        "misplaced_recovered": n_recovered,
        "invalid_values": n_invalid,
        "uid_prefix_mismatches": len(mismatches),
        "distinct_identifiers": len(claimed),
    }
    return {"identifiers": identifiers, "conflicts": conflicts,
            "reverse_map": reverse_map, "terms": terms, "stats": stats,
            "uid_prefix_mismatches": mismatches}


def _json_uids(uids):
    return "[" + ",".join('"%s"' % u for u in uids) + "]"


def _unpack_row(r):
    """统一行解包：dict 或 5-tuple -> (uid, doi, wid, eid, title)。"""
    if isinstance(r, dict):
        return (r["paper_uid"], r.get("doi"), r.get("openalex_id"),
                r.get("scopus_eid"), r.get("title"))
    return (r[0], r[1] if len(r) > 1 else None, r[2] if len(r) > 2 else None,
            r[3] if len(r) > 3 else None, r[4] if len(r) > 4 else None)


def empty_terms():
    """统一口径计数器的零值（术语定义见模块 docstring）。"""
    return {"rows": 0, "DOI_PRIMARY": 0, "W_PRIMARY": 0, "EID_PRIMARY": 0,
            "NO_PREFIX": 0, "EFFECTIVE_DOI_PRIMARY": 0, "UID_PREFIX_MISMATCH": 0,
            "W_TRUE_ONLY": 0, "MULTI_ID_WITH_W": 0, "NO_DOI": 0,
            "NO_IDENTIFIER": 0, "with_doi": 0, "with_wid": 0, "with_eid": 0}


def accumulate_terms(t, uid, claims):
    """按 **claims**（而非原始列）累积口径计数。

    关键：口径必须与写进 paper_identifiers 的身份完全同源，否则会出现
    「表里有 DOI 但计数说 NO_DOI」这类双口径打架 —— 正是多事实源的复发路径。
    因此这里计入 uid 前缀自证与错位恢复得到的身份。
    """
    types = {c.id_type for c in claims}
    d, w, e = "DOI" in types, "OPENALEX" in types, "SCOPUS_EID" in types
    p = uid_prefix_type(uid)
    t["rows"] += 1
    if p == "DOI":
        t["DOI_PRIMARY"] += 1
        if d:
            t["EFFECTIVE_DOI_PRIMARY"] += 1
    elif p == "OPENALEX":
        t["W_PRIMARY"] += 1
    elif p == "SCOPUS_EID":
        t["EID_PRIMARY"] += 1
    else:
        t["NO_PREFIX"] += 1
    if p and p not in types:
        t["UID_PREFIX_MISMATCH"] += 1
    if w and not d and not e:
        t["W_TRUE_ONLY"] += 1
    if w and (d or e):
        t["MULTI_ID_WITH_W"] += 1
    if not d:
        t["NO_DOI"] += 1
    if not (d or w or e):
        t["NO_IDENTIFIER"] += 1
    t["with_doi"] += 1 if d else 0
    t["with_wid"] += 1 if w else 0
    t["with_eid"] += 1 if e else 0
    return t


def compute_terms(rows):
    """独立口径统计（与 plan_backfill 同源：都走 collect_row_claims）。

    身份判断基于 **有效值**（normalize 通过），含 uid 前缀自证与错位恢复 ——
    不是「列非空」。用户 2026-09-10 要求固定术语，避免把「以 W 为主键」说成「只有 W」。
    """
    t = empty_terms()
    for r in rows:
        uid, doi, wid, eid, _title = _unpack_row(r)
        claims, _ = collect_row_claims(uid, doi=doi, openalex_id=wid, scopus_eid=eid)
        accumulate_terms(t, uid, claims)
    return t


def make_canonical_uid(*, doi=None, openalex_id=None, scopus_eid=None,
                       title=None, year=None):
    """便捷入口：从常见**声明字段**产出 canonical uid（P0-B1b）。

    与 ``paper_writer.classify_metadata`` 的区别：本函数**只在传入的那几个字段内**
    按值形态识别（不跨列乱猜、不产生冲突记录），供 tools 层的写入/审计脚本使用 ——
    那些脚本原先各自手拼 ``f"doi:{doi}" if doi else paper_id``，其中「信任 doi 字段」
    正是 30 条错位扩散的路径（见 ``run_s8_extraction`` 的近失事件）。

    无任何有效标识 -> ``local:<hash>``（由 ``make_paper_uid`` 处理），**绝不**借用
    ``doi:`` / ``openalex:`` / ``scopus:`` 命名空间。
    """
    claims = []
    for id_type, raw in (("DOI", doi), ("OPENALEX", openalex_id),
                         ("SCOPUS_EID", scopus_eid)):
        if raw is None:
            continue
        s = str(raw).strip()
        if not s:
            continue
        n = normalize_identifier(id_type, s)
        if n is not None:
            claims.append(IdentifierClaim(id_type, n, s, None,
                                          "identity.make_canonical_uid"))
    return make_paper_uid(claims=claims, title=title, year=year)


def uid_id_type(paper_uid):
    """uid -> 它**声明**的 id_type（前缀语义的唯一查询出口）。

    ``local:`` 前缀返回 None（本地命名空间，不代表任何外部标识）。
    用它可以避免各处自己写 ``uid.startswith("doi:")`` 之类的判断 ——
    那类判断一旦散开，前缀命名空间就不再受单一权威表约束。
    """
    prefix = str(paper_uid or "").partition(":")[0]
    if prefix == LOCAL_UID_PREFIX:
        return None
    return UID_TYPE_BY_PREFIX.get(prefix)

