"""The approved body must exist, be retrievable, and be what workers receive.

Before this file, ``draft_for_routed`` discarded the rendered pack and kept only
its hash. Nothing stored the body, so the approval token bound a hash whose
content existed nowhere: a reviewer approved a string of hex, and every delivery
carried ``"body": {}``. Neither ``draft_for_routed`` nor ``approve_and_distribute``
had a single test, which is how that survived a full audit.

These tests pin the chain end to end - drafted body is stored, stored body is
re-hashed on the way out, and the thing delivered is the thing approved.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hindsight_agent import rule_pack
from hindsight_agent.distribution import register_channels
from hindsight_agent.learning_pack import hash_canonical_payload
from hindsight_agent.workflow import approve_and_distribute, draft_for_routed
from hindsight_agent.workflow_db import (
    CLASSIFIED,
    DEDUPED,
    EXTRACTED,
    PENDING_APPROVAL,
    PackArtifactError,
    connect,
    ensure_workflow_schema,
    load_pack_artifact,
    open_case,
    pack_artifact_for_case,
    store_pack_artifact,
    transition,
)

from tests.test_content_orchestration import (  # noqa: F401  (fixture reuse)
    ScriptedGenerator,
    _good,
    corpus,
    factory,
    incidents,
)
from tests.test_workflow import GOOD_NARRATIVE


#: 测试工厂夹具里 risk-1 的暴露岗位是 role-op，渠道注册表必须与之对应，
#: 否则定向分发会正确地定到一个没有渠道的岗位（那条路径由专门的测试覆盖）。
SITE_CHANNELS = (
    {"channel_id": "ch-press-en", "name": "冲压 A 班", "kind": "mock_inbox",
     "shift": "A", "role_id": "role-op", "language": "en", "member_count": 6},
    {"channel_id": "ch-press-es", "name": "冲压 A 班（西语）", "kind": "mock_inbox",
     "shift": "A", "role_id": "role-op", "language": "es", "member_count": 4},
)


def _classified_case(incidents_db: Path, incident_key: str = "inc-1") -> str:
    """Walk one case to CLASSIFIED, the state drafting starts from."""
    ensure_workflow_schema(incidents_db)
    case = open_case(incidents_db, incident_key=incident_key,
                     incident_source_type="osha_sir")
    case_id = case["case_id"]
    transition(incidents_db, case_id, DEDUPED, actor="test", reason_code="T")
    transition(incidents_db, case_id, EXTRACTED, actor="test", reason_code="T")
    transition(incidents_db, case_id, CLASSIFIED, actor="test", reason_code="T",
               level="RED", risk_assessment_id="risk-1")
    return case_id


def _routed(case_id: str, incident_key: str = "inc-1") -> list[dict]:
    return [{
        "case_id": case_id,
        "incident_key": incident_key,
        "record": {"narrative": GOOD_NARRATIVE, "source_title": "Press die crush"},
        "routing": {"level": "RED", "driving_path": "risk-1"},
        "hazards": ["haz-caught-in"],
    }]


def _drafted(incidents_db, factory_db, corpus_db):
    case_id = _classified_case(incidents_db)
    result = draft_for_routed(
        incident_database=incidents_db, factory_database=factory_db,
        corpus_database=corpus_db, routed=_routed(case_id),
        generator=ScriptedGenerator(_good()),
    )
    assert result["drafted"], result
    return case_id, result["drafted"][0]["content_hash"]


# --------------------------------------------------------------------------
# 存储本身
# --------------------------------------------------------------------------

def test_drafting_stores_the_body_it_queues_for_approval(incidents, factory, corpus):
    case_id, content_hash = _drafted(incidents, factory, corpus)

    stored = load_pack_artifact(incidents, content_hash)
    assert stored["case_id"] == case_id
    assert stored["pack"]["briefing"]["en"]["sections"]
    assert stored["pack"]["briefing"]["es"]["sections"]
    # The case points at exactly this body.
    assert pack_artifact_for_case(incidents, case_id)["content_hash"] == content_hash


def test_a_case_awaiting_approval_can_always_be_read(incidents, factory, corpus):
    """The state a person is asked to act on must never be unreadable."""
    case_id, _ = _drafted(incidents, factory, corpus)
    connection = connect(incidents)
    state = connection.execute(
        "SELECT state FROM cases WHERE case_id=?", (case_id,)
    ).fetchone()["state"]
    connection.close()
    assert state == PENDING_APPROVAL
    assert pack_artifact_for_case(incidents, case_id) is not None


def test_a_body_that_does_not_match_its_own_hash_is_refused(incidents, factory, corpus):
    case_id = _classified_case(incidents)
    pack = {"canonical": {"a": 1}, "content_hash": "0" * 64, "draft_id": "d1"}
    with pytest.raises(PackArtifactError, match="不一致"):
        store_pack_artifact(incidents, case_id=case_id, pack=pack, self_check={})


def test_a_pack_without_canonical_claims_cannot_be_stored(incidents, factory, corpus):
    case_id = _classified_case(incidents)
    with pytest.raises(PackArtifactError, match="canonical"):
        store_pack_artifact(incidents, case_id=case_id,
                            pack={"content_hash": "x"}, self_check={})


def test_tampering_with_a_stored_body_is_caught_on_read(incidents, factory, corpus):
    """The hash is recomputed from the stored claims, not trusted from its column."""
    _, content_hash = _drafted(incidents, factory, corpus)

    connection = sqlite3.connect(incidents)
    canonical = json.loads(connection.execute(
        "SELECT canonical_json FROM pack_artifacts WHERE content_hash=?",
        (content_hash,),
    ).fetchone()[0])
    canonical["briefing"][0]["text_en"] = "Remove the guard to work faster."
    connection.execute(
        "UPDATE pack_artifacts SET canonical_json=? WHERE content_hash=?",
        (json.dumps(canonical, ensure_ascii=False), content_hash),
    )
    connection.commit()
    connection.close()

    with pytest.raises(PackArtifactError, match="已被改动"):
        load_pack_artifact(incidents, content_hash)


def test_storing_the_same_claims_twice_is_one_row(incidents, factory, corpus):
    """Re-drafting identical claims must not fork the record a person approved."""
    case_id, content_hash = _drafted(incidents, factory, corpus)
    stored = load_pack_artifact(incidents, content_hash)
    again = store_pack_artifact(incidents, case_id=case_id,
                                pack=stored["pack"], self_check={})
    assert again == content_hash

    connection = sqlite3.connect(incidents)
    count = connection.execute("SELECT COUNT(*) FROM pack_artifacts").fetchone()[0]
    connection.close()
    assert count == 1


# --------------------------------------------------------------------------
# 审批与分发
# --------------------------------------------------------------------------

def test_approval_is_refused_when_no_body_can_be_produced(incidents, factory, corpus):
    """A case cannot become APPROVED against a hash with nothing behind it."""
    case_id = _classified_case(incidents)
    rule_pack.initialize(incidents)

    with pytest.raises(PackArtifactError, match="没有哈希"):
        approve_and_distribute(
            incident_database=incidents, case_id=case_id,
            pack_content_hash="deadbeef" * 8, approver="ehs-lead",
        )

    connection = connect(incidents)
    state = connection.execute(
        "SELECT state FROM cases WHERE case_id=?", (case_id,)
    ).fetchone()["state"]
    connection.close()
    # Refused before the state changed: the case is still where it was.
    assert state == CLASSIFIED


def test_what_is_delivered_is_what_was_approved(incidents, factory, corpus):
    case_id, content_hash = _drafted(incidents, factory, corpus)
    rule_pack.initialize(incidents)
    register_channels(incidents, SITE_CHANNELS)

    result = approve_and_distribute(
        incident_database=incidents, case_id=case_id,
        pack_content_hash=content_hash, approver="ehs-lead",
        factory_database=factory,
    )
    delivered = [d for d in result["deliveries"] if d.get("delivered")]
    assert delivered, result

    connection = sqlite3.connect(incidents)
    rows = connection.execute(
        "SELECT pack_content_hash FROM deliveries WHERE case_id=?", (case_id,)
    ).fetchall()
    connection.close()
    # Every delivery carries the hash of the body the approver read.
    assert {row[0] for row in rows} == {content_hash}


def test_the_delivered_body_is_not_empty_and_hashes_back(incidents, factory, corpus,
                                                         tmp_path, monkeypatch):
    """Regression: every delivery used to ship ``body: {}``."""
    import hindsight_agent.distribution as distribution

    inbox = tmp_path / "inbox.jsonl"
    monkeypatch.setattr(distribution, "MOCK_INBOX", inbox)

    case_id, content_hash = _drafted(incidents, factory, corpus)
    rule_pack.initialize(incidents)
    register_channels(incidents, SITE_CHANNELS)
    approve_and_distribute(
        incident_database=incidents, case_id=case_id,
        pack_content_hash=content_hash, approver="ehs-lead",
        factory_database=factory,
    )

    lines = [json.loads(line) for line in inbox.read_text(encoding="utf-8").splitlines()]
    assert lines, "nothing reached the outbox"
    for line in lines:
        body = line["body"]
        assert body, "delivery shipped an empty body"
        assert body["content_hash"] == content_hash
        assert body["briefing"]["language"] == line["language"]
        assert body["briefing"]["sections"]
        assert body["toolbox_talk"]["language"] == line["language"]

    languages = {line["language"] for line in lines}
    assert {"en", "es"} <= languages


def test_both_languages_ship_under_one_approved_hash(incidents, factory, corpus):
    """EN and ES render from the same claims, so one approval covers both."""
    _, content_hash = _drafted(incidents, factory, corpus)
    stored = load_pack_artifact(incidents, content_hash)
    pack = stored["pack"]
    assert pack["briefing"]["en"]["content_hash"] == content_hash
    assert pack["briefing"]["es"]["content_hash"] == content_hash
    assert hash_canonical_payload(pack["canonical"]) == content_hash


def test_targeting_never_reaches_for_a_globally_configured_site(
    incidents, factory, corpus, monkeypatch
):
    """Regression: 定向分发曾直接 load_settings().factory_database，
    于是在干净检出里崩溃，在双站点安装里会拿另一个厂的登记册回答本厂的问题。"""
    import hindsight_agent.workflow as workflow_module

    def _explode():
        raise AssertionError("approve_and_distribute 不得读取全局配置的站点库")

    monkeypatch.setattr("hindsight_agent.config.load_settings", _explode)

    case_id, content_hash = _drafted(incidents, factory, corpus)
    rule_pack.initialize(incidents)
    register_channels(incidents, SITE_CHANNELS)
    result = approve_and_distribute(
        incident_database=incidents, case_id=case_id,
        pack_content_hash=content_hash, approver="ehs-lead",
        factory_database=factory,
    )
    assert any(d.get("delivered") for d in result["deliveries"])


def test_a_missing_site_register_degrades_instead_of_breaking(
    incidents, factory, corpus, tmp_path
):
    """读不到登记册时退回按语言分发，而不是让整条审批链崩掉。"""
    case_id, content_hash = _drafted(incidents, factory, corpus)
    rule_pack.initialize(incidents)
    register_channels(incidents, SITE_CHANNELS)
    result = approve_and_distribute(
        incident_database=incidents, case_id=case_id,
        pack_content_hash=content_hash, approver="ehs-lead",
        factory_database=tmp_path / "does-not-exist.db",
    )
    assert any(d.get("delivered") for d in result["deliveries"])


def test_approving_into_a_role_with_no_channel_is_loud(incidents, factory, corpus):
    """批准之后送达零人，必须留下明确信号，而不是安静地返回一个空列表。"""
    case_id, content_hash = _drafted(incidents, factory, corpus)
    rule_pack.initialize(incidents)
    register_channels(incidents)  # 默认渠道全部不匹配 role-op

    result = approve_and_distribute(
        incident_database=incidents, case_id=case_id,
        pack_content_hash=content_hash, approver="ehs-lead",
        factory_database=factory,
    )
    assert result["delivered_to"] == 0
    assert result["warning"] == "APPROVED_BUT_NO_CHANNEL"
    assert "没有注册任何渠道" in result["detail"]

    connection = sqlite3.connect(incidents)
    kinds = [r[0] for r in connection.execute(
        "SELECT signal_kind FROM feedback_signals WHERE case_id=?", (case_id,)
    )]
    connection.close()
    assert "NO_CHANNEL_FOR_ROLE" in kinds


# --------------------------------------------------------------------------
# 端点出问题时，这一轮筛选不能白跑
# --------------------------------------------------------------------------

def test_a_failing_model_costs_one_case_not_the_whole_run(incidents, factory, corpus):
    """一个抛异常的模型端点，不该丢掉刚跑完的整轮筛选。

    起草是逐条调模型的：第三条超时，不该让前两条已经写好的稿件跟着消失，
    也不该让 500 条事故的筛选结果作废。失败的那条落到「需人工补写」，
    理由码写明它是失败而不是写得不够，于是它出现在控制台里，而不是消失在
    一条日志里。
    """
    from hindsight_agent.workflow_db import NEEDS_HUMAN_WRITER, connect

    good_case = _classified_case(incidents, "inc-1")
    bad_case = _classified_case(incidents, "inc-2")

    class _DiesOnTheSecondCall:
        model_id = "flaky"

        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, prompt: str):
            self.calls += 1
            if self.calls == 1:
                return _good()
            raise TimeoutError("端点没有响应")

    routed = _routed(good_case, "inc-1") + _routed(bad_case, "inc-2")
    result = draft_for_routed(
        incident_database=incidents, factory_database=factory,
        corpus_database=corpus, routed=routed,
        generator=_DiesOnTheSecondCall(),
    )

    assert len(result["drafted"]) == 1, "好的那一条也被拖下水了"
    assert [f["case_id"] for f in result["failed"]] == [bad_case]
    assert "TimeoutError" in result["failed"][0]["error"]

    connection = connect(incidents)
    try:
        state = connection.execute(
            "SELECT state FROM cases WHERE case_id=?", (bad_case,)
        ).fetchone()["state"]
        reason = connection.execute(
            "SELECT reason_code FROM case_transitions WHERE case_id=? "
            "ORDER BY seq DESC LIMIT 1", (bad_case,)
        ).fetchone()["reason_code"]
    finally:
        connection.close()
    assert state == NEEDS_HUMAN_WRITER
    assert reason == "DRAFT_FAILED"
