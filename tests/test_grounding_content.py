"""Tests for G1 grounding, G2/G3 drafting and the G4 self-check.

The G4 tests build their own miniature databases so they assert on behaviour
rather than on the shipped seed data. The end-to-end test uses the real
databases when they are present and skips otherwise, so the suite stays green
on a fresh clone before ``hindsight-grounding-collect`` has ever run.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hindsight_agent.corpus_db import (
    ensure_corpus_schema,
    get_passage,
    record_snapshot,
    upsert_document,
)
from hindsight_agent.grounding import GroundingIndex, tokenize
from hindsight_agent.learning_pack import (
    CLAIM_CONTROL,
    CLAIM_INCIDENT_FACT,
    CLAIM_REQUIREMENT,
    CLAIM_SITE_FACT,
    CLAIM_UNKNOWN,
    STATE_NEEDS_HUMAN_WRITER,
    STATE_SELF_CHECK_OK,
    Claim,
    LearningDraft,
    QuizItem,
    normalize_numerals,
    render_briefing,
    render_toolbox_talk,
)
from hindsight_agent.self_check import (
    CHECK_BILINGUAL_NUMERALS,
    CHECK_CITATION_RESOLVES,
    CHECK_CONTROL_VOCABULARY,
    CHECK_NUMERALS_SUPPORTED,
    CHECK_QUOTE_VERBATIM,
    CHECK_SITE_SCOPE,
    run_self_check,
)
from hindsight_agent.tools.collect_grounding import (
    parse_cfr_section,
    parse_fatal_facts_listing,
    split_fatal_facts_text,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# G1 parsing
# --------------------------------------------------------------------------

CFR_XML = b"""<DIV8 N="1910.212" TYPE="SECTION">
<HEAD>&#167; 1910.212 General requirements for all machines.</HEAD>
<P>(a) Machine guarding &#8212;(1) Types of guarding. One or more methods of machine
guarding shall be provided to protect the operator from hazards such as point of
operation and ingoing nip points.</P>
<P>(2) General requirements for machine guards. Guards shall be affixed to the
machine where possible.</P>
</DIV8>"""


def test_parse_cfr_section_keeps_printed_paragraph_marker():
    parsed = parse_cfr_section(CFR_XML, "1910.212")
    assert "1910.212" in parsed["heading"]
    assert len(parsed["passages"]) == 2

    first, second = parsed["passages"]
    assert first["section_label"] == "(a)"
    assert second["section_label"] == "(2)"
    # Section-level citation only: a hierarchical path is never assembled.
    assert first["citation_label"] == "29 CFR 1910.212"
    assert first["section_role"] == "REQUIREMENT"


def test_passages_are_tagged_from_the_shared_hazard_vocabulary():
    """Corpus tagging reuses matching.HAZARD_TERMS rather than a second vocabulary."""
    from hindsight_agent.tools.collect_grounding import tag_hazards

    assert "haz-caught-in" in tag_hazards("a pinch point on the press")
    assert "haz-electrical" in tag_hazards("contact with an energized conductor")
    assert tag_hazards("no hazard words here at all") == []


def test_cfr_passages_carry_tag_fields():
    parsed = parse_cfr_section(CFR_XML, "1910.212")
    assert all("hazard_tags" in p for p in parsed["passages"])


@pytest.mark.parametrize(
    "heading, role",
    [
        ("INCIDENT PREVENTION", "PREVENTION"),
        ("ACCIDENT PREVENTION", "PREVENTION"),
        ("HOW TO PREVENT THESE INCIDENTS", "PREVENTION"),
        ("PREVENCIÓN DE INCIDENTES", "PREVENTION"),
        ("CÓMO PREVENIR ESTOS INCIDENTES", "PREVENTION"),
    ],
)
def test_prevention_headings_across_editions_and_languages(heading, role):
    text = f"front matter\n{heading}\nDo the thing."
    sections = split_fatal_facts_text(text)
    assert any(s["section_role"] == role for s in sections)


def test_longer_spanish_heading_wins_over_the_one_it_contains():
    text = "BREVE DESCRIPCIÓN DEL INCIDENTE Dos trabajadores murieron."
    sections = split_fatal_facts_text(text)
    labels = [s["section_label"] for s in sections]
    assert "BREVE DESCRIPCIÓN DEL INCIDENTE" in labels
    assert "DESCRIPCIÓN DEL INCIDENTE" not in labels


def test_unrecognised_layout_keeps_text_instead_of_dropping_it():
    sections = split_fatal_facts_text("A sheet with no known headings at all.")
    assert len(sections) == 1
    assert sections[0]["section_label"] == "FULL TEXT"
    assert "no known headings" in sections[0]["text"]


def test_fatal_facts_listing_pairs_spanish_edition():
    html = """
    <div class="col-md-8">
      <p>No. 19 -2023 - Confined Space Fire ( Español )
        <a href="/sites/default/files/publications/OSHA_FF-4278.pdf">Confined Space Fire</a>
        <a href="/sites/default/files/publications/OSHA_FF-4366.pdf">Español</a>
      </p>
      <p>No. 6-2013 - Cotton Press - Struck/crushed by
        <a href="/sites/default/files/publications/OSHA_3679.pdf">Cotton Press</a>
      </p>
    </div>
    """
    entries = parse_fatal_facts_listing(html)
    assert len(entries) == 2
    spanish, english_only = entries
    assert spanish["number"] == "19" and spanish["spanish_url"].endswith("OSHA_FF-4366.pdf")
    assert spanish["english_url"].endswith("OSHA_FF-4278.pdf")
    assert english_only["spanish_url"] is None


# --------------------------------------------------------------------------
# Numeral normalisation - regression tests for two real false positives
# --------------------------------------------------------------------------

def test_equipment_number_is_not_read_as_a_measurement():
    # "press 01 in the stamping area" once produced the measurement "01in".
    assert normalize_numerals("press 01 in the stamping area") == ["01"]


def test_spanish_word_is_not_read_as_a_unit():
    # "5 veces" once produced "5V".
    assert normalize_numerals("citado 5 veces antes") == ["5"]


def test_real_measurements_normalise_across_spacing():
    assert normalize_numerals("250 mm") == normalize_numerals("250mm") == ["250mm"]
    assert normalize_numerals("1,000 cycles") == ["1000"]


# --------------------------------------------------------------------------
# Fixtures for G4
# --------------------------------------------------------------------------

@pytest.fixture()
def corpus(tmp_path: Path) -> Path:
    database = tmp_path / "grounding.db"
    ensure_corpus_schema(database)
    raw = tmp_path / "snap.xml"
    record_snapshot(
        database,
        snapshot_key="cfr:1910.212:2026-08-31",
        source_type="cfr",
        source_url="https://www.ecfr.gov/api/versioner/v1/full/2026-08-31/title-29.xml",
        payload=CFR_XML,
        local_path=raw,
        http_status=200,
    )
    upsert_document(
        database,
        document_id="cfr:1910.212",
        source_type="cfr",
        source_key="1910.212",
        title="General requirements for all machines",
        publisher="eCFR",
        official_url="https://www.ecfr.gov/current/title-29/section-1910.212",
        snapshot_key="cfr:1910.212:2026-08-31",
        passages=[
            {
                "section_label": "(a)",
                "section_role": "REQUIREMENT",
                "citation_label": "29 CFR 1910.212",
                "text": "Guards shall be affixed to the machine where possible.",
                "hazard_tags": ["haz-caught-in"],
                "equipment_tags": ["press-01"],
            }
        ],
    )
    return database


@pytest.fixture()
def factory(tmp_path: Path) -> Path:
    database = tmp_path / "factory.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE safety_controls(id TEXT PRIMARY KEY, name TEXT, control_type TEXT,
            hierarchy_code TEXT, performance_requirement TEXT);
        CREATE TABLE risk_assessments(id TEXT PRIMARY KEY, assessment_code TEXT,
            equipment_id TEXT, hazardous_situation TEXT);
        CREATE TABLE risk_assessment_controls(risk_assessment_id TEXT, safety_control_id TEXT);
        INSERT INTO safety_controls VALUES
            ('control-guard','Fixed machine guard','engineering','engineering','Guard fixed in place'),
            ('control-loto','Lockout tagout','administrative','administrative','Energy isolated');
        INSERT INTO risk_assessments VALUES
            ('risk-press-01-crush','RA-2026-001','press-01','Hand between dies'),
            ('risk-other','RA-2026-099','shear-01','Other path');
        INSERT INTO risk_assessment_controls VALUES ('risk-press-01-crush','control-guard');
        """
    )
    connection.commit()
    connection.close()
    return database


@pytest.fixture()
def incidents(tmp_path: Path) -> Path:
    database = tmp_path / "incidents.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE osha_sir_incidents(incident_key TEXT PRIMARY KEY, narrative TEXT,
            employer TEXT, event_date TEXT, hospitalized INTEGER, amputation INTEGER,
            loss_of_eye INTEGER, nature_title TEXT, body_part_title TEXT,
            event_title TEXT, source_title TEXT);
        INSERT INTO osha_sir_incidents VALUES
            ('inc-1','A hand was caught between the dies of a mechanical press.',
             'Acme','2025-11-04',1,1,0,'Amputation','Finger','Caught in','Press');
        """
    )
    connection.commit()
    connection.close()
    return database


def _draft(claims: list[Claim], quiz: list[QuizItem] | None = None) -> LearningDraft:
    return LearningDraft(
        draft_id="draft:test",
        risk_assessment_id="risk-press-01-crush",
        assessment_code="RA-2026-001",
        incident_citation="incident:inc-1",
        incident_title="Press die crush",
        equipment_name="Mechanical press 01",
        hazard_name="Crush",
        task_name="Clear jam",
        exposed_role="Press operator",
        briefing_claims=list(claims),
        quiz=list(quiz or []),
    )


def _good_claims() -> list[Claim]:
    return [
        Claim("c1", CLAIM_INCIDENT_FACT,
              "A hand was caught between the dies of a mechanical press.",
              "Una mano quedó atrapada entre los troqueles de una prensa mecánica.",
              ["incident:inc-1"]),
        Claim("c2", CLAIM_SITE_FACT,
              "We run the same task on this press.",
              "Realizamos la misma tarea en esta prensa.",
              ["site:risk-press-01-crush"]),
        Claim("c3", CLAIM_CONTROL,
              "Confirm the fixed machine guard is in place.",
              "Confirme que el resguardo fijo esté colocado.",
              ["control:control-guard"], control_id="control-guard"),
    ]


def _check(draft, corpus, factory, incidents, repair=True):
    return run_self_check(
        draft, corpus_database=corpus, factory_database=factory,
        incident_database=incidents, repair=repair,
    )


# --------------------------------------------------------------------------
# G4 behaviour
# --------------------------------------------------------------------------

def test_well_sourced_draft_passes_untouched(corpus, factory, incidents):
    draft = _draft(_good_claims())
    report = _check(draft, corpus, factory, incidents)
    assert report.passed
    assert report.state == STATE_SELF_CHECK_OK
    assert report.removed_claim_ids == []
    assert len(draft.briefing_claims) == 3


def test_invented_measurement_is_deleted(corpus, factory, incidents):
    claims = _good_claims()
    claims.append(
        Claim("bad", CLAIM_CONTROL,
              "Set the guard at a safety distance of 250 mm.",
              "Coloque el resguardo a 250 mm.",
              ["control:control-guard"], control_id="control-guard")
    )
    draft = _draft(claims)
    report = _check(draft, corpus, factory, incidents)
    assert "bad" in report.removed_claim_ids
    assert any(f.check == CHECK_NUMERALS_SUPPORTED for f in report.findings)
    assert all(c.claim_id != "bad" for c in draft.briefing_claims)


def test_unresolvable_citation_is_deleted(corpus, factory, incidents):
    claims = _good_claims()
    claims.append(
        Claim("bad", CLAIM_REQUIREMENT, "OSHA requires annual certification.",
              "OSHA exige certificación anual.", ["corpus:cfr:1910.212#9999"])
    )
    report = _check(_draft(claims), corpus, factory, incidents)
    assert any(f.check == CHECK_CITATION_RESOLVES for f in report.findings)


def test_control_outside_the_approved_vocabulary_is_deleted(corpus, factory, incidents):
    claims = _good_claims()
    claims.append(
        Claim("bad", CLAIM_CONTROL, "Install a light curtain.",
              "Instale una cortina de luz.",
              ["corpus:cfr:1910.212#0001"], control_id="control-light-curtain")
    )
    report = _check(_draft(claims), corpus, factory, incidents)
    assert any(f.check == CHECK_CONTROL_VOCABULARY for f in report.findings)


def test_reference_to_another_hazard_path_is_deleted(corpus, factory, incidents):
    claims = _good_claims()
    claims.append(
        Claim("bad", CLAIM_SITE_FACT, "The shear has the same problem.",
              "La cizalla tiene el mismo problema.", ["site:risk-other"])
    )
    report = _check(_draft(claims), corpus, factory, incidents)
    assert any(f.check == CHECK_SITE_SCOPE for f in report.findings)


def test_numbers_must_match_between_languages(corpus, factory, incidents):
    claims = _good_claims()
    claims.append(
        Claim("bad", CLAIM_INCIDENT_FACT, "The employer was cited 3 times.",
              "El empleador fue citado 5 veces.", ["incident:inc-1"])
    )
    report = _check(_draft(claims), corpus, factory, incidents)
    assert any(f.check == CHECK_BILINGUAL_NUMERALS for f in report.findings)


def test_quote_must_be_verbatim(corpus, factory, incidents):
    passage = get_passage(corpus, "cfr:1910.212#0001")
    assert passage is not None
    claims = _good_claims()
    claims.append(
        Claim("bad", CLAIM_REQUIREMENT, "The standard requires guarding.",
              "La norma exige resguardo.", ["corpus:cfr:1910.212#0001"],
              quote="Guards must always be welded to the machine.",
              quote_citation="corpus:cfr:1910.212#0001")
    )
    report = _check(_draft(claims), corpus, factory, incidents)
    assert any(f.check == CHECK_QUOTE_VERBATIM for f in report.findings)


def test_verbatim_quote_is_accepted(corpus, factory, incidents):
    claims = _good_claims()
    claims.append(
        Claim("ok", CLAIM_REQUIREMENT, "The standard requires guarding.",
              "La norma exige resguardo.", ["corpus:cfr:1910.212#0001"],
              quote="Guards shall be affixed to the machine",
              quote_citation="corpus:cfr:1910.212#0001")
    )
    report = _check(_draft(claims), corpus, factory, incidents)
    assert report.removed_claim_ids == []


def test_unknown_claim_needs_no_citation(corpus, factory, incidents):
    claims = _good_claims()
    claims.append(
        Claim("u1", CLAIM_UNKNOWN, "The source does not say whether the guard was fitted.",
              "La fuente no indica si el resguardo estaba colocado.", [])
    )
    report = _check(_draft(claims), corpus, factory, incidents)
    assert "u1" not in report.removed_claim_ids


def test_quiz_dies_with_the_claim_it_rested_on(corpus, factory, incidents):
    claims = _good_claims()
    claims.append(
        Claim("bad", CLAIM_CONTROL, "Set the guard at 250 mm.", "Coloque a 250 mm.",
              ["control:control-guard"], control_id="control-guard")
    )
    quiz = [QuizItem("q1", "Distance?", "¿Distancia?", ["250 mm", "None"],
                     ["250 mm", "Ninguna"], 0, "bad")]
    draft = _draft(claims, quiz)
    report = _check(draft, corpus, factory, incidents)
    assert report.removed_quiz_ids == ["q1"]
    assert draft.quiz == []


def test_draft_stripped_of_required_sections_becomes_a_writing_task(corpus, factory, incidents):
    # Only an incident fact survives: no site fact, no action.
    claims = [
        Claim("c1", CLAIM_INCIDENT_FACT, "A hand was caught between the dies.",
              "Una mano quedó atrapada.", ["incident:inc-1"]),
        Claim("bad", CLAIM_SITE_FACT, "We had 7 similar events.",
              "Tuvimos 7 eventos similares.", ["site:risk-press-01-crush"]),
    ]
    draft = _draft(claims)
    report = _check(draft, corpus, factory, incidents)
    assert report.state == STATE_NEEDS_HUMAN_WRITER
    assert not report.passed
    assert "SITE_FACT" in report.missing_required


# --------------------------------------------------------------------------
# G2 / G3 rendering
# --------------------------------------------------------------------------

def test_briefing_and_talk_render_from_the_same_claims(corpus, factory, incidents):
    draft = _draft(_good_claims())
    draft.talk_claims = list(draft.briefing_claims)
    _check(draft, corpus, factory, incidents)

    english = render_briefing(draft, "en")
    spanish = render_briefing(draft, "es")
    assert [s["heading"] for s in english["sections"]][0] == "WHAT HAPPENED"
    assert [s["heading"] for s in spanish["sections"]][0] == "QUÉ OCURRIÓ"
    # Same claims, same citations, different language.
    assert [l["citations"] for s in english["sections"] for l in s["lines"]] == \
           [l["citations"] for s in spanish["sections"] for l in s["lines"]]
    assert spanish["translated_from_en"] is True
    assert spanish["advisory"].startswith("Solo con carácter informativo")

    talk = render_toolbox_talk(draft, "es")
    assert talk["estimated_minutes"] >= 1.0
    assert talk["audience_role"] == "Press operator"


# --------------------------------------------------------------------------
# G1 retrieval against the shipped corpus (skipped before it is collected)
# --------------------------------------------------------------------------

@pytest.mark.skipif(
    not (REPO_ROOT / "data" / "corpus" / "grounding.db").exists(),
    reason="grounding corpus not collected; run hindsight-grounding-collect",
)
def test_press_incident_retrieves_the_power_press_standard():
    index = GroundingIndex(REPO_ROOT / "data" / "corpus" / "grounding.db")
    hits = index.retrieve(
        "employee clearing a jam in a mechanical power press amputated fingers "
        "point of operation guard removed not locked out",
        hazard_ids=["haz-caught-in"],
        language="en",
        roles={"REQUIREMENT", "PREVENTION"},
        top_k=5,
        require_tag_overlap=False,
    )
    labels = {hit.citation_label for hit in hits}
    assert any("1910.217" in label for label in labels), labels


def test_tokenize_drops_stopwords_and_keeps_terms():
    assert tokenize("The guard shall be affixed to the machine") == ["guard", "affixed", "machine"]
