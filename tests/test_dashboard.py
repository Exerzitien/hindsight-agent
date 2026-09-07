"""The console must show what the architecture says decides things.

dashboard.py had 0% coverage, and that is how it drifted: it still presented the
retrieval relevance score as the decision surface long after the decision had
moved to residual path verification, and its pack renderer read a key
(``sections``) that the toolbox talk renderer does not emit (``blocks``) - a
mismatch that renders an empty screen without raising anything.

These tests exercise the console's data functions against real schemas and drive
its renderers through a recording stand-in for streamlit, so a renamed key fails
here instead of in front of a judge.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from hindsight_agent import dashboard
from hindsight_agent.learning_pack import render_pack
from hindsight_agent.transfer import LEVEL_RED, LEVEL_YELLOW, assess_paths
from hindsight_agent.workflow_db import (
    CLASSIFIED,
    DEDUPED,
    EXTRACTED,
    PENDING_APPROVAL,
    ensure_workflow_schema,
    open_case,
    transition,
)

from tests.test_content_orchestration import (  # noqa: F401  (fixture reuse)
    ScriptedGenerator,
    _good,
    corpus,
    factory,
    incidents,
)
from tests.test_pack_artifacts import _classified_case, _routed
from tests.test_workflow import GOOD_NARRATIVE


# --------------------------------------------------------------------------
# 一个会记录调用的假 streamlit
# --------------------------------------------------------------------------

class RecordingStreamlit:
    """Captures everything the renderers write, so tests can assert on it."""

    def __init__(self) -> None:
        self.text: list[str] = []

    # 文本类
    def markdown(self, body="", **kwargs): self.text.append(str(body))
    def caption(self, body="", **kwargs): self.text.append(str(body))
    def write(self, body="", **kwargs): self.text.append(str(body))
    def subheader(self, body="", **kwargs): self.text.append(str(body))
    def success(self, body="", **kwargs): self.text.append(str(body))
    def warning(self, body="", **kwargs): self.text.append(str(body))
    def error(self, body="", **kwargs): self.text.append(str(body))
    def info(self, body="", **kwargs): self.text.append(str(body))
    def dataframe(self, data=None, **kwargs): self.text.append(repr(data))
    def metric(self, label="", value="", **kwargs):
        self.text.append(f"{label}={value}")

    # 容器类
    @contextmanager
    def _ctx(self):
        yield self

    def columns(self, spec, **kwargs):
        count = spec if isinstance(spec, int) else len(spec)
        return [_Column(self) for _ in range(count)]

    def tabs(self, labels, **kwargs):
        return [_Column(self) for _ in labels]

    def expander(self, label="", **kwargs):
        self.text.append(str(label))
        return self._ctx()

    def joined(self) -> str:
        return "\n".join(self.text)


class _Column:
    def __init__(self, parent: RecordingStreamlit) -> None:
        self._parent = parent

    def __enter__(self): return self._parent
    def __exit__(self, *exc): return False
    def __getattr__(self, name): return getattr(self._parent, name)


# --------------------------------------------------------------------------
# 漏斗与队列
# --------------------------------------------------------------------------

def test_funnel_counts_reports_the_shape_of_the_week(incidents):
    """考察多少、归档多少、留给人多少——这三个数是本项目的主张本身。"""
    ensure_workflow_schema(incidents)
    kept = _classified_case(incidents, "inc-1")
    transition(incidents, kept, "DRAFTED", actor="t", reason_code="T")
    transition(incidents, kept, "SELF_CHECK_OK", actor="t", reason_code="T")
    transition(incidents, kept, PENDING_APPROVAL, actor="t", reason_code="T")

    for key in ("inc-2", "inc-3", "inc-4"):
        case = open_case(incidents, incident_key=key, incident_source_type="osha_sir")
        cid = case["case_id"]
        transition(incidents, cid, DEDUPED, actor="t", reason_code="T")
        transition(incidents, cid, EXTRACTED, actor="t", reason_code="T")
        transition(incidents, cid, CLASSIFIED, actor="t", reason_code="T",
                   level="GREEN")
        transition(incidents, cid, "ARCHIVED", actor="t", reason_code="GREEN")

    counts = dashboard.funnel_counts(incidents)
    assert counts["considered"] == 4
    assert counts["archived"] == 3
    assert counts["queued"] == 1
    assert counts["red"] == 1
    assert counts["yellow"] == 0


def test_queue_puts_red_before_yellow_and_carries_the_incident(incidents):
    ensure_workflow_schema(incidents)
    yellow = open_case(incidents, incident_key="inc-2",
                       incident_source_type="osha_sir")["case_id"]
    transition(incidents, yellow, DEDUPED, actor="t", reason_code="T")
    transition(incidents, yellow, EXTRACTED, actor="t", reason_code="T")
    transition(incidents, yellow, CLASSIFIED, actor="t", reason_code="T",
               level=LEVEL_YELLOW)
    transition(incidents, yellow, "DRAFTED", actor="t", reason_code="T")
    transition(incidents, yellow, "SELF_CHECK_OK", actor="t", reason_code="T")
    transition(incidents, yellow, PENDING_APPROVAL, actor="t", reason_code="T")

    red = _classified_case(incidents, "inc-1")
    transition(incidents, red, "DRAFTED", actor="t", reason_code="T")
    transition(incidents, red, "SELF_CHECK_OK", actor="t", reason_code="T")
    transition(incidents, red, PENDING_APPROVAL, actor="t", reason_code="T")

    rows = dashboard.queue_rows(incidents)
    assert [row["level"] for row in rows] == [LEVEL_RED, LEVEL_YELLOW]
    # The join reaches the incident, so the queue reads as incidents not ids.
    assert rows[0]["narrative"] == GOOD_NARRATIVE
    assert rows[0]["event_title"]


def test_archived_cases_never_occupy_the_queue(incidents):
    """静默归档的含义就是：它不出现在任何人的待办里。"""
    ensure_workflow_schema(incidents)
    cid = _classified_case(incidents, "inc-1")
    transition(incidents, cid, "ARCHIVED", actor="t", reason_code="GREEN")
    assert dashboard.queue_rows(incidents) == []


# --------------------------------------------------------------------------
# 渲染：把键名钉死
# --------------------------------------------------------------------------

def test_pack_renderer_shows_both_languages_the_talk_and_the_citations(
    incidents, factory, corpus
):
    """Regression: the renderer used to read ``sections`` on the toolbox talk,
    which that renderer does not emit, so the script silently disappeared."""
    from hindsight_agent.content import generate_learning_draft

    result = generate_learning_draft(
        corpus_database=corpus, factory_database=factory,
        incident_database=incidents, risk_assessment_id="risk-1",
        incident_key="inc-1", incident_narrative=GOOD_NARRATIVE,
        incident_title="Press die crush", generator=ScriptedGenerator(_good()),
    )
    pack = render_pack(result["draft"])

    st = RecordingStreamlit()
    dashboard.render_pack_body(st, pack)
    rendered = st.joined()

    assert "A hand was caught between the dies." in rendered      # EN briefing
    assert "Una mano quedó atrapada entre los troqueles." in rendered  # ES briefing
    assert "班前会脚本" in rendered                                 # talk block rendered
    assert "What happened somewhere else" in rendered             # a talk heading
    assert "incident:inc-1" in rendered                           # citation shown
    assert "What do you check?" in rendered                       # quiz shown


def test_certificate_renderer_shows_the_controls_and_the_evidence(factory):
    certificates = assess_paths(factory, as_of="2026-09-06")
    assert certificates
    st = RecordingStreamlit()
    dashboard.render_certificate(st, certificates[0])
    rendered = st.joined()

    assert certificates[0].assessment_code in rendered
    assert "评估基准日" in rendered
    assert "判定版本" in rendered
    # Whichever way it went, the person is told which controls were involved.
    assert ("切断这条路径的控制" in rendered
            or "路径上未能切断的控制" in rendered
            or "缺失的关键事实" in rendered)


def test_the_four_approval_checks_are_all_distinct_and_present():
    """A2 的人类问责被拆成四条明确的确认，缺一条都不算。"""
    keys = [key for key, _ in dashboard.APPROVAL_CHECKS]
    assert len(keys) == 4
    assert len(set(keys)) == 4
    assert all(label.strip() for _, label in dashboard.APPROVAL_CHECKS)


# --------------------------------------------------------------------------
# 给人看的数字必须是真的
# --------------------------------------------------------------------------

def _archive_many(incidents_db, count: int, reason: str = "INDUSTRY_NOT_NEIGHBOUR"):
    ensure_workflow_schema(incidents_db)
    for index in range(count):
        cid = open_case(incidents_db, incident_key=f"bulk-{index}",
                        incident_source_type="osha_sir")["case_id"]
        transition(incidents_db, cid, DEDUPED, actor="t", reason_code="T")
        transition(incidents_db, cid, EXTRACTED, actor="t", reason_code="T")
        transition(incidents_db, cid, CLASSIFIED, actor="t", reason_code=reason,
                   level="NOT_RELEVANT")
        transition(incidents_db, cid, "ARCHIVED", actor="t", reason_code=reason)


def test_the_archive_count_is_a_count_not_a_query_limit(incidents):
    """Regression: ``archived`` used to report the LIMIT, so 495 archived cases
    were announced as 200 - the one number that measures this system's value,
    reported systematically low."""
    from hindsight_agent.screening import weekly_archive_digest

    _archive_many(incidents, 30)
    digest = weekly_archive_digest(incidents, limit=10)
    assert digest["archived"] == 30
    assert len(digest["lines"]) == 10
    assert digest["lines_truncated"] is True
    # The reason breakdown must cover everything, not just the printed page.
    assert sum(digest["by_reason_code"].values()) == 30


def test_the_digest_counts_routed_cases_as_awaiting_a_person(incidents):
    """Regression: a routed RED sitting in CLASSIFIED was announced as
    「需要你决定 0 条」 while the throttle simultaneously suppressed further
    alerts for that same path. Both surfaces went quiet at once."""
    from hindsight_agent.distribution import monday_digest

    _classified_case(incidents, "inc-1")  # RED, routed, not yet drafted
    digest = monday_digest(incidents)
    assert digest["awaiting_you"] == 1
    assert "需要你决定 1 条" in digest["headline"]
