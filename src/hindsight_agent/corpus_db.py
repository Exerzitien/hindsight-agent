"""Grounding corpus: authoritative passages every generated sentence must cite.

G1 stores three kinds of source material, each split into individually citable
passages:

* ``cfr``          29 CFR sections from the eCFR versioner API (normative text)
* ``fatal_facts``  OSHA Fatal Facts case sheets (narrative + prevention steps)
* ``face``         NIOSH FACE investigation reports (placed manually, see below)

Every passage carries a ``citation_label`` a safety manager can verify by hand,
a ``text_sha256`` so a quote can be proved verbatim, and a link back to the
immutable snapshot the text came from. Nothing is generated here and nothing is
paraphrased; the corpus is the ground truth G4 checks drafts against.

The corpus is downloaded once and then read offline, which is what the daily
run relies on: content generation never touches the network.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

CORPUS_SCHEMA_VERSION = "corpus-v1"

SOURCE_CFR = "cfr"
SOURCE_FATAL_FACTS = "fatal_facts"
SOURCE_FACE = "face"
SOURCE_TYPES = (SOURCE_CFR, SOURCE_FATAL_FACTS, SOURCE_FACE)

CORPUS_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS corpus_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS corpus_snapshots (
    snapshot_key TEXT PRIMARY KEY,
    source_type TEXT NOT NULL CHECK (source_type IN ('cfr','fatal_facts','face')),
    source_url TEXT NOT NULL,
    http_status INTEGER,
    byte_size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    local_path TEXT NOT NULL,
    retrieved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS corpus_documents (
    document_id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL CHECK (source_type IN ('cfr','fatal_facts','face')),
    source_key TEXT NOT NULL,
    title TEXT NOT NULL,
    publisher TEXT NOT NULL,
    official_url TEXT NOT NULL,
    published_on TEXT,
    language TEXT NOT NULL DEFAULT 'en',
    edition TEXT,
    snapshot_key TEXT NOT NULL REFERENCES corpus_snapshots(snapshot_key),
    content_sha256 TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    UNIQUE (source_type, source_key, language)
);

CREATE TABLE IF NOT EXISTS corpus_passages (
    passage_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES corpus_documents(document_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    section_label TEXT NOT NULL,
    section_role TEXT NOT NULL DEFAULT 'OTHER',
    citation_label TEXT NOT NULL,
    text TEXT NOT NULL,
    text_sha256 TEXT NOT NULL,
    char_count INTEGER NOT NULL,
    hazard_tags_json TEXT NOT NULL DEFAULT '[]',
    equipment_tags_json TEXT NOT NULL DEFAULT '[]',
    UNIQUE (document_id, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_passages_document ON corpus_passages(document_id);
CREATE INDEX IF NOT EXISTS idx_passages_role ON corpus_passages(section_role);
CREATE INDEX IF NOT EXISTS idx_documents_source ON corpus_documents(source_type, source_key);

DROP VIEW IF EXISTS "接地语料";
CREATE VIEW "接地语料" AS
SELECT p.passage_id AS "段落ID", d.source_type AS "来源类型",
       d.title AS "文件标题", p.citation_label AS "引用标签",
       p.section_role AS "章节角色",
       d.language AS "语言", p.char_count AS "字符数",
       d.official_url AS "官方链接", s.sha256 AS "快照指纹",
       s.retrieved_at AS "抓取时间"
FROM corpus_passages AS p
JOIN corpus_documents AS d ON d.document_id = p.document_id
JOIN corpus_snapshots AS s ON s.snapshot_key = d.snapshot_key;
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def connect(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def ensure_corpus_schema(database_path: Path) -> None:
    connection = connect(database_path)
    try:
        connection.executescript(CORPUS_SCHEMA_SQL)
        connection.execute(
            "INSERT INTO corpus_metadata(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (CORPUS_SCHEMA_VERSION,),
        )
        connection.commit()
    finally:
        connection.close()


def record_snapshot(
    database_path: Path,
    *,
    snapshot_key: str,
    source_type: str,
    source_url: str,
    payload: bytes,
    local_path: Path,
    http_status: int | None = None,
) -> str:
    """Freeze raw bytes on disk and register their digest before any parsing."""
    if source_type not in SOURCE_TYPES:
        raise ValueError(f"Unknown corpus source type: {source_type}")
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(payload)
    digest = sha256_bytes(payload)

    connection = connect(database_path)
    try:
        connection.execute(
            """
            INSERT INTO corpus_snapshots(snapshot_key, source_type, source_url,
                http_status, byte_size, sha256, local_path, retrieved_at)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(snapshot_key) DO UPDATE SET
                source_url=excluded.source_url, http_status=excluded.http_status,
                byte_size=excluded.byte_size, sha256=excluded.sha256,
                local_path=excluded.local_path, retrieved_at=excluded.retrieved_at
            """,
            (
                snapshot_key, source_type, source_url, http_status,
                len(payload), digest, str(local_path), utc_now(),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return digest


def upsert_document(
    database_path: Path,
    *,
    document_id: str,
    source_type: str,
    source_key: str,
    title: str,
    publisher: str,
    official_url: str,
    snapshot_key: str,
    passages: list[dict[str, Any]],
    published_on: str | None = None,
    language: str = "en",
    edition: str | None = None,
) -> dict[str, Any]:
    """Replace a document and all of its passages in one transaction.

    ``passages`` entries need ``section_label``, ``citation_label`` and ``text``;
    ``hazard_tags`` and ``equipment_tags`` are optional lists.
    """
    joined = "\n\n".join(item["text"] for item in passages)
    content_hash = sha256_text(joined)

    connection = connect(database_path)
    try:
        with connection:
            connection.execute(
                "DELETE FROM corpus_passages WHERE document_id=?", (document_id,)
            )
            connection.execute(
                """
                INSERT INTO corpus_documents(document_id, source_type, source_key,
                    title, publisher, official_url, published_on, language, edition,
                    snapshot_key, content_sha256, imported_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(document_id) DO UPDATE SET
                    title=excluded.title, publisher=excluded.publisher,
                    official_url=excluded.official_url,
                    published_on=excluded.published_on, language=excluded.language,
                    edition=excluded.edition, snapshot_key=excluded.snapshot_key,
                    content_sha256=excluded.content_sha256,
                    imported_at=excluded.imported_at
                """,
                (
                    document_id, source_type, source_key, title, publisher,
                    official_url, published_on, language, edition, snapshot_key,
                    content_hash, utc_now(),
                ),
            )
            for ordinal, item in enumerate(passages, start=1):
                text = item["text"]
                connection.execute(
                    """
                    INSERT INTO corpus_passages(passage_id, document_id, ordinal,
                        section_label, section_role, citation_label, text,
                        text_sha256, char_count, hazard_tags_json,
                        equipment_tags_json)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        f"{document_id}#{ordinal:04d}", document_id, ordinal,
                        item["section_label"], item.get("section_role", "OTHER"),
                        item["citation_label"], text,
                        sha256_text(text), len(text),
                        json.dumps(sorted(item.get("hazard_tags", [])), ensure_ascii=False),
                        json.dumps(sorted(item.get("equipment_tags", [])), ensure_ascii=False),
                    ),
                )
    finally:
        connection.close()
    return {
        "document_id": document_id,
        "passages": len(passages),
        "content_sha256": content_hash,
    }


def get_passage(database_path: Path, passage_id: str) -> dict[str, Any] | None:
    """Resolve one citation. G4 calls this for every claim in a draft."""
    connection = connect(database_path)
    try:
        row = connection.execute(
            """
            SELECT p.*, d.source_type, d.title, d.publisher, d.official_url,
                   d.language, d.published_on, s.sha256 AS snapshot_sha256
            FROM corpus_passages AS p
            JOIN corpus_documents AS d ON d.document_id = p.document_id
            JOIN corpus_snapshots AS s ON s.snapshot_key = d.snapshot_key
            WHERE p.passage_id = ?
            """,
            (passage_id,),
        ).fetchone()
    finally:
        connection.close()
    return dict(row) if row else None


def iter_passages(
    database_path: Path,
    *,
    source_types: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    clause, params = "", []
    if source_types:
        types = list(source_types)
        clause = f"WHERE d.source_type IN ({','.join('?' * len(types))})"
        params = types
    connection = connect(database_path)
    try:
        rows = connection.execute(
            f"""
            SELECT p.passage_id, p.document_id, p.section_label, p.section_role,
                   p.citation_label, p.text, p.text_sha256,
                   p.hazard_tags_json, p.equipment_tags_json,
                   d.source_type, d.title, d.official_url, d.language
            FROM corpus_passages AS p
            JOIN corpus_documents AS d ON d.document_id = p.document_id
            {clause}
            ORDER BY p.document_id, p.ordinal
            """,
            tuple(params),
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def corpus_summary(database_path: Path) -> dict[str, Any]:
    connection = connect(database_path)
    try:
        by_source = {
            row["source_type"]: {"documents": row["documents"], "passages": row["passages"]}
            for row in connection.execute(
                """
                SELECT d.source_type,
                       COUNT(DISTINCT d.document_id) AS documents,
                       COUNT(p.passage_id) AS passages
                FROM corpus_documents AS d
                LEFT JOIN corpus_passages AS p ON p.document_id = d.document_id
                GROUP BY d.source_type
                """
            )
        }
        totals = connection.execute(
            "SELECT COUNT(*) AS passages FROM corpus_passages"
        ).fetchone()
        snapshots = connection.execute(
            "SELECT COUNT(*) AS n, MAX(retrieved_at) AS latest FROM corpus_snapshots"
        ).fetchone()
    finally:
        connection.close()
    return {
        "schema_version": CORPUS_SCHEMA_VERSION,
        "by_source": by_source,
        "total_passages": totals["passages"],
        "snapshots": snapshots["n"],
        "latest_snapshot_at": snapshots["latest"],
    }
