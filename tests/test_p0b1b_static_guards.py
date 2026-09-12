"""P0-B1b 静态守卫：把「身份命名空间」钉死在唯一出口。

守卫四类（用户 2026-09-12 指定）：
  G1  ``INSERT INTO papers``        生产路径禁止直写 papers
  G2  ``make uid``                  禁止在唯一出口之外重实现 uid 生成原语
  G3  ``f"doi:"`` / ``f"scopus:"``  禁止手拼 uid 前缀（f-string 与 ``+`` 两种写法）
  G4  ``startswith("<prefix>:")``   禁止手工剥/判前缀

为什么用 AST 而不是 grep
------------------------
1. **注释与文档字符串天然不参与**。本仓库有大量「解释这个历史缺陷」的说明文字
   （例如 identity.py、disposition_r06_funnel.py 里逐字引用的 ``f"scopus:{...}"``），
   用文本扫描会把它们全部误报成违规，守卫很快就会被加满豁免而失效。
2. **只在字面量部分命中**。收口后的写法是 ``f"{uid_prefix(t)}:{v}"`` —— 第一个
   分段是 FormattedValue；而手拼 ``f"doi:{v}"`` 的第一个分段是常量 ``"doi:"``。
   这条分界线恰好就是「受控前缀」与「硬编码命名空间」的分界，AST 能精确切开。
3. G1 的 SQL 只在**字符串常量**里匹配，所以 cache.py 里写给 ``scopus_cache.db``
   的同名 papers 语句能被显式登记，而不是靠正则碰运气。

白名单纪律
----------
每一项白名单都必须写明「为什么这里是合法的」，且由
:func:`test_whitelists_are_minimal` 持续证明它们**仍然必要**——
一旦某文件不再命中，就说明豁免已经变成僵尸，必须删除。
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]

# uid 命名空间前缀白名单（与 identity.UID_PREFIX_BY_TYPE / LOCAL_UID_PREFIX 同步）
PREFIXES = ("doi", "openalex", "scopus", "pubmed", "arxiv", "isbn", "url", "local")

# 扫描范围：排除测试自身（测试需要构造各种形态的样例数据）、依赖与数据目录
_SKIP_DIRS = {"__pycache__", ".venv", ".git", "data", "node_modules", "tests"}

# ── 白名单（只许缩短）──────────────────────────────────────────────
# G3：手拼 uid 前缀 —— **白名单为空**，全仓零例外。
# identity.py 也做到了：它拼前缀时用 ``f"{uid_prefix(t)}:{v}"``（首段是
# FormattedValue，前缀值来自 UID_PREFIX_BY_TYPE），因此不再需要豁免。
NAMESPACE_ALLOWED_G3 = {}

# G4：手工剥/判前缀 —— identity.normalize_identifier() 必须剥掉 ``scopus:`` 等前缀
# 才能归一化，这是唯一无法避免、也唯一应该存在的一处。
NAMESPACE_ALLOWED_G4 = {
    "search_engine/identity.py": "normalize_identifier 必须能剥输入里带的 uid 前缀",
}

# G1：允许出现 ``INSERT ... INTO papers`` 的文件，**按库**界定（P0-B1 的命名陷阱：
# knowledge_base.db.papers 9 列/331 行 与 scopus_cache.db.papers 3 列/98232 行同名不同库）。
PAPERS_INSERT_ALLOWED = {
    "search_engine/paper_writer.py": "KB papers 的唯一生产者（R1）",
    "search_engine/cache.py": "写 scopus_cache.db.papers（引擎缓存库），不触碰 KB",
}

# G2：uid 生成原语的唯一实现处
UID_PRIMITIVE_ALLOWED = {
    "search_engine/identity.py": "make_paper_uid / make_record_id 的唯一实现",
}

# 用户 2026-09-12 指定：该脚本未跟踪、与 P0 身份修复隔离，属独立任务。
# 其归属确定后**必须**从本白名单移除。
UNTRACKED_DEBT = {"tools/review_r06_funnel_19.py"}

_NS_AT_START = re.compile(r"^(?:" + "|".join(PREFIXES) + r")\s*:")
_NS_TOKEN = re.compile(r"^(?:" + "|".join(PREFIXES) + r")\s*:$")
_SQL_INSERT_PAPERS = re.compile(r"insert\s+(?:or\s+\w+\s+)?into\s+papers\b", re.I)


def _iter_sources():
    for root, dirs, files in os.walk(BASE):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fn in sorted(files):
            if not fn.endswith(".py"):
                continue
            p = Path(root) / fn
            yield p.relative_to(BASE).as_posix(), p


def _docstring_constant_ids(tree):
    """收集所有 docstring 字符串节点的 id（G1 扫描时跳过）。"""
    ids = set()
    owners = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    for node in ast.walk(tree):
        if not isinstance(node, owners):
            continue
        body = getattr(node, "body", None) or []
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            ids.add(id(body[0].value))
    return ids


def _scan(path):
    """-> {类别: [(行号, 说明)]}。AST 解析失败则直接失败（不静默跳过）。"""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    doc_ids = _docstring_constant_ids(tree)
    out = {"papers_insert": [], "namespace": [], "strip_prefix": [], "own_uid_impl": []}

    for node in ast.walk(tree):
        # ── G1：SQL 字面量 ──────────────────────────────────────
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in doc_ids and _SQL_INSERT_PAPERS.search(node.value)):
            out["papers_insert"].append((node.lineno, "INSERT ... INTO papers"))

        # ── G3a：f"<prefix>:..." ────────────────────────────────
        if isinstance(node, ast.JoinedStr) and node.values:
            v0 = node.values[0]
            if (isinstance(v0, ast.Constant) and isinstance(v0.value, str)
                    and _NS_AT_START.match(v0.value)):
                out["namespace"].append(
                    (node.lineno, f'f-string 字面量以 {v0.value!r} 开头'))

        # ── G3b："<prefix>:" + x ────────────────────────────────
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = node.left
            if (isinstance(left, ast.Constant) and isinstance(left.value, str)
                    and _NS_TOKEN.match(left.value)):
                out["namespace"].append(
                    (node.lineno, f'字符串拼接 {left.value!r} + ...'))

        # ── G4：startswith("<prefix>:") ─────────────────────────
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "startswith" and node.args):
            a0 = node.args[0]
            if (isinstance(a0, ast.Constant) and isinstance(a0.value, str)
                    and _NS_TOKEN.match(a0.value)):
                out["strip_prefix"].append(
                    (node.lineno, f'startswith({a0.value!r})'))

        # ── G2：uid 生成原语的定义 ───────────────────────────────
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in ("make_paper_uid", "make_record_id")):
            out["own_uid_impl"].append((node.lineno, f"def {node.name}"))
    return out


@pytest.fixture(scope="module")
def scan():
    """一次性扫描全仓，返回 {相对路径: 命中}（只留非空项）。"""
    result = {}
    for rel, path in _iter_sources():
        hits = _scan(path)
        if any(hits.values()):
            result[rel] = hits
    return result


def _violations(scan, kind, allowed):
    allowed = set(allowed) | UNTRACKED_DEBT
    bad = {}
    for rel, hits in scan.items():
        if rel in allowed:
            continue
        if hits[kind]:
            bad[rel] = hits[kind]
    return bad


# ══ G1：生产路径禁止直写 papers ════════════════════════════════════
def test_g1_no_direct_papers_insert(scan):
    bad = _violations(scan, "papers_insert", PAPERS_INSERT_ALLOWED)
    assert not bad, (
        "发现未登记的 papers 直写点（必须改走 paper_writer.resolve_or_create_paper）：\n"
        + "\n".join(f"  {rel}:{ln} {note}" for rel, hs in bad.items() for ln, note in hs))


# ══ G2：uid 生成原语只在唯一出口定义 ═══════════════════════════════
def test_g2_uid_primitives_defined_only_in_exit(scan):
    bad = _violations(scan, "own_uid_impl", UID_PRIMITIVE_ALLOWED)
    assert not bad, (
        "禁止在唯一出口之外重实现 uid 生成原语（请 import identity 的实现）：\n"
        + "\n".join(f"  {rel}:{ln} {note}" for rel, hs in bad.items() for ln, note in hs))


# ══ G3：禁止手拼 uid 前缀 ══════════════════════════════════════════
def test_g3_no_manual_namespace_concatenation(scan):
    bad = _violations(scan, "namespace", NAMESPACE_ALLOWED_G3)
    assert not bad, (
        "发现手拼 uid 前缀（请改用 identity.make_record_id / make_paper_uid / "
        "scopus_cache_key / uid_prefix）：\n"
        + "\n".join(f"  {rel}:{ln} {note}" for rel, hs in bad.items() for ln, note in hs))


# ══ G4：禁止手工剥/判前缀 ═════════════════════════════════════════
def test_g4_no_manual_prefix_stripping(scan):
    bad = _violations(scan, "strip_prefix", NAMESPACE_ALLOWED_G4)
    assert not bad, (
        "发现手工剥/判 uid 前缀（请改用 identity.extract_from_paper_uid / "
        "scopus_cache_key_value）：\n"
        + "\n".join(f"  {rel}:{ln} {note}" for rel, hs in bad.items() for ln, note in hs))


# ══ 白名单只许缩短（防僵尸豁免） ═══════════════════════════════════
def test_whitelists_are_minimal(scan):
    """每个白名单项必须**仍然命中**，否则就是僵尸豁免，必须删除。

    本检查不是形式主义：P0-B1b 收口过程中它**真的报出过一次** ——
    ``identity.py`` 原本在 G3 白名单里，收口后它改用受控拼接
    （``f"{uid_prefix(t)}:{v}"``），G3 命中归零，于是白名单被缩到空。
    「某项无命中」= 豁免已死，必须删除，否则守卫会慢慢被豁免淹没。
    """
    checks = [
        ("G1 papers_insert", PAPERS_INSERT_ALLOWED, "papers_insert"),
        ("G2 own_uid_impl", UID_PRIMITIVE_ALLOWED, "own_uid_impl"),
        ("G3 namespace", NAMESPACE_ALLOWED_G3, "namespace"),
        ("G4 strip_prefix", NAMESPACE_ALLOWED_G4, "strip_prefix"),
    ]
    stale = []
    for label, allowed, kind in checks:
        for rel in allowed:
            hits = (scan.get(rel) or {}).get(kind) or []
            if not hits:
                stale.append(f"{label}: {rel} 已无命中")
    assert not stale, "白名单出现僵尸豁免（请从 tests/test_p0b1b_static_guards.py 删除）：\n  " + \
        "\n  ".join(stale)


def test_untracked_debt_is_still_isolated(scan):
    """``UNTRACKED_DEBT`` 必须真的是**未跟踪**文件 —— 一旦被 git 跟踪，豁免即失效。

    用户 2026-09-12 的处置是「不删除、不纳入本次提交、作独立任务」。
    本测试保证这个隔离状态是可验证的，而不是一句口头承诺。
    """
    import subprocess

    for rel in UNTRACKED_DEBT:
        # 文件应存在（否则豁免无意义）
        assert (BASE / rel).exists(), f"{rel} 不存在：请从白名单移除"
        r = subprocess.run(["git", "ls-files", "--error-unmatch", rel],
                           cwd=str(BASE), capture_output=True, text=True)
        assert r.returncode != 0, (
            f"{rel} 已被 git 跟踪 —— 其豁免理由（未跟踪的独立任务）不再成立，"
            "请把它纳入守卫范围或从白名单删除")
