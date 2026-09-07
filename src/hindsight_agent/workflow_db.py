"""Workflow state: the case machine, the rule pack, and the delivery ledger.

These tables live alongside the existing audit tables in ``incidents.db`` and
carry the parts of the workflow spec that the collection pipeline never had:
where a case is in its lifecycle, which rule pack version judged it, who it was
sent to, and what came back.

Three invariants are enforced here rather than left to callers:

* ``case_transitions`` is append-only. A case's history is never rewritten, so
  "why did this reach a worker" is always answerable after the fact.
* A case only moves along edges declared in ``ALLOWED_TRANSITIONS``. There is no
  edge into ``DISTRIBUTED`` that does not pass through ``APPROVED``, which is
  what makes "未批不触达" a property of the schema and not of good intentions.
* ``deliveries`` is keyed by an idempotency key, so a re-run after a crash
  cannot deliver the same pack to the same audience twice.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

WORKFLOW_SCHEMA_VERSION = "workflow-v1"

# --- case lifecycle -------------------------------------------------------
NEW = "NEW"
DEDUPED = "DEDUPED"
EXTRACTED = "EXTRACTED"
CLASSIFIED = "CLASSIFIED"
ARCHIVED = "ARCHIVED"
DRAFTED = "DRAFTED"
SELF_CHECK_OK = "SELF_CHECK_OK"
NEEDS_HUMAN_WRITER = "NEEDS_HUMAN_WRITER"
PENDING_APPROVAL = "PENDING_APPROVAL"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
CLOSED_REJECTED = "CLOSED_REJECTED"
DISTRIBUTED = "DISTRIBUTED"
TRACKING = "TRACKING"
CLOSED = "CLOSED"
REMINDED = "REMINDED"
ESCALATED = "ESCALATED"
QUARANTINED = "QUARANTINED"
STALE_SOURCE = "STALE_SOURCE"

CASE_STATES = (
    NEW, DEDUPED, EXTRACTED, CLASSIFIED, ARCHIVED, DRAFTED, SELF_CHECK_OK,
    NEEDS_HUMAN_WRITER, PENDING_APPROVAL, APPROVED, REJECTED, CLOSED_REJECTED,
    DISTRIBUTED, TRACKING, CLOSED, REMINDED, ESCALATED, QUARANTINED, STALE_SOURCE,
)

#: The only moves a case may make. Note what is absent: nothing reaches
#: DISTRIBUTED except from APPROVED. There is deliberately no AUTO_SEND state.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    NEW: frozenset({DEDUPED, QUARANTINED, STALE_SOURCE}),
    DEDUPED: frozenset({EXTRACTED, QUARANTINED}),
    EXTRACTED: frozenset({CLASSIFIED, QUARANTINED}),
    # A reviewer may dismiss a routed alert before anything is written for it.
    # That is the cheap path and the one a busy manager actually uses; forcing a
    # draft first would spend model budget on material nobody wanted.
    CLASSIFIED: frozenset({ARCHIVED, DRAFTED, REJECTED}),
    DRAFTED: frozenset({SELF_CHECK_OK, NEEDS_HUMAN_WRITER}),
    SELF_CHECK_OK: frozenset({PENDING_APPROVAL}),
    NEEDS_HUMAN_WRITER: frozenset({DRAFTED, REJECTED, CLOSED}),
    PENDING_APPROVAL: frozenset({APPROVED, REJECTED, REMINDED}),
    REMINDED: frozenset({APPROVED, REJECTED, ESCALATED}),
    ESCALATED: frozenset({APPROVED, REJECTED}),
    APPROVED: frozenset({DISTRIBUTED}),
    REJECTED: frozenset({CLOSED_REJECTED}),
    DISTRIBUTED: frozenset({TRACKING}),
    TRACKING: frozenset({CLOSED}),
    ARCHIVED: frozenset(),
    CLOSED: frozenset(),
    CLOSED_REJECTED: frozenset(),
    QUARANTINED: frozenset({DEDUPED, CLOSED}),
    STALE_SOURCE: frozenset({NEW}),
}

#: Once a case is here it is read-only; a re-run resumes from the last state
#: rather than starting over.
TERMINAL_STATES = frozenset({ARCHIVED, CLOSED, CLOSED_REJECTED})

WORKFLOW_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS workflow_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    incident_key TEXT NOT NULL,
    incident_source_type TEXT NOT NULL,
    risk_assessment_id TEXT,
    state TEXT NOT NULL,
    level TEXT,
    rule_pack_version TEXT,
    draft_content_hash TEXT,
    extraction_confidence REAL,
    opened_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT,
    UNIQUE (incident_key, risk_assessment_id)
);

CREATE TABLE IF NOT EXISTS case_transitions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    transition_id TEXT NOT NULL UNIQUE,
    case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    from_state TEXT,
    to_state TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    detail TEXT,
    run_id TEXT,
    occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quarantine (
    quarantine_id TEXT PRIMARY KEY,
    incident_key TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    detail TEXT NOT NULL,
    extraction_confidence REAL,
    sampled_for_review INTEGER NOT NULL DEFAULT 0,
    reviewed_by TEXT,
    reviewed_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_packs (
    version TEXT PRIMARY KEY,
    parent_version TEXT,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('candidate','active','retired')),
    approved_by TEXT,
    approved_at TEXT,
    created_at TEXT NOT NULL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS rule_pack_diffs (
    diff_id TEXT PRIMARY KEY,
    from_version TEXT NOT NULL,
    proposed_version TEXT NOT NULL,
    change_kind TEXT NOT NULL,
    change_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('proposed','accepted','rejected')),
    decided_by TEXT,
    decided_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channels (
    channel_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    shift TEXT,
    role_id TEXT,
    language TEXT NOT NULL DEFAULT 'en',
    member_count INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    channel_id TEXT NOT NULL REFERENCES channels(channel_id),
    pack_content_hash TEXT NOT NULL,
    language TEXT NOT NULL,
    audience_size INTEGER NOT NULL,
    approval_token TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('delivered','withdrawn')),
    delivered_at TEXT NOT NULL,
    withdrawn_at TEXT
);

CREATE TABLE IF NOT EXISTS completions (
    completion_id TEXT PRIMARY KEY,
    delivery_id TEXT NOT NULL REFERENCES deliveries(delivery_id) ON DELETE CASCADE,
    worker_ref TEXT NOT NULL,
    completed_at TEXT,
    quiz_score REAL,
    quiz_answers_json TEXT,
    UNIQUE (delivery_id, worker_ref)
);

CREATE TABLE IF NOT EXISTS reminders (
    reminder_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    outstanding INTEGER NOT NULL,
    sent_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pack_artifacts (
    content_hash TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    draft_id TEXT NOT NULL,
    risk_assessment_id TEXT,
    assessment_code TEXT,
    incident_citation TEXT,
    pack_json TEXT NOT NULL,
    canonical_json TEXT NOT NULL,
    self_check_json TEXT NOT NULL,
    pack_version TEXT NOT NULL,
    model_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback_signals (
    signal_id TEXT PRIMARY KEY,
    case_id TEXT REFERENCES cases(case_id) ON DELETE SET NULL,
    signal_kind TEXT NOT NULL,
    feature_json TEXT NOT NULL,
    reason_code TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cases_state ON cases(state, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_cases_incident ON cases(incident_key);
CREATE INDEX IF NOT EXISTS idx_transitions_case ON case_transitions(case_id, seq);
CREATE INDEX IF NOT EXISTS idx_deliveries_case ON deliveries(case_id);
CREATE INDEX IF NOT EXISTS idx_signals_kind ON feedback_signals(signal_kind, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pack_artifacts_case ON pack_artifacts(case_id, created_at DESC);

DROP VIEW IF EXISTS "案例状态";
CREATE VIEW "案例状态" AS
SELECT c.case_id AS "案例ID", c.incident_key AS "事故键",
       c.risk_assessment_id AS "危险路径", c.level AS "级别",
       CASE c.state
         WHEN 'ARCHIVED' THEN '静默归档' WHEN 'PENDING_APPROVAL' THEN '待审批'
         WHEN 'APPROVED' THEN '已批准'   WHEN 'DISTRIBUTED' THEN '已分发'
         WHEN 'TRACKING' THEN '追踪中'   WHEN 'CLOSED' THEN '已闭环'
         WHEN 'REJECTED' THEN '已驳回'   WHEN 'QUARANTINED' THEN '隔离待检'
         WHEN 'NEEDS_HUMAN_WRITER' THEN '需人工补写'
         ELSE c.state END AS "状态",
       c.rule_pack_version AS "规则包版本", c.opened_at AS "开案时间",
       c.updated_at AS "更新时间", c.closed_at AS "闭环时间"
FROM cases AS c;

DROP VIEW IF EXISTS "分发与完成";
CREATE VIEW "分发与完成" AS
SELECT d.delivery_id AS "分发ID", d.case_id AS "案例ID", ch.name AS "渠道",
       ch.shift AS "班次", d.language AS "语言", d.audience_size AS "触达人数",
       COUNT(cp.completion_id) AS "已登记", 
       SUM(CASE WHEN cp.completed_at IS NOT NULL THEN 1 ELSE 0 END) AS "已完成",
       ROUND(AVG(cp.quiz_score), 3) AS "平均成绩",
       CASE d.status WHEN 'delivered' THEN '已送达' ELSE '已撤回' END AS "状态",
       d.delivered_at AS "送达时间"
FROM deliveries AS d
JOIN channels AS ch ON ch.channel_id = d.channel_id
LEFT JOIN completions AS cp ON cp.delivery_id = d.delivery_id
GROUP BY d.delivery_id;
"""


class TransitionError(RuntimeError):
    """Raised when a case is asked to make a move the machine does not allow."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def idempotency_key(*parts: str) -> str:
    """Stable key so a re-run recognises work it already did."""
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def connect(database_path: Path | str) -> sqlite3.Connection:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def ensure_workflow_schema(database_path: Path | str) -> None:
    connection = connect(database_path)
    try:
        connection.executescript(WORKFLOW_SCHEMA_SQL)
        connection.execute(
            "INSERT INTO workflow_metadata(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (WORKFLOW_SCHEMA_VERSION,),
        )
        connection.commit()
    finally:
        connection.close()


def open_case(
    database_path: Path | str,
    *,
    incident_key: str,
    incident_source_type: str,
    risk_assessment_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Open a case, or return the existing one untouched.

    Re-running a day is not supposed to reopen or duplicate anything, so an
    existing case is returned with ``created=False`` and its state intact.
    """
    connection = connect(database_path)
    try:
        # When no path is named yet, "does a case exist for this incident?" is
        # the right question - the path is filled in later by CLASSIFIED. Asking
        # for (incident, NULL) instead would miss the case the moment it got a
        # path and open a duplicate on the next run.
        if risk_assessment_id is None:
            existing = connection.execute(
                "SELECT * FROM cases WHERE incident_key=? ORDER BY opened_at LIMIT 1",
                (incident_key,),
            ).fetchone()
        else:
            existing = connection.execute(
                "SELECT * FROM cases WHERE incident_key=? AND risk_assessment_id=?",
                (incident_key, risk_assessment_id),
            ).fetchone()
        if existing is not None:
            return {"created": False, **dict(existing)}

        case_id = new_id("case")
        now = utc_now()
        with connection:
            connection.execute(
                "INSERT INTO cases(case_id, incident_key, incident_source_type, "
                "risk_assessment_id, state, opened_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                (case_id, incident_key, incident_source_type, risk_assessment_id,
                 NEW, now, now),
            )
            connection.execute(
                "INSERT INTO case_transitions(transition_id, case_id, from_state, "
                "to_state, actor, reason_code, detail, run_id, occurred_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (new_id("tr"), case_id, None, NEW, "system", "CASE_OPENED", None,
                 run_id, now),
            )
        row = connection.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
        return {"created": True, **dict(row)}
    finally:
        connection.close()


def transition(
    database_path: Path | str,
    case_id: str,
    to_state: str,
    *,
    actor: str,
    reason_code: str,
    detail: str | None = None,
    run_id: str | None = None,
    **case_fields: Any,
) -> dict[str, Any]:
    """Move a case, refusing any edge the machine does not declare.

    ``case_fields`` updates columns on the case in the same transaction, so a
    level or a draft hash can never be recorded without the move that earned it.
    """
    if to_state not in CASE_STATES:
        raise TransitionError(f"未知状态: {to_state}")

    connection = connect(database_path)
    try:
        row = connection.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
        if row is None:
            raise TransitionError(f"案例不存在: {case_id}")
        current = row["state"]
        if current in TERMINAL_STATES:
            raise TransitionError(f"{case_id} 已处于终态 {current}，只读")
        if to_state not in ALLOWED_TRANSITIONS.get(current, frozenset()):
            raise TransitionError(
                f"不允许的状态转移: {current} → {to_state}（案例 {case_id}）"
            )

        now = utc_now()
        allowed_columns = {
            "risk_assessment_id", "level", "rule_pack_version",
            "draft_content_hash", "extraction_confidence",
        }
        unknown = set(case_fields) - allowed_columns
        if unknown:
            raise TransitionError(f"未知的案例字段: {sorted(unknown)}")

        assignments = ["state=?", "updated_at=?"]
        values: list[Any] = [to_state, now]
        for column, value in case_fields.items():
            assignments.append(f"{column}=?")
            values.append(value)
        if to_state in TERMINAL_STATES:
            assignments.append("closed_at=?")
            values.append(now)
        values.append(case_id)

        with connection:
            connection.execute(
                f"UPDATE cases SET {', '.join(assignments)} WHERE case_id=?", values
            )
            connection.execute(
                "INSERT INTO case_transitions(transition_id, case_id, from_state, "
                "to_state, actor, reason_code, detail, run_id, occurred_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (new_id("tr"), case_id, current, to_state, actor, reason_code,
                 detail, run_id, now),
            )
        updated = connection.execute(
            "SELECT * FROM cases WHERE case_id=?", (case_id,)
        ).fetchone()
        return dict(updated)
    finally:
        connection.close()


def case_history(database_path: Path | str, case_id: str) -> list[dict[str, Any]]:
    connection = connect(database_path)
    try:
        rows = connection.execute(
            "SELECT * FROM case_transitions WHERE case_id=? ORDER BY seq",
            (case_id,),
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def cases_in_state(
    database_path: Path | str, states: Iterable[str], limit: int = 200
) -> list[dict[str, Any]]:
    states = list(states)
    connection = connect(database_path)
    try:
        rows = connection.execute(
            f"SELECT * FROM cases WHERE state IN ({','.join('?' * len(states))}) "
            "ORDER BY updated_at DESC LIMIT ?",
            (*states, limit),
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def record_signal(
    database_path: Path | str,
    *,
    signal_kind: str,
    feature: dict[str, Any],
    case_id: str | None = None,
    reason_code: str | None = None,
) -> str:
    """R1: append one featurised feedback signal. Never overwritten."""
    signal_id = new_id("sig")
    connection = connect(database_path)
    try:
        with connection:
            connection.execute(
                "INSERT INTO feedback_signals(signal_id, case_id, signal_kind, "
                "feature_json, reason_code, created_at) VALUES(?,?,?,?,?,?)",
                (signal_id, case_id, signal_kind,
                 json.dumps(feature, ensure_ascii=False), reason_code, utc_now()),
            )
    finally:
        connection.close()
    return signal_id


class PackArtifactError(RuntimeError):
    """Raised when a stored pack body does not match the hash it is filed under."""


def store_pack_artifact(
    database_path: Path | str,
    *,
    case_id: str,
    pack: dict[str, Any],
    self_check: dict[str, Any],
) -> str:
    """Persist the body a person is about to approve, filed under its own hash.

    Without this the approval token binds a hash whose body exists nowhere:
    the reviewer approves a string of hex, and workers receive an empty
    payload. Storing the body is what makes the token a real binding rather
    than a gesture.

    Append-only by construction. The same claims always hash to the same
    value, so a re-draft that changes nothing is a no-op; a re-draft that
    changes anything gets a new hash and a new row, and the row a person
    already approved is never rewritten underneath them.
    """
    from .learning_pack import hash_canonical_payload

    canonical = pack.get("canonical")
    if not isinstance(canonical, dict):
        raise PackArtifactError(
            "学习包缺少 canonical 字段，无法证明正文与哈希一致；拒绝入库。"
        )
    content_hash = hash_canonical_payload(canonical)
    declared = pack.get("content_hash")
    if declared != content_hash:
        raise PackArtifactError(
            f"学习包自称的哈希 {declared} 与正文重算结果 {content_hash} 不一致；拒绝入库。"
        )

    connection = connect(database_path)
    try:
        with connection:
            connection.execute(
                "INSERT OR IGNORE INTO pack_artifacts(content_hash, case_id, draft_id, "
                "risk_assessment_id, assessment_code, incident_citation, pack_json, "
                "canonical_json, self_check_json, pack_version, model_id, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    content_hash, case_id, str(pack.get("draft_id") or ""),
                    pack.get("risk_assessment_id"), pack.get("assessment_code"),
                    pack.get("incident_citation"),
                    json.dumps(pack, ensure_ascii=False),
                    json.dumps(canonical, ensure_ascii=False),
                    json.dumps(self_check, ensure_ascii=False),
                    str(pack.get("pack_version") or ""), pack.get("model_id"),
                    utc_now(),
                ),
            )
    finally:
        connection.close()
    return content_hash


def load_pack_artifact(
    database_path: Path | str, content_hash: str, *, required: bool = True
) -> dict[str, Any] | None:
    """Read a stored pack back, re-hashing it before handing it over.

    The check is not ceremonial. Anything that reaches a worker's phone has to
    be the thing the approver read, and the only way to know that here is to
    recompute the hash from the stored claims rather than trusting the column
    it is filed under.
    """
    from .learning_pack import hash_canonical_payload

    connection = connect(database_path)
    try:
        row = connection.execute(
            "SELECT * FROM pack_artifacts WHERE content_hash=?", (content_hash,)
        ).fetchone()
    finally:
        connection.close()

    if row is None:
        if required:
            raise PackArtifactError(
                f"没有哈希为 {content_hash} 的学习包正文；这条案例不能分发。"
            )
        return None

    canonical = json.loads(row["canonical_json"])
    recomputed = hash_canonical_payload(canonical)
    if recomputed != content_hash:
        raise PackArtifactError(
            f"正文与哈希不符：入库为 {content_hash}，重算为 {recomputed}。"
            f"这份稿件已被改动，拒绝分发。"
        )

    return {
        "content_hash": content_hash,
        "case_id": row["case_id"],
        "draft_id": row["draft_id"],
        "risk_assessment_id": row["risk_assessment_id"],
        "assessment_code": row["assessment_code"],
        "incident_citation": row["incident_citation"],
        "pack": json.loads(row["pack_json"]),
        "self_check": json.loads(row["self_check_json"]),
        "pack_version": row["pack_version"],
        "model_id": row["model_id"],
        "created_at": row["created_at"],
    }


def pack_artifact_for_case(
    database_path: Path | str, case_id: str
) -> dict[str, Any] | None:
    """The body currently attached to a case, or None if it has not been drafted."""
    connection = connect(database_path)
    try:
        row = connection.execute(
            "SELECT draft_content_hash FROM cases WHERE case_id=?", (case_id,)
        ).fetchone()
    finally:
        connection.close()
    if row is None or not row["draft_content_hash"]:
        return None
    return load_pack_artifact(database_path, row["draft_content_hash"], required=False)
