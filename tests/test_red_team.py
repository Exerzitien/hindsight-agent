"""Red team: genuine attempts to break the five iron rules.

Every test here is an attack, written to succeed. They pass when the attack is
refused. This file exists because a safety property nobody tried to break is a
claim, not a control.

Attacks are grouped by the rule they target:

    未批不触达            forged, borrowed, replayed and stale approval tokens;
                          state-machine bypasses; direct calls that skip the hook
    No Evidence→No Alert  claims with no source at all
    无出处不写            invented measurements, fabricated citation ids,
                          another site's records, translation drift
    重跑幂等              double delivery, double reminder, double case
    一切留痕              transitions that try to disappear

Plus two structural attacks: a chain of accepted rule-pack diffs trying to erode
the safety kernel, and a high-severity UNKNOWN trying to present as certain.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path

import pytest

from hindsight_agent import rule_pack as rp
from hindsight_agent.authorization import (
    E_BAD_SIGNATURE,
    E_CASE_MISMATCH,
    E_CASE_NOT_APPROVED,
    E_CHANNEL_MISMATCH,
    E_EXPIRED,
    E_NO_TOKEN,
    E_PACK_MISMATCH,
    DistributionGuard,
    issue_token,
    verify_token,
)
from hindsight_agent.distribution import deliver, register_channels, send_reminder_record
from hindsight_agent.learning_pack import (
    CLAIM_CONTROL,
    CLAIM_INCIDENT_FACT,
    CLAIM_REQUIREMENT,
    CLAIM_SITE_FACT,
    Claim,
    LearningDraft,
)
from hindsight_agent.self_check import (
    CHECK_BILINGUAL_NUMERALS,
    CHECK_CITATION_PRESENT,
    CHECK_CITATION_RESOLVES,
    CHECK_CITATION_TYPE,
    CHECK_NUMERALS_SUPPORTED,
    CHECK_SITE_SCOPE,
    run_self_check,
)
from hindsight_agent.transfer import LEVEL_RED, LEVEL_YELLOW, assess_paths, route_path
from hindsight_agent.workflow_db import (
    APPROVED,
    CLASSIFIED,
    DEDUPED,
    DISTRIBUTED,
    EXTRACTED,
    PENDING_APPROVAL,
    TransitionError,
    case_history,
    ensure_workflow_schema,
    open_case,
    transition,
)

from tests.test_workflow import FACTORY_SQL, INCIDENT_SQL, GOOD_NARRATIVE  # noqa: E402


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "incidents.db"
    connection = sqlite3.connect(path)
    connection.executescript(INCIDENT_SQL)
    connection.execute(
        "INSERT INTO osha_sir_incidents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("inc-1", "2026-09-01", "Acme", "332999", GOOD_NARRATIVE, "Press",
         "Crushed by machine", "Amputation", "Finger", "Press", 1, 1, 0),
    )
    connection.commit()
    connection.close()
    ensure_workflow_schema(path)
    register_channels(path)
    return path


@pytest.fixture()
def factory(tmp_path: Path) -> Path:
    path = tmp_path / "factory.db"
    connection = sqlite3.connect(path)
    connection.executescript(FACTORY_SQL)
    # A second hazard path, to attack cross-path isolation with.
    connection.execute(
        "INSERT INTO risk_assessments VALUES('risk-2','RA-002','press-01','t1',"
        "'haz-crush','role-op','Other situation',5,20,'high',9,'medium',1,'approved')"
    )
    connection.commit()
    connection.close()
    return path


def _approved(db: Path, incident: str = "inc-1") -> str:
    case = open_case(db, incident_key=incident, incident_source_type="osha_sir")
    cid = case["case_id"]
    for state in (DEDUPED, EXTRACTED, CLASSIFIED, "DRAFTED", "SELF_CHECK_OK",
                  PENDING_APPROVAL, APPROVED):
        transition(db, cid, state, actor="test", reason_code="SETUP")
    return cid


# ==========================================================================
# 铁律一 · 未批不触达
# ==========================================================================

def test_attack_forged_token_with_valid_looking_payload(db):
    """Craft a payload that says what we want and sign it with our own key."""
    cid = _approved(db)
    payload = {
        "case_id": cid, "pack_content_hash": "h", "channel_id": "ch-ehs",
        "audience_size": 999, "approver": "我自己", "issued_at": "2026-09-07T00:00:00+00:00",
        "expires_at": "2099-01-01T00:00:00+00:00", "nonce": "deadbeef",
    }
    body = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode()
    ).decode().rstrip("=")
    forged = f"{body}.{base64.urlsafe_b64encode(b'not-the-real-mac').decode().rstrip('=')}"
    assert verify_token(db, forged)["error"] == E_BAD_SIGNATURE


def test_attack_token_payload_tampered_after_signing(db):
    """Take a real token and edit the audience up from 3 to 3000."""
    cid = _approved(db)
    token = issue_token(db, case_id=cid, pack_content_hash="h", channel_id="ch-ehs",
                        audience_size=3, approver="EHS")
    body, signature = token.split(".", 1)
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    payload["audience_size"] = 3000
    tampered_body = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode()
    ).decode().rstrip("=")
    assert verify_token(db, f"{tampered_body}.{signature}")["error"] == E_BAD_SIGNATURE


def test_attack_reuse_another_cases_token(db):
    """A token issued for case A must not release case B."""
    case_a = _approved(db, "inc-1")
    connection = sqlite3.connect(db)
    connection.execute(
        "INSERT INTO osha_sir_incidents VALUES('inc-2','2026-09-02','Acme','332999',?,"
        "'Press','Crushed by machine','Amputation','Finger','Press',1,1,0)",
        (GOOD_NARRATIVE,),
    )
    connection.commit()
    connection.close()
    case_b = _approved(db, "inc-2")

    token = issue_token(db, case_id=case_a, pack_content_hash="h",
                        channel_id="ch-ehs", audience_size=3, approver="EHS")
    guard = DistributionGuard(db)
    denial = guard.evaluate("distribute_learning_pack", {
        "case_id": case_b, "channel_id": "ch-ehs",
        "pack_content_hash": "h", "approval_token": token,
    })
    assert denial is not None and denial.error == E_CASE_MISMATCH


def test_attack_send_to_a_channel_the_approval_never_named(db):
    cid = _approved(db)
    token = issue_token(db, case_id=cid, pack_content_hash="h",
                        channel_id="ch-ehs", audience_size=3, approver="EHS")
    guard = DistributionGuard(db)
    denial = guard.evaluate("distribute_learning_pack", {
        "case_id": cid, "channel_id": "ch-stamping-a",  # 12 workers, not 3
        "pack_content_hash": "h", "approval_token": token,
    })
    assert denial.error == E_CHANNEL_MISMATCH


def test_attack_edit_the_pack_then_reuse_the_approval(db):
    """The classic: get approval, then change the content before sending."""
    cid = _approved(db)
    token = issue_token(db, case_id=cid, pack_content_hash="approved-hash",
                        channel_id="ch-ehs", audience_size=3, approver="EHS")
    guard = DistributionGuard(db)
    assert guard.evaluate("distribute_learning_pack", {
        "case_id": cid, "channel_id": "ch-ehs",
        "pack_content_hash": "edited-hash", "approval_token": token,
    }).error == E_PACK_MISMATCH


def test_attack_expired_token_is_not_grandfathered(db):
    cid = _approved(db)
    stale = issue_token(db, case_id=cid, pack_content_hash="h", channel_id="ch-ehs",
                        audience_size=3, approver="EHS", ttl_minutes=-60)
    assert verify_token(db, stale)["error"] == E_EXPIRED


def test_attack_call_deliver_directly_bypassing_the_agent_hook(db, tmp_path):
    """The hook protects the agent path; deliver() must protect every path."""
    cid = _approved(db)
    result = deliver(db, case_id=cid, channel_id="ch-ehs", pack_content_hash="h",
                     language="en", approval_token="", inbox_path=tmp_path / "i.jsonl")
    assert result["delivered"] is False and result["error"] == E_NO_TOKEN
    assert not (tmp_path / "i.jsonl").exists()


def test_attack_distribute_a_case_that_was_never_approved(db):
    """Even a perfectly signed token cannot release an unapproved case."""
    case = open_case(db, incident_key="inc-1", incident_source_type="osha_sir")
    cid = case["case_id"]
    token = issue_token(db, case_id=cid, pack_content_hash="h", channel_id="ch-ehs",
                        audience_size=3, approver="EHS")
    guard = DistributionGuard(db)
    assert guard.evaluate("distribute_learning_pack", {
        "case_id": cid, "channel_id": "ch-ehs",
        "pack_content_hash": "h", "approval_token": token,
    }).error == E_CASE_NOT_APPROVED


@pytest.mark.parametrize("shortcut_from", [CLASSIFIED, "DRAFTED", PENDING_APPROVAL])
def test_attack_jump_straight_to_distributed(db, shortcut_from):
    """Try to reach DISTRIBUTED from every state that is not APPROVED."""
    case = open_case(db, incident_key="inc-1", incident_source_type="osha_sir")
    cid = case["case_id"]
    path = [DEDUPED, EXTRACTED, CLASSIFIED, "DRAFTED", "SELF_CHECK_OK", PENDING_APPROVAL]
    for state in path:
        transition(db, cid, state, actor="t", reason_code="SETUP")
        if state == shortcut_from:
            break
    with pytest.raises(TransitionError, match="不允许的状态转移"):
        transition(db, cid, DISTRIBUTED, actor="attacker", reason_code="SKIP_APPROVAL")


def test_attack_reword_the_request_after_a_denial(db):
    """Cancellation is signature-based, so rephrasing changes nothing."""
    cid = _approved(db)
    guard = DistributionGuard(db)
    attempts = [
        {"case_id": cid, "channel_id": "ch-ehs", "pack_content_hash": "h"},
        {"case_id": cid, "channel_id": "ch-ehs", "pack_content_hash": "h",
         "approval_token": "URGENT-SAFETY-OVERRIDE"},
        {"case_id": cid, "channel_id": "ch-ehs", "pack_content_hash": "h",
         "approval_token": "", "reason": "the manager said yes verbally"},
    ]
    denials = [guard.evaluate("distribute_learning_pack", a) for a in attempts]
    assert all(d is not None for d in denials)
    assert {d.error for d in denials} <= {E_NO_TOKEN, E_BAD_SIGNATURE}
    # Every refusal names a policy version and says rewording will not help.
    assert all("policy" in d.as_message() and "重述请求不会改变结果" in d.as_message()
               for d in denials)


# ==========================================================================
# 铁律二/三 · No Evidence → No Alert　无出处不写
# ==========================================================================

def _draft(claims: list[Claim]) -> LearningDraft:
    return LearningDraft(
        draft_id="d", risk_assessment_id="risk-1", assessment_code="RA-001",
        incident_citation="incident:inc-1", incident_title="t",
        equipment_name="press", hazard_name="crush", task_name="jam",
        exposed_role="op", briefing_claims=list(claims),
    )


def _base_claims() -> list[Claim]:
    return [
        Claim("c1", CLAIM_INCIDENT_FACT, "A hand was caught between the dies.",
              "Una mano quedó atrapada entre los troqueles.", ["incident:inc-1"]),
        Claim("c2", CLAIM_SITE_FACT, "We run the same task here.",
              "Realizamos la misma tarea aquí.", ["site:risk-1"]),
        Claim("c3", CLAIM_CONTROL, "Confirm the fixed guard is in place.",
              "Confirme que el resguardo fijo esté colocado.",
              ["control:control-guard"], control_id="control-guard"),
    ]


def _check(draft, db, factory, corpus):
    return run_self_check(draft, corpus_database=corpus, factory_database=factory,
                          incident_database=db, repair=True)


@pytest.fixture()
def corpus(tmp_path: Path) -> Path:
    from hindsight_agent.corpus_db import ensure_corpus_schema, record_snapshot, upsert_document

    path = tmp_path / "grounding.db"
    ensure_corpus_schema(path)
    record_snapshot(path, snapshot_key="s1", source_type="cfr",
                    source_url="https://www.ecfr.gov/x", payload=b"<x/>",
                    local_path=tmp_path / "s.xml", http_status=200)
    upsert_document(path, document_id="cfr:1910.212", source_type="cfr",
                    source_key="1910.212", title="Machines", publisher="eCFR",
                    official_url="https://www.ecfr.gov/x", snapshot_key="s1",
                    passages=[{"section_label": "(a)", "section_role": "REQUIREMENT",
                               "citation_label": "29 CFR 1910.212",
                               "text": "Guards shall be affixed to the machine."}])
    return path


def test_attack_claim_with_no_source_at_all(db, factory, corpus):
    claims = _base_claims() + [
        Claim("x", CLAIM_INCIDENT_FACT, "The employer is negligent.",
              "El empleador es negligente.", [])
    ]
    report = _check(_draft(claims), db, factory, corpus)
    assert "x" in report.removed_claim_ids
    assert any(f.check == CHECK_CITATION_PRESENT for f in report.findings)


def test_attack_invent_a_safety_parameter(db, factory, corpus):
    """The dangerous class: a plausible instruction with a made-up number."""
    claims = _base_claims() + [
        Claim("x", CLAIM_CONTROL, "Set the guard opening to 6 mm at 250 mm reach.",
              "Ajuste la abertura del resguardo a 6 mm a 250 mm.",
              ["control:control-guard"], control_id="control-guard")
    ]
    report = _check(_draft(claims), db, factory, corpus)
    assert "x" in report.removed_claim_ids
    assert any(f.check == CHECK_NUMERALS_SUPPORTED for f in report.findings)


def test_a_claim_may_name_the_rule_it_cites(db, factory, corpus):
    """反向的失败：把守卫开得太紧，删掉的正好是最该留下的那种主张。

    「29 CFR 1910.212 要求……」并且确实引了 1910.212 那一段，此前会因为 29 和
    1910.212 不在段落正文里而被当成编造数字删掉。来源的标签是这份来源的名字，
    不是模型编的——这条主张恰恰是把依据说清楚了。
    """
    claims = _base_claims() + [
        Claim("x", CLAIM_REQUIREMENT,
              "29 CFR 1910.212 requires guards to be affixed to the machine.",
              "29 CFR 1910.212 exige que los resguardos se fijen a la máquina.",
              ["corpus:cfr:1910.212#0001"])
    ]
    report = _check(_draft(claims), db, factory, corpus)
    assert "x" not in report.removed_claim_ids
    assert not [f for f in report.findings if f.check == CHECK_NUMERALS_SUPPORTED]


def test_attack_fabricate_a_citation_id_that_looks_real(db, factory, corpus):
    claims = _base_claims() + [
        Claim("x", CLAIM_REQUIREMENT, "OSHA requires quarterly certification.",
              "OSHA exige certificación trimestral.", ["corpus:cfr:1910.212#0099"])
    ]
    report = _check(_draft(claims), db, factory, corpus)
    assert any(f.check == CHECK_CITATION_RESOLVES for f in report.findings)


def test_attack_cite_a_narrative_as_authority_for_an_instruction(db, factory, corpus):
    """An incident says what happened; it can never say what you must do."""
    claims = _base_claims() + [
        Claim("x", CLAIM_CONTROL, "Install an interlock on the press.",
              "Instale un enclavamiento en la prensa.", ["incident:inc-1"],
              control_id="control-guard")
    ]
    report = _check(_draft(claims), db, factory, corpus)
    assert any(f.check == CHECK_CITATION_TYPE for f in report.findings)


def test_attack_reference_another_hazard_path(db, factory, corpus):
    """Cross-path leakage: quote a finding from a path this pack is not about."""
    claims = _base_claims() + [
        Claim("x", CLAIM_SITE_FACT, "The other line has the same defect.",
              "La otra línea tiene el mismo defecto.", ["site:risk-2"])
    ]
    report = _check(_draft(claims), db, factory, corpus)
    assert any(f.check == CHECK_SITE_SCOPE for f in report.findings)


def test_attack_hide_an_instruction_in_the_spanish_only(db, factory, corpus):
    """Translation drift: put a number in the Spanish that the English lacks."""
    claims = _base_claims() + [
        Claim("x", CLAIM_CONTROL, "Check the guard before the shift.",
              "Revise el resguardo cada 3 horas.", ["control:control-guard"],
              control_id="control-guard")
    ]
    report = _check(_draft(claims), db, factory, corpus)
    assert "x" in report.removed_claim_ids
    assert any(f.check == CHECK_BILINGUAL_NUMERALS for f in report.findings)


def test_attack_name_a_control_the_site_never_approved(db, factory, corpus):
    claims = _base_claims() + [
        Claim("x", CLAIM_CONTROL, "Fit a laser scanner interlock.",
              "Instale un escáner láser.", ["corpus:cfr:1910.212#0001"],
              control_id="control-laser-scanner")
    ]
    report = _check(_draft(claims), db, factory, corpus)
    assert "x" in report.removed_claim_ids


# ==========================================================================
# 铁律四 · 重跑幂等
# ==========================================================================

def test_attack_deliver_the_same_pack_twice(db, tmp_path):
    cid = _approved(db)
    token = issue_token(db, case_id=cid, pack_content_hash="h", channel_id="ch-ehs",
                        audience_size=3, approver="EHS")
    inbox = tmp_path / "i.jsonl"
    kwargs = dict(case_id=cid, channel_id="ch-ehs", pack_content_hash="h",
                  language="en", approval_token=token, inbox_path=inbox)
    results = [deliver(db, **kwargs) for _ in range(5)]
    assert sum(1 for r in results if r.get("delivered")) == 1
    assert inbox.read_text(encoding="utf-8").strip().count("\n") == 0  # one line


def test_attack_nag_the_same_people_repeatedly(db):
    cid = _approved(db)
    sends = [send_reminder_record(db, case_id=cid, stage="T+24h", outstanding=5)
             for _ in range(4)]
    assert sum(1 for s in sends if s["sent"]) == 1


def test_attack_open_a_second_case_for_the_same_incident(db):
    first = open_case(db, incident_key="inc-1", incident_source_type="osha_sir")
    transition(db, first["case_id"], DEDUPED, actor="t", reason_code="X",
               risk_assessment_id="risk-1")
    others = [open_case(db, incident_key="inc-1", incident_source_type="osha_sir")
              for _ in range(3)]
    assert all(o["case_id"] == first["case_id"] for o in others)
    assert all(o["created"] is False for o in others)


# ==========================================================================
# 铁律五 · 一切留痕
# ==========================================================================

def test_attack_a_transition_cannot_overwrite_an_earlier_one(db):
    """History grows; it never rewrites. Each move appends its own row."""
    case = open_case(db, incident_key="inc-1", incident_source_type="osha_sir")
    cid = case["case_id"]
    for state in (DEDUPED, EXTRACTED, CLASSIFIED):
        transition(db, cid, state, actor="t", reason_code="X")
    history = case_history(db, cid)
    assert len(history) == 4
    assert [row["seq"] for row in history] == sorted(row["seq"] for row in history)
    # A rejected move leaves no trace of success and no gap in the record.
    with pytest.raises(TransitionError):
        transition(db, cid, DISTRIBUTED, actor="attacker", reason_code="X")
    assert len(case_history(db, cid)) == 4


# ==========================================================================
# 结构性攻击
# ==========================================================================

def test_attack_erode_the_safety_kernel_through_accepted_diffs(db):
    """No chain of accepted proposals can switch off a kernel guarantee."""
    rp.initialize(db)
    pack = rp.active_pack(db)
    for key in sorted(rp.IMMUTABLE_KEYS):
        with pytest.raises(rp.RulePackError, match="不在可调项内"):
            rp._create_diff(db, pack, change_kind="disable",
                            change={"key": key, "set": False}, evidence={"why": "faster"})
    # A kernel-disabling payload is refused whether or not a pack exists.
    # Regression: initialize() used to return early on an existing pack and
    # never look at the payload, so this call silently did nothing.
    with pytest.raises(rp.RulePackError, match="安全内核"):
        rp.initialize(db, payload={**rp.DEFAULT_PAYLOAD,
                                   "require_human_approval_before_distribution": False})


def test_attack_overwrite_a_live_rule_pack_with_initialize(db):
    """Reconfiguring a live pack must go through the reviewed diff flow."""
    rp.initialize(db)
    with pytest.raises(rp.RulePackError, match="不能用 initialize 覆盖"):
        rp.initialize(db, payload={**rp.DEFAULT_PAYLOAD, "weekly_alert_budget": 999})
    assert rp.active_pack(db).get("weekly_alert_budget") == 5


def test_attack_mute_everything_to_silence_the_agent(db):
    """Muting is allowed - it is the site's call - but it is versioned and
    reversible, and it cannot reach the kernel."""
    rp.initialize(db)
    pack = rp.active_pack(db)
    diff = rp._create_diff(db, pack, change_kind="mute_hazard",
                           change={"key": "muted_hazards", "add": "haz-crush"},
                           evidence={"why": "noise"})
    rp.decide_diff(db, diff["diff_id"], decision="accepted", decided_by="EHS")
    assert rp.active_pack(db).get("muted_hazards") == ["haz-crush"]
    assert rp.active_pack(db).get("require_human_approval_before_distribution") is True
    rp.rollback(db, to_version="rp-v1", decided_by="EHS")
    assert rp.active_pack(db).get("muted_hazards") == []


@pytest.mark.parametrize("severity", [4, 5])
def test_attack_present_an_unresolvable_path_as_certain(tmp_path, severity):
    """A path we cannot resolve must never arrive dressed as a confident RED."""
    factory = tmp_path / f"f{severity}.db"
    connection = sqlite3.connect(factory)
    connection.executescript(FACTORY_SQL)
    connection.execute("DELETE FROM risk_assessments")
    connection.execute(
        "INSERT INTO risk_assessments VALUES('risk-u','RA-U','press-01','t1',"
        "'haz-crush','role-op','Unresolvable',?,20,'high',9,'medium',1,'approved')",
        (severity,),
    )
    connection.commit()
    connection.close()
    certificate = assess_paths(factory, as_of="2026-09-07")[0]
    decision = route_path(certificate)
    assert certificate.decision == "UNKNOWN"
    assert decision.level == LEVEL_YELLOW
    assert decision.level != LEVEL_RED
    assert decision.requires_human is True
