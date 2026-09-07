"""Tests for the case machine, the rule pack, screening, authorization and delivery.

The properties worth pinning are the ones a deadline erodes first:

* nothing reaches ``DISTRIBUTED`` except from ``APPROVED``
* a re-run opens no second case and sends no second message
* an accepted rule-pack diff can never switch off the safety kernel
* an edited pack invalidates the approval that was given for it
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hindsight_agent import rule_pack as rp
from hindsight_agent.authorization import (
    E_CASE_NOT_APPROVED,
    E_EXPIRED,
    E_NO_TOKEN,
    E_PACK_MISMATCH,
    DistributionGuard,
    issue_token,
    verify_token,
)
from hindsight_agent.distribution import (
    completion_status,
    deliver,
    due_reminders,
    monday_digest,
    record_completion,
    register_channels,
    resolve_audience,
    send_reminder_record,
)
from hindsight_agent.screening import (
    CONFIDENCE_FLOOR,
    R_INDUSTRY_NOT_NEIGHBOUR,
    R_NO_SITE_HAZARD_TERM,
    R_PASS,
    extract_with_confidence,
    hard_filter,
    industry_relation,
)
from hindsight_agent.workflow import (
    R_PATH_ALREADY_ESCALATED,
    screen_and_route,
)
from hindsight_agent.workflow_db import (
    ALLOWED_TRANSITIONS,
    APPROVED,
    ARCHIVED,
    CLASSIFIED,
    DEDUPED,
    DISTRIBUTED,
    EXTRACTED,
    NEW,
    PENDING_APPROVAL,
    TransitionError,
    case_history,
    ensure_workflow_schema,
    open_case,
    record_signal,
    transition,
)

FACTORY_SQL = """
CREATE TABLE equipment(id TEXT PRIMARY KEY, name TEXT, area_id TEXT, factory_id TEXT, active INTEGER DEFAULT 1, equipment_type TEXT);
CREATE TABLE areas(id TEXT PRIMARY KEY, name TEXT);
CREATE TABLE factories(id TEXT PRIMARY KEY, name TEXT, naics TEXT);
CREATE TABLE tasks(id TEXT PRIMARY KEY, name TEXT);
CREATE TABLE hazards(id TEXT PRIMARY KEY, name TEXT, category TEXT);
CREATE TABLE worker_roles(id TEXT PRIMARY KEY, name TEXT);
CREATE TABLE safety_controls(id TEXT PRIMARY KEY, name TEXT, control_type TEXT,
    hierarchy_code TEXT, performance_requirement TEXT, verification_method TEXT);
CREATE TABLE risk_assessments(id TEXT PRIMARY KEY, assessment_code TEXT,
    equipment_id TEXT, task_id TEXT, hazard_id TEXT, exposed_role_id TEXT,
    hazardous_situation TEXT, initial_severity INTEGER, initial_score INTEGER,
    initial_level TEXT, residual_score INTEGER, residual_level TEXT,
    risk_accepted INTEGER, status TEXT);
CREATE TABLE risk_assessment_controls(risk_assessment_id TEXT, safety_control_id TEXT,
    implementation_status TEXT, effectiveness_rating INTEGER);
CREATE TABLE control_verifications(id TEXT PRIMARY KEY, verification_code TEXT,
    equipment_id TEXT, safety_control_id TEXT, verification_date TEXT, result TEXT,
    finding TEXT, evidence_reference TEXT, next_due_on TEXT);
CREATE TABLE corrective_actions(id TEXT PRIMARY KEY, action_code TEXT,
    risk_assessment_id TEXT, title TEXT, priority TEXT, owner_role_id TEXT,
    due_on TEXT, status TEXT);
INSERT INTO factories VALUES('f1','Demo Works','332999');
INSERT INTO areas VALUES('a1','Stamping');
INSERT INTO equipment VALUES('press-01','Mechanical press 01','a1','f1',1,'press');
INSERT INTO tasks VALUES('t1','Clear jam');
INSERT INTO hazards VALUES('haz-crush','Crush','mechanical');
INSERT INTO worker_roles VALUES('role-op','Press operator');
INSERT INTO safety_controls VALUES('control-guard','Fixed guard','engineering','engineering','Fixed','visual');
INSERT INTO risk_assessments VALUES('risk-1','RA-001','press-01','t1','haz-crush','role-op',
   'Hand between dies',5,20,'high',9,'medium',0,'action_required');
"""

INCIDENT_SQL = """
CREATE TABLE osha_sir_incidents(incident_key TEXT PRIMARY KEY, event_date TEXT,
    employer TEXT, primary_naics TEXT, narrative TEXT, source_title TEXT,
    event_title TEXT, nature_title TEXT, body_part_title TEXT,
    secondary_source_title TEXT, amputation INT, hospitalized INT, loss_of_eye INT);
"""

GOOD_NARRATIVE = (
    "An employee was clearing a machine jam on a mechanical press when the ram "
    "cycled and crushed the right hand between the dies, causing an amputation."
)


@pytest.fixture()
def factory(tmp_path: Path) -> Path:
    path = tmp_path / "factory.db"
    connection = sqlite3.connect(path)
    connection.executescript(FACTORY_SQL)
    connection.commit()
    connection.close()
    return path


@pytest.fixture()
def incidents(tmp_path: Path) -> Path:
    path = tmp_path / "incidents.db"
    connection = sqlite3.connect(path)
    connection.executescript(INCIDENT_SQL)
    for index in range(1, 6):
        connection.execute(
            "INSERT INTO osha_sir_incidents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"inc-{index}", "2026-09-01", "Acme", "332999", GOOD_NARRATIVE,
             "Press", "Caught in running equipment", "Amputation", "Finger",
             "Press", 1, 1, 0),
        )
    connection.commit()
    connection.close()
    ensure_workflow_schema(path)
    return path


def _records(incidents: Path) -> list[dict]:
    connection = sqlite3.connect(incidents)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in connection.execute("SELECT * FROM osha_sir_incidents")]
    finally:
        connection.close()


# --------------------------------------------------------------------------
# W3 · 案例状态机
# --------------------------------------------------------------------------

def test_nothing_reaches_distributed_except_from_approved():
    """The schema-level expression of 未批不触达."""
    sources = [s for s, targets in ALLOWED_TRANSITIONS.items() if DISTRIBUTED in targets]
    assert sources == [APPROVED]


def test_there_is_no_auto_send_state():
    assert "AUTO_SEND" not in ALLOWED_TRANSITIONS


def test_transition_refuses_an_undeclared_edge(incidents):
    case = open_case(incidents, incident_key="inc-1", incident_source_type="osha_sir")
    with pytest.raises(TransitionError, match="不允许的状态转移"):
        transition(incidents, case["case_id"], DISTRIBUTED, actor="agent",
                   reason_code="TRY_TO_SKIP_APPROVAL")


def test_terminal_case_is_read_only(incidents):
    case = open_case(incidents, incident_key="inc-1", incident_source_type="osha_sir")
    cid = case["case_id"]
    transition(incidents, cid, DEDUPED, actor="s", reason_code="X")
    transition(incidents, cid, EXTRACTED, actor="s", reason_code="X")
    transition(incidents, cid, CLASSIFIED, actor="s", reason_code="X")
    transition(incidents, cid, ARCHIVED, actor="s", reason_code="X")
    with pytest.raises(TransitionError, match="终态"):
        transition(incidents, cid, CLASSIFIED, actor="s", reason_code="X")


def test_reopening_the_same_incident_returns_the_same_case(incidents):
    first = open_case(incidents, incident_key="inc-1", incident_source_type="osha_sir")
    transition(incidents, first["case_id"], DEDUPED, actor="s", reason_code="X",
               risk_assessment_id="risk-1")
    second = open_case(incidents, incident_key="inc-1", incident_source_type="osha_sir")
    assert second["created"] is False
    assert second["case_id"] == first["case_id"]


def test_history_is_ordered_by_sequence_not_timestamp(incidents):
    """Second-resolution timestamps tie; the audit chain must still read in order."""
    case = open_case(incidents, incident_key="inc-1", incident_source_type="osha_sir")
    cid = case["case_id"]
    for state in (DEDUPED, EXTRACTED, CLASSIFIED):
        transition(incidents, cid, state, actor="s", reason_code="X")
    states = [row["to_state"] for row in case_history(incidents, cid)]
    assert states == [NEW, DEDUPED, EXTRACTED, CLASSIFIED]


# --------------------------------------------------------------------------
# W4 · 抽取置信度　W5a · 硬过滤
# --------------------------------------------------------------------------

def test_a_full_record_scores_above_the_floor():
    record = {"incident_key": "x", "narrative": GOOD_NARRATIVE,
              "primary_naics": "332999", "event_title": "Caught in running equipment",
              "nature_title": "Amputation", "body_part_title": "Finger",
              "source_title": "Press"}
    result = extract_with_confidence(record)
    assert result.confidence > CONFIDENCE_FLOOR
    assert result.quarantined is False


def test_a_stub_narrative_is_quarantined():
    result = extract_with_confidence({"incident_key": "x", "narrative": "Hurt."})
    assert result.quarantined is True
    assert result.reason_code == "NARRATIVE_TOO_SHORT"


def test_narrative_disagreeing_with_its_own_label_loses_score():
    base = {"incident_key": "x", "narrative": GOOD_NARRATIVE, "primary_naics": "332999",
            "nature_title": "Amputation", "body_part_title": "Finger", "source_title": "Press"}
    agreeing = extract_with_confidence({**base, "event_title": "Crushed by machine"})
    disagreeing = extract_with_confidence({**base, "event_title": "Drowning submersion"})
    assert agreeing.weak_label_agrees is True
    assert disagreeing.weak_label_agrees is False
    assert disagreeing.confidence < agreeing.confidence


@pytest.mark.parametrize("naics, expected", [
    ("332999", "exact"), ("331110", "neighbour"), ("722511", "unrelated"), (None, "unknown"),
])
def test_industry_relation_is_named_not_scored(naics, expected):
    assert industry_relation(naics, ["332", "331", "333"]) == expected


def test_hard_filter_rejects_an_unrelated_industry(factory):
    from hindsight_agent.screening import site_hazard_vocabulary

    result = hard_filter(
        {"incident_key": "x", "primary_naics": "722511", "narrative": GOOD_NARRATIVE},
        vocabulary=site_hazard_vocabulary(factory),
        rule_pack_payload=rp.DEFAULT_PAYLOAD,
    )
    assert result.passed is False
    assert result.reason_code == R_INDUSTRY_NOT_NEIGHBOUR


def test_hard_filter_rejects_a_hazard_this_site_never_registered(factory):
    from hindsight_agent.screening import site_hazard_vocabulary

    result = hard_filter(
        {"incident_key": "x", "primary_naics": "332999",
         "narrative": "A worker slipped on a wet floor in the corridor."},
        vocabulary=site_hazard_vocabulary(factory),
        rule_pack_payload=rp.DEFAULT_PAYLOAD,
    )
    assert result.passed is False
    assert result.reason_code == R_NO_SITE_HAZARD_TERM


def test_hard_filter_passes_a_relevant_incident(factory):
    from hindsight_agent.screening import site_hazard_vocabulary

    result = hard_filter(
        {"incident_key": "x", "primary_naics": "332999", "narrative": GOOD_NARRATIVE},
        vocabulary=site_hazard_vocabulary(factory),
        rule_pack_payload=rp.DEFAULT_PAYLOAD,
    )
    assert result.passed is True
    assert result.reason_code == R_PASS
    assert "haz-crush" in result.matched_hazards


# --------------------------------------------------------------------------
# S3 · R2–R4 规则包
# --------------------------------------------------------------------------

def test_rule_pack_initialises_once_and_is_conservative(incidents):
    first = rp.initialize(incidents)
    second = rp.initialize(incidents)
    assert first.version == second.version == "rp-v1"
    assert first.get("muted_hazards") == []
    assert first.get("weekly_alert_budget") == 5


def test_a_diff_may_not_touch_the_safety_kernel(incidents):
    pack = rp.initialize(incidents)
    for key in ("require_human_approval_before_distribution",
                "require_evidence_for_every_claim", "unknown_never_routes_red"):
        with pytest.raises(rp.RulePackError, match="不在可调项内"):
            rp._create_diff(incidents, pack, change_kind="disable",
                            change={"key": key, "set": False}, evidence={})


def test_two_rejections_propose_a_mute_and_a_person_activates_it(incidents):
    rp.initialize(incidents)
    for _ in range(2):
        record_signal(incidents, signal_kind="REJECTED",
                      feature={"hazard_id": "haz-crush"}, reason_code="not applicable")
    proposals = rp.propose_diffs(incidents)
    assert len(proposals) == 1
    assert proposals[0]["change"] == {"key": "muted_hazards", "add": "haz-crush"}
    # A proposal changes nothing on its own.
    assert rp.active_pack(incidents).get("muted_hazards") == []

    rp.decide_diff(incidents, proposals[0]["diff_id"], decision="accepted", decided_by="EHS")
    assert rp.active_pack(incidents).get("muted_hazards") == ["haz-crush"]


def test_a_rule_pack_change_requires_a_named_approver(incidents):
    rp.initialize(incidents)
    record_signal(incidents, signal_kind="REJECTED", feature={"hazard_id": "haz-crush"})
    record_signal(incidents, signal_kind="REJECTED", feature={"hazard_id": "haz-crush"})
    diff = rp.propose_diffs(incidents)[0]
    with pytest.raises(rp.RulePackError, match="匿名"):
        rp.decide_diff(incidents, diff["diff_id"], decision="accepted", decided_by="  ")


def test_rollback_restores_the_previous_pack_and_keeps_history(incidents):
    rp.initialize(incidents)
    for _ in range(2):
        record_signal(incidents, signal_kind="REJECTED", feature={"hazard_id": "haz-crush"})
    diff = rp.propose_diffs(incidents)[0]
    rp.decide_diff(incidents, diff["diff_id"], decision="accepted", decided_by="EHS")
    rp.rollback(incidents, to_version="rp-v1", decided_by="EHS")
    assert rp.active_pack(incidents).version == "rp-v1"
    assert rp.active_pack(incidents).get("muted_hazards") == []
    assert len(rp.version_history(incidents)) == 2  # nothing deleted


# --------------------------------------------------------------------------
# A1 · 授权边界
# --------------------------------------------------------------------------

def _approved_case(incidents: Path) -> str:
    case = open_case(incidents, incident_key="inc-1", incident_source_type="osha_sir")
    cid = case["case_id"]
    for state in (DEDUPED, EXTRACTED, CLASSIFIED, "DRAFTED", "SELF_CHECK_OK",
                  PENDING_APPROVAL, APPROVED):
        transition(incidents, cid, state, actor="test", reason_code="SETUP")
    return cid


def test_guard_cancels_a_call_with_no_token(incidents):
    guard = DistributionGuard(incidents)
    denial = guard.evaluate("distribute_learning_pack", {"case_id": "case-x"})
    assert denial is not None
    assert denial.error == E_CASE_NOT_APPROVED


def test_guard_cancels_an_approved_case_without_a_token(incidents):
    cid = _approved_case(incidents)
    guard = DistributionGuard(incidents)
    denial = guard.evaluate("distribute_learning_pack", {"case_id": cid})
    assert denial.error == E_NO_TOKEN


def test_editing_the_pack_invalidates_its_approval(incidents):
    cid = _approved_case(incidents)
    token = issue_token(incidents, case_id=cid, pack_content_hash="hash-a",
                        channel_id="ch-ehs", audience_size=3, approver="EHS")
    guard = DistributionGuard(incidents)
    denial = guard.evaluate("distribute_learning_pack", {
        "case_id": cid, "channel_id": "ch-ehs",
        "pack_content_hash": "hash-b", "approval_token": token,
    })
    assert denial.error == E_PACK_MISMATCH


def test_a_valid_token_is_allowed_through(incidents):
    cid = _approved_case(incidents)
    token = issue_token(incidents, case_id=cid, pack_content_hash="hash-a",
                        channel_id="ch-ehs", audience_size=3, approver="EHS")
    guard = DistributionGuard(incidents)
    assert guard.evaluate("distribute_learning_pack", {
        "case_id": cid, "channel_id": "ch-ehs",
        "pack_content_hash": "hash-a", "approval_token": token,
    }) is None


def test_an_expired_token_is_denied(incidents):
    cid = _approved_case(incidents)
    token = issue_token(incidents, case_id=cid, pack_content_hash="h",
                        channel_id="ch-ehs", audience_size=1, approver="EHS",
                        ttl_minutes=-1)
    assert verify_token(incidents, token)["error"] == E_EXPIRED


def test_unprotected_tools_are_not_touched(incidents):
    guard = DistributionGuard(incidents)
    assert guard.evaluate("get_pending_decisions", {}) is None


# --------------------------------------------------------------------------
# S4 · D1–D5 分发与追踪
# --------------------------------------------------------------------------

def test_delivery_is_idempotent(incidents, tmp_path):
    cid = _approved_case(incidents)
    register_channels(incidents)
    token = issue_token(incidents, case_id=cid, pack_content_hash="h",
                        channel_id="ch-ehs", audience_size=3, approver="EHS")
    inbox = tmp_path / "inbox.jsonl"
    kwargs = dict(case_id=cid, channel_id="ch-ehs", pack_content_hash="h",
                  language="en", approval_token=token, inbox_path=inbox)
    first = deliver(incidents, **kwargs)
    second = deliver(incidents, **kwargs)
    assert first["delivered"] is True
    assert second["delivered"] is False and second["duplicate"] is True
    assert inbox.read_text(encoding="utf-8").count("\n") == 1


def test_delivery_without_a_valid_token_is_refused(incidents, tmp_path):
    cid = _approved_case(incidents)
    register_channels(incidents)
    result = deliver(incidents, case_id=cid, channel_id="ch-ehs",
                     pack_content_hash="h", language="en", approval_token="",
                     inbox_path=tmp_path / "inbox.jsonl")
    assert result["delivered"] is False
    assert result["error"] == E_NO_TOKEN


def test_targeting_selects_by_role_and_language(incidents):
    register_channels(incidents)
    spanish = resolve_audience(incidents, role_id="role-press-operator", language="es")
    assert [c["channel_id"] for c in spanish] == ["ch-stamping-a-es"]


def test_completion_rate_and_reminder_are_idempotent(incidents, tmp_path):
    cid = _approved_case(incidents)
    register_channels(incidents)
    token = issue_token(incidents, case_id=cid, pack_content_hash="h",
                        channel_id="ch-ehs", audience_size=3, approver="EHS")
    delivery = deliver(incidents, case_id=cid, channel_id="ch-ehs",
                       pack_content_hash="h", language="en", approval_token=token,
                       inbox_path=tmp_path / "i.jsonl")
    record_completion(incidents, delivery_id=delivery["delivery_id"],
                      worker_ref="ch-ehs#001", quiz_score=1.0)
    status = completion_status(incidents, cid)
    assert status["audience"] == 3 and status["completed"] == 1
    assert status["completion_rate"] == pytest.approx(1 / 3, abs=1e-3)

    first = send_reminder_record(incidents, case_id=cid, stage="T+24h", outstanding=2)
    second = send_reminder_record(incidents, case_id=cid, stage="T+24h", outstanding=2)
    assert first["sent"] is True and second["duplicate"] is True


def test_monday_digest_speaks_even_in_a_quiet_week(incidents):
    digest = monday_digest(incidents)
    assert "本周没有与本厂相关的外部事故" in digest["headline"]


# --------------------------------------------------------------------------
# 端到端：节流与预算
# --------------------------------------------------------------------------

def test_one_open_path_does_not_produce_five_alerts(factory, incidents):
    """Five incidents on one open hazard path must not become five red alerts."""
    result = screen_and_route(
        incident_database=incidents, factory_database=factory,
        records=_records(incidents), as_of="2026-09-07", run_id="t",
    )
    assert result["counts"]["classified"] == 1
    archived_reasons = [
        row[0]
        for row in sqlite3.connect(incidents).execute(
            "SELECT reason_code FROM case_transitions WHERE to_state='ARCHIVED'"
        )
    ]
    assert archived_reasons.count(R_PATH_ALREADY_ESCALATED) == 4


def test_rerunning_the_same_batch_changes_nothing(factory, incidents):
    kwargs = dict(incident_database=incidents, factory_database=factory,
                  records=_records(incidents), as_of="2026-09-07")
    screen_and_route(**kwargs, run_id="first")
    before = sqlite3.connect(incidents).execute("SELECT COUNT(*) FROM cases").fetchone()[0]
    second = screen_and_route(**kwargs, run_id="second")
    after = sqlite3.connect(incidents).execute("SELECT COUNT(*) FROM cases").fetchone()[0]
    assert before == after
    assert second["counts"]["classified"] == 0
