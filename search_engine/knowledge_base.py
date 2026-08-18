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
from pathlib import Path
from .models import KnowledgeRecord, Mechanism, SearchHypothesis

logger = logging.getLogger(__name__)


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
        """)

    def store(self, record: KnowledgeRecord):
        """落库一条知识记录。"""
        self.init_tables()
        import time
        self.conn.execute(
            "INSERT OR REPLACE INTO knowledge_records "
            "(paper_id, record_json, extractor_version, confidence, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (record.paper_id, self._serialize(record),
             record.extractor_version, record.confidence, time.time()),
        )
        self.conn.commit()

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
            "problem": record.problem,
            "strategy_routes": record.strategy_routes,
            "materials": record.materials,
            "physical_mechanisms": [
                {"cause": m.cause, "mechanism": m.mechanism, "effect": m.effect}
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
            "source_text": record.source_text,
            "extractor_version": record.extractor_version,
            "confidence": record.confidence,
        }, ensure_ascii=False)

    @staticmethod
    def _deserialize(data: str) -> KnowledgeRecord:
        d = json.loads(data)
        return KnowledgeRecord(
            paper_id=d.get("paper_id", ""),
            problem=d.get("problem", ""),
            strategy_routes=d.get("strategy_routes", []),
            materials=d.get("materials", []),
            physical_mechanisms=[
                Mechanism(cause=m.get("cause", ""), mechanism=m.get("mechanism", ""),
                          effect=m.get("effect", ""))
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
            source_text=d.get("source_text", ""),
            extractor_version=d.get("extractor_version", "1.1"),
            confidence=d.get("confidence", 0.0),
        )

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
