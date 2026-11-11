"""提示词必须装得进模型真正的上下文窗口。

这个文件对应本项目第一次接上真实本地端点时撞到的墙：起草提示词稳定在
4300 token 左右，而 LM Studio 默认只给 gemma-2-9b 装 4096，于是整条起草链
在服务端直接 400——筛选跑完了，一条稿件也出不来。

修法不是把语料写死砍短（那样大模型也跟着变笨），而是先问服务端「你装了
多大」，再按这个数字决定放几段语料、留多少字给模型开口。这里钉住的就是
这条链上每一环的边界。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from hindsight_agent.content import (
    CHARS_PER_TOKEN,
    DEFAULT_CONTEXT_TOKENS,
    ENV_MODEL_CONTEXT,
    JSON_CONTRACT,
    SYSTEM_PROMPT,
    JsonClaimGenerator,
    ModelContextError,
    build_prompt,
    discover_context_window,
    estimate_tokens,
    extract_json_object,
    plan_budget,
    select_sources,
)
from hindsight_agent.grounding import gather_evidence

from tests.test_content_orchestration import (  # noqa: F401  (fixture reuse)
    corpus,
    factory,
    incidents,
)
from tests.test_workflow import GOOD_NARRATIVE


# --------------------------------------------------------------------------
# 探测与预算
# --------------------------------------------------------------------------

def test_an_explicit_setting_beats_any_probe(monkeypatch):
    """你比探测更知道自己的部署。显式设置永远优先。"""
    monkeypatch.setenv(ENV_MODEL_CONTEXT, "16384")
    tokens, source = discover_context_window("http://127.0.0.1:1/v1", "whatever")
    assert tokens == 16384
    assert ENV_MODEL_CONTEXT in source


def test_a_nonsense_setting_is_named_not_ignored(monkeypatch):
    monkeypatch.setenv(ENV_MODEL_CONTEXT, "big")
    with pytest.raises(ModelContextError, match=ENV_MODEL_CONTEXT):
        discover_context_window("http://127.0.0.1:1/v1", "whatever")


def test_an_endpoint_that_will_not_say_falls_back_quietly(monkeypatch):
    """探测是可选的：探不到就用默认值，绝不因为一次可选查询让起草失败。"""
    monkeypatch.delenv(ENV_MODEL_CONTEXT, raising=False)
    tokens, _source = discover_context_window("http://127.0.0.1:1/v1", "nothing-here")
    assert tokens is None
    assert plan_budget(tokens).context_tokens == DEFAULT_CONTEXT_TOKENS


def test_the_probe_reads_the_loaded_length_not_the_model_maximum(monkeypatch):
    """一个 8192 的模型可以只装 4096。能用的是后者。"""
    monkeypatch.delenv(ENV_MODEL_CONTEXT, raising=False)

    class _Models(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = json.dumps({"data": [
                {"id": "other-model", "max_context_length": 99999},
                {"id": "gemma", "max_context_length": 8192,
                 "loaded_context_length": 4096},
            ]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), _Models)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        host, port = server.server_address
        tokens, source = discover_context_window(f"http://{host}:{port}/v1", "gemma")
        assert tokens == 4096
        assert "loaded_context_length" in source
    finally:
        server.shutdown()
        server.server_close()


def test_the_budget_leaves_room_for_both_halves():
    budget = plan_budget(4096, source="test")
    assert budget.prompt_tokens + budget.completion_tokens < 4096, "没给聊天模板留余量"
    assert budget.completion_tokens >= 512, "留给模型开口的空间太小"


def test_a_window_too_small_to_work_in_says_so_rather_than_trying():
    """与其交一份注定被 G4 删光的稿件，不如当场说清楚是上下文不够。"""
    with pytest.raises(ModelContextError, match="8192"):
        plan_budget(1024)


# --------------------------------------------------------------------------
# 按预算裁语料
# --------------------------------------------------------------------------

def _bundle(corpus, factory):
    return gather_evidence(
        corpus_database=corpus, factory_database=factory,
        risk_assessment_id="risk-1", incident_text=GOOD_NARRATIVE, top_k=6,
    )


def _passage(passage_id: str, text: str) -> dict:
    return {"passage_id": passage_id, "text": text, "source_type": "cfr",
            "section_role": "REQUIREMENT", "citation_label": f"29 CFR {passage_id}"}


def _synthetic_bundle() -> dict:
    """语料齐全的证据包。真实语料库按危险路径检索，这里只关心取舍规则本身。"""
    return {
        "risk_assessment_id": "risk-1",
        "risk_path": {"controls": []},
        "by_language": {
            "en": {
                "requirements": [_passage(f"en-req-{i}", "R" * 300) for i in range(6)],
                "narratives": [_passage(f"en-narr-{i}", "N" * 1200) for i in range(6)],
            },
            "es": {
                "requirements": [_passage("es-req-0", "S" * 1200)],
                "narratives": [_passage("es-narr-0", "S" * 1200)],
            },
        },
    }


def test_a_tight_budget_still_leaves_one_source():
    """一份没有任何出处的提示词只会让 G4 把稿件删光。至少留一段。"""
    chosen = select_sources(_synthetic_bundle(), char_budget=1, per_passage_chars=200)
    assert len(chosen) == 1


def test_a_wide_budget_takes_more_than_a_tight_one():
    bundle = _synthetic_bundle()
    tight = select_sources(bundle, char_budget=1500, per_passage_chars=300)
    wide = select_sources(bundle, char_budget=40_000, per_passage_chars=1200)
    assert len(wide) > len(tight)


def test_requirements_are_kept_before_background_narratives():
    """窗口紧张时先保住要求条文：它短，而且是唯一能支撑 REQUIREMENT 主张的东西。"""
    chosen = select_sources(_synthetic_bundle(), char_budget=2000,
                            per_passage_chars=1200)
    kept = [passage["passage_id"] for _language, passage, _text in chosen]
    assert kept[0].startswith("en-req")
    assert not any(pid.startswith("es-") for pid in kept), "西语语料不该挤掉英文要求"


def test_a_long_passage_is_truncated_not_dropped_whole():
    chosen = select_sources(_synthetic_bundle(), char_budget=40_000,
                            per_passage_chars=400)
    assert all(len(text) <= 400 for _language, _passage, text in chosen)


def test_the_prompt_shrinks_to_fit_the_window(corpus, factory):
    """同一份证据，窗口小的时候提示词必须更短——这正是当初 400 的那一步。"""
    bundle = _bundle(corpus, factory)
    small = build_prompt(bundle, incident_key="inc-1",
                         incident_narrative=GOOD_NARRATIVE,
                         token_budget=plan_budget(4096).prompt_tokens)
    large = build_prompt(bundle, incident_key="inc-1",
                         incident_narrative=GOOD_NARRATIVE,
                         token_budget=plan_budget(32768).prompt_tokens)
    assert len(small) <= len(large)
    request = estimate_tokens(SYSTEM_PROMPT) + estimate_tokens(
        small + "\n\n" + JSON_CONTRACT
    )
    assert request < 4096, "裁完还是装不下，等于没裁"


def test_a_trimmed_passage_is_not_offered_as_a_citation(corpus, factory):
    """给出一个正文没进提示词的 id，等于邀请模型引用它没读过的东西。"""
    bundle = _bundle(corpus, factory)
    prompt = build_prompt(bundle, incident_key="inc-1",
                          incident_narrative=GOOD_NARRATIVE,
                          token_budget=plan_budget(4096).prompt_tokens)
    head, _, sources = prompt.partition("SOURCE TEXT")
    for line in head.splitlines():
        line = line.strip().strip(',"')
        if line.startswith('"id": "corpus:'):
            passage_id = line.split('"id": "corpus:')[1].rstrip('"')
            assert f"[corpus:{passage_id}]" in sources


def test_the_prompt_states_the_coverage_g4_will_demand(corpus, factory):
    """G4 拒收缺段落的稿件，那么提示词就必须把这三类说出来。

    没说的时候，模型交出两条引用干净的主张、一条 SITE_FACT 也没有，整份稿件
    被打回人工补写——不是因为它编造，而是因为没人告诉过它必须写哪几类。
    """
    prompt = build_prompt(_bundle(corpus, factory), incident_key="inc-1",
                          incident_narrative=GOOD_NARRATIVE)
    assert "INCIDENT_FACT" in prompt
    assert "SITE_FACT" in prompt
    assert "REQUIREMENT" in prompt and "CONTROL" in prompt


def test_a_rejection_for_missing_types_is_handed_back_to_the_model(corpus, factory):
    """上一轮什么都没被删、只是少写了一类主张时，重生成不能收到一个空列表。"""
    prompt = build_prompt(_bundle(corpus, factory), incident_key="inc-1",
                          incident_narrative=GOOD_NARRATIVE,
                          previous_missing=["SITE_FACT"])
    assert "MISSING REQUIRED CLAIM TYPES" in prompt
    assert "SITE_FACT" in prompt


# --------------------------------------------------------------------------
# 截断回复
# --------------------------------------------------------------------------

TRUNCATED = """```json
{"briefing_claims": [
  {"claim_id": "c1", "claim_type": "INCIDENT_FACT", "text_en": "A", "text_es": "B"},
  {"claim_id": "c2", "claim_type": "SITE_FACT", "text_en": "C", "text_"""


def test_a_truncated_reply_keeps_the_claims_that_did_finish():
    """小模型最常见的失败是写到一半没预算了。别为了半条丢掉整份。"""
    recovered = extract_json_object(TRUNCATED, repair_truncated=True)
    assert [c["claim_id"] for c in recovered["briefing_claims"]] == ["c1"]


def test_repair_is_opt_in_so_a_real_parse_failure_still_fails():
    with pytest.raises(ValueError):
        extract_json_object(TRUNCATED)


def test_a_complete_reply_is_never_repaired():
    payload = extract_json_object('prose {"unknowns": ["x"]} more', repair_truncated=True)
    assert payload == {"unknowns": ["x"]}


# --------------------------------------------------------------------------
# 生成器
# --------------------------------------------------------------------------

class _Overflow(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        body = json.dumps({"error": "Trying to keep the first 4332 tokens when "
                                    "context the overflows."}).encode()
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_a_context_overflow_is_translated_into_something_actionable(monkeypatch):
    """服务端只回一个 400。人需要知道该改哪个数字。"""
    monkeypatch.delenv(ENV_MODEL_CONTEXT, raising=False)
    server = HTTPServer(("127.0.0.1", 0), _Overflow)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        host, port = server.server_address
        generator = JsonClaimGenerator(base_url=f"http://{host}:{port}/v1",
                                       model_id="stub")
        with pytest.raises(ModelContextError, match=ENV_MODEL_CONTEXT):
            generator("ALLOWED CITATIONS\n[]")
    finally:
        server.shutdown()
        server.server_close()


def test_the_request_asks_for_no_more_than_the_window_can_hold(monkeypatch):
    """max_tokens 写死 4096 是原来那个 400 的另一半原因。"""
    monkeypatch.delenv(ENV_MODEL_CONTEXT, raising=False)
    seen: list[dict] = []

    class _Echo(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            seen.append(json.loads(self.rfile.read(length) or b"{}"))
            body = json.dumps({
                "id": "s", "object": "chat.completion", "model": "stub",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {
                    "role": "assistant", "content": '{"unknowns": []}'}}],
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), _Echo)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        host, port = server.server_address
        generator = JsonClaimGenerator(base_url=f"http://{host}:{port}/v1",
                                       model_id="stub", context_tokens=4096)
        generator("ALLOWED CITATIONS\n[]")
        asked = seen[0]["max_tokens"]
        prompt_tokens = sum(
            estimate_tokens(m["content"]) for m in seen[0]["messages"]
        )
        assert prompt_tokens + asked <= 4096
    finally:
        server.shutdown()
        server.server_close()


def test_the_estimate_never_undercounts_real_text():
    """低估会让请求在服务端直接 400，所以这个比值只能偏保守。"""
    assert CHARS_PER_TOKEN <= 4.0
    assert estimate_tokens("x" * 350) >= 100
