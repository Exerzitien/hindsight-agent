"""一键脚本自己也要被测。

``hindsight-demo`` 现在是别人接触这个项目的第一条命令——评委、队友、三天后
的自己。它此前覆盖率 0%，而这正是本次审计反复找到的同一种模式：最关键的
那条路径没有测试。

这里最重要的一条断言是 ``--reset`` 只清工作流状态。一个会顺手抹掉 105,996
条事故底库的 --reset，会让人在演示前一小时失去所有数据。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from http.server import HTTPServer
from pathlib import Path

import pytest

from hindsight_agent import demo
from hindsight_agent.workflow_db import ensure_workflow_schema, open_case, transition

from tests.test_content_orchestration import (  # noqa: F401  (fixture reuse)
    ScriptedGenerator,
    _good,
    corpus,
    factory,
    incidents,
)
from tests.test_end_to_end import _Handler


class _Settings:
    def __init__(self, root: Path, incident_db: Path, factory_db: Path) -> None:
        self.project_root = root
        self.incident_database = incident_db
        self.factory_database = factory_db


# --------------------------------------------------------------------------
# 前置检查
# --------------------------------------------------------------------------

def test_missing_data_names_the_command_that_fixes_it(tmp_path):
    settings = _Settings(tmp_path, tmp_path / "nope.db", tmp_path / "also-nope.db")
    with pytest.raises(demo.PreflightError) as caught:
        demo.check_data(settings)
    message = str(caught.value)
    for command in ("hindsight-incidents-import", "hindsight-factory-create-zh",
                    "hindsight-grounding-collect"):
        assert command in message


def test_an_empty_corpus_is_caught_before_anything_runs(tmp_path, incidents, factory):
    """语料是空的就起草不出任何东西，这个失败必须在跑筛选之前发生。"""
    corpus_dir = tmp_path / "data" / "corpus"
    corpus_dir.mkdir(parents=True)
    connection = sqlite3.connect(corpus_dir / "grounding.db")
    connection.executescript("CREATE TABLE corpus_passages(passage_id TEXT PRIMARY KEY);")
    connection.commit()
    connection.close()

    settings = _Settings(tmp_path, incidents, factory)
    with pytest.raises(demo.PreflightError, match="接地语料是空的"):
        demo.check_data(settings)


def test_missing_model_config_names_every_variable(monkeypatch):
    monkeypatch.setenv("HINDSIGHT_MODEL_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL_ID", raising=False)
    with pytest.raises(demo.PreflightError) as caught:
        demo.check_model()
    message = str(caught.value)
    assert "OPENAI_BASE_URL" in message and "OPENAI_MODEL_ID" in message


def test_an_unreachable_endpoint_fails_with_the_address_in_the_message(monkeypatch):
    """错误里必须带上它试过的地址——否则人不知道是不是配错了。"""
    monkeypatch.setenv("HINDSIGHT_MODEL_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("OPENAI_MODEL_ID", "nothing-here")
    with pytest.raises(demo.PreflightError) as caught:
        demo.check_model(timeout=3)
    assert "http://127.0.0.1:1/v1" in str(caught.value)


def test_the_model_check_actually_calls_the_model(monkeypatch):
    """只看环境变量设没设，等于把失败推迟到跑完筛选之后才爆。"""
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    server.seen_requests = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        host, port = server.server_address
        monkeypatch.setenv("HINDSIGHT_MODEL_PROVIDER", "openai")
        monkeypatch.setenv("OPENAI_BASE_URL", f"http://{host}:{port}/v1")
        monkeypatch.setenv("OPENAI_MODEL_ID", "stub-model")
        info = demo.check_model(timeout=15)
        assert info["checked"] is True
        assert info["model_id"] == "stub-model"
        assert server.seen_requests, "前置检查没有真的发出请求"
    finally:
        server.shutdown()
        server.server_close()


def test_a_non_openai_provider_skips_the_endpoint_check(monkeypatch):
    monkeypatch.setenv("HINDSIGHT_MODEL_PROVIDER", "ollama")
    info = demo.check_model()
    assert info["checked"] is False


# --------------------------------------------------------------------------
# --reset 的边界
# --------------------------------------------------------------------------

def test_reset_clears_workflow_state_and_nothing_else(incidents):
    """最重要的一条：--reset 绝不能碰事故底库。

    一个会顺手抹掉十万条事故的 --reset，会让人在演示前一小时失去所有数据。
    """
    ensure_workflow_schema(incidents)
    connection = sqlite3.connect(incidents)
    incidents_before = connection.execute(
        "SELECT COUNT(*) FROM osha_sir_incidents"
    ).fetchone()[0]
    connection.close()
    assert incidents_before > 0

    case = open_case(incidents, incident_key="inc-1", incident_source_type="osha_sir")
    transition(incidents, case["case_id"], "DEDUPED", actor="t", reason_code="T")

    cleared = demo.reset_workflow(incidents)
    assert cleared.get("cases") == 1

    connection = sqlite3.connect(incidents)
    assert connection.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 0
    # 事故底库一条没少
    assert connection.execute(
        "SELECT COUNT(*) FROM osha_sir_incidents"
    ).fetchone()[0] == incidents_before
    connection.close()


def test_reset_never_names_a_data_table():
    """白名单本身就是这条保证：常量里不该出现任何事故或语料表。"""
    forbidden = {"osha_sir_incidents", "osha_news_releases", "corpus_passages",
                 "corpus_documents", "corpus_snapshots", "source_snapshots",
                 "incident_factory_matches"}
    assert not (set(demo.WORKFLOW_TABLES) & forbidden)


def test_reset_on_a_fresh_database_is_not_an_error(tmp_path):
    fresh = tmp_path / "fresh.db"
    assert demo.reset_workflow(fresh) == {}
    assert fresh.exists()
