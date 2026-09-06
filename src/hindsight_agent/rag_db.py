"""SQLite audit tables for vector synchronization, matches, and human decisions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .rag_documents import RagDocument


RAG_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS osha_news_documents (
    entry_id TEXT PRIMARY KEY REFERENCES osha_news_releases(entry_id) ON DELETE CASCADE,
    source_url TEXT NOT NULL,
    body_text TEXT NOT NULL,
    body_hash TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    parse_method TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rag_runs (
    run_id TEXT PRIMARY KEY,
    trigger_type TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    collection_result_json TEXT,
    import_result_json TEXT,
    sync_result_json TEXT,
    match_result_json TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS rag_sync_state (
    document_id TEXT PRIMARY KEY,
    document_type TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    vector_collection TEXT NOT NULL,
    vector_updated_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS incident_factory_matches (
    match_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES rag_runs(run_id),
    incident_document_id TEXT NOT NULL,
    incident_source_type TEXT NOT NULL,
    incident_record_id TEXT NOT NULL,
    incident_title TEXT NOT NULL,
    incident_date TEXT,
    incident_url TEXT,
    factory_risk_id TEXT NOT NULL,
    factory_id TEXT NOT NULL,
    factory_name TEXT NOT NULL,
    area_id TEXT NOT NULL,
    area_name TEXT NOT NULL,
    equipment_id TEXT NOT NULL,
    asset_code TEXT NOT NULL,
    equipment_name TEXT NOT NULL,
    assessment_code TEXT NOT NULL,
    hazard_id TEXT NOT NULL,
    hazard_name TEXT NOT NULL,
    semantic_score REAL NOT NULL,
    hazard_score REAL NOT NULL,
    industry_score REAL NOT NULL,
    consequence_score REAL NOT NULL,
    relevance_score REAL NOT NULL,
    score_version TEXT NOT NULL,
    rationale_json TEXT NOT NULL,
    decision_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (decision_status IN ('pending', 'confirmed', 'dismissed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (incident_document_id, factory_risk_id)
);

CREATE TABLE IF NOT EXISTS human_decisions (
    decision_id TEXT PRIMARY KEY,
    match_id TEXT NOT NULL REFERENCES incident_factory_matches(match_id) ON DELETE CASCADE,
    decision TEXT NOT NULL CHECK (decision IN ('confirmed', 'dismissed', 'pending')),
    decided_by TEXT NOT NULL,
    note TEXT,
    decided_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_matches_relevance
    ON incident_factory_matches(relevance_score DESC);
CREATE INDEX IF NOT EXISTS idx_matches_decision
    ON incident_factory_matches(decision_status, relevance_score DESC);
CREATE INDEX IF NOT EXISTS idx_matches_incident
    ON incident_factory_matches(incident_document_id);
CREATE INDEX IF NOT EXISTS idx_runs_started
    ON rag_runs(started_at DESC);

DROP VIEW IF EXISTS "每日运行";
CREATE VIEW "每日运行" AS
SELECT run_id AS "运行ID", trigger_type AS "触发方式", started_at AS "开始时间",
       finished_at AS "结束时间",
       CASE status WHEN 'running' THEN '运行中' WHEN 'completed' THEN '已完成'
            ELSE '失败' END AS "状态",
       collection_result_json AS "采集结果", import_result_json AS "导入结果",
       sync_result_json AS "向量同步结果", match_result_json AS "匹配结果",
       error AS "错误"
FROM rag_runs;

DROP VIEW IF EXISTS "事故工厂匹配";
CREATE VIEW "事故工厂匹配" AS
SELECT match_id AS "匹配ID", incident_source_type AS "事故来源",
       incident_title AS "事故标题", incident_date AS "事故日期",
       incident_url AS "事故链接", factory_name AS "工厂", area_name AS "区域",
       asset_code AS "设备编号", equipment_name AS "设备",
       hazard_name AS "对应危险", assessment_code AS "风险评估编号",
       ROUND(relevance_score * 100, 1) AS "相关度分数",
       ROUND(semantic_score * 100, 1) AS "语义分数",
       CASE decision_status WHEN 'pending' THEN '待决定'
            WHEN 'confirmed' THEN '已确认相关' ELSE '已忽略' END AS "人工状态",
       score_version AS "评分版本", created_at AS "首次匹配时间",
       updated_at AS "更新时间"
FROM incident_factory_matches;

DROP VIEW IF EXISTS "待人工决定";
CREATE VIEW "待人工决定" AS
SELECT * FROM "事故工厂匹配"
WHERE "人工状态" = '待决定'
ORDER BY "相关度分数" DESC;
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def connect(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.executescript(RAG_SCHEMA_SQL)
    connection.execute(
        "INSERT OR REPLACE INTO database_metadata(key, value) VALUES (?, ?)",
        ("rag_schema_version", "1.0"),
    )
    connection.commit()
    return connection


def ensure_rag_schema(database_path: Path) -> None:
    connection = connect(database_path)
    connection.close()


def start_run(database_path: Path, trigger_type: str) -> str:
    run_id = str(uuid.uuid4())
    connection = connect(database_path)
    try:
        with connection:
            connection.execute(
                "INSERT INTO rag_runs(run_id, trigger_type, started_at, status) "
                "VALUES (?, ?, ?, 'running')",
                (run_id, trigger_type, utc_now()),
            )
    finally:
        connection.close()
    return run_id


def finish_run(
    database_path: Path,
    run_id: str,
    status: str,
    *,
    collection_result: Any = None,
    import_result: Any = None,
    sync_result: Any = None,
    match_result: Any = None,
    error: str | None = None,
) -> None:
    def encoded(value: Any) -> str | None:
        return None if value is None else json.dumps(value, ensure_ascii=False)

    connection = connect(database_path)
    try:
        with connection:
            connection.execute(
                """
                UPDATE rag_runs
                SET finished_at=?, status=?, collection_result_json=?,
                    import_result_json=?, sync_result_json=?, match_result_json=?, error=?
                WHERE run_id=?
                """,
                (
                    utc_now(), status, encoded(collection_result), encoded(import_result),
                    encoded(sync_result), encoded(match_result), error, run_id,
                ),
            )
    finally:
        connection.close()


def _chunks(items: list[str], size: int = 800) -> Iterable[list[str]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def documents_needing_sync(
    database_path: Path,
    documents: list[RagDocument],
    embedding_model: str,
    vector_collection: str,
) -> list[RagDocument]:
    if not documents:
        return []
    connection = connect(database_path)
    try:
        existing: dict[str, sqlite3.Row] = {}
        ids = [document.id for document in documents]
        for part in _chunks(ids):
            placeholders = ",".join("?" for _ in part)
            rows = connection.execute(
                "SELECT document_id, content_hash, embedding_model, vector_collection "
                f"FROM rag_sync_state WHERE document_id IN ({placeholders})",
                part,
            ).fetchall()
            existing.update({row["document_id"]: row for row in rows})
    finally:
        connection.close()
    return [
        document
        for document in documents
        if document.id not in existing
        or existing[document.id]["content_hash"] != document.content_hash
        or existing[document.id]["embedding_model"] != embedding_model
        or existing[document.id]["vector_collection"] != vector_collection
    ]


def mark_documents_synced(
    database_path: Path,
    documents: list[RagDocument],
    embedding_model: str,
    vector_collection: str,
) -> None:
    if not documents:
        return
    timestamp = utc_now()
    rows = [
        (
            document.id,
            document.document_type,
            document.source_record_id,
            document.content_hash,
            embedding_model,
            vector_collection,
            timestamp,
            json.dumps(document.metadata, ensure_ascii=False, sort_keys=True),
        )
        for document in documents
    ]
    connection = connect(database_path)
    try:
        with connection:
            connection.executemany(
                """
                INSERT INTO rag_sync_state(
                    document_id, document_type, source_record_id, content_hash,
                    embedding_model, vector_collection, vector_updated_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(document_id) DO UPDATE SET
                    document_type=excluded.document_type,
                    source_record_id=excluded.source_record_id,
                    content_hash=excluded.content_hash,
                    embedding_model=excluded.embedding_model,
                    vector_collection=excluded.vector_collection,
                    vector_updated_at=excluded.vector_updated_at,
                    metadata_json=excluded.metadata_json
                """,
                rows,
            )
    finally:
        connection.close()


def unsynced_incident_keys(database_path: Path, limit: int) -> dict[str, list[str]]:
    connection = connect(database_path)
    try:
        limit_sql = "" if limit <= 0 else " LIMIT ?"
        params: tuple[Any, ...] = () if limit <= 0 else (limit,)
        rows = connection.execute(
            """
            SELECT document_type, source_record_id
            FROM (
                SELECT 'osha_sir' AS document_type, s.incident_key AS source_record_id,
                       s.event_date AS sort_date, 'osha_sir:' || s.incident_key AS document_id
                FROM osha_sir_incidents AS s
                UNION ALL
                SELECT 'osha_news', n.entry_id, COALESCE(n.published_at, n.first_collected_at),
                       'osha_news:' || n.entry_id
                FROM osha_news_releases AS n
            ) AS candidates
            WHERE NOT EXISTS (
                SELECT 1 FROM rag_sync_state AS rs
                WHERE rs.document_id = candidates.document_id
            )
            ORDER BY sort_date DESC
            """ + limit_sql,
            params,
        ).fetchall()
    finally:
        connection.close()
    result = {"osha_sir": [], "osha_news": []}
    for row in rows:
        result[row["document_type"]].append(row["source_record_id"])
    return result


def save_matches(database_path: Path, run_id: str, matches: list[dict[str, Any]]) -> int:
    if not matches:
        return 0
    timestamp = utc_now()
    rows = []
    for match in matches:
        match_id = hashlib.sha256(
            f"{match['incident_document_id']}|{match['factory_risk_id']}".encode("utf-8")
        ).hexdigest()
        rows.append(
            (
                match_id, run_id, match["incident_document_id"],
                match["incident_source_type"], match["incident_record_id"],
                match["incident_title"], match.get("incident_date"),
                match.get("incident_url"), match["factory_risk_id"],
                match["factory_id"], match["factory_name"], match["area_id"],
                match["area_name"], match["equipment_id"], match["asset_code"],
                match["equipment_name"], match["assessment_code"],
                match["hazard_id"], match["hazard_name"], match["semantic_score"],
                match["hazard_score"], match["industry_score"],
                match["consequence_score"], match["relevance_score"],
                match["score_version"],
                json.dumps(match["rationale"], ensure_ascii=False, sort_keys=True),
                timestamp, timestamp,
            )
        )
    connection = connect(database_path)
    try:
        with connection:
            connection.executemany(
                """
                INSERT INTO incident_factory_matches(
                    match_id, run_id, incident_document_id, incident_source_type,
                    incident_record_id, incident_title, incident_date, incident_url,
                    factory_risk_id, factory_id, factory_name, area_id, area_name,
                    equipment_id, asset_code, equipment_name, assessment_code,
                    hazard_id, hazard_name, semantic_score, hazard_score,
                    industry_score, consequence_score, relevance_score,
                    score_version, rationale_json, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(incident_document_id, factory_risk_id) DO UPDATE SET
                    run_id=excluded.run_id,
                    incident_title=excluded.incident_title,
                    incident_date=excluded.incident_date,
                    incident_url=excluded.incident_url,
                    semantic_score=excluded.semantic_score,
                    hazard_score=excluded.hazard_score,
                    industry_score=excluded.industry_score,
                    consequence_score=excluded.consequence_score,
                    relevance_score=excluded.relevance_score,
                    score_version=excluded.score_version,
                    rationale_json=excluded.rationale_json,
                    updated_at=excluded.updated_at
                """,
                rows,
            )
    finally:
        connection.close()
    return len(rows)


def record_decision(
    database_path: Path,
    match_id: str,
    decision: str,
    decided_by: str,
    note: str = "",
) -> dict[str, Any]:
    if decision not in {"confirmed", "dismissed", "pending"}:
        raise ValueError("decision must be confirmed, dismissed, or pending")
    decision_id = str(uuid.uuid4())
    timestamp = utc_now()
    connection = connect(database_path)
    try:
        with connection:
            cursor = connection.execute(
                "UPDATE incident_factory_matches SET decision_status=?, updated_at=? "
                "WHERE match_id=?",
                (decision, timestamp, match_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Unknown match_id: {match_id}")
            connection.execute(
                "INSERT INTO human_decisions(decision_id, match_id, decision, "
                "decided_by, note, decided_at) VALUES (?, ?, ?, ?, ?, ?)",
                (decision_id, match_id, decision, decided_by, note or None, timestamp),
            )
    finally:
        connection.close()
    return {
        "decision_id": decision_id,
        "match_id": match_id,
        "decision": decision,
        "decided_by": decided_by,
        "decided_at": timestamp,
    }


def query_rows(database_path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    connection = connect(database_path)
    try:
        return [dict(row) for row in connection.execute(sql, params).fetchall()]
    finally:
        connection.close()


def dashboard_summary(database_path: Path) -> dict[str, Any]:
    connection = connect(database_path)
    try:
        row = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM osha_sir_incidents) AS incidents,
                (SELECT COUNT(*) FROM osha_news_releases) AS news,
                (SELECT COUNT(*) FROM rag_sync_state WHERE document_type='factory_risk')
                    AS factory_vectors,
                (SELECT COUNT(*) FROM rag_sync_state WHERE document_type!='factory_risk')
                    AS incident_vectors,
                (SELECT COUNT(*) FROM incident_factory_matches) AS matches,
                (SELECT COUNT(*) FROM incident_factory_matches
                 WHERE decision_status='pending') AS pending
            """
        ).fetchone()
        latest = connection.execute(
            "SELECT run_id, started_at, finished_at, status, error FROM rag_runs "
            "ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    finally:
        connection.close()
    return {"counts": dict(row), "latest_run": dict(latest) if latest else None}
