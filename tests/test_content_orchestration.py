"""Tests for the G1→G4 orchestration in content.py.

Coverage here was 0% before this file: the module had been exercised by hand but
nothing pinned its contract. What matters is not that it calls a model, but what
it does with what comes back - the prompt it builds, the claim types it refuses,
and the regenerate-once-then-escalate loop.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hindsight_agent.content import (
    DraftClaimModel,
    DraftQuizModel,
    GeneratedDraftModel,
    build_prompt,
    generate_learning_draft,
)
from hindsight_agent.grounding import gather_evidence
from hindsight_agent.learning_pack import STATE_NEEDS_HUMAN_WRITER, STATE_PENDING_APPROVAL

from tests.test_red_team import corpus  # noqa: F401  (fixture reuse)
from tests.test_workflow import GOOD_NARRATIVE, INCIDENT_SQL  # noqa: E402

#: grounding.load_risk_path joins the full display shape, so this fixture
#: carries every column that join reads - a narrower one silently fails here
#: rather than in the code under test.
FULL_FACTORY_SQL = """
CREATE TABLE factories(id TEXT PRIMARY KEY, name TEXT, naics TEXT);
CREATE TABLE areas(id TEXT PRIMARY KEY, name TEXT);
CREATE TABLE equipment(id TEXT PRIMARY KEY, name TEXT, area_id TEXT, factory_id TEXT,
    active INTEGER DEFAULT 1, equipment_type TEXT, asset_code TEXT);
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
INSERT INTO equipment VALUES('press-01','Mechanical press 01','a1','f1',1,'press','EQ-001');
INSERT INTO tasks VALUES('t1','Clear jam');
INSERT INTO hazards VALUES('haz-crush','Crush','mechanical');
INSERT INTO worker_roles VALUES('role-op','Press operator');
INSERT INTO safety_controls VALUES('control-guard','Fixed guard','engineering',
    'engineering','Guard fixed in place','visual');
INSERT INTO risk_assessments VALUES('risk-1','RA-001','press-01','t1','haz-crush',
    'role-op','Hand between dies',5,20,'high',9,'medium',0,'action_required');
INSERT INTO risk_assessment_controls VALUES('risk-1','control-guard','implemented',4);
"""


@pytest.fixture()
def factory(tmp_path: Path) -> Path:
    path = tmp_path / "factory.db"
    connection = sqlite3.connect(path)
    connection.executescript(FULL_FACTORY_SQL)
    connection.commit()
    connection.close()
    return path


@pytest.fixture()
def incidents(tmp_path: Path) -> Path:
    path = tmp_path / "incidents.db"
    connection = sqlite3.connect(path)
    connection.executescript(INCIDENT_SQL)
    connection.execute(
        "INSERT INTO osha_sir_incidents VALUES('inc-1','2026-09-01','Acme','332999',?,"
        "'Press','Crushed by machine','Amputation','Finger','Press',1,1,0)",
        (GOOD_NARRATIVE,),
    )
    connection.commit()
    connection.close()
    return path


class ScriptedGenerator:
    """Returns a queued response per call, and records the prompts it saw."""

    model_id = "scripted"

    def __init__(self, *responses: GeneratedDraftModel) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> GeneratedDraftModel:
        self.prompts.append(prompt)
        return self.responses[min(len(self.prompts) - 1, len(self.responses) - 1)]


def _good(control_id: str = "control-guard") -> GeneratedDraftModel:
    claims = [
        DraftClaimModel(claim_id="c1", claim_type="INCIDENT_FACT",
                        text_en="A hand was caught between the dies.",
                        text_es="Una mano quedó atrapada entre los troqueles.",
                        citations=["incident:inc-1"]),
        DraftClaimModel(claim_id="c2", claim_type="SITE_FACT",
                        text_en="We run the same task here.",
                        text_es="Realizamos la misma tarea aquí.",
                        citations=["site:risk-1"]),
        DraftClaimModel(claim_id="c3", claim_type="CONTROL",
                        text_en="Confirm the fixed guard is in place.",
                        text_es="Confirme que el resguardo fijo esté colocado.",
                        citations=[f"control:{control_id}"], control_id=control_id),
    ]
    return GeneratedDraftModel(
        briefing_claims=claims, talk_claims=claims[:2],
        quiz=[DraftQuizModel(question_id="q1", question_en="What do you check?",
                             question_es="¿Qué revisa?", options_en=["The guard", "Nothing"],
                             options_es=["El resguardo", "Nada"], correct_index=0,
                             claim_id="c1")],
        unknowns=["Whether the guard was fitted is not established."],
    )


def _unusable() -> GeneratedDraftModel:
    """Everything unsourced: the draft cannot fill its required sections."""
    return GeneratedDraftModel(briefing_claims=[
        DraftClaimModel(claim_id="b1", claim_type="INCIDENT_FACT",
                        text_en="The employer was negligent.",
                        text_es="El empleador fue negligente.", citations=[]),
    ])


def _run(incidents, factory, corpus, generator):
    return generate_learning_draft(
        corpus_database=corpus, factory_database=factory,
        incident_database=incidents, risk_assessment_id="risk-1",
        incident_key="inc-1", incident_narrative=GOOD_NARRATIVE,
        incident_title="Press die crush", generator=generator,
    )


def test_prompt_offers_a_closed_set_of_citations(incidents, factory, corpus):
    bundle = gather_evidence(corpus_database=corpus, factory_database=factory,
                             risk_assessment_id="risk-1",
                             incident_text=GOOD_NARRATIVE, top_k=3)
    prompt = build_prompt(bundle, incident_key="inc-1", incident_narrative=GOOD_NARRATIVE)
    assert "ALLOWED CITATIONS" in prompt
    assert "incident:inc-1" in prompt and "site:risk-1" in prompt
    assert "control:control-guard" in prompt
    assert "APPROVED CONTROLS" in prompt


def test_a_clean_draft_reaches_the_approval_queue(incidents, factory, corpus):
    generator = ScriptedGenerator(_good())
    result = _run(incidents, factory, corpus, generator)
    assert result["draft"].state == STATE_PENDING_APPROVAL
    assert result["needs_human_writer"] is False
    assert len(generator.prompts) == 1  # no regeneration needed
    assert result["pack"]["content_hash"]


def test_an_unusable_draft_regenerates_once_then_escalates(incidents, factory, corpus):
    generator = ScriptedGenerator(_unusable(), _unusable())
    result = _run(incidents, factory, corpus, generator)
    assert result["needs_human_writer"] is True
    assert result["draft"].state == STATE_NEEDS_HUMAN_WRITER
    assert len(generator.prompts) == 2  # exactly one retry, never a loop
    assert "YOUR PREVIOUS ATTEMPT FAILED" in generator.prompts[1]


def test_a_second_attempt_can_recover(incidents, factory, corpus):
    generator = ScriptedGenerator(_unusable(), _good())
    result = _run(incidents, factory, corpus, generator)
    assert result["needs_human_writer"] is False
    assert result["draft"].state == STATE_PENDING_APPROVAL
    assert len(generator.prompts) == 2


def test_an_unknown_claim_type_is_dropped_not_shipped(incidents, factory, corpus):
    model = _good()
    model.briefing_claims.append(
        DraftClaimModel(claim_id="weird", claim_type="MADE_UP_TYPE",
                        text_en="Anything.", text_es="Cualquier cosa.",
                        citations=["incident:inc-1"])
    )
    result = _run(incidents, factory, corpus, ScriptedGenerator(model))
    assert all(c.claim_id != "weird" for c in result["draft"].all_claims())


def test_both_languages_render_from_the_same_claims(incidents, factory, corpus):
    result = _run(incidents, factory, corpus, ScriptedGenerator(_good()))
    english = result["pack"]["briefing"]["en"]
    spanish = result["pack"]["briefing"]["es"]
    assert [line["citations"] for s in english["sections"] for line in s["lines"]] == \
           [line["citations"] for s in spanish["sections"] for line in s["lines"]]
    assert spanish["translated_from_en"] is True


def test_the_pack_hash_changes_when_a_claim_changes(incidents, factory, corpus):
    first = _run(incidents, factory, corpus, ScriptedGenerator(_good()))
    edited = _good()
    edited.briefing_claims[0].text_en = "A hand was caught in the machine."
    second = _run(incidents, factory, corpus, ScriptedGenerator(edited))
    assert first["pack"]["content_hash"] != second["pack"]["content_hash"]
