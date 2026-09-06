"""Turn relational incident and factory records into auditable RAG documents."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class RagDocument:
    id: str
    document_type: str
    source_record_id: str
    title: str
    text: str
    content_hash: str
    event_date: str
    naics: str
    metadata: dict[str, Any]


def _hash_payload(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _chunks(items: list[str], size: int = 800) -> Iterable[list[str]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def build_factory_risk_documents(database_path: Path) -> list[RagDocument]:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT ra.id, ra.assessment_code, ra.version, f.id AS factory_id,
                   f.name AS factory_name, f.naics AS factory_naics,
                   a.id AS area_id, a.name AS area_name,
                   e.id AS equipment_id, e.asset_code, e.name AS equipment_name,
                   e.equipment_type, e.description AS equipment_description,
                   t.id AS task_id, t.name AS task_name, t.description AS task_description,
                   h.id AS hazard_id, h.name AS hazard_name,
                   h.category AS hazard_category, h.description AS hazard_description,
                   w.id AS exposed_role_id, w.name AS exposed_role,
                   ra.lifecycle_phase, ra.operating_mode, ra.hazardous_situation,
                   ra.foreseeable_misuse, ra.initial_score, ra.initial_level,
                   ra.residual_score, ra.residual_level, ra.risk_accepted,
                   ra.status, ra.assessment_method, ra.evidence_basis,
                   GROUP_CONCAT(sc.name, ' | ') AS control_names,
                   GROUP_CONCAT(sc.description, ' | ') AS control_descriptions
            FROM risk_assessments AS ra
            JOIN equipment AS e ON e.id = ra.equipment_id
            JOIN factories AS f ON f.id = e.factory_id
            JOIN areas AS a ON a.id = e.area_id
            JOIN tasks AS t ON t.id = ra.task_id
            JOIN hazards AS h ON h.id = ra.hazard_id
            JOIN worker_roles AS w ON w.id = ra.exposed_role_id
            LEFT JOIN risk_assessment_controls AS rac
                   ON rac.risk_assessment_id = ra.id
            LEFT JOIN safety_controls AS sc ON sc.id = rac.safety_control_id
            GROUP BY ra.id
            ORDER BY ra.assessment_code
            """
        ).fetchall()
    finally:
        connection.close()

    documents: list[RagDocument] = []
    for row in rows:
        data = dict(row)
        title = (
            f"{data['assessment_code']} | {data['area_name']} | "
            f"{data['equipment_name']} | {data['hazard_name']}"
        )
        text = "\n".join(
            [
                f"Factory: {data['factory_name']} (NAICS {data['factory_naics']})",
                f"Area: {data['area_name']}",
                f"Equipment: {data['equipment_name']} ({data['equipment_type']}); "
                f"{data['equipment_description']}",
                f"Task: {data['task_name']}; {data['task_description']}",
                f"Hazard: {data['hazard_name']} [{data['hazard_category']}]; "
                f"{data['hazard_description']}",
                f"Exposed role: {data['exposed_role']}",
                f"Hazardous situation: {data['hazardous_situation']}",
                f"Foreseeable misuse: {data['foreseeable_misuse']}",
                f"Controls: {data['control_names'] or 'none recorded'}; "
                f"{data['control_descriptions'] or ''}",
                f"Risk: initial {data['initial_score']} {data['initial_level']}; "
                f"residual {data['residual_score']} {data['residual_level']}",
            ]
        )
        hash_fields = dict(data)
        documents.append(
            RagDocument(
                id=f"factory_risk:{data['id']}",
                document_type="factory_risk",
                source_record_id=data["id"],
                title=title,
                text=text,
                content_hash=_hash_payload(hash_fields),
                event_date="",
                naics=data["factory_naics"],
                metadata={
                    "factory_id": data["factory_id"],
                    "factory_name": data["factory_name"],
                    "area_id": data["area_id"],
                    "area_name": data["area_name"],
                    "equipment_id": data["equipment_id"],
                    "asset_code": data["asset_code"],
                    "equipment_name": data["equipment_name"],
                    "equipment_type": data["equipment_type"],
                    "task_id": data["task_id"],
                    "task_name": data["task_name"],
                    "hazard_id": data["hazard_id"],
                    "hazard_name": data["hazard_name"],
                    "assessment_code": data["assessment_code"],
                    "initial_score": data["initial_score"],
                    "residual_score": data["residual_score"],
                    "risk_accepted": bool(data["risk_accepted"]),
                },
            )
        )
    return documents


def _sir_documents(connection: sqlite3.Connection, keys: list[str] | None) -> list[RagDocument]:
    base_sql = """
        SELECT incident_key, event_date, employer, city, state, primary_naics,
               hospitalized, amputation, loss_of_eye, narrative, nature_code,
               nature_title, body_part_code, body_part_title, event_code,
               event_title, source_code, source_title, secondary_source_title,
               content_hash, source_snapshot_key
        FROM osha_sir_incidents
    """
    rows: list[sqlite3.Row] = []
    if keys is None:
        rows = connection.execute(base_sql + " ORDER BY event_date DESC").fetchall()
    else:
        for part in _chunks(keys):
            placeholders = ",".join("?" for _ in part)
            rows.extend(
                connection.execute(
                    base_sql + f" WHERE incident_key IN ({placeholders})",
                    part,
                ).fetchall()
            )

    documents: list[RagDocument] = []
    for row in rows:
        data = dict(row)
        title = f"{data['event_date']} | {data['employer']} | {data['event_title'] or 'OSHA SIR'}"
        text = "\n".join(
            [
                f"Employer: {data['employer']} (NAICS {data['primary_naics'] or 'unknown'})",
                f"Location: {data['city'] or ''}, {data['state']}",
                f"Event: {data['event_title'] or ''} ({data['event_code']})",
                f"Narrative: {data['narrative']}",
                f"Injury: {data['nature_title'] or ''}; body part: {data['body_part_title'] or ''}",
                f"Source: {data['source_title'] or ''}; secondary source: "
                f"{data['secondary_source_title'] or ''}",
                f"Consequences: hospitalized={data['hospitalized']}, "
                f"amputation={data['amputation']}, loss_of_eye={data['loss_of_eye']}",
            ]
        )
        documents.append(
            RagDocument(
                id=f"osha_sir:{data['incident_key']}",
                document_type="osha_sir",
                source_record_id=data["incident_key"],
                title=title,
                text=text,
                content_hash=data["content_hash"],
                event_date=data["event_date"],
                naics=data["primary_naics"] or "",
                metadata={
                    "employer": data["employer"],
                    "state": data["state"],
                    "event_code": data["event_code"],
                    "event_title": data["event_title"] or "",
                    "nature_code": data["nature_code"],
                    "body_part_code": data["body_part_code"],
                    "source_code": data["source_code"],
                    "hospitalized": data["hospitalized"],
                    "amputation": data["amputation"],
                    "loss_of_eye": data["loss_of_eye"],
                    "source_snapshot_key": data["source_snapshot_key"],
                },
            )
        )
    return documents


def _news_documents(connection: sqlite3.Connection, keys: list[str] | None) -> list[RagDocument]:
    base_sql = """
        SELECT n.entry_id, n.title, n.url, n.published_at, n.summary,
               n.content_hash, n.source_snapshot_key,
               COALESCE(d.body_text, '') AS body_text,
               COALESCE(d.body_hash, '') AS body_hash
        FROM osha_news_releases AS n
        LEFT JOIN osha_news_documents AS d ON d.entry_id = n.entry_id
    """
    rows: list[sqlite3.Row] = []
    if keys is None:
        rows = connection.execute(base_sql + " ORDER BY n.published_at DESC").fetchall()
    else:
        for part in _chunks(keys):
            placeholders = ",".join("?" for _ in part)
            rows.extend(
                connection.execute(
                    base_sql + f" WHERE n.entry_id IN ({placeholders})",
                    part,
                ).fetchall()
            )

    documents: list[RagDocument] = []
    for row in rows:
        data = dict(row)
        body = data["body_text"].strip()
        text = "\n".join(
            part
            for part in (
                f"OSHA news release: {data['title']}",
                f"Published: {data['published_at'] or 'unknown'}",
                f"Summary: {data['summary'] or ''}",
                f"Body: {body}" if body else "",
                f"Source URL: {data['url']}",
            )
            if part
        )
        effective_hash = _hash_payload(
            {"feed_hash": data["content_hash"], "body_hash": data["body_hash"]}
        )
        documents.append(
            RagDocument(
                id=f"osha_news:{data['entry_id']}",
                document_type="osha_news",
                source_record_id=data["entry_id"],
                title=data["title"],
                text=text,
                content_hash=effective_hash,
                event_date=data["published_at"] or "",
                naics="",
                metadata={
                    "url": data["url"],
                    "source_snapshot_key": data["source_snapshot_key"],
                    "has_full_text": bool(body),
                },
            )
        )
    return documents


def load_incident_documents(
    database_path: Path,
    sir_keys: list[str] | None = None,
    news_keys: list[str] | None = None,
) -> list[RagDocument]:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        documents: list[RagDocument] = []
        if sir_keys is None or sir_keys:
            documents.extend(_sir_documents(connection, sir_keys))
        if news_keys is None or news_keys:
            documents.extend(_news_documents(connection, news_keys))
        return documents
    finally:
        connection.close()
