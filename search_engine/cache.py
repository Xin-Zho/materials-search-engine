"""SQLite 缓存 — 避免重复 API 调用 + 日志记录用于轨迹回放。"""

import json
import hashlib
import sqlite3
import time
from pathlib import Path

from .models import Paper, SearchResult


class SearchCache:
    """SQLite 缓存：论文去重 + 搜索日志 + API 缓存。"""

    def __init__(self, db_path: str | Path = "data/cache/scopus_cache.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    def init_tables(self):
        """创建表（幂等）。"""
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS api_cache (
                query_hash TEXT PRIMARY KEY,
                query_string TEXT NOT NULL,
                result_json TEXT NOT NULL,
                total_count INTEGER,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS papers (
                paper_id TEXT PRIMARY KEY,
                normalized_json TEXT NOT NULL,
                retrieved_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS search_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                step INTEGER NOT NULL,
                action_type TEXT NOT NULL,
                query_string TEXT,
                result_count INTEGER DEFAULT 0,
                result_ids TEXT,
                cost_time REAL DEFAULT 0.0,
                timestamp REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_search_log_session
                ON search_log(session_id, step);
            CREATE INDEX IF NOT EXISTS idx_papers_retrieved
                ON papers(retrieved_at);
        """)

    # ── API 缓存 ─────────────────────────────────────

    def get_cached_result(self, query: str) -> SearchResult | None:
        """如果查询已有缓存，返回结果。"""
        query_hash = self._hash(query)
        row = self.conn.execute(
            "SELECT result_json FROM api_cache WHERE query_hash = ?",
            (query_hash,),
        ).fetchone()
        if row:
            return self._deserialize_result(row[0])
        return None

    def set_cached_result(self, query: str, result: SearchResult):
        """缓存搜索结果。"""
        query_hash = self._hash(query)
        self.conn.execute(
            """INSERT OR REPLACE INTO api_cache
               (query_hash, query_string, result_json, total_count, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                query_hash,
                query,
                self._serialize_result(result),
                result.total_count,
                time.time(),
            ),
        )
        self.conn.commit()

    # ── 论文存储 ─────────────────────────────────────

    def store_papers(self, papers: list[Paper]):
        """批量存储论文（去重）。"""
        now = time.time()
        for paper in papers:
            self.conn.execute(
                """INSERT OR IGNORE INTO papers
                   (paper_id, normalized_json, retrieved_at)
                   VALUES (?, ?, ?)""",
                (paper.paper_id, json.dumps(self._paper_to_dict(paper), ensure_ascii=False), now),
            )
        self.conn.commit()

    def get_paper(self, paper_id: str) -> Paper | None:
        """按 ID 取论文。"""
        row = self.conn.execute(
            "SELECT normalized_json FROM papers WHERE paper_id = ?",
            (paper_id,),
        ).fetchone()
        if row:
            return self._paper_from_dict(json.loads(row[0]))
        return None

    def get_all_papers(self) -> list[Paper]:
        """获取所有缓存的论文。"""
        rows = self.conn.execute("SELECT normalized_json FROM papers").fetchall()
        return [self._paper_from_dict(json.loads(r[0])) for r in rows]

    # ── 搜索日志 ─────────────────────────────────────

    def log_search(
        self,
        session_id: str,
        step: int,
        action_type: str,
        query_string: str,
        result_ids: list[str],
        cost_time: float,
    ):
        """记录一次搜索动作。"""
        self.conn.execute(
            """INSERT INTO search_log
               (session_id, step, action_type, query_string,
                result_count, result_ids, cost_time, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                step,
                action_type,
                query_string,
                len(result_ids),
                json.dumps(result_ids),
                cost_time,
                time.time(),
            ),
        )
        self.conn.commit()

    def get_session_log(self, session_id: str) -> list[dict]:
        """读取某次会话的完整日志（用于轨迹回放）。"""
        rows = self.conn.execute(
            "SELECT * FROM search_log WHERE session_id = ? ORDER BY step",
            (session_id,),
        ).fetchall()
        return [
            {
                "session_id": r[1],
                "step": r[2],
                "action_type": r[3],
                "query_string": r[4],
                "result_count": r[5],
                "result_ids": json.loads(r[6]) if r[6] else [],
                "cost_time": r[7],
                "timestamp": r[8],
            }
            for r in rows
        ]

    # ── 辅助 ─────────────────────────────────────────

    @staticmethod
    def _hash(query: str) -> str:
        return hashlib.sha256(query.strip().lower().encode()).hexdigest()[:16]

    @staticmethod
    def _serialize_result(result: SearchResult) -> str:
        return json.dumps(
            {
                "query": result.query,
                "papers": [SearchCache._paper_to_dict(p) for p in result.papers],
                "total_count": result.total_count,
                "pages_fetched": result.pages_fetched,
                "time_taken": result.time_taken,
                "source": result.source,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _deserialize_result(data: str) -> SearchResult:
        d = json.loads(data)
        return SearchResult(
            query=d["query"],
            papers=[SearchCache._paper_from_dict(p) for p in d["papers"]],
            total_count=d["total_count"],
            pages_fetched=d.get("pages_fetched", 0),
            time_taken=d.get("time_taken", 0.0),
            source=d.get("source", "scopus"),
        )

    @staticmethod
    def _paper_to_dict(paper: Paper) -> dict:
        return {
            "paper_id": paper.paper_id,
            "title": paper.title,
            "authors": [{"surname": a.surname, "given_name": a.given_name, "scopus_id": a.scopus_id}
                        for a in paper.authors],
            "year": paper.year,
            "abstract": paper.abstract,
            "doi": paper.doi,
            "scopus_url": paper.scopus_url,
            "venue": paper.venue,
            "volume": paper.volume,
            "pages": paper.pages,
            "citation_count": paper.citation_count,
            "document_type": paper.document_type,
            "source": paper.source,
        }

    @staticmethod
    def _paper_from_dict(d: dict) -> Paper:
        return Paper(
            paper_id=d.get("paper_id", ""),
            title=d.get("title", ""),
            authors=[Author(surname=a.get("surname", ""),
                           given_name=a.get("given_name", ""),
                           scopus_id=a.get("scopus_id"))
                     for a in d.get("authors", [])],
            year=d.get("year"),
            abstract=d.get("abstract"),
            doi=d.get("doi"),
            scopus_url=d.get("scopus_url"),
            venue=d.get("venue"),
            volume=d.get("volume"),
            pages=d.get("pages"),
            citation_count=d.get("citation_count"),
            document_type=d.get("document_type"),
            source=d.get("source", "scopus"),
        )

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
