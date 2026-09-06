"""Tests for the residual-path assessment and the V1 four-level routing.

The five precedence rules are the whole point of the module, so each one gets a
fixture that isolates it. The routing tests then pin the one rule that is easy
to erode under deadline pressure: an UNKNOWN path never reaches RED, however
severe it is.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hindsight_agent.transfer import (
    ACTIONABLE,
    LEVEL_GREEN,
    LEVEL_NOT_RELEVANT,
    LEVEL_RED,
    LEVEL_YELLOW,
    NO_ACTIONABLE_TRANSFER,
    R_ALL_CONTROLS_LAPSED,
    R_BLOCKED_BY_VERIFIED_CONTROL,
    R_CONTROL_NEVER_VERIFIED,
    R_NO_CONTROL_RECORDED,
    R_SITE_DECLARES_UNCONTROLLED,
    UNKNOWN,
    assess_paths,
    route_all_paths,
    route_incident,
    route_path,
    summarize,
    weekly_digest,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
AS_OF = "2026-09-06"

SCHEMA = """
CREATE TABLE equipment(id TEXT PRIMARY KEY, name TEXT, area_id TEXT, factory_id TEXT);
CREATE TABLE tasks(id TEXT PRIMARY KEY, name TEXT);
CREATE TABLE hazards(id TEXT PRIMARY KEY, name TEXT);
CREATE TABLE worker_roles(id TEXT PRIMARY KEY, name TEXT);
CREATE TABLE safety_controls(id TEXT PRIMARY KEY, name TEXT, control_type TEXT,
    hierarchy_code TEXT, performance_requirement TEXT);
CREATE TABLE risk_assessments(id TEXT PRIMARY KEY, assessment_code TEXT,
    equipment_id TEXT, task_id TEXT, hazard_id TEXT, exposed_role_id TEXT,
    hazardous_situation TEXT, initial_severity INTEGER, initial_score INTEGER,
    initial_level TEXT, residual_score INTEGER, residual_level TEXT,
    risk_accepted INTEGER, status TEXT);
CREATE TABLE risk_assessment_controls(risk_assessment_id TEXT, safety_control_id TEXT,
    implementation_status TEXT, effectiveness_rating INTEGER);
CREATE TABLE control_verifications(id TEXT PRIMARY KEY, verification_code TEXT,
    equipment_id TEXT, safety_control_id TEXT, verification_date TEXT,
    result TEXT, finding TEXT, evidence_reference TEXT, next_due_on TEXT);
CREATE TABLE corrective_actions(id TEXT PRIMARY KEY, action_code TEXT,
    risk_assessment_id TEXT, title TEXT, priority TEXT, owner_role_id TEXT,
    due_on TEXT, status TEXT);
INSERT INTO equipment VALUES('press-01','Mechanical press 01','area-1','f1');
INSERT INTO tasks VALUES('task-jam','Clear jam');
INSERT INTO hazards VALUES('haz-crush','Crush');
INSERT INTO worker_roles VALUES('role-op','Press operator');
INSERT INTO safety_controls VALUES
  ('control-guard','Fixed guard','engineering','engineering','Guard fixed'),
  ('control-loto','Lockout tagout','administrative','administrative','Energy isolated');
"""


def _make(tmp_path: Path, name: str, **kwargs) -> Path:
    """Build a one-path factory database with the wiring each rule needs."""
    database = tmp_path / f"{name}.db"
    connection = sqlite3.connect(database)
    connection.executescript(SCHEMA)
    connection.execute(
        "INSERT INTO risk_assessments VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "risk-1", "RA-001", "press-01", "task-jam", "haz-crush", "role-op",
            "Hand between dies",
            kwargs.get("severity", 5), 20, "high", 9, "medium",
            kwargs.get("risk_accepted", 1), kwargs.get("status", "approved"),
        ),
    )
    for control in kwargs.get("controls", []):
        connection.execute(
            "INSERT INTO risk_assessment_controls VALUES(?,?,?,?)",
            ("risk-1", control, kwargs.get("implementation", "implemented"), 4),
        )
    for index, (control, result, due) in enumerate(kwargs.get("verifications", [])):
        connection.execute(
            "INSERT INTO control_verifications VALUES(?,?,?,?,?,?,?,?,?)",
            (f"v{index}", f"CV-{index}", "press-01", control, "2026-08-15",
             result, "finding text", f"EV-{index}", due),
        )
    if kwargs.get("open_action"):
        connection.execute(
            "INSERT INTO corrective_actions VALUES(?,?,?,?,?,?,?,?)",
            ("a1", "CA-001", "risk-1", "Fit interlock", "high", "role-op",
             "2026-10-01", "open"),
        )
    connection.commit()
    connection.close()
    return database


def _one(database: Path):
    certificates = assess_paths(database, as_of=AS_OF)
    assert len(certificates) == 1
    return certificates[0]


# --------------------------------------------------------------------------
# The five precedence rules
# --------------------------------------------------------------------------

def test_rule1_site_declaring_the_risk_unaccepted_beats_a_verified_guard(tmp_path):
    """A verified control cannot block a path the site itself calls open."""
    database = _make(
        tmp_path, "r1", risk_accepted=0, status="action_required",
        controls=["control-guard"],
        verifications=[("control-guard", "pass", "2027-01-01")],
    )
    certificate = _one(database)
    assert certificate.decision == ACTIONABLE
    assert certificate.reason_code == R_SITE_DECLARES_UNCONTROLLED
    # The blocking cut is still reported - the reviewer sees what was checked.
    assert [c.control_name for c in certificate.blocking_cut] == ["Fixed guard"]


def test_rule1_open_corrective_action_also_forces_actionable(tmp_path):
    database = _make(
        tmp_path, "r1b", controls=["control-guard"],
        verifications=[("control-guard", "pass", "2027-01-01")], open_action=True,
    )
    certificate = _one(database)
    assert certificate.decision == ACTIONABLE
    assert certificate.reason_code == R_SITE_DECLARES_UNCONTROLLED
    assert certificate.open_corrective_actions[0]["action_code"] == "CA-001"


def test_rule2_a_path_with_no_recorded_control_is_unknown_not_actionable(tmp_path):
    """No control on record means we do not know, not that none exists."""
    database = _make(tmp_path, "r2", controls=[])
    certificate = _one(database)
    assert certificate.decision == UNKNOWN
    assert certificate.reason_code == R_NO_CONTROL_RECORDED
    assert certificate.missing_facts


def test_rule3_one_verified_control_blocks_the_path(tmp_path):
    database = _make(
        tmp_path, "r3", controls=["control-guard", "control-loto"],
        verifications=[("control-guard", "pass", "2027-01-01"),
                       ("control-loto", "pass", "2020-01-01")],
    )
    certificate = _one(database)
    assert certificate.decision == NO_ACTIONABLE_TRANSFER
    assert certificate.reason_code == R_BLOCKED_BY_VERIFIED_CONTROL
    assert [c.control_name for c in certificate.blocking_cut] == ["Fixed guard"]
    # The lapsed one is reported as a contradiction, not hidden.
    assert any("到期" in text for text in certificate.contradictions)


def test_rule4_a_never_verified_control_is_unknown(tmp_path):
    database = _make(tmp_path, "r4", controls=["control-guard"], verifications=[])
    certificate = _one(database)
    assert certificate.decision == UNKNOWN
    assert certificate.reason_code == R_CONTROL_NEVER_VERIFIED
    assert "Fixed guard" in certificate.missing_facts[0]


def test_rule5_all_controls_lapsed_leaves_the_path_open(tmp_path):
    database = _make(
        tmp_path, "r5", controls=["control-guard"],
        verifications=[("control-guard", "pass", "2026-09-05")],  # due yesterday
    )
    certificate = _one(database)
    assert certificate.decision == ACTIONABLE
    assert certificate.reason_code == R_ALL_CONTROLS_LAPSED


def test_a_conditional_verification_does_not_block(tmp_path):
    database = _make(
        tmp_path, "cond", controls=["control-guard"],
        verifications=[("control-guard", "conditional", "2027-01-01")],
    )
    certificate = _one(database)
    assert certificate.decision == ACTIONABLE
    assert certificate.blocking_cut == []


def test_expiry_is_evaluated_against_the_given_cut_off(tmp_path):
    """Same data, two dates, two answers - so a demo can be reproduced."""
    database = _make(
        tmp_path, "asof", controls=["control-guard"],
        verifications=[("control-guard", "pass", "2026-09-10")],
    )
    before = assess_paths(database, as_of="2026-09-06")[0]
    after = assess_paths(database, as_of="2026-09-11")[0]
    assert before.decision == NO_ACTIONABLE_TRANSFER
    assert after.decision == ACTIONABLE


# --------------------------------------------------------------------------
# V1 routing
# --------------------------------------------------------------------------

def test_actionable_high_severity_routes_red(tmp_path):
    database = _make(tmp_path, "red", severity=5, risk_accepted=0)
    assert route_path(_one(database)).level == LEVEL_RED


def test_actionable_low_severity_routes_yellow(tmp_path):
    database = _make(tmp_path, "yel", severity=3, risk_accepted=0)
    assert route_path(_one(database)).level == LEVEL_YELLOW


@pytest.mark.parametrize("severity", [1, 3, 4, 5])
def test_unknown_never_routes_red_however_severe(tmp_path, severity):
    """高严重 × 低置信强制降级为黄 - implemented as a hard rule, not a score."""
    database = _make(tmp_path, f"unk{severity}", severity=severity, controls=[])
    decision = route_path(_one(database))
    assert decision.level == LEVEL_YELLOW
    assert decision.requires_human is True


def test_blocked_path_routes_green_and_asks_for_nobody(tmp_path):
    database = _make(
        tmp_path, "grn", controls=["control-guard"],
        verifications=[("control-guard", "pass", "2027-01-01")],
    )
    decision = route_path(_one(database))
    assert decision.level == LEVEL_GREEN
    assert decision.requires_human is False
    assert "Fixed guard" in decision.explanation


def test_incident_with_no_candidate_paths_is_not_relevant():
    routing = route_incident([])
    assert routing["level"] == LEVEL_NOT_RELEVANT
    assert routing["requires_human"] is False


def test_incident_takes_the_worst_level_among_its_paths(tmp_path):
    red = _one(_make(tmp_path, "i-red", severity=5, risk_accepted=0))
    green = _one(
        _make(tmp_path, "i-green", controls=["control-guard"],
              verifications=[("control-guard", "pass", "2027-01-01")])
    )
    routing = route_incident([green, red])
    assert routing["level"] == LEVEL_RED
    assert routing["requires_human"] is True
    # Both paths stay visible; the cleared one is evidence too.
    assert len(routing["paths"]) == 2
    assert routing["counts"][LEVEL_GREEN] == 1


def test_weekly_digest_carries_only_the_silent_levels(tmp_path):
    database = _make(
        tmp_path, "digest", controls=["control-guard"],
        verifications=[("control-guard", "pass", "2027-01-01")],
    )
    routing = route_all_paths(database, as_of=AS_OF)
    lines = weekly_digest(routing)
    assert len(lines) == 1
    assert lines[0].startswith(f"[{LEVEL_GREEN}]")


# --------------------------------------------------------------------------
# The shipped factory register (skipped on a fresh clone)
# --------------------------------------------------------------------------

FACTORY = REPO_ROOT / "data" / "factory" / "factory_zh.db"


@pytest.mark.skipif(not FACTORY.exists(), reason="factory database not generated")
def test_shipped_register_routes_reproducibly():
    routing = route_all_paths(FACTORY, as_of=AS_OF)
    counts = routing["counts"]
    assert routing["paths_assessed"] == 97
    assert counts[LEVEL_RED] + counts[LEVEL_YELLOW] == routing["requires_human"]
    assert counts[LEVEL_GREEN] == routing["silently_archived"]
    # The headline the demo shows: most paths are cleared, a few need a person.
    assert counts[LEVEL_GREEN] > counts[LEVEL_RED] + counts[LEVEL_YELLOW]
    assert summarize(assess_paths(FACTORY, as_of=AS_OF))["paths_assessed"] == 97
