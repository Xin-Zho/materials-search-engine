"""KnowledgeBase — 存储从论文提取的知识，并用知识生成 historical queries。

Phase 1 核心之一：把 KnowledgeRecord 落库（保留来源追溯），
并从中自动生成历史文献检索 query，替换硬编码的 ROUTE_QUERIES。

使用方式:
    kb = KnowledgeBase()
    kb.store(record)
    historical_queries = kb.generate_historical_queries()
"""

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from .models import KnowledgeRecord, Mechanism, SearchHypothesis, RouteMechanismEvidenceEdge

logger = logging.getLogger(__name__)


@dataclass
class HistoricalQuery:
    """一条 knowledge-derived historical query（带来源追溯）。"""
    query: str
    source_paper_id: str
    source_term: str
    source_type: str          # historical_term / route / material / synonym
    target_property: str = "" # anchor（如 polymerization shrinkage）


class HistoricalQueryBuilder:
    """从 KnowledgeRecord 生成 historical queries（term + anchor 组合，带语义去重）。

    流程：raw learned terms → canonicalize → semantic dedup → query generation。
    每条 query 保留来源，避免几百条几乎一样的 query。
    """

    # 常见冗余后缀（canonicalize 时剥离）
    _STRIP_SUFFIXES = [" polymerization", " chemistry", " addition", " reaction"]

    def __init__(self, anchor: str = "polymerization shrinkage"):
        self.anchor = anchor

    def build(self, records: list[KnowledgeRecord]) -> list[HistoricalQuery]:
        """从知识记录生成 historical queries。"""
        # 1. 收集 raw terms（带来源）
        raw: list[tuple[str, str, str]] = []  # (term, paper_id, source_type)
        for r in records:
            for t in r.historical_terms:
                raw.append((t, r.paper_id, "historical_term"))
            for t in r.strategy_routes:
                raw.append((t, r.paper_id, "route"))
            for t in r.synonyms:
                raw.append((t, r.paper_id, "synonym"))
            for t in r.materials:
                raw.append((t, r.paper_id, "material"))

        # 2. canonicalize + semantic dedup
        canonical: dict[str, tuple[str, str, str]] = {}
        for term, pid, stype in raw:
            key = self._canonicalize(term)
            if key and key not in canonical:
                canonical[key] = (term, pid, stype)

        # 3. 组合 query（term + anchor）
        queries = []
        for key, (term, pid, stype) in canonical.items():
            queries.append(HistoricalQuery(
                query=f'"{term}" AND "{self.anchor}"',
                source_paper_id=pid,
                source_term=term,
                source_type=stype,
                target_property=self.anchor,
            ))

        logger.info("historical query builder: %d raw → %d canonical → %d queries",
                     len(raw), len(canonical), len(queries))
        return queries

    def build_by_channel(self, records: list[KnowledgeRecord]) -> dict[str, list[HistoricalQuery]]:
        """按 source_type 分流到不同 query channel。

        historical_term → Historical Recall 主力
        route           → Route Expansion
        synonym         → Lexical Expansion
        material        → Material-conditioned Search
        """
        queries = self.build(records)
        channels: dict[str, list[HistoricalQuery]] = {
            "historical_term": [],
            "route": [],
            "synonym": [],
            "material": [],
        }
        for q in queries:
            if q.source_type in channels:
                channels[q.source_type].append(q)
        return channels

    @staticmethod
    def _canonicalize(term: str) -> str:
        """归一化：小写、去连字符/空格（统一连写/分开写）、去复数、去冗余后缀。"""
        t = term.lower().strip().strip('"').strip("'")
        t = t.replace("-", "").replace(" ", "")  # 连写 vs 分开写统一（spiroortho vs spiro ortho）
        # 去复数（末尾 s，且不是 ss）
        if t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        for suffix in HistoricalQueryBuilder._STRIP_SUFFIXES:
            suffix_compact = suffix.replace(" ", "")
            if t.endswith(suffix_compact):
                t = t[:-len(suffix_compact)]
        return t


class KnowledgeBase:
    """知识库：KnowledgeRecord 落库 + knowledge-derived historical query 生成。"""

    def __init__(self, db_path: str | Path = "data/cache/knowledge_base.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
        return self._conn

    def init_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS knowledge_records (
                paper_id TEXT PRIMARY KEY,
                record_json TEXT NOT NULL,
                extractor_version TEXT,
                confidence REAL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_knowledge_version ON knowledge_records(extractor_version);

            -- Phase 1.8: route—mechanism 证据边（coverage 的唯一证据来源）
            -- 与 knowledge_records.record_json 里的 route_mechanism_edges 保持同步，
            -- 此表供 SQL 审计/查询；运行时权威来源是 record 对象。
            CREATE TABLE IF NOT EXISTS route_mechanism_edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_id TEXT NOT NULL,
                raw_route TEXT,
                canonical_route TEXT,
                raw_mechanism TEXT,
                canonical_mechanism TEXT,
                evidence TEXT,
                confidence REAL,
                relation_type TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_edges_paper ON route_mechanism_edges(paper_id);
            CREATE INDEX IF NOT EXISTS idx_edges_route_mech ON route_mechanism_edges(canonical_route, canonical_mechanism);
        """)

    def store(self, record: KnowledgeRecord):
        """落库一条知识记录（含 route_mechanism_edges 同步到 edges 表）。"""
        self.init_tables()
        import time
        self.conn.execute(
            "INSERT OR REPLACE INTO knowledge_records "
            "(paper_id, record_json, extractor_version, confidence, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (record.paper_id, self._serialize(record),
             record.extractor_version, record.confidence, time.time()),
        )
        # edges 表同步（DELETE + INSERT，保证与 record 一致）
        self.conn.execute("DELETE FROM route_mechanism_edges WHERE paper_id = ?", (record.paper_id,))
        self.conn.executemany(
            "INSERT INTO route_mechanism_edges "
            "(paper_id, raw_route, canonical_route, raw_mechanism, canonical_mechanism, "
            " evidence, confidence, relation_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(e.paper_id or record.paper_id, e.raw_route, e.canonical_route,
              e.raw_mechanism, e.canonical_mechanism, e.evidence,
              e.confidence, e.relation_type)
             for e in record.route_mechanism_edges],
        )
        self.conn.commit()

    def delete(self, paper_id: str):
        """删除一条记录（knowledge_records + edges 表），供 identity merge 使用。"""
        self.init_tables()
        self.conn.execute("DELETE FROM route_mechanism_edges WHERE paper_id = ?", (paper_id,))
        self.conn.execute("DELETE FROM knowledge_records WHERE paper_id = ?", (paper_id,))
        self.conn.commit()

    def get_edges(self, canonical_route: str | None = None,
                  canonical_mechanism: str | None = None) -> list[RouteMechanismEvidenceEdge]:
        """读取证据边（可选项过滤），供 coverage/审计使用。"""
        self.init_tables()
        sql = ("SELECT paper_id, raw_route, canonical_route, raw_mechanism, "
               "canonical_mechanism, evidence, confidence, relation_type "
               "FROM route_mechanism_edges")
        conds, args = [], []
        if canonical_route:
            conds.append("canonical_route = ?")
            args.append(canonical_route)
        if canonical_mechanism:
            conds.append("canonical_mechanism = ?")
            args.append(canonical_mechanism)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        rows = self.conn.execute(sql, args).fetchall()
        return [
            RouteMechanismEvidenceEdge(
                paper_id=r[0], raw_route=r[1] or "", canonical_route=r[2] or "",
                raw_mechanism=r[3] or "", canonical_mechanism=r[4] or "",
                evidence=r[5] or "", confidence=float(r[6] or 0.0),
                relation_type=r[7] or "direct",
            )
            for r in rows
        ]

    def store_many(self, records: list[KnowledgeRecord]):
        for r in records:
            self.store(r)
        logger.info("知识库落库: %d 条记录", len(records))

    def get_all(self) -> list[KnowledgeRecord]:
        """读取所有知识记录。"""
        self.init_tables()
        rows = self.conn.execute("SELECT record_json FROM knowledge_records").fetchall()
        return [self._deserialize(r[0]) for r in rows]

    def get(self, paper_id: str) -> KnowledgeRecord | None:
        self.init_tables()
        row = self.conn.execute(
            "SELECT record_json FROM knowledge_records WHERE paper_id = ?", (paper_id,)
        ).fetchone()
        return self._deserialize(row[0]) if row else None

    # ── 术语收集 ──────────────────────────────────────

    def collect_terms(self, field: str) -> list[str]:
        """收集所有记录中某字段的术语（去重）。"""
        seen = set()
        result = []
        for r in self.get_all():
            if field == "strategy_routes":
                terms = r.strategy_routes
            elif field == "materials":
                terms = r.materials
            elif field == "concepts":
                terms = r.concepts
            elif field == "synonyms":
                terms = r.synonyms
            elif field == "broader_terms":
                terms = r.broader_terms
            elif field == "historical_terms":
                terms = r.historical_terms
            else:
                terms = []
            for t in terms:
                if t and t.lower() not in seen:
                    seen.add(t.lower())
                    result.append(t)
        return result

    def generate_historical_queries(self, max_queries: int = 50) -> list[str]:
        """用提取的知识自动生成 historical queries（替换硬编码 ROUTE_QUERIES）。

        来源：historical_terms（旧称/别名）+ strategy_routes（技术路线）+ synonyms。
        每条 query 就是一个可独立检索的历史术语/路线。
        """
        terms = []
        terms += self.collect_terms("historical_terms")   # 优先旧称
        terms += self.collect_terms("strategy_routes")     # 技术路线
        terms += self.collect_terms("synonyms")            # 同义词变体

        # 去重（保持顺序）
        seen = set()
        queries = []
        for t in terms:
            if t and t.lower() not in seen:
                seen.add(t.lower())
                queries.append(t)

        logger.info("knowledge-derived historical queries: %d 条", len(queries))
        return queries[:max_queries]

    # ── 序列化 ────────────────────────────────────────

    @staticmethod
    def _serialize(record: KnowledgeRecord) -> str:
        return json.dumps({
            "paper_id": record.paper_id,
            "canonical_paper_id": record.canonical_paper_id,
            "doi": record.doi,
            "openalex_id": record.openalex_id,
            "problem": record.problem,
            "strategy_routes": record.strategy_routes,
            "materials": record.materials,
            "physical_mechanisms": [
                {"cause": m.cause, "mechanism": m.mechanism, "effect": m.effect,
                 "canonical": m.canonical, "evidence": m.evidence, "confidence": m.confidence}
                for m in record.physical_mechanisms
            ],
            "characterization_methods": record.characterization_methods,
            "concepts": record.concepts,
            "synonyms": record.synonyms,
            "broader_terms": record.broader_terms,
            "historical_terms": record.historical_terms,
            "search_hypotheses": [
                {"hypothesis": h.hypothesis, "rationale": h.rationale,
                 "support_type": h.support_type, "evidence": h.evidence,
                 "queries": h.queries}
                for h in record.search_hypotheses
            ],
            "route_mechanism_edges": [
                {"paper_id": e.paper_id, "raw_route": e.raw_route,
                 "canonical_route": e.canonical_route, "raw_mechanism": e.raw_mechanism,
                 "canonical_mechanism": e.canonical_mechanism, "evidence": e.evidence,
                 "confidence": e.confidence, "relation_type": e.relation_type,
                 "provenance": e.provenance}
                for e in record.route_mechanism_edges
            ],
            "source_text": record.source_text,
            "extractor_version": record.extractor_version,
            "confidence": record.confidence,
            "extraction_status": record.extraction_status,
        }, ensure_ascii=False)

    @staticmethod
    def _deserialize(data: str) -> KnowledgeRecord:
        d = json.loads(data)
        return KnowledgeRecord(
            paper_id=d.get("paper_id", ""),
            canonical_paper_id=d.get("canonical_paper_id", ""),
            doi=d.get("doi", ""),
            openalex_id=d.get("openalex_id", ""),
            problem=d.get("problem", ""),
            strategy_routes=d.get("strategy_routes", []),
            materials=d.get("materials", []),
            physical_mechanisms=[
                Mechanism(cause=m.get("cause", ""), mechanism=m.get("mechanism", ""),
                          effect=m.get("effect", ""), canonical=m.get("canonical", ""),
                          evidence=m.get("evidence", ""),
                          confidence=float(m.get("confidence", 0.0) or 0.0))
                for m in d.get("physical_mechanisms", [])
            ],
            characterization_methods=d.get("characterization_methods", []),
            concepts=d.get("concepts", []),
            synonyms=d.get("synonyms", []),
            broader_terms=d.get("broader_terms", []),
            historical_terms=d.get("historical_terms", []),
            search_hypotheses=[
                SearchHypothesis(
                    hypothesis=h.get("hypothesis", ""),
                    rationale=h.get("rationale", ""),
                    support_type=h.get("support_type", ""),
                    evidence=h.get("evidence", ""),
                    queries=h.get("queries", []),
                )
                for h in d.get("search_hypotheses", [])
            ],
            route_mechanism_edges=[
                RouteMechanismEvidenceEdge(
                    paper_id=e.get("paper_id", "") or d.get("paper_id", ""),
                    raw_route=e.get("raw_route", ""),
                    canonical_route=e.get("canonical_route", ""),
                    raw_mechanism=e.get("raw_mechanism", ""),
                    canonical_mechanism=e.get("canonical_mechanism", ""),
                    evidence=e.get("evidence", ""),
                    confidence=float(e.get("confidence", 0.0) or 0.0),
                    relation_type=e.get("relation_type", "direct"),
                    provenance=e.get("provenance", ""),
                )
                for e in d.get("route_mechanism_edges", [])
                if isinstance(e, dict)
            ],
            source_text=d.get("source_text", ""),
            extractor_version=d.get("extractor_version", "1.1"),
            confidence=d.get("confidence", 0.0),
            extraction_status=d.get("extraction_status", ""),
        )

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
