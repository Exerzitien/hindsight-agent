"""D3：没人读完就催，催两次就升级——而且绝不重复轰炸。

这条链是产品承诺的一部分（「分发不是终点，读完才是」），但此前
``due_reminders`` 与 ``send_reminder_record`` 一行测试都没有。一个会重复
发提醒的安全系统，会在两周内被现场关掉——这正是本项目立项要消灭的失败模式。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from hindsight_agent import rule_pack
from hindsight_agent.distribution import (
    REMINDER_T24,
    REMINDER_T72,
    due_reminders,
    record_completion,
    register_channels,
    send_reminder_record,
    withdraw,
)
from hindsight_agent.workflow import approve_and_distribute, run_reminders

from tests.test_content_orchestration import (  # noqa: F401  (fixture reuse)
    ScriptedGenerator,
    _good,
    corpus,
    factory,
    incidents,
)
from tests.test_pack_artifacts import SITE_CHANNELS, _drafted


@pytest.fixture()
def distributed(incidents, factory, corpus):
    """一条已分发、还没人读完的案例。"""
    case_id, content_hash = _drafted(incidents, factory, corpus)
    rule_pack.initialize(incidents)
    register_channels(incidents, SITE_CHANNELS)
    result = approve_and_distribute(
        incident_database=incidents, case_id=case_id,
        pack_content_hash=content_hash, approver="ehs-lead",
        factory_database=factory,
    )
    assert result["delivered_to"] >= 1
    return case_id


def _hours_later(incidents_db, hours: int) -> str:
    connection = sqlite3.connect(incidents_db)
    first = connection.execute(
        "SELECT MIN(delivered_at) FROM deliveries WHERE status='delivered'"
    ).fetchone()[0]
    connection.close()
    return (datetime.fromisoformat(first) + timedelta(hours=hours)).isoformat()


def test_nobody_is_chased_before_24_hours(incidents, distributed):
    """刚发出去就催人，是最快把系统变成噪音的做法。"""
    assert due_reminders(incidents, now=_hours_later(incidents, 1)) == []
    assert due_reminders(incidents, now=_hours_later(incidents, 23)) == []


def test_at_24_hours_the_outstanding_are_chased(incidents, distributed):
    due = due_reminders(incidents, now=_hours_later(incidents, 25))
    assert len(due) == 1
    assert due[0]["case_id"] == distributed
    assert due[0]["stage"] == REMINDER_T24
    assert due[0]["outstanding"] > 0


def test_at_72_hours_it_escalates_rather_than_repeating(incidents, distributed):
    due = due_reminders(incidents, now=_hours_later(incidents, 73))
    assert due[0]["stage"] == REMINDER_T72
    sent = send_reminder_record(incidents, case_id=distributed,
                                stage=REMINDER_T72, outstanding=due[0]["outstanding"])
    assert sent["escalated"] is True


def test_a_re_run_does_not_nag_twice(incidents, distributed):
    """幂等键 = 案例 + 阶段。重跑流水线不该让同一批人再被通知一次。"""
    first = send_reminder_record(incidents, case_id=distributed,
                                 stage=REMINDER_T24, outstanding=6)
    second = send_reminder_record(incidents, case_id=distributed,
                                  stage=REMINDER_T24, outstanding=6)
    assert first["sent"] is True
    assert second["sent"] is False and second["duplicate"] is True
    assert second["reminder_id"] == first["reminder_id"]

    connection = sqlite3.connect(incidents)
    count = connection.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]
    connection.close()
    assert count == 1

    # 但升级是不同的阶段，它应当照发
    escalation = send_reminder_record(incidents, case_id=distributed,
                                      stage=REMINDER_T72, outstanding=6)
    assert escalation["sent"] is True


def test_everyone_finishing_stops_the_chase(incidents, distributed):
    """读完了就不该再被打扰——这是「值得信任」而不是「合规」的部分。"""
    connection = sqlite3.connect(incidents)
    rows = connection.execute(
        "SELECT c.delivery_id, c.worker_ref FROM completions AS c "
        "JOIN deliveries AS d ON d.delivery_id=c.delivery_id WHERE d.case_id=?",
        (distributed,),
    ).fetchall()
    connection.close()
    for delivery_id, worker_ref in rows:
        record_completion(incidents, delivery_id=delivery_id,
                          worker_ref=worker_ref, quiz_score=1.0)

    assert due_reminders(incidents, now=_hours_later(incidents, 100)) == []


def test_run_reminders_is_the_same_thing_end_to_end(incidents, distributed):
    sent = run_reminders(incidents, now=_hours_later(incidents, 25))
    assert len(sent) == 1 and sent[0]["sent"] is True
    # 立刻再跑一次：一条都不该再发
    again = run_reminders(incidents, now=_hours_later(incidents, 26))
    assert all(item["sent"] is False for item in again)


def test_a_withdrawn_delivery_is_recorded_never_deleted(incidents, distributed):
    """撤回改状态，不删记录：发生过的事不能从审计里消失。"""
    before = due_reminders(incidents, now=_hours_later(incidents, 100))
    assert before, "撤回前本来应当有人要催"
    outstanding_before = before[0]["outstanding"]

    connection = sqlite3.connect(incidents)
    delivery_id, audience = connection.execute(
        "SELECT delivery_id, audience_size FROM deliveries WHERE case_id=?",
        (distributed,),
    ).fetchone()
    connection.close()

    result = withdraw(incidents, delivery_id, reason="稿件引用有误")
    assert result["withdrawn"] is True

    connection = sqlite3.connect(incidents)
    status, withdrawn_at = connection.execute(
        "SELECT status, withdrawn_at FROM deliveries WHERE delivery_id=?",
        (delivery_id,),
    ).fetchone()
    still_there = connection.execute(
        "SELECT COUNT(*) FROM deliveries WHERE delivery_id=?", (delivery_id,)
    ).fetchone()[0]
    connection.close()
    assert status == "withdrawn"
    assert withdrawn_at
    assert still_there == 1

    # 被撤回那条渠道上的人不再被催；其他渠道照旧——撤回是针对一次投递的，
    # 不是把整条案例静音。
    after = due_reminders(incidents, now=_hours_later(incidents, 100))
    assert after, "其他渠道仍未读完，仍应继续催"
    assert after[0]["outstanding"] == outstanding_before - audience
