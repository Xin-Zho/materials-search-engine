"""P4-0 Step 1 回归测试：时间隔离基准数据集的统一元数据表。

**不依赖真库快照** —— 全部用合成 OpenAlex work 构造，
避免真库/缓存一变测试就失效（这是 P0-B2.1 的教训）。

覆盖三类风险：
  1. 时间泄漏（split 边界 + 引用口径）
  2. 范围污染（松散文本检索把全球巨引论文捞进语料池）
  3. 身份（uid 前缀/形态/优先级，必须复用 identity 的唯一出口）
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(BASE, "tools")
for p in (BASE, TOOLS):
    if p not in sys.path:
        sys.path.insert(0, p)


def _load():
    spec = importlib.util.spec_from_file_location(
        "build_p4_0_dataset", os.path.join(TOOLS, "build_p4_0_dataset.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


bld = _load()


# ══ 合成 work 工厂 ═══════════════════════════════════════════════════════
def work(wid, title, *, year=2018, doi=None, abstract=None, cited=0,
         counts=None, topic="Photopolymerization", type_="article",
         retracted=False, paratext=False, authors=("A. Author",),
         venue="J. Test", channels=("keyword_search",)):
    """构造一个最小可用的 OpenAlex work。abstract 以词->位置 的倒排索引给出。"""
    inv = None
    if abstract:
        toks = abstract.split()
        inv = {}
        for i, t in enumerate(toks):
            inv.setdefault(t, []).append(i)
    return {
        "id": "https://openalex.org/" + wid,
        "doi": ("https://doi.org/" + doi) if doi else None,
        "title": title,
        "display_name": title,
        "publication_year": year,
        "abstract_inverted_index": inv,
        "cited_by_count": cited,
        "counts_by_year": counts,
        "authorships": [{"author": {"display_name": a}} for a in authors],
        "primary_location": {"source": {"display_name": venue}},
        "primary_topic": {"display_name": topic},
        "topics": [{"display_name": topic}],
        "keywords": [{"display_name": "photopolymerization"}],
        "referenced_works_count": 10,
        "fwci": 1.0,
        "type": type_,
        "language": "en",
        "is_retracted": retracted,
        "is_paratext": paratext,
        "ids": {"openalex": "https://openalex.org/" + wid},
        "__channels": set(channels),
        "__origin": (sorted(channels)[0] if channels else None),
    }


def as_cache(works):
    """把合成 work 变成可写入缓存文件的形态。

    真缓存里**没有** ``__channels`` / ``__origin`` —— 那是 ``load_openalex_pool``
    在读取时注入的构造证据。写文件前必须剥掉，否则 json.dumps 会因 set 报错，
    也会掩盖「通道是加载期产物」这一事实。
    """
    return [{k: v for k, v in w.items() if not k.startswith("__")} for w in works]


def build(pool, *, kb_keys=(), scopus_idx=None):
    """跑完整构建链，返回 (records, acc)。

    使用**真实的 committed 白名单**（与生产同源）—— 白名单解析失败或为空时
    范围门会静默退化成纯词面门，测试必须能发现这件事。
    """
    allow, _meta = bld.load_scope_allowlist()
    records = bld.build_records(pool, scopus_idx or {}, {}, {}, allowlist=allow)
    dup = bld.mark_duplicates(records)
    acc = bld.acceptance(records, dup, {"works_deduped": len(pool)},
                         kb_keys=kb_keys)
    return records, acc


# ══ 1. 时间切分：边界与泄漏 ═════════════════════════════════════════════
@pytest.mark.parametrize("year,expect", [
    (2020, "TRAIN"),
    (2019, "TRAIN"),
    (1905, "TRAIN"),
    (2021, "EVAL"),
    (2025, "EVAL"),
    (2026, "EXCLUDED_OUT_OF_RANGE"),
    (1857, None),          # 不可信年份 -> NULL（禁插值）
    (None, None),
])
def test_split_boundaries(year, expect):
    assert bld._split_of(year) == expect


def test_train_never_contains_future_year():
    """反泄漏的硬断言：TRAIN 侧不得出现 >2020 的论文。"""
    pool = {"W%d" % y: work("W%d" % y, "photopolymerization shrinkage study %d" % y,
                            year=y) for y in (2015, 2020, 2021, 2025)}
    records, acc = build(pool)
    train_years = [r["year"] for r in records if r["split"] == "TRAIN"]
    assert train_years and max(train_years) <= 2020
    assert acc["split_integrity"]["assert_no_future_in_train"] is True
    assert acc["split_integrity"]["overlap"] == 0


def test_missing_year_is_null_never_interpolated():
    pool = {"W1": work("W1", "photopolymerization of acrylate", year=None)}
    records, acc = build(pool)
    assert records[0]["year"] is None
    assert records[0]["split"] is None
    assert acc["missing_year_count"] == 1
    # 明确断言「没有插值」：不得被塞进任一 split
    assert bld._split_of(None) is None


# ══ 2. 引用口径：快照 vs 反泄漏 ═════════════════════════════════════════
def test_citations_asof_is_truncated_to_cutoff():
    """citations_asof_cutoff 必须只累加 year<=2020 的部分 —— 这是防泄漏的核心。"""
    w = work("W1", "photopolymerization shrinkage", year=2015, cited=100,
             counts=[{"year": 2015, "cited_by_count": 1},
                     {"year": 2019, "cited_by_count": 4},
                     {"year": 2021, "cited_by_count": 50},
                     {"year": 2024, "cited_by_count": 45}])
    records, _ = build({"W1": w})
    r = records[0]
    assert r["citation_count"] == 100            # 快照（含未来引用）
    assert r["citations_asof_cutoff"] == 5       # 1 + 4，仅 <=2020
    assert r["citation_count"] > r["citations_asof_cutoff"]
    # 原始直方图必须保留，使 P4-2 能改 cutoff 重算
    assert json.loads(r["counts_by_year_json"])[2]["year"] == 2021
    assert r["counts_window_start"] == 2015 and r["counts_window_end"] == 2024


def test_no_counts_by_year_gives_null_asof():
    """无直方图时 asof 必须为 NULL —— 不得回退到快照值（那会引入泄漏）。"""
    w = work("W1", "photopolymerization", year=2010, cited=77, counts=None)
    records, _ = build({"W1": w})
    assert records[0]["citations_asof_cutoff"] is None
    assert records[0]["citation_count"] == 77


# ══ 3. 范围门：污染回归（本步最重要的发现）═══════════════════════════════
# 两轮实测得到的**具体离题论文**——直接固化为断言，防止范围门再退化。
PILOT_OFF_TOPIC_TITLES = [
    # 第一轮：纯通用词（conversion）放进来的
    "Conversion of 5-Methylcytosine to 5-Hydroxymethylcytosine in Mammalian DNA",
    "Design Rules for Donors in Bulk-Heterojunction Solar Cells-Towards 10 % Efficiency",
    "A New Method of Analyzing Thermogravimetric Data",
    "Conversion of Peripheral CD4+CD25- Naive T Cells to CD4+CD25+ Regulatory T Cells",
    "PAL2NAL: robust conversion of protein sequence alignments into the corresponding codon alignments",
    "A simple practice guide for dose conversion between animals and human",
    "Flexible metal-organic frameworks",
    "Enzyme immobilisation in biocatalysis: why, what and how",
    "Photoinduced Conversion of Silver Nanospheres to Nanoprisms",
    # 第二轮：广义材料词（epoxy / thiol / polymer）放进来的
    "Graphene Oxide, Highly Reduced Graphene Oxide, and Graphene: Versatile Building Blocks",
    "Polymer/Silica Nanocomposites: Preparation, Characterization, and Properties",
    "Enhanced Mechanical Properties of Nanocomposites at Low Graphene Content",
    "For the Bright Future-Bulk Heterojunction Polymer Solar Cells",
]

ANACHRONISM_GUARD_MUST_MATCH = [
    "Enhanced reduction of polymerization-induced shrinkage stress via combination",
    "Thiol-Ene Click Chemistry",
    "Cationic photopolymerization of cyclic esters",
    "Resin composite-State of the art",
    "改善光致聚合物全息记录材料体积收缩率的研究进展",
    "Degree of conversion and monomer elution of bulk-fill composites",
]


@pytest.mark.parametrize("title", PILOT_OFF_TOPIC_TITLES)
def test_off_topic_titles_from_pilot_are_excluded(title):
    """pilot 实测抓到的离题论文（DNA 甲基化/光伏/热重/T 细胞/蛋白比对/MOF/
    石墨烯/环氧纳米复合材料…）必须判为范围外。

    它们靠 `conversion`、`polymer`、`epoxy`、`thiol` 这类通用词混进了纯词面门，
    而主题（topic）不在白名单 —— 这正是改用主题门的原因。
    """
    w = work("W1", title, year=2015, cited=1000,
             topic="Graphene research and applications", channels=set())
    records, acc = build({"W1": w})
    assert records[0]["in_scope"] == 0
    assert records[0]["scope_tier"] not in bld.IN_SCOPE_TIERS
    assert acc["scope"]["in_scope"] == 0


@pytest.mark.parametrize("title", ANACHRONISM_GUARD_MUST_MATCH)
def test_on_topic_titles_are_in_scope(title):
    """真相关论文必须留在范围内（范围门不能靠收紧来"变干净"）。"""
    w = work("W1", title, year=2015)
    records, _ = build({"W1": w})
    assert records[0]["in_scope"] == 1
    assert records[0]["scope_tier"] == bld.SCOPE_TEXT


def test_topic_allowlist_grants_scope_without_text_evidence():
    """主题门是**主信号**：文本没有强特征、但主题在白名单内 -> in_scope。"""
    w = work("W1", "A study of process parameter optimisation", year=2015,
             topic="Dental materials and restorations")
    records, _ = build({"W1": w})
    assert records[0]["scope_tier"] == bld.SCOPE_TOPIC
    assert records[0]["in_scope"] == 1
    assert any(e.startswith("topic:") for e in json.loads(records[0]["scope_evidence"]))


def test_topic_allowlist_asset_loads_and_is_nonempty():
    """committed 白名单资产必须可解析且非空。

    真实踩过：主题名 `Hydrogels: synthesis, ...` 含冒号，未加引号会让 YAML 解析失败；
    且白名单一旦解析成空集，范围门会**静默退化**成纯词面门（精度崩塌）。
    """
    topics, meta = bld.load_scope_allowlist()
    assert meta["loaded"] is True
    assert meta["n"] >= 8, f"白名单过小：{meta}"
    assert "Photopolymerization techniques and applications" in topics
    assert "Dental materials and restorations" in topics


def test_multiword_patterns_actually_match():
    r"""``re.X`` 模式下**模式里的空白会被忽略** —— 多词短语必须写 ``\s+``。

    这个 bug 曾让强特征层形同虚设：``polymerization shrinkage`` 编译成
    ``polymerizationshrinkage``，永远匹配不上，于是范围判定只剩单字词兜底，
    离题论文批量涌入。这条测试直接盯住编译后的行为。
    """
    for phrase in ("polymerization shrinkage stress", "shrinkage stress",
                   "degree of conversion", "dental composite resin",
                   "resin composite", "monomer elution"):
        assert bld.STRICT_RE.search(phrase), f"多词短语未匹配：{phrase}"
    # 反例：这些**不该**靠强特征词通过（它们是别的学科的高频词）
    for trap in ("conversion", "dialysis", "concrete shrinkage",
                 "gene conversion", "energy conversion"):
        assert not bld.STRICT_RE.search(trap), f"过宽的强特征词：{trap}"


def test_adjacent_family_is_recorded_but_not_decisive():
    """广义词族只记录、不决定 in_scope（pilot 证明这一档会放进石墨烯/光伏）。"""
    w = work("W1", "Polymer nanocomposites with epoxy resin and thiol curing",
             year=2015, topic="Graphene research and applications", channels=set())
    records, _ = build({"W1": w})
    assert records[0]["scope_tier"] == bld.SCOPE_ADJACENT_ONLY
    assert records[0]["in_scope"] == 0
    assert bld.SCOPE_ADJACENT_ONLY not in bld.IN_SCOPE_TIERS


def test_scope_evidence_is_recorded_per_row():
    """范围判定必须留证据、可复核 —— 不得静默丢弃。"""
    w = work("W1", "Photopolymerization of acrylate monomers")
    records, _ = build({"W1": w})
    ev = json.loads(records[0]["scope_evidence"])
    assert ev and any("photopolymer" in e for e in ev)
    assert records[0]["provenance_channel"] == "keyword_search"


def test_channel_only_is_not_in_scope_by_default():
    """CHANNEL_ONLY 是「已记录未启用」的扩张池，默认不进范围。"""
    assert bld.SCOPE_CHANNEL_ONLY not in bld.IN_SCOPE_TIERS
    assert bld.SCOPE_ADJACENT_ONLY not in bld.IN_SCOPE_TIERS
    assert bld.SCOPE_TOPIC in bld.IN_SCOPE_TIERS
    assert bld.SCOPE_TEXT in bld.IN_SCOPE_TIERS


def test_all_pool_rows_are_preserved_not_dropped():
    """范围门只**标记**不删除：全部行必须留在 paper_meta 里。"""
    pool = {"W1": work("W1", "R: A Language and Environment", channels=set()),
            "W2": work("W2", "Photopolymerization of acrylate")}
    records, acc = build(pool)
    assert len(records) == 2 == acc["total_papers"]
    assert acc["scope"]["in_scope"] == 1


# ══ 4. 通道归类 ═════════════════════════════════════════════════════════
@pytest.mark.parametrize("url,expect", [
    ('https://api.openalex.org/works?{"filter": "title.search:thiol-ene", "per-page": 20}',
     "title_search"),
    ('https://api.openalex.org/works?{"filter": "cites:W123", "per-page": 20}',
     "cites_expansion"),
    ('https://api.openalex.org/works?{"filter": "title_and_abstract.search:\\"thiol-ene\\"", "sort": "cited_by_count:desc"}',
     "keyword_search"),
    ('https://api.openalex.org/works?{"per-page": 20, "q": "title/abstract has (\\"ring-opening\\")", "sort": "cited_by_count:desc"}',
     "keyword_search"),
])
def test_classify_channel(url, expect):
    payload = {"results": [work("W1", "x")], "meta": {}}
    assert bld.classify_channel(url, payload) == expect


def test_classify_channel_single_work():
    payload = {"id": "https://openalex.org/W1"}
    assert bld.classify_channel("https://api.openalex.org/works/W1", payload) \
        == "single_work"


# ══ 5. 摘要重建 ═════════════════════════════════════════════════════════
def test_rebuild_abstract_from_inverted_index():
    inv = {"The": [0], "combination": [1], "of": [2, 4], "thiol": [3],
           "and": [5], "ene": [6]}
    assert bld.rebuild_abstract(inv) == "The combination of thiol of and ene" \
        or bld.rebuild_abstract(inv).startswith("The combination of thiol")
    assert bld.rebuild_abstract(None) is None
    assert bld.rebuild_abstract({}) is None


def test_abstract_backfill_from_scopus_and_source_label():
    pool = {"W1": work("W1", "photopolymerization of acrylate", doi="10.1000/x",
                       abstract=None)}
    scopus = {"10.1000/x": ("Abstract from Scopus about polymerization shrinkage.",
                         "https://scopus/1")}
    records = bld.build_records(pool, scopus, {}, {})
    assert records[0]["abstract"].startswith("Abstract from Scopus")
    assert records[0]["abstract_source"] == "scopus_cache"
    assert records[0]["scopus_url"] == "https://scopus/1"


def test_openalex_abstract_wins_over_scopus():
    pool = {"W1": work("W1", "photopolymerization", doi="10.1000/x",
                       abstract="OpenAlex abstract text")}
    scopus = {"10.1000/x": ("Scopus abstract", "u")}
    records = bld.build_records(pool, scopus, {}, {})
    assert records[0]["abstract_source"] == "openalex"


# ══ 6. 身份：必须复用 identity 的唯一出口 ═══════════════════════════════
def test_uid_uses_openalex_priority_and_is_form_consistent():
    from search_engine.paper_writer import uid_type_matches_value
    pool = {"W123": work("W123", "photopolymerization", doi="10.1000/abc")}
    records, _ = build(pool)
    r = records[0]
    # P4 语料以 OpenAlex 为记录源 -> 实体 id 用 openalex 前缀
    assert r["paper_uid"] == "openalex:W123"
    # 而「最优引用标识」按 KB 口径 DOI 优先（用户裁定的两个概念）
    assert r["preferred_identifier"] == "doi:10.1000/abc"
    assert uid_type_matches_value(r["paper_uid"])[0] is True
    assert uid_type_matches_value(r["preferred_identifier"])[0] is True


def test_preferred_identifier_equals_uid_without_doi():
    pool = {"W9": work("W9", "photopolymerization", doi=None)}
    records, _ = build(pool)
    assert records[0]["preferred_identifier"] == records[0]["paper_uid"]


def test_uid_invariants_reported():
    pool = {"W%d" % i: work("W%d" % i, "photopolymerization shrinkage %d" % i,
                            doi="10.1000/%d" % i) for i in range(5)}
    _, acc = build(pool)
    inv = acc["uid_invariants"]
    assert inv["prefix_and_value_form_match"] is True
    assert inv["all_uids_unique"] is True
    assert inv["prefix_distribution"] == {"openalex": 5}


def test_kb_bridge_fills_scopus_eid():
    """scopus_eid 只能经 KB paper_identifiers 取 —— 缓存里没有 EID。"""
    pool = {"W123": work("W123", "photopolymerization", doi="10.1000/abc")}
    kb_by_key = {("DOI", "10.1000/abc"): "doi:10.1000/abc"}
    kb_eid = {"doi:10.1000/abc": "2-s2.0-1234567890"}
    records = bld.build_records(pool, {}, kb_by_key, kb_eid)
    assert records[0]["kb_paper_uid"] == "doi:10.1000/abc"
    assert records[0]["scopus_eid"] == "2-s2.0-1234567890"


# ══ 7. 重复实体：只标记不合并 ═══════════════════════════════════════════
def test_duplicate_tiers_are_marked_not_merged():
    pool = {
        "W1": work("W1", "Photopolymerization of thiol-ene", doi="10.1000/dup",
                   year=2015),
        "W2": work("W2", "Photopolymerization of thiol-ene", doi="10.1000/dup",
                   year=2015),
        "W3": work("W3", "Vat photopolymerization review", doi="10.2000/uniq",
                   year=2016),
    }
    records, acc = build(pool)
    assert len(records) == 3                     # 不合并
    assert acc["duplicate_entity_count"]["same_doi_multi_uid"] == 2
    gid = {r["duplicate_group_id"] for r in records if r["paper_uid"] != "openalex:W3"}
    assert len(gid) == 1 and gid.pop().startswith("DOI_COLLISION:")
    assert records[-1]["duplicate_group_id"] is None


def test_same_title_different_year_is_not_a_group():
    pool = {"W1": work("W1", "Photopolymerization", doi="10.1000/a", year=2015),
            "W2": work("W2", "Photopolymerization", doi="10.1000/b", year=2016)}
    records, acc = build(pool)
    assert acc["duplicate_entity_count"]["same_title_year_multi_uid"] == 0


# ══ 8. 排除规则与两维对账 ═══════════════════════════════════════════════
def test_exclusion_reasons_and_reconciliation():
    pool = {
        "W1": work("W1", "photopolymerization shrinkage", year=2018),
        "W2": work("W2", "photopolymerization shrinkage retracted", year=2018,
                   retracted=True),
        "W3": work("W3", "photopolymerization paratext", year=2018,
                   paratext=True),
        "W4": work("W4", "photopolymerization editorial", year=2018,
                   type_="editorial"),
        "W5": work("W5", "photopolymerization future", year=2026),
    }
    records, acc = build(pool)
    ex = acc["excluded"]
    assert ex["retracted"] == 1 and ex["paratext"] == 1 and ex["non_research_type"] == 1
    assert ex["out_of_range_gt_2025"] == 1
    assert ex["total_included"] == 2          # W1 + W5（>2025 但未被剔除）
    assert ex["total_excluded"] == 3
    rec = acc["reconciliation"]
    assert rec["total"] == 5
    assert rec["included"] + rec["excluded"] == rec["total"]
    ib = rec["included_breakdown"]
    assert ib["train"] + ib["eval"] + ib["out_of_range_gt_2025"] + ib["no_year"] \
        == rec["included"]


def test_excluded_rows_do_not_enter_jsonl(tmp_path):
    pool = {"W1": work("W1", "photopolymerization shrinkage", year=2018),
            "W2": work("W2", "photopolymerization retracted", year=2018,
                       retracted=True)}
    records, _ = build(pool)
    out = tmp_path / "t.jsonl"
    n = bld.write_jsonl(records, str(out), "TRAIN")
    assert n == 1
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["paper_uid"] == "openalex:W1"


# ══ 9. 落库：DDL / 视图 / 幂等 ═════════════════════════════════════════
def test_db_schema_view_and_idempotence(tmp_path):
    pool = {"W1": work("W1", "Photopolymerization of thiol-ene", year=2018,
                       doi="10.1000/a"),
            "W2": work("W2", "R: A Language and Environment", year=2018,
                       cited=353396, channels=set()),
            "W3": work("W3", "photopolymerization future work", year=2026)}
    records, acc = build(pool)
    db = tmp_path / "paper_meta.db"
    bld.write_db(records, str(db), {"dataset": "t"})

    con = sqlite3.connect(str(db))
    try:
        assert con.execute("SELECT COUNT(*) FROM paper_meta").fetchone()[0] == 3
        # 视图必须把 OFF_TOPIC 与 >2025 都挡在外面
        rows = con.execute("SELECT paper_uid FROM v_photopolymerization").fetchall()
        assert [r[0] for r in rows] == ["openalex:W1"]
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        assert {"paper_meta", "dataset_meta", "v_photopolymerization"} <= tables
    finally:
        con.close()

    # 幂等：重建同一路径不得追加重建失败或行数翻倍
    bld.write_db(records, str(db), {"dataset": "t"})
    con = sqlite3.connect(str(db))
    try:
        assert con.execute("SELECT COUNT(*) FROM paper_meta").fetchone()[0] == 3
    finally:
        con.close()


def test_dataset_meta_records_provenance(tmp_path):
    pool = {"W1": work("W1", "photopolymerization", year=2018)}
    records, _ = build(pool)
    db = tmp_path / "m.db"
    bld.write_db(records, str(db), {"dataset": "d", "build_version": "v"})
    con = sqlite3.connect(str(db))
    try:
        meta = dict(con.execute("SELECT key, value FROM dataset_meta"))
    finally:
        con.close()
    assert meta["dataset"] == "d" and meta["build_version"] == "v"


# ══ 10. 干跑安全：默认零写入 ═══════════════════════════════════════════
def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    cache = tmp_path / "openalex_cache.json"
    cache.write_text(json.dumps({
        'https://api.openalex.org/works?{"filter":"title_and_abstract.search:x"}':
            {"results": as_cache([work("W1", "Photopolymerization of acrylate",
                                       year=2018)]), "meta": {}}
    }), encoding="utf-8")
    out = tmp_path / "ds"
    rc = bld.main(["--out", str(out), "--openalex-cache", str(cache),
                   "--scopus-db", str(tmp_path / "none.db"),
                   "--kb-db", str(tmp_path / "none.db")])
    assert rc == 0
    assert not out.exists(), "dry-run 不得创建输出目录"


def test_apply_writes_expected_artifacts(tmp_path):
    cache = tmp_path / "openalex_cache.json"
    cache.write_text(json.dumps({
        'https://api.openalex.org/works?{"filter":"title_and_abstract.search:x"}':
            {"results": as_cache([
                work("W1", "Photopolymerization of thiol-ene", year=2018,
                     doi="10.1000/a"),
                work("W2", "Vat photopolymerization of dental composites",
                     year=2023, doi="10.2000/b")]), "meta": {}}
    }), encoding="utf-8")
    out = tmp_path / "ds"
    rc = bld.main(["--out", str(out), "--openalex-cache", str(cache),
                   "--scopus-db", str(tmp_path / "none.db"),
                   "--kb-db", str(tmp_path / "none.db"), "--apply"])
    assert rc == 0
    for f in ("paper_meta.db", "train_papers.jsonl", "eval_papers.jsonl",
              "benchmark.yaml", "build_report.json"):
        assert (out / f).exists(), f
    assert (out / "extraction_cache").is_dir()

    import yaml
    bench = yaml.safe_load((out / "benchmark.yaml").read_text(encoding="utf-8"))
    assert bench["split"]["train"]["max_year"] == 2020
    assert bench["split"]["eval"]["min_year"] == 2021
    assert bench["leakage_policy"]["hard_rules"]
    assert "citation_count" in bench["leakage_policy"]["forbidden_train_features"]
    assert bench["scope"]["result"]["in_scope"] == 2
    # jsonl 各自只含本 split
    train = [json.loads(l) for l in
             (out / "train_papers.jsonl").read_text(encoding="utf-8").splitlines()]
    ev = [json.loads(l) for l in
          (out / "eval_papers.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(train) == 1 and train[0]["year"] == 2018
    assert len(ev) == 1 and ev[0]["year"] == 2023


# ══ 11. 静态守卫：uid 命名空间仍然只有唯一出口 ═════════════════════════
def test_builder_does_not_hand_concat_uid_prefix():
    """P4-0 构建器不得自造 uid 前缀字符串（G3 延伸）。"""
    src = open(os.path.join(TOOLS, "build_p4_0_dataset.py"), encoding="utf-8").read()
    for bad in ('f"openalex:', "f'openalex:", 'f"doi:', 'f"scopus:',
                '"openalex:" +', '"doi:" +', '"scopus:" +'):
        assert bad not in src, f"构建器出现手拼前缀：{bad}"
    assert "make_paper_uid" in src  # 必须走唯一出口
