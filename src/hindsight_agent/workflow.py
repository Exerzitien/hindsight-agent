"""The full workflow, end to end: W1 through R4 in one run.

This is where the two halves of the project meet. The collection pipeline
(``daily_pipeline``) puts incidents in the database; this module takes them the
rest of the way - screen, assess, route, draft, queue, deliver, track, close -
and records every step as a case transition.

It runs without Milvus or Ollama. Candidate retrieval defaults to the site's own
hazard register: an incident that survived the hard filter is paired with the
risk paths carrying the hazards its narrative actually mentioned. That is a
deterministic retriever, it needs no vector store, and it makes the demo path
free of moving parts. Pass ``candidate_finder`` to swap in the vector matcher
where one is running.

The pipeline is resumable. Every stage is idempotent and every case remembers
where it got to, so a crashed run picks up rather than starting over - and,
because ``ALLOWED_TRANSITIONS`` has no edge into ``DISTRIBUTED`` except from
``APPROVED``, no amount of re-running can deliver something unapproved.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from . import rule_pack as rule_pack_module
from .authorization import issue_token
from .distribution import (
    close_case,
    deliver,
    due_reminders,
    monday_digest,
    register_channels,
    resolve_audience,
    send_reminder_record,
)
from .screening import (
    R_HAZARD_MUTED,
    R_INDUSTRY_NOT_NEIGHBOUR,
    R_NO_NAICS,
    R_NO_SITE_HAZARD_TERM,
    extract_with_confidence,
    hard_filter,
    quarantine_record,
    site_hazard_vocabulary,
)
from .transfer import (
    LEVEL_GREEN,
    LEVEL_NOT_RELEVANT,
    LEVEL_RED,
    LEVEL_YELLOW,
    assess_paths,
    route_incident,
)
from .workflow_db import (
    APPROVED,
    ARCHIVED,
    CLASSIFIED,
    DEDUPED,
    DISTRIBUTED,
    DRAFTED,
    EXTRACTED,
    NEEDS_HUMAN_WRITER,
    PENDING_APPROVAL,
    QUARANTINED,
    REJECTED,
    CLOSED_REJECTED,
    SELF_CHECK_OK,
    PackArtifactError,
    cases_in_state,
    connect,
    ensure_workflow_schema,
    load_pack_artifact,
    open_case,
    record_signal,
    store_pack_artifact,
    transition,
)

WORKFLOW_VERSION = "workflow-v1.0"

CandidateFinder = Callable[[dict[str, Any], list[str]], list[str]]

#: A hazard path that is already escalated does not need escalating again this
#: week. Without this, the site's 14 permanently-open paths turn every matching
#: incident into a RED alert - which is the alert-fatigue failure the risk
#: register warns about, arriving as a feature.
R_PATH_ALREADY_ESCALATED = "PATH_ALREADY_ESCALATED"
#: Over the rule pack's weekly budget, a RED is demoted to YELLOW (batch
#: confirm) rather than dropped. Nothing is hidden; the queue is bounded.
R_OVER_ALERT_BUDGET = "OVER_ALERT_BUDGET"

#: States in which a case still occupies a person's attention.
OPEN_STATES = (
    "CLASSIFIED", "DRAFTED", "SELF_CHECK_OK", "NEEDS_HUMAN_WRITER",
    "PENDING_APPROVAL", "REMINDED", "ESCALATED", "APPROVED", "DISTRIBUTED",
    "TRACKING",
)


def path_already_open(
    incident_database: Path | str, risk_assessment_id: str, *, exclude_case_id: str
) -> str | None:
    """Is another case for this same hazard path already in front of a person?"""
    connection = connect(incident_database)
    try:
        row = connection.execute(
            f"SELECT case_id FROM cases WHERE risk_assessment_id = ? AND case_id != ? "
            f"AND state IN ({','.join('?' * len(OPEN_STATES))}) "
            "ORDER BY updated_at DESC LIMIT 1",
            (risk_assessment_id, exclude_case_id, *OPEN_STATES),
        ).fetchone()
    finally:
        connection.close()
    return row["case_id"] if row else None


@dataclass
class StageCounts:
    considered: int = 0
    quarantined: int = 0
    archived: int = 0
    classified: int = 0
    drafted: int = 0
    needs_writer: int = 0
    queued: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "considered": self.considered,
            "quarantined": self.quarantined,
            "archived": self.archived,
            "classified": self.classified,
            "drafted": self.drafted,
            "needs_human_writer": self.needs_writer,
            "queued_for_approval": self.queued,
        }


def default_candidate_finder(
    factory_database: Path | str,
) -> CandidateFinder:
    """Pair an incident with the site risk paths that share its hazards.

    Deterministic and dependency-free. The vector matcher is a better ranker
    when it is running, but it is not required to answer the only question this
    stage asks: which of our paths could this incident possibly be about?
    """
    connection = sqlite3.connect(str(factory_database))
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT id, hazard_id FROM risk_assessments"
        ).fetchall()
    finally:
        connection.close()
    by_hazard: dict[str, list[str]] = {}
    for row in rows:
        by_hazard.setdefault(row["hazard_id"], []).append(row["id"])

    def finder(_record: dict[str, Any], hazards: list[str]) -> list[str]:
        found: list[str] = []
        for hazard in hazards:
            found.extend(by_hazard.get(hazard, []))
        return sorted(dict.fromkeys(found))

    return finder


def fetch_incidents(
    incident_database: Path | str, *, limit: int = 200, since: str | None = None
) -> list[dict[str, Any]]:
    clause = "WHERE event_date >= ?" if since else ""
    params: tuple[Any, ...] = (since, limit) if since else (limit,)
    connection = sqlite3.connect(str(incident_database))
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT incident_key, event_date, employer, primary_naics, narrative, "
            "source_title, event_title, nature_title, body_part_title, "
            "secondary_source_title, amputation, hospitalized, loss_of_eye "
            f"FROM osha_sir_incidents {clause} ORDER BY event_date DESC LIMIT ?",
            params,
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def screen_and_route(
    *,
    incident_database: Path | str,
    factory_database: Path | str,
    records: list[dict[str, Any]],
    as_of: str | None = None,
    run_id: str | None = None,
    candidate_finder: CandidateFinder | None = None,
) -> dict[str, Any]:
    """W3 → W6 + V1: open cases, screen, assess, route, archive or classify."""
    ensure_workflow_schema(incident_database)
    pack = rule_pack_module.initialize(incident_database)
    vocabulary = site_hazard_vocabulary(factory_database)
    finder = candidate_finder or default_candidate_finder(factory_database)

    counts = StageCounts(considered=len(records))
    routed: list[dict[str, Any]] = []
    alert_budget = int(pack.get("weekly_alert_budget", 5))
    red_issued = 0
    demoted = 0

    for record in records:
        key = str(record["incident_key"])

        # W3 · Case Memory：已处理过的不重开、不重复打扰
        case = open_case(
            incident_database, incident_key=key,
            incident_source_type="osha_sir", run_id=run_id,
        )
        if not case["created"] and case["state"] != "NEW":
            continue
        case_id = case["case_id"]
        transition(incident_database, case_id, DEDUPED, actor="system",
                   reason_code="CONTENT_HASH_NEW", run_id=run_id)

        # W4 · 抽取置信度与隔离
        extraction = extract_with_confidence(record)
        if extraction.quarantined:
            quarantine_record(incident_database, extraction,
                              sampled=counts.quarantined < 10)
            transition(incident_database, case_id, QUARANTINED, actor="system",
                       reason_code=extraction.reason_code or "LOW_CONFIDENCE",
                       detail=json.dumps(extraction.to_dict(), ensure_ascii=False),
                       run_id=run_id, extraction_confidence=extraction.confidence)
            counts.quarantined += 1
            continue
        transition(incident_database, case_id, EXTRACTED, actor="system",
                   reason_code="EXTRACTED", run_id=run_id,
                   extraction_confidence=extraction.confidence)

        # W5a · 确定性硬过滤
        screen = hard_filter(record, vocabulary=vocabulary,
                             rule_pack_payload=pack.payload)
        if not screen.passed:
            transition(incident_database, case_id, CLASSIFIED, actor="system",
                       reason_code=screen.reason_code, run_id=run_id,
                       level=LEVEL_NOT_RELEVANT, rule_pack_version=pack.version)
            transition(incident_database, case_id, ARCHIVED, actor="system",
                       reason_code=screen.reason_code, run_id=run_id,
                       detail=f"行业关系 {screen.industry_relation}")
            counts.archived += 1
            continue

        # W5b/W5c · 候选检索 + 残余路径验证
        candidates = finder(record, screen.matched_hazards)
        certificates = assess_paths(
            factory_database, as_of=as_of, risk_assessment_ids=candidates
        ) if candidates else []
        routing = route_incident(certificates)

        level = routing["level"]
        driving = routing.get("driving_path")

        # 节流：同一条危险路径已经在某个人面前了，第二起事故不再单开一条红
        if level in (LEVEL_RED, LEVEL_YELLOW) and driving:
            open_elsewhere = path_already_open(
                incident_database, driving, exclude_case_id=case_id
            )
            if open_elsewhere:
                transition(incident_database, case_id, CLASSIFIED, actor="system",
                           reason_code=R_PATH_ALREADY_ESCALATED, run_id=run_id,
                           level=LEVEL_GREEN, rule_pack_version=pack.version,
                           risk_assessment_id=driving)
                transition(incident_database, case_id, ARCHIVED, actor="system",
                           reason_code=R_PATH_ALREADY_ESCALATED, run_id=run_id,
                           detail=f"路径 {driving} 已在案例 {open_elsewhere} 中待处理，"
                                  f"本条作为佐证归档，不重复打扰")
                counts.archived += 1
                continue

        # 提醒预算：超出本周上限的红降为黄，进批量确认，不丢弃
        if level == LEVEL_RED and red_issued >= alert_budget:
            level = LEVEL_YELLOW
            demoted += 1

        transition(incident_database, case_id, CLASSIFIED, actor="system",
                   reason_code=(R_OVER_ALERT_BUDGET if level != routing["level"]
                                else f"ROUTED_{level}"),
                   run_id=run_id, level=level, rule_pack_version=pack.version,
                   risk_assessment_id=driving)
        if level == LEVEL_RED:
            red_issued += 1
        routing = {**routing, "level": level}

        # W6 · 静默归档
        if level in (LEVEL_GREEN, LEVEL_NOT_RELEVANT):
            transition(incident_database, case_id, ARCHIVED, actor="system",
                       reason_code=routing["level"], run_id=run_id,
                       detail=routing["explanation"])
            counts.archived += 1
            if level == LEVEL_GREEN:
                record_signal(incident_database, signal_kind="BLOCKED_GREEN",
                              case_id=case_id,
                              feature={"hazard_id": screen.matched_hazards[0],
                                       "level": level})
            continue

        counts.classified += 1
        routed.append({"case_id": case_id, "incident_key": key,
                       "record": record, "routing": routing,
                       "hazards": screen.matched_hazards})
        if level == LEVEL_YELLOW and any(
            c.decision == "UNKNOWN" for c in certificates
        ):
            record_signal(incident_database, signal_kind="UNKNOWN_BLOCKED",
                          case_id=case_id,
                          feature={"hazard_id": screen.matched_hazards[0],
                                   "level": level})

    return {
        "workflow_version": WORKFLOW_VERSION,
        "rule_pack_version": pack.version,
        "counts": counts.to_dict(),
        "routed": routed,
        "levels": {
            level: sum(1 for r in routed if r["routing"]["level"] == level)
            for level in (LEVEL_RED, LEVEL_YELLOW)
        },
        "alert_budget": alert_budget,
        "demoted_over_budget": demoted,
    }


def draft_for_routed(
    *,
    incident_database: Path | str,
    factory_database: Path | str,
    corpus_database: Path | str,
    routed: list[dict[str, Any]],
    generator: Any | None = None,
    run_id: str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """G1–G4 for the cases that survived routing, then queue them for approval."""
    from .content import generate_learning_draft

    drafted, needs_writer = [], []
    for item in routed[:limit]:
        case_id = item["case_id"]
        path_id = item["routing"].get("driving_path")
        if not path_id:
            continue
        transition(incident_database, case_id, DRAFTED, actor="agent",
                   reason_code="DRAFT_STARTED", run_id=run_id)
        result = generate_learning_draft(
            corpus_database=corpus_database,
            factory_database=factory_database,
            incident_database=incident_database,
            risk_assessment_id=path_id,
            incident_key=item["incident_key"],
            incident_narrative=str(item["record"].get("narrative") or ""),
            incident_title=str(item["record"].get("source_title") or ""),
            generator=generator,
        )
        report = result["self_check_reports"][-1]
        if result["needs_human_writer"]:
            transition(incident_database, case_id, NEEDS_HUMAN_WRITER, actor="agent",
                       reason_code="SELF_CHECK_CANNOT_FILL_REQUIRED_SECTIONS",
                       detail=json.dumps(report["missing_required"], ensure_ascii=False),
                       run_id=run_id)
            needs_writer.append(case_id)
            continue
        # 先把正文按哈希存下来，再把案例推到待审批。
        # 顺序不能反：一个人被叫来审批一份取不出来的稿件，是最糟的失败方式。
        content_hash = store_pack_artifact(
            incident_database, case_id=case_id, pack=result["pack"],
            self_check=report,
        )
        transition(incident_database, case_id, SELF_CHECK_OK, actor="agent",
                   reason_code="SELF_CHECK_PASSED",
                   detail=f"删除 {len(report['removed_claim_ids'])} 条无出处主张",
                   run_id=run_id, draft_content_hash=content_hash)
        transition(incident_database, case_id, PENDING_APPROVAL, actor="agent",
                   reason_code="QUEUED_FOR_EHS", run_id=run_id)
        drafted.append({"case_id": case_id, "content_hash": content_hash,
                        "pack": result["pack"]})

    return {"drafted": drafted, "needs_human_writer": needs_writer}


def _exposed_role_for_case(
    incident_database: Path | str,
    case_id: str,
    *,
    factory_database: Path | str | None = None,
) -> str | None:
    """Which role is actually exposed on this case's hazard path.

    ``factory_database`` is a parameter rather than a global lookup on purpose.
    Reaching for the process-wide configured site while the caller handed us a
    different incident database is how a two-site install starts answering one
    site's question with another site's register - and cross-site isolation is
    one of the rule pack's immutable guarantees. It also made this function
    untestable: it failed in a clean checkout because it insisted on a site
    database that only exists on a configured machine.
    """
    connection = connect(incident_database)
    try:
        row = connection.execute(
            "SELECT risk_assessment_id FROM cases WHERE case_id=?", (case_id,)
        ).fetchone()
    finally:
        connection.close()
    if row is None or not row["risk_assessment_id"]:
        return None

    import sqlite3 as _sqlite3

    if factory_database is None:
        from .config import load_settings

        factory_database = load_settings().factory_database
    if not Path(factory_database).exists():
        # 定向分发是优化，不是安全边界：读不到登记册时退回按语言分发，
        # 而不是让整条审批链因为一次可选查询而崩掉。
        return None
    connection = _sqlite3.connect(str(factory_database))
    try:
        found = connection.execute(
            "SELECT exposed_role_id FROM risk_assessments WHERE id=?",
            (row["risk_assessment_id"],),
        ).fetchone()
    finally:
        connection.close()
    return found[0] if found else None


def _body_for_language(pack: dict[str, Any], language: str) -> dict[str, Any]:
    """The half of the approved pack that this channel's language actually reads.

    Both languages render from the same claim list and therefore share one
    content hash, so sending only one language still delivers exactly what was
    approved. The hash travels with it so a recipient can check.
    """
    return {
        "content_hash": pack.get("content_hash"),
        "assessment_code": pack.get("assessment_code"),
        "incident_citation": pack.get("incident_citation"),
        "language": language,
        "briefing": (pack.get("briefing") or {}).get(language),
        "toolbox_talk": (pack.get("toolbox_talk") or {}).get(language),
        "unknowns": pack.get("unknowns"),
    }


def approve_and_distribute(
    *,
    incident_database: Path | str,
    case_id: str,
    pack_content_hash: str,
    approver: str,
    factory_database: Path | str | None = None,
    role_id: str | None = None,
    languages: tuple[str, ...] = ("en", "es"),
    run_id: str | None = None,
) -> dict[str, Any]:
    """A2/A3 → D1: a person approves, a token is issued, delivery happens once."""
    # 取正文在改状态之前。若正文取不出来，这条案例应当原地停在待审批，
    # 而不是变成一条「已批准但没有内容」的记录——那种记录在审计里最难解释。
    artifact = load_pack_artifact(
        incident_database, pack_content_hash, required=False
    )
    if artifact is None:
        raise PackArtifactError(
            f"案例 {case_id} 没有哈希为 {pack_content_hash} 的正文可供分发；"
            f"批准被拒绝。请先重新起草。"
        )

    transition(incident_database, case_id, APPROVED, actor=approver,
               reason_code="EHS_APPROVED", run_id=run_id)

    # D1 是「定向」分发：默认只发给这条危险路径上真正暴露的岗位，而不是全厂。
    # 把焊接事故推给叉车班是最容易造成警报疲劳、也最容易被现场关掉的做法。
    if role_id is None:
        role_id = _exposed_role_for_case(
            incident_database, case_id, factory_database=factory_database
        )
    record_signal(incident_database, signal_kind="APPROVED", case_id=case_id,
                  feature={"approver": approver})

    register_channels(incident_database)
    targets = [
        (language, channel)
        for language in languages
        for channel in resolve_audience(incident_database, role_id=role_id,
                                        language=language)
    ]

    # 定向定到了一个没有任何渠道的岗位，等于批准之后没人收到。
    # 这种失败必须是响的：审批人刚刚为这份内容签了字，如果它悄无声息地
    # 发给了零个人，那么整套问责机制在这一步就断了。这里不退回广播——
    # 广播正是定向分发要避免的——而是留下明确信号，让控制台把它显示出来。
    if not targets:
        record_signal(incident_database, signal_kind="NO_CHANNEL_FOR_ROLE",
                      case_id=case_id, reason_code="APPROVED_BUT_NO_CHANNEL",
                      feature={"role_id": role_id, "languages": list(languages)})
        return {
            "case_id": case_id, "deliveries": [], "delivered_to": 0,
            "warning": "APPROVED_BUT_NO_CHANNEL",
            "detail": (
                f"岗位 {role_id or '（未识别）'} 没有注册任何渠道，"
                f"这份稿件已批准但没有送达任何人。请先在渠道注册表中登记该岗位。"
            ),
        }

    results = []
    for language, channel in targets:
        token = issue_token(
            incident_database, case_id=case_id,
            pack_content_hash=pack_content_hash,
            channel_id=channel["channel_id"],
            audience_size=int(channel["member_count"]), approver=approver,
        )
        results.append(deliver(
            incident_database, case_id=case_id,
            channel_id=channel["channel_id"],
            pack_content_hash=pack_content_hash, language=language,
            approval_token=token,
            body=_body_for_language(artifact["pack"], language),
        ))
    delivered = sum(1 for r in results if r.get("delivered"))
    if delivered:
        transition(incident_database, case_id, DISTRIBUTED, actor="system",
                   reason_code="DELIVERED", run_id=run_id,
                   detail=f"{delivered} 个渠道")
    return {"case_id": case_id, "deliveries": results, "delivered_to": delivered}


def reject_case(
    incident_database: Path | str, case_id: str, *, approver: str, reason: str,
    hazard_id: str | None = None,
) -> dict[str, Any]:
    """A2 驳回 → A3 留痕 → R1 信号池。驳回不改变事实，只进入进化循环。"""
    transition(incident_database, case_id, REJECTED, actor=approver,
               reason_code="EHS_REJECTED", detail=reason)
    record_signal(incident_database, signal_kind="REJECTED", case_id=case_id,
                  reason_code=reason,
                  feature={"hazard_id": hazard_id, "approver": approver})
    transition(incident_database, case_id, CLOSED_REJECTED, actor="system",
               reason_code="CLOSED_AFTER_REJECTION")
    return {"case_id": case_id, "state": CLOSED_REJECTED, "reason": reason}


def run_reminders(incident_database: Path | str, *, now: str | None = None) -> list[dict[str, Any]]:
    """D3: idempotent T+24h / T+72h chase."""
    sent = []
    for item in due_reminders(incident_database, now=now):
        sent.append(send_reminder_record(
            incident_database, case_id=item["case_id"], stage=item["stage"],
            outstanding=item["outstanding"],
        ))
    return sent


def run_full_workflow(
    *,
    incident_database: Path | str,
    factory_database: Path | str,
    corpus_database: Path | str,
    limit: int = 200,
    as_of: str | None = None,
    generator: Any | None = None,
    draft_limit: int = 3,
    run_id: str | None = None,
) -> dict[str, Any]:
    """One call covering W1 storage read through D5. Approval stays human."""
    records = fetch_incidents(incident_database, limit=limit)
    screened = screen_and_route(
        incident_database=incident_database, factory_database=factory_database,
        records=records, as_of=as_of, run_id=run_id,
    )
    drafts = draft_for_routed(
        incident_database=incident_database, factory_database=factory_database,
        corpus_database=corpus_database, routed=screened["routed"],
        generator=generator, run_id=run_id, limit=draft_limit,
    ) if generator is not None else {"drafted": [], "needs_human_writer": [],
                                     "skipped": "no generator configured"}
    reminders = run_reminders(incident_database)
    return {
        "workflow_version": WORKFLOW_VERSION,
        "screening": screened["counts"],
        "levels": screened["levels"],
        "alert_budget": screened["alert_budget"],
        "demoted_over_budget": screened["demoted_over_budget"],
        "rule_pack_version": screened["rule_pack_version"],
        "drafts": {"drafted": len(drafts.get("drafted", [])),
                   "needs_human_writer": len(drafts.get("needs_human_writer", []))},
        "reminders_sent": len(reminders),
        "awaiting_approval": len(cases_in_state(incident_database, [PENDING_APPROVAL])),
        "digest": monday_digest(incident_database),
    }


def main() -> int:
    import argparse

    from .config import load_settings

    parser = argparse.ArgumentParser(
        description="完整工作流 W1→D5：筛选、判定、路由、归档、队列、提醒、晨报。"
    )
    parser.add_argument("--limit", type=int, default=500, help="本次考察的事故条数")
    parser.add_argument("--as-of", default=None, help="控制有效期截止日 YYYY-MM-DD")
    parser.add_argument("--draft", action="store_true", help="对红/黄级调用模型起草（需模型服务）")
    parser.add_argument("--out", default=None, help="把完整结果写成 JSON")
    args = parser.parse_args()

    settings = load_settings()
    generator = None
    if args.draft:
        from .content import build_generator

        generator = build_generator()

    result = run_full_workflow(
        incident_database=settings.incident_database,
        factory_database=settings.factory_database,
        corpus_database=settings.project_root / "data" / "corpus" / "grounding.db",
        limit=args.limit, as_of=args.as_of, generator=generator, run_id="cli",
    )
    counts = result["screening"]
    print(f"考察 {counts['considered']} 条事故　规则包 {result['rule_pack_version']}")
    print(f"  隔离待检 {counts['quarantined']}　静默归档 {counts['archived']}"
          f"　进入人工队列 {counts['classified']}")
    print(f"  分级 {result['levels']}　提醒预算 {result['alert_budget']}"
          f"　超预算降级 {result['demoted_over_budget']}")
    print(f"  待你决定 {result['awaiting_approval']} 条　本轮提醒 {result['reminders_sent']} 次")
    print(f"\n{result['digest']['headline']}")
    if args.out:
        Path(args.out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"完整结果已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
