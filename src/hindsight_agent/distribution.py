"""S4 · D1–D5: channels, targeted delivery, tracking, reminders, digest.

Delivery goes to a mock inbox, which is what the workflow spec asks for at this
stage: 真实短信/邮件渠道进 v2. The mechanism around it is real, and it is the
part that matters - idempotency, the approval token, the audit trail, and the
fact that no function here can send anything a person has not signed.

Every side-effecting call takes an ``approval_token`` and is registered in
``authorization.PROTECTED_TOOLS``, so the guard cancels it before it runs if the
token does not verify against this exact pack.

Idempotency is by key, not by memory: ``site + case + channel + audience`` for a
delivery and ``case + stage`` for a reminder. Re-running a day, or recovering
from a crash mid-run, cannot deliver twice or nag twice.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from strands import tool

from .authorization import verify_token
from .workflow_db import (
    DISTRIBUTED,
    TRACKING,
    CLOSED,
    connect,
    ensure_workflow_schema,
    idempotency_key,
    new_id,
    transition,
    utc_now,
)

DISTRIBUTION_VERSION = "distribution-v1.0"

#: Where the mock inbox writes. One JSONL line per delivery, so the demo can
#: show a worker-visible artefact without pretending an SMS gateway exists.
MOCK_INBOX = Path("data/outbox/mock_inbox.jsonl")

REMINDER_T24 = "T+24h"
REMINDER_T72 = "T+72h"


# --------------------------------------------------------------------------
# S4 · 渠道注册表
# --------------------------------------------------------------------------

DEFAULT_CHANNELS: tuple[dict[str, Any], ...] = (
    {"channel_id": "ch-stamping-a", "name": "冲压区 A 班", "kind": "mock_inbox",
     "shift": "A", "role_id": "role-press-operator", "language": "en", "member_count": 12},
    {"channel_id": "ch-stamping-a-es", "name": "冲压区 A 班（西语）", "kind": "mock_inbox",
     "shift": "A", "role_id": "role-press-operator", "language": "es", "member_count": 5},
    {"channel_id": "ch-warehouse-b", "name": "仓储区 B 班", "kind": "mock_inbox",
     "shift": "B", "role_id": "role-forklift-operator", "language": "en", "member_count": 8},
    {"channel_id": "ch-ehs", "name": "EHS 与主管", "kind": "mock_inbox",
     "shift": None, "role_id": "role-ehs-manager", "language": "en", "member_count": 3},
)


def register_channels(
    database_path: Path | str, channels: Iterable[dict[str, Any]] | None = None
) -> int:
    """S4: register the delivery channels. Idempotent by channel_id."""
    ensure_workflow_schema(database_path)
    rows = list(channels or DEFAULT_CHANNELS)
    connection = connect(database_path)
    try:
        with connection:
            for row in rows:
                connection.execute(
                    "INSERT INTO channels(channel_id, name, kind, shift, role_id, "
                    "language, member_count, active, created_at) VALUES(?,?,?,?,?,?,?,1,?) "
                    "ON CONFLICT(channel_id) DO UPDATE SET name=excluded.name, "
                    "kind=excluded.kind, shift=excluded.shift, role_id=excluded.role_id, "
                    "language=excluded.language, member_count=excluded.member_count",
                    (row["channel_id"], row["name"], row["kind"], row.get("shift"),
                     row.get("role_id"), row.get("language", "en"),
                     int(row.get("member_count", 0)), utc_now()),
                )
    finally:
        connection.close()
    return len(rows)


def resolve_audience(
    database_path: Path | str, *, role_id: str | None = None, language: str | None = None
) -> list[dict[str, Any]]:
    """D1: which channels does this pack actually belong to?"""
    clauses, params = ["active = 1"], []
    if role_id:
        clauses.append("role_id = ?")
        params.append(role_id)
    if language:
        clauses.append("language = ?")
        params.append(language)
    connection = connect(database_path)
    try:
        rows = connection.execute(
            f"SELECT * FROM channels WHERE {' AND '.join(clauses)} ORDER BY channel_id",
            tuple(params),
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# D1 · 定向分发
# --------------------------------------------------------------------------

def deliver(
    database_path: Path | str,
    *,
    case_id: str,
    channel_id: str,
    pack_content_hash: str,
    language: str,
    approval_token: str,
    body: dict[str, Any] | None = None,
    inbox_path: Path | None = None,
) -> dict[str, Any]:
    """Deliver one pack to one channel, once.

    Verifies the token again here even though the hook already did: the hook
    protects the agent path, this protects every path.
    """
    check = verify_token(
        database_path, approval_token, case_id=case_id,
        pack_content_hash=pack_content_hash, channel_id=channel_id,
    )
    if not check["ok"]:
        return {"delivered": False, "error": check["error"], "detail": check["detail"]}

    connection = connect(database_path)
    try:
        channel = connection.execute(
            "SELECT * FROM channels WHERE channel_id=?", (channel_id,)
        ).fetchone()
        if channel is None:
            return {"delivered": False, "error": "UNKNOWN_CHANNEL",
                    "detail": f"渠道未注册: {channel_id}"}

        key = idempotency_key(case_id, channel_id, pack_content_hash, language)
        existing = connection.execute(
            "SELECT * FROM deliveries WHERE idempotency_key=?", (key,)
        ).fetchone()
        if existing is not None:
            return {"delivered": False, "duplicate": True,
                    "delivery_id": existing["delivery_id"],
                    "detail": "同一稿件已送达该渠道，幂等键命中，未重复发送"}

        delivery_id = new_id("dlv")
        audience = int(channel["member_count"])
        with connection:
            connection.execute(
                "INSERT INTO deliveries(delivery_id, idempotency_key, case_id, "
                "channel_id, pack_content_hash, language, audience_size, "
                "approval_token, status, delivered_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (delivery_id, key, case_id, channel_id, pack_content_hash, language,
                 audience, approval_token, "delivered", utc_now()),
            )
            for index in range(audience):
                connection.execute(
                    "INSERT OR IGNORE INTO completions(completion_id, delivery_id, worker_ref) "
                    "VALUES(?,?,?)",
                    (new_id("cp"), delivery_id, f"{channel_id}#{index + 1:03d}"),
                )
    finally:
        connection.close()

    target = Path(inbox_path or MOCK_INBOX)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "delivery_id": delivery_id, "case_id": case_id, "channel_id": channel_id,
            "channel_name": channel["name"], "language": language,
            "audience_size": audience, "pack_content_hash": pack_content_hash,
            "approver": check["token"]["approver"], "delivered_at": utc_now(),
            "body": body or {},
        }, ensure_ascii=False) + "\n")

    return {"delivered": True, "delivery_id": delivery_id,
            "channel": channel["name"], "audience_size": audience,
            "distribution_version": DISTRIBUTION_VERSION}


def withdraw(database_path: Path | str, delivery_id: str, *, reason: str) -> dict[str, Any]:
    """Recall one delivery. Kept as a status change; the record is never deleted."""
    connection = connect(database_path)
    try:
        with connection:
            cursor = connection.execute(
                "UPDATE deliveries SET status='withdrawn', withdrawn_at=? "
                "WHERE delivery_id=? AND status='delivered'",
                (utc_now(), delivery_id),
            )
    finally:
        connection.close()
    return {"withdrawn": cursor.rowcount == 1, "delivery_id": delivery_id, "reason": reason}


# --------------------------------------------------------------------------
# D2 · 完成与测验追踪
# --------------------------------------------------------------------------

def record_completion(
    database_path: Path | str,
    *,
    delivery_id: str,
    worker_ref: str,
    quiz_score: float | None = None,
    quiz_answers: list[int] | None = None,
) -> dict[str, Any]:
    connection = connect(database_path)
    try:
        with connection:
            connection.execute(
                "INSERT INTO completions(completion_id, delivery_id, worker_ref, "
                "completed_at, quiz_score, quiz_answers_json) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(delivery_id, worker_ref) DO UPDATE SET "
                "completed_at=excluded.completed_at, quiz_score=excluded.quiz_score, "
                "quiz_answers_json=excluded.quiz_answers_json",
                (new_id("cp"), delivery_id, worker_ref, utc_now(), quiz_score,
                 json.dumps(quiz_answers or [], ensure_ascii=False)),
            )
    finally:
        connection.close()
    return {"recorded": True, "delivery_id": delivery_id, "worker_ref": worker_ref}


def completion_status(database_path: Path | str, case_id: str) -> dict[str, Any]:
    connection = connect(database_path)
    try:
        rows = connection.execute(
            "SELECT d.delivery_id, d.channel_id, d.audience_size, "
            "  SUM(CASE WHEN c.completed_at IS NOT NULL THEN 1 ELSE 0 END) AS done, "
            "  AVG(c.quiz_score) AS avg_score "
            "FROM deliveries AS d LEFT JOIN completions AS c ON c.delivery_id = d.delivery_id "
            "WHERE d.case_id = ? AND d.status='delivered' GROUP BY d.delivery_id",
            (case_id,),
        ).fetchall()
        outstanding = connection.execute(
            "SELECT c.worker_ref FROM completions AS c "
            "JOIN deliveries AS d ON d.delivery_id = c.delivery_id "
            "WHERE d.case_id=? AND d.status='delivered' AND c.completed_at IS NULL",
            (case_id,),
        ).fetchall()
    finally:
        connection.close()

    total = sum(int(r["audience_size"]) for r in rows)
    done = sum(int(r["done"] or 0) for r in rows)
    scores = [r["avg_score"] for r in rows if r["avg_score"] is not None]
    return {
        "case_id": case_id,
        "deliveries": len(rows),
        "audience": total,
        "completed": done,
        "completion_rate": round(done / total, 3) if total else 0.0,
        "average_quiz_score": round(sum(scores) / len(scores), 3) if scores else None,
        "outstanding": [r["worker_ref"] for r in outstanding],
    }


# --------------------------------------------------------------------------
# D3 · 提醒与升级
# --------------------------------------------------------------------------

def due_reminders(
    database_path: Path | str, *, now: str | None = None
) -> list[dict[str, Any]]:
    """Which cases have people who have not completed, 24h or 72h on."""
    reference = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
    connection = connect(database_path)
    try:
        rows = connection.execute(
            "SELECT DISTINCT case_id, MIN(delivered_at) AS first_delivery "
            "FROM deliveries WHERE status='delivered' GROUP BY case_id"
        ).fetchall()
    finally:
        connection.close()

    due: list[dict[str, Any]] = []
    for row in rows:
        delivered = datetime.fromisoformat(row["first_delivery"])
        elapsed = reference - delivered
        status = completion_status(database_path, row["case_id"])
        if not status["outstanding"]:
            continue
        if elapsed >= timedelta(hours=72):
            stage = REMINDER_T72
        elif elapsed >= timedelta(hours=24):
            stage = REMINDER_T24
        else:
            continue
        due.append({"case_id": row["case_id"], "stage": stage,
                    "outstanding": len(status["outstanding"])})
    return due


def send_reminder_record(
    database_path: Path | str, *, case_id: str, stage: str, outstanding: int
) -> dict[str, Any]:
    """Idempotent by case+stage, so a re-run does not nag twice."""
    key = idempotency_key(case_id, stage)
    connection = connect(database_path)
    try:
        existing = connection.execute(
            "SELECT reminder_id FROM reminders WHERE idempotency_key=?", (key,)
        ).fetchone()
        if existing is not None:
            return {"sent": False, "duplicate": True, "reminder_id": existing["reminder_id"]}
        reminder_id = new_id("rem")
        with connection:
            connection.execute(
                "INSERT INTO reminders(reminder_id, case_id, stage, idempotency_key, "
                "outstanding, sent_at) VALUES(?,?,?,?,?,?)",
                (reminder_id, case_id, stage, key, outstanding, utc_now()),
            )
    finally:
        connection.close()
    return {"sent": True, "reminder_id": reminder_id, "stage": stage,
            "outstanding": outstanding,
            "escalated": stage == REMINDER_T72}


# --------------------------------------------------------------------------
# D4 · 案例闭环　D5 · 周一晨报
# --------------------------------------------------------------------------

def close_case(database_path: Path | str, case_id: str, *, actor: str) -> dict[str, Any]:
    """D4: close the loop and return the full chain the reviewer can audit."""
    status = completion_status(database_path, case_id)
    connection = connect(database_path)
    try:
        case = connection.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
    finally:
        connection.close()
    if case is None:
        return {"closed": False, "error": "UNKNOWN_CASE"}

    if case["state"] == DISTRIBUTED:
        transition(database_path, case_id, TRACKING, actor=actor,
                   reason_code="TRACKING_STARTED")
    transition(database_path, case_id, CLOSED, actor=actor,
               reason_code="CASE_CLOSED",
               detail=f"完成率 {status['completion_rate']}")
    from .workflow_db import case_history

    return {
        "closed": True,
        "case_id": case_id,
        "chain": {
            "incident": case["incident_key"],
            "risk_path": case["risk_assessment_id"],
            "level": case["level"],
            "rule_pack_version": case["rule_pack_version"],
            "pack_content_hash": case["draft_content_hash"],
            "completion": status,
            "transitions": case_history(database_path, case_id),
        },
    }


def monday_digest(database_path: Path | str) -> dict[str, Any]:
    """D5: sent even in a quiet week. 「本周无相关事故」也是价值。"""
    from .screening import weekly_archive_digest

    connection = connect(database_path)
    try:
        by_state = {
            row["state"]: row["n"]
            for row in connection.execute(
                "SELECT state, COUNT(*) AS n FROM cases GROUP BY state"
            )
        }
        deliveries = connection.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(audience_size),0) AS reach "
            "FROM deliveries WHERE status='delivered'"
        ).fetchone()
        completions = connection.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN completed_at IS NOT NULL THEN 1 ELSE 0 END) AS done, "
            "AVG(quiz_score) AS avg_score FROM completions"
        ).fetchone()
        # 与 workflow.OPEN_STATES 中「等待某个人动作」的那部分保持一致。
        # 少算 CLASSIFIED 会让晨报在 5 条红级已路由待处理时写「需要你决定 0 条」，
        # 而告警节流又因为这 5 条而抑制后续同路径告警——两边同时沉默。
        pending = connection.execute(
            "SELECT COUNT(*) AS n FROM cases WHERE state IN "
            "('CLASSIFIED','DRAFTED','SELF_CHECK_OK','NEEDS_HUMAN_WRITER',"
            "'PENDING_APPROVAL','REMINDED','ESCALATED')"
        ).fetchone()
    finally:
        connection.close()

    archive = weekly_archive_digest(database_path, limit=200)
    total = int(completions["total"] or 0)
    done = int(completions["done"] or 0)
    return {
        "generated_at": utc_now(),
        "cases_by_state": by_state,
        "scanned_and_archived": archive["archived"],
        "archive_reasons": archive["by_reason_code"],
        "deliveries": int(deliveries["n"] or 0),
        "workers_reached": int(deliveries["reach"] or 0),
        "completion_rate": round(done / total, 3) if total else None,
        "average_quiz_score": (
            round(float(completions["avg_score"]), 3)
            if completions["avg_score"] is not None else None
        ),
        "awaiting_you": int(pending["n"] or 0),
        "headline": (
            f"本周扫描后静默归档 {archive['archived']} 条，"
            f"需要你决定 {int(pending['n'] or 0)} 条。"
            if archive["archived"] or pending["n"]
            else "本周没有与本厂相关的外部事故。系统正常运行。"
        ),
    }


# --------------------------------------------------------------------------
# Strands 工具（受 A1 守卫保护）
# --------------------------------------------------------------------------

@tool
def distribute_learning_pack(
    case_id: str,
    channel_id: str,
    pack_content_hash: str,
    approval_token: str,
    language: str = "en",
) -> dict:
    """Deliver an approved learning pack to one registered channel."""
    from .config import load_settings

    settings = load_settings()
    return deliver(
        settings.incident_database, case_id=case_id, channel_id=channel_id,
        pack_content_hash=pack_content_hash, language=language,
        approval_token=approval_token,
    )


@tool
def get_completion_status(case_id: str) -> dict:
    """Report delivery reach, completion rate and quiz scores for one case."""
    from .config import load_settings

    return completion_status(load_settings().incident_database, case_id)


@tool
def get_monday_digest() -> dict:
    """Return the Monday morning digest, including quiet weeks."""
    from .config import load_settings

    return monday_digest(load_settings().incident_database)
