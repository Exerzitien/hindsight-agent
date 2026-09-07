"""One run of the whole chain, over a real HTTP model server.

Everything else in this suite injects a Python callable as the generator, which
proves the orchestration but never touches the transport. This file stands up an
actual OpenAI-compatible HTTP server and drives:

    起草 → G4 自检 → 待审批 → 批准（令牌）→ 幂等分发 → 完成登记 → 闭环

The stub answers by reading the ALLOWED CITATIONS out of the prompt it was sent
and citing only ids from that list, which is what a correctly behaving model
does. It is a transport and orchestration harness - the wording it returns is
fixture text, not safety guidance, and nothing here is shipped as demo content.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from hindsight_agent.content import JsonClaimGenerator
from hindsight_agent.distribution import (
    close_case,
    completion_status,
    record_completion,
    register_channels,
)
from hindsight_agent.workflow import (
    approve_and_distribute,
    draft_for_routed,
    fetch_incidents,
    screen_and_route,
)
from hindsight_agent.workflow_db import (
    CLOSED,
    DISTRIBUTED,
    PENDING_APPROVAL,
    TransitionError,
    connect,
    load_pack_artifact,
)

from tests.test_content_orchestration import (  # noqa: F401  (fixture reuse)
    corpus,
    factory,
    incidents,
)
from tests.test_pack_artifacts import SITE_CHANNELS


# --------------------------------------------------------------------------
# 一个真的 OpenAI 兼容服务
# --------------------------------------------------------------------------

#: 「ALLOWED CITATIONS」这几个字在系统提示词和输出契约里都出现过，
#: 所以必须锚定真正的小节标题——即后面紧跟一个 JSON 数组的那一处。
_ALLOWED_SECTION = "ALLOWED CITATIONS\n["


def _allowed_ids(prompt: str) -> list[str]:
    """Read the closed citation set straight out of the prompt."""
    index = prompt.index(_ALLOWED_SECTION)
    body = prompt[index + len(_ALLOWED_SECTION) - 1:]
    return [item["id"] for item in extract_json_list(body)]


def extract_json_list(text: str) -> list[dict]:
    depth, start = 0, -1
    for index, char in enumerate(text):
        if char == "[":
            if depth == 0:
                start = index
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0 and start >= 0:
                return json.loads(text[start : index + 1])
    raise AssertionError("prompt 中没有 ALLOWED CITATIONS 列表")


def _reply_for(prompt: str) -> str:
    # 真实服务器对任何提示词都会回点什么。健康检查不带 ALLOWED CITATIONS，
    # 桩服务必须照样应答——否则连接被直接切断，报出来的错会指向客户端，
    # 而真正坏掉的是服务端。
    if _ALLOWED_SECTION not in prompt:
        return "ok"
    allowed = _allowed_ids(prompt)
    incident = next(i for i in allowed if i.startswith("incident:"))
    site = next(i for i in allowed if i.startswith("site:"))
    controls = [i for i in allowed if i.startswith("control:")]
    control = controls[0] if controls else site
    control_id = control.split(":", 1)[1] if controls else None

    claims = [
        {"claim_id": "c1", "claim_type": "INCIDENT_FACT",
         "text_en": "A worker was injured while the equipment was running.",
         "text_es": "Un trabajador resultó herido con el equipo en marcha.",
         "citations": [incident], "quote": None, "quote_citation": None,
         "control_id": None},
        {"claim_id": "c2", "claim_type": "SITE_FACT",
         "text_en": "The same task is performed on this line.",
         "text_es": "La misma tarea se realiza en esta línea.",
         "citations": [site], "quote": None, "quote_citation": None,
         "control_id": None},
        {"claim_id": "c3", "claim_type": "CONTROL",
         "text_en": "Confirm the approved control is in place before the shift.",
         "text_es": "Confirme que el control aprobado esté colocado antes del turno.",
         "citations": [control], "quote": None, "quote_citation": None,
         "control_id": control_id},
    ]
    payload = {
        "briefing_claims": claims,
        "talk_claims": claims[:2],
        "quiz": [{"question_id": "q1", "question_en": "What do you confirm first?",
                  "question_es": "¿Qué confirma primero?",
                  "options_en": ["The approved control", "Nothing"],
                  "options_es": ["El control aprobado", "Nada"],
                  "correct_index": 0, "claim_id": "c1"}],
        "unknowns": ["Whether the control was in place is not established."],
    }
    # 故意包一层思考前缀和 markdown 围栏：自建推理服务经常这么回，
    # 解析器必须能穿过它们。
    return (
        "<think>The user wants typed claims.</think>\n"
        "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
    )


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静音
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length) or b"{}")
        self.server.seen_requests.append(request)
        prompt = "\n".join(m["content"] for m in request["messages"])
        body = json.dumps({
            "id": "stub", "object": "chat.completion", "model": request["model"],
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant",
                                     "content": _reply_for(prompt)}}],
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def model_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    server.seen_requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture()
def generator(model_server):
    host, port = model_server.server_address
    return JsonClaimGenerator(
        base_url=f"http://{host}:{port}/v1",
        model_id="stub-model", api_key="",
    )


# --------------------------------------------------------------------------
# 全链路
# --------------------------------------------------------------------------

def test_the_whole_chain_runs_over_http(incidents, factory, corpus, generator,
                                        model_server, tmp_path, monkeypatch):
    import hindsight_agent.distribution as distribution

    inbox = tmp_path / "inbox.jsonl"
    monkeypatch.setattr(distribution, "MOCK_INBOX", inbox)

    register_channels(incidents, SITE_CHANNELS)

    # 0. 从原始事故开始筛选与路由——真正的起点，不是手工摆好的案例
    records = fetch_incidents(incidents, limit=50)
    assert records, "事故底库为空"
    screened = screen_and_route(
        incident_database=incidents, factory_database=factory,
        records=records, as_of="2026-09-06", run_id="e2e",
    )
    assert screened["rule_pack_version"]
    assert screened["routed"], f"没有任何事故被路由：{screened['counts']}"
    case_id = screened["routed"][0]["case_id"]

    # 1. 起草：真的走了一次 HTTP
    drafted = draft_for_routed(
        incident_database=incidents, factory_database=factory,
        corpus_database=corpus, routed=screened["routed"], generator=generator,
        run_id="e2e",
    )
    assert drafted["drafted"], drafted
    assert model_server.seen_requests, "没有任何请求到达模型服务"
    assert model_server.seen_requests[0]["model"] == "stub-model"

    content_hash = drafted["drafted"][0]["content_hash"]

    # 2. 正文可按哈希取回，且能重新哈希验证
    stored = load_pack_artifact(incidents, content_hash)
    assert stored["pack"]["briefing"]["en"]["sections"]
    assert stored["pack"]["briefing"]["es"]["sections"]

    # 3. 停在待审批——机器不会替人往下走
    connection = connect(incidents)
    assert connection.execute("SELECT state FROM cases WHERE case_id=?",
                             (case_id,)).fetchone()["state"] == PENDING_APPROVAL
    connection.close()

    # 4. 批准 → 令牌 → 分发
    result = approve_and_distribute(
        incident_database=incidents, case_id=case_id,
        pack_content_hash=content_hash, approver="ehs-lead",
        factory_database=factory,
    )
    assert result["delivered_to"] >= 1, result
    connection = connect(incidents)
    assert connection.execute("SELECT state FROM cases WHERE case_id=?",
                             (case_id,)).fetchone()["state"] == DISTRIBUTED
    connection.close()

    # 5. 送到一线的是被批准的那份正文，不是空壳
    lines = [json.loads(l) for l in inbox.read_text(encoding="utf-8").splitlines()]
    assert lines
    for line in lines:
        assert line["body"]["content_hash"] == content_hash
        assert line["body"]["briefing"]["sections"]

    # 6. 已分发的案例不能被再批准一次：状态机拒绝，而不是悄悄再发一遍
    with pytest.raises(TransitionError):
        approve_and_distribute(
            incident_database=incidents, case_id=case_id,
            pack_content_hash=content_hash, approver="ehs-lead",
            factory_database=factory,
        )
    connection = sqlite3.connect(incidents)
    delivery_count = connection.execute(
        "SELECT COUNT(*) FROM deliveries WHERE case_id=?", (case_id,)
    ).fetchone()[0]
    connection.close()
    assert delivery_count == len(lines), "重复批准不得产生新的投递"

    # 7. 完成登记与闭环
    connection = sqlite3.connect(incidents)
    delivery_id, worker = connection.execute(
        "SELECT d.delivery_id, c.worker_ref FROM deliveries AS d "
        "JOIN completions AS c ON c.delivery_id=d.delivery_id "
        "WHERE d.case_id=? LIMIT 1", (case_id,)
    ).fetchone()
    connection.close()
    record_completion(incidents, delivery_id=delivery_id, worker_ref=worker,
                      quiz_score=1.0, quiz_answers=[0])
    status = completion_status(incidents, case_id)
    assert status["completed"] >= 1

    # 8. 闭环，并交回一条完整的可审计链：
    #    外部事故 → 本厂危险路径 → 级别 → 规则包版本 → 稿件哈希 → 完成情况 → 全部状态迁移
    closed = close_case(incidents, case_id, actor="ehs-lead")
    assert closed["closed"] is True
    chain = closed["chain"]
    assert chain["incident"] == records[0]["incident_key"]
    assert chain["risk_path"]
    assert chain["level"] in ("RED", "YELLOW")
    assert chain["rule_pack_version"]
    assert chain["pack_content_hash"] == content_hash
    assert chain["completion"]["completed"] >= 1

    states = [row["to_state"] for row in chain["transitions"]]
    for expected in ("CLASSIFIED", "DRAFTED", "SELF_CHECK_OK", "PENDING_APPROVAL",
                     "APPROVED", "DISTRIBUTED", "TRACKING", "CLOSED"):
        assert expected in states, f"审计链缺少 {expected}：{states}"
    # 顺序不可重排：seq 单调递增
    seqs = [row["seq"] for row in chain["transitions"]]
    assert seqs == sorted(seqs)

    connection = connect(incidents)
    assert connection.execute("SELECT state FROM cases WHERE case_id=?",
                             (case_id,)).fetchone()["state"] == CLOSED
    connection.close()


def test_the_parser_survives_fences_and_a_reasoning_prefix(generator):
    """自建推理服务常在 JSON 前后加东西；解析器必须穿过去。"""
    prompt = (
        'ALLOWED CITATIONS\n[{"id": "incident:inc-1"}, {"id": "site:risk-1"}, '
        '{"id": "control:control-guard"}]\n'
    )
    draft = generator(prompt)
    assert [c.claim_id for c in draft.briefing_claims] == ["c1", "c2", "c3"]
    assert draft.briefing_claims[2].control_id == "control-guard"
    assert draft.unknowns


def test_a_model_that_answers_badly_gets_exactly_one_retry(model_server):
    """第一次答不出合法 JSON 时重试一次，绝不成环。"""
    calls = {"n": 0}
    original = _Handler.do_POST

    class _Flaky(_Handler):
        def do_POST(self):
            calls["n"] += 1
            if calls["n"] == 1:
                body = json.dumps({
                    "id": "stub", "object": "chat.completion", "model": "stub-model",
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant",
                                             "content": "I cannot do that."}}],
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            original(self)

    server = HTTPServer(("127.0.0.1", 0), _Flaky)
    server.seen_requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        gen = JsonClaimGenerator(base_url=f"http://{host}:{port}/v1",
                                 model_id="stub-model")
        draft = gen(
            'ALLOWED CITATIONS\n[{"id": "incident:inc-1"}, {"id": "site:risk-1"}]\n'
        )
        assert draft.briefing_claims
        assert calls["n"] == 2  # 恰好一次重试
    finally:
        server.shutdown()
        server.server_close()
