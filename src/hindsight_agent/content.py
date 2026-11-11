"""G2 and G3 generation: turn an evidence bundle into claims, never into prose.

The model is given a closed set of citation ids from the frozen corpus, the
incident row and the site's own risk register, and is asked for structured
claims - not paragraphs. It cannot cite anything outside that set, because
anything it invents fails to resolve in G4 and is deleted.

The generator is injectable, following the same pattern the daily pipeline uses
for embeddings and the vector store, so the whole G1-G4 chain can be exercised
in tests without a model server.

Orchestration follows the documented state machine::

    DRAFTED -> G4 -> SELF_CHECK_OK -> PENDING_APPROVAL
    SELF_CHECK_FAIL -> REGEN(x1) -> NEEDS_HUMAN_WRITER

Regeneration happens at most once, and the second attempt is told exactly which
claims failed and why. If the draft still cannot fill its required sections it
becomes a writing task for a person; it never ships thin.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

import requests
from pydantic import BaseModel, Field
from strands import tool

from .grounding import GroundingIndex, gather_evidence
from .learning_pack import (
    CLAIM_TYPES,
    PACK_VERSION,
    STATE_NEEDS_HUMAN_WRITER,
    STATE_PENDING_APPROVAL,
    Claim,
    LearningDraft,
    QuizItem,
    render_pack,
)
from .self_check import SelfCheckReport, run_self_check

GENERATOR_VERSION = "content-v1.0"

SYSTEM_PROMPT = """
You draft industrial-safety learning material for a factory safety manager to
review. You do not write paragraphs. You emit typed claims, each carrying the
ids of the sources it rests on.

Hard rules, enforced by code after you answer:

1. Cite only ids from the ALLOWED CITATIONS list. Any other id is deleted.
2. Never state a root cause, a legal conclusion, blame, or a fault the site has
   not recorded. Describe what the sources say happened, not why.
3. Never introduce a number, measurement, interval or torque that does not
   appear verbatim in one of the sources you cite for that claim.
4. A CONTROL claim must name a control_id from the APPROVED CONTROLS list and
   must cite either that control or a regulatory/prevention passage. You may
   not invent a control the site has not approved.
5. Write text_en and text_es for every claim. They must carry the same numbers.
   Spanish is a rendering of the same claim, not a new claim.
6. Anything the sources do not establish goes in unknowns, not into a claim.

Keep each claim to one sentence. Aim for a five-minute talk: at most six talk
claims. Write for a shop-floor audience in the talk, and for a safety manager
in the briefing.
""".strip()


class DraftClaimModel(BaseModel):
    claim_id: str = Field(description="stable short id, e.g. c1")
    claim_type: str = Field(description="one of INCIDENT_FACT, SITE_FACT, REQUIREMENT, CONTROL, UNKNOWN")
    text_en: str
    text_es: str
    citations: list[str] = Field(default_factory=list)
    quote: str | None = None
    quote_citation: str | None = None
    control_id: str | None = None


class DraftQuizModel(BaseModel):
    question_id: str
    question_en: str
    question_es: str
    options_en: list[str]
    options_es: list[str]
    correct_index: int
    claim_id: str


class GeneratedDraftModel(BaseModel):
    briefing_claims: list[DraftClaimModel] = Field(default_factory=list)
    talk_claims: list[DraftClaimModel] = Field(default_factory=list)
    quiz: list[DraftQuizModel] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)


class ClaimGenerator(Protocol):
    """Anything that turns a prompt into a structured draft."""

    model_id: str

    def __call__(self, prompt: str) -> GeneratedDraftModel: ...


class StrandsClaimGenerator:
    """Structured generation through the project's configured Strands model.

    Uses the provider's native structured-output path, which for OpenAI-shaped
    servers means ``response_format`` with a strict JSON schema. Hosted APIs
    support that; many self-hosted servers do not, or reject the schema for
    nested models. When that is the case, use :class:`JsonClaimGenerator`.
    """

    def __init__(self, model: Any | None = None, model_id: str | None = None) -> None:
        from strands import Agent

        if model is None:
            from .rag_agent import _model

            model = _model()
        self._agent = Agent(model=model, system_prompt=SYSTEM_PROMPT, name="HindsightContentDrafter")
        if model_id is None:
            from .rag_agent import active_model_id

            model_id = active_model_id()
        self.model_id = model_id

    def __call__(self, prompt: str) -> GeneratedDraftModel:
        return self._agent.structured_output(GeneratedDraftModel, prompt)


#: 附在提示词末尾的输出契约。写成显式 JSON 形状，而不是依赖服务端 schema 强制，
#: 因为自建推理服务对 response_format 的支持参差不齐。
JSON_CONTRACT = """
Reply with one JSON object and nothing else. No prose before or after it, no
markdown fences. The object has exactly these keys:

{
  "briefing_claims": [ {claim}, ... ],
  "talk_claims":     [ {claim}, ... ],
  "quiz":            [ {quiz},  ... ],
  "unknowns":        [ "string", ... ]
}

{claim} = {
  "claim_id": "c1",
  "claim_type": "INCIDENT_FACT" | "SITE_FACT" | "REQUIREMENT" | "CONTROL" | "UNKNOWN",
  "text_en": "one sentence",
  "text_es": "una oración",
  "citations": ["id from ALLOWED CITATIONS"],
  "quote": null,
  "quote_citation": null,
  "control_id": null
}

{quiz} = {
  "question_id": "q1",
  "question_en": "...", "question_es": "...",
  "options_en": ["...", "..."], "options_es": ["...", "..."],
  "correct_index": 0,
  "claim_id": "c1"
}
""".strip()


# --------------------------------------------------------------------------
# 上下文预算
#
# 这一段存在的理由，是本项目在第一次接上真实本地端点时撞到的墙：提示词
# 稳定在 4300 token 左右，而 LM Studio 默认只给 gemma-2-9b 装 4096——整条
# 起草链在服务端直接 400，一条稿件也出不来。
#
# 修法不是把语料写死砍短（那样大模型也跟着变笨），而是先问服务端「你装了
# 多大」，再按这个数字决定放几段语料、留多少字给模型开口。装得多就多给。
# --------------------------------------------------------------------------

#: 估算用的字符/token 比。故意比真实值（英文约 4）保守：高估只是少放一段
#: 语料，低估会让请求在服务端直接 400，那是整条链最贵的失败方式。
CHARS_PER_TOKEN = 3.5
#: 聊天模板自身的开销（角色标记、分隔符），按最坏情况留。
TEMPLATE_OVERHEAD_TOKENS = 64
#: 服务端不肯说上下文有多大时的保守假设。
DEFAULT_CONTEXT_TOKENS = 8192
#: 规划阶段给模型留的开口空间，上下限。
MIN_COMPLETION_TOKENS = 512
MAX_COMPLETION_TOKENS = 2048
#: 真正发请求时的绝对下限。低于它，模型连三条双语主张都写不完，
#: 与其交一份注定被 G4 删光的稿件，不如当场说清楚是上下文不够。
FLOOR_COMPLETION_TOKENS = 256
#: 提示词自己的下限：事故 + 危险路径 + 已批准控制措施 + 引用清单大约就要
#: 700 token，再少就不剩任何证据可放了。
MIN_PROMPT_TOKENS = 900
#: 每段语料除正文外的固定开销：标题行 + 引用清单里的那一条。
SOURCE_OVERHEAD_CHARS = 230
#: 提示词里雷打不动的那部分——事故叙述、危险路径、已批准控制措施、引用清单。
#: 实测 2800-3600 字符；按上界留，宁可少放一段语料也不要算漏。
FIXED_PROMPT_CHARS = 3600

ENV_MODEL_CONTEXT = "HINDSIGHT_MODEL_CONTEXT"


class ModelContextError(RuntimeError):
    """上下文装不下这次请求，而且已经没有可裁的东西了。"""


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 8


def discover_context_window(
    base_url: str, model_id: str, *, timeout: float = 5.0
) -> tuple[int | None, str]:
    """问服务端它到底给这个模型装了多大的上下文。

    返回 ``(tokens, 来源)``；问不到就返回 ``(None, ...)``，由调用方退回默认值。
    显式设置的 ``HINDSIGHT_MODEL_CONTEXT`` 永远优先——你比探测更知道自己的部署。

    探测走 LM Studio 的 ``/api/v0/models``，因为它是本地部署里唯一会如实
    报出「实际装载的」上下文长度的常见端点；``/v1/models`` 不含这个字段。
    探不到不是错误：任何异常都退回默认值，绝不因为一次可选查询而让起草失败。
    """
    override = os.getenv(ENV_MODEL_CONTEXT, "").strip()
    if override:
        try:
            value = int(override)
        except ValueError:
            raise ModelContextError(
                f"{ENV_MODEL_CONTEXT}={override!r} 不是一个整数 token 数。"
            ) from None
        if value > 0:
            return value, ENV_MODEL_CONTEXT

    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    try:
        response = requests.get(f"{root}/api/v0/models", timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except Exception:  # noqa: BLE001 - 探测是可选的，失败一律退回默认值
        return None, "默认值"
    for entry in payload.get("data") or []:
        if entry.get("id") != model_id:
            continue
        # 装载值优先于模型上限：能用的是前者，8192 的模型可以只装 4096。
        for key in ("loaded_context_length", "max_context_length"):
            value = entry.get(key)
            if isinstance(value, int) and value > 0:
                return value, f"服务端上报（{key}）"
    return None, "默认值"


@dataclass(frozen=True)
class PromptBudget:
    """一次起草请求怎么分这块上下文。"""

    context_tokens: int
    prompt_tokens: int
    completion_tokens: int
    source: str

    @property
    def source_char_budget(self) -> int:
        """扣掉事故、路径、控制措施等固定部分之后，能给语料原文的字符数。

        见 :data:`FIXED_PROMPT_CHARS`。
        """
        return max(
            0, int(self.prompt_tokens * CHARS_PER_TOKEN) - FIXED_PROMPT_CHARS
        )

    @property
    def per_passage_chars(self) -> int:
        budget = self.source_char_budget
        if budget > 6000:
            return 1200
        if budget > 3000:
            return 700
        return 420

    def allowance(self) -> dict[str, int]:
        """这块开口空间写得下几条主张、几道题。

        双语一条主张连 JSON 结构约 70 token，一道双语选择题约 110。写不下
        就明确让模型少写——小模型的默认失败方式是写到一半被截断，那比少写
        两条糟得多。
        """
        room = max(0, self.completion_tokens - 140)
        slots = max(4, min(8, room // 70))
        briefing = max(2, slots // 2)
        talk = max(2, min(6, slots - briefing))
        quiz = 2 if room >= 700 else 1
        return {"briefing": briefing, "talk": talk, "quiz": quiz}


def plan_budget(context_tokens: int | None, *, source: str = "默认值") -> PromptBudget:
    """把上下文切成「提示词」和「开口空间」两块。"""
    context = int(context_tokens or DEFAULT_CONTEXT_TOKENS)
    fixed = (
        estimate_tokens(SYSTEM_PROMPT)
        + estimate_tokens(JSON_CONTRACT)
        + TEMPLATE_OVERHEAD_TOKENS
    )
    completion = max(
        MIN_COMPLETION_TOKENS, min(MAX_COMPLETION_TOKENS, int(context * 0.30))
    )
    prompt = context - fixed - completion
    if prompt < MIN_PROMPT_TOKENS:
        raise ModelContextError(
            f"模型上下文只有 {context} token，扣掉系统提示与输出契约后放不下"
            f"一次起草所需的最小证据（约 {MIN_PROMPT_TOKENS + fixed + MIN_COMPLETION_TOKENS} token）。\n"
            f"  · 用更大的上下文重新装载这个模型（本项目建议 ≥ 8192）；\n"
            f"  · 或设 {ENV_MODEL_CONTEXT} 显式告诉本项目真实可用的 token 数。"
        )
    return PromptBudget(
        context_tokens=context,
        prompt_tokens=prompt,
        completion_tokens=completion,
        source=source,
    )


# --------------------------------------------------------------------------
# 解析模型回复
# --------------------------------------------------------------------------

def extract_json_object(text: str, *, repair_truncated: bool = False) -> dict[str, Any]:
    """Pull the first balanced JSON object out of a model's reply.

    Self-hosted models wrap their answer in all sorts of things - markdown
    fences, a preamble, a reasoning block. Rather than trying to enumerate
    those wrappers, this scans for the first ``{`` and returns the substring
    that balances, ignoring braces inside strings. Anything that is not valid
    JSON raises, and the caller retries with the error attached.

    ``repair_truncated`` handles the one failure a small local model makes
    constantly: it runs out of completion budget mid-claim, so the object never
    closes. Rather than throwing away eight good claims because the ninth was
    cut in half, this rewinds to the last element that did close and shuts the
    brackets there. The recovered claims still face G4 unchanged - repair
    changes how much survives truncation, never what is allowed to ship.
    """
    depth = 0
    start = -1
    in_string = False
    escaped = False
    stack: list[str] = []
    # 每次在深层闭合一个括号，就记下「在这里收尾是合法的」。
    safe_cuts: list[tuple[int, str]] = []
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            if char == "{" and depth == 0 and start < 0:
                start = index
            if start >= 0:
                stack.append("}" if char == "{" else "]")
                depth += 1
        elif char in "}]":
            if depth and start >= 0:
                stack.pop()
                depth -= 1
                if depth == 0:
                    return json.loads(text[start : index + 1])
                safe_cuts.append((index + 1, "".join(reversed(stack))))
    if repair_truncated and start >= 0 and safe_cuts:
        for cut, closers in reversed(safe_cuts):
            try:
                return json.loads(text[start:cut] + closers)
            except ValueError:
                continue
    raise ValueError("回复中没有找到完整的 JSON 对象。")


class JsonClaimGenerator:
    """Plain chat completion plus our own validation.

    This exists because the guarantee that matters is not that the server
    enforced a schema - it is that *we* validated what came back. Pydantic
    validation happens here either way, so dropping the server-side schema
    requirement costs nothing and makes the project run against any
    OpenAI-compatible endpoint rather than only the ones that implement strict
    ``response_format``.

    One retry, with the validation error handed back to the model. Never a
    loop: the caller's own regenerate-once policy sits above this.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model_id: str,
        api_key: str = "",
        temperature: float = 0.2,
        timeout: float = 300.0,
        context_tokens: int | None = None,
        discover_context: bool = True,
    ) -> None:
        from openai import OpenAI

        self._client = OpenAI(base_url=base_url, api_key=api_key or "not-required",
                              timeout=timeout)
        self.model_id = model_id
        self.base_url = base_url
        self._temperature = temperature
        source = "调用方指定"
        if context_tokens is None and discover_context:
            context_tokens, source = discover_context_window(base_url, model_id)
        elif context_tokens is None:
            source = "默认值"
        self.budget = plan_budget(context_tokens, source=source)
        #: 便于排查：最近一次调用的原始回复，以及它是不是被截断的。
        self.last_raw: str = ""
        self.last_truncated: bool = False

    @property
    def prompt_token_budget(self) -> int:
        """``build_prompt`` 可以用掉的 token 数。"""
        return self.budget.prompt_tokens

    @property
    def output_allowance(self) -> dict[str, int]:
        """写得下几条主张、几道题——直接写进提示词，让模型自己收着写。"""
        return self.budget.allowance()

    def _complete(self, messages: list[dict[str, str]]) -> str:
        used = sum(estimate_tokens(m["content"]) for m in messages)
        room = self.budget.context_tokens - used - TEMPLATE_OVERHEAD_TOKENS
        if room < FLOOR_COMPLETION_TOKENS:
            raise ModelContextError(
                f"这次请求的提示词约 {used} token，模型上下文 "
                f"{self.budget.context_tokens} token（{self.budget.source}），"
                f"留给回答的只剩 {room} token。\n"
                f"  · 用更大的上下文重新装载模型；\n"
                f"  · 或设 {ENV_MODEL_CONTEXT} 告诉本项目真实可用的 token 数。"
            )
        max_tokens = min(self.budget.completion_tokens, room)
        try:
            response = self._client.chat.completions.create(
                model=self.model_id, messages=messages,
                temperature=self._temperature, max_tokens=max_tokens,
            )
        except Exception as error:
            # 上下文溢出在服务端只是一个 400。把它翻译成一句人能照着做的话，
            # 而不是让 openai 的堆栈从 20 层深处冒出来。
            if "context" in str(error).casefold():
                raise ModelContextError(
                    f"端点拒绝了这次请求，原因是上下文放不下："
                    f"{error}\n"
                    f"  本项目按 {self.budget.context_tokens} token"
                    f"（{self.budget.source}）裁剪提示词。若这个数字不对，"
                    f"请设 {ENV_MODEL_CONTEXT} 覆盖它。"
                ) from error
            raise
        choice = response.choices[0]
        self.last_truncated = getattr(choice, "finish_reason", None) == "length"
        return choice.message.content or ""

    def __call__(self, prompt: str) -> GeneratedDraftModel:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"{prompt}\n\n{JSON_CONTRACT}"},
        ]
        self.last_raw = self._complete(messages)
        try:
            return GeneratedDraftModel.model_validate(
                extract_json_object(self.last_raw, repair_truncated=True)
            )
        except Exception as first_error:
            # 回显被截断的坏回复，而不是整篇塞回去：在一个 4096 的窗口里，
            # 把上一轮的失败原样带上，第二轮必然更装不下。
            messages.append({"role": "assistant", "content": self.last_raw[:400]})
            messages.append({
                "role": "user",
                "content": (
                    f"That reply could not be parsed: {first_error}\n"
                    f"Reply again with one valid JSON object only, matching the "
                    f"contract exactly. No prose, no markdown fences. Write fewer "
                    f"claims if that is what it takes to finish the object."
                ),
            })
            self.last_raw = self._complete(messages)
            return GeneratedDraftModel.model_validate(
                extract_json_object(self.last_raw, repair_truncated=True)
            )


def build_generator() -> ClaimGenerator:
    """按环境变量挑一个生成器。

    provider=openai 时默认走 JSON 校验路径，因为它对服务端没有额外要求；
    若你的端点确实支持严格 response_format，设 HINDSIGHT_GENERATOR=strands
    可切回原生结构化输出。
    """
    from .rag_agent import (
        ENV_OPENAI_API_KEY,
        ENV_OPENAI_BASE_URL,
        ENV_OPENAI_MODEL_ID,
        ENV_PROVIDER,
        active_model_id,
    )

    provider = os.getenv(ENV_PROVIDER, "ollama").casefold()
    style = os.getenv("HINDSIGHT_GENERATOR", "").casefold()
    if provider == "openai" and style != "strands":
        base_url = os.getenv(ENV_OPENAI_BASE_URL, "").strip()
        model_id = os.getenv(ENV_OPENAI_MODEL_ID, "").strip()
        if not base_url or not model_id:
            raise RuntimeError(
                f"provider=openai 需要 {ENV_OPENAI_BASE_URL} 与 "
                f"{ENV_OPENAI_MODEL_ID} 同时设置。"
            )
        return JsonClaimGenerator(
            base_url=base_url, model_id=model_id,
            api_key=os.getenv(ENV_OPENAI_API_KEY, ""),
        )
    return StrandsClaimGenerator(model_id=active_model_id() or None)


def _source_priority(bundle: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """语料的取用顺序，按「每个字符换来多少证据」排。

    英文要求条文排最前：它们短（约 250 字符），而且是唯一能支撑
    REQUIREMENT 主张的东西。叙述类（Fatal Facts）每段 1200 字符却只提供
    背景，所以先放两段就够。西语语料排最后——西语稿件是同一条主张的翻译，
    G4 的双语检查不要求它有独立的西语出处，所以在窗口紧张时它是第一个该
    让位的。
    """
    en = bundle["by_language"].get("en") or {}
    es = bundle["by_language"].get("es") or {}
    requirements_en = list(en.get("requirements") or [])
    narratives_en = list(en.get("narratives") or [])
    requirements_es = list(es.get("requirements") or [])
    narratives_es = list(es.get("narratives") or [])
    ordered: list[tuple[str, dict[str, Any]]] = []
    ordered += [("en", p) for p in requirements_en]
    ordered += [("en", p) for p in narratives_en[:2]]
    ordered += [("es", p) for p in requirements_es[:1]]
    ordered += [("en", p) for p in narratives_en[2:]]
    ordered += [("es", p) for p in narratives_es[:1]]
    ordered += [("es", p) for p in requirements_es[1:]]
    ordered += [("es", p) for p in narratives_es[1:]]
    return ordered


def select_sources(
    bundle: dict[str, Any],
    *,
    char_budget: int | None = None,
    per_passage_chars: int = 1200,
) -> list[tuple[str, dict[str, Any], str]]:
    """在预算内挑语料，返回 ``(语言, 段落, 截断后的正文)``。

    放不下就跳过继续看下一段，而不是就此收手——后面可能还有一段短的装得下。
    永远至少留一段：一份没有任何出处的提示词只会让 G4 把稿件删光，那不如
    让模型看到一段真东西。
    """
    chosen: list[tuple[str, dict[str, Any], str]] = []
    spent = 0
    for language, passage in _source_priority(bundle):
        text = str(passage["text"])[:per_passage_chars]
        cost = len(text) + SOURCE_OVERHEAD_CHARS
        if chosen and char_budget is not None and spent + cost > char_budget:
            continue
        chosen.append((language, passage, text))
        spent += cost
    return chosen


def _citation_menu(
    bundle: dict[str, Any],
    incident_key: str,
    sources: list[tuple[str, dict[str, Any], str]] | None = None,
) -> dict[str, Any]:
    """The closed world the model is allowed to cite from.

    Only the passages whose text actually made it into the prompt are listed.
    Offering an id whose text was trimmed away invites a citation the model
    cannot have read - which G4 then deletes, spending a generation to produce
    nothing.
    """
    allowed: list[dict[str, str]] = [
        {"id": f"incident:{incident_key}", "kind": "incident record"},
        {
            "id": f"site:{bundle['risk_assessment_id']}",
            "kind": "site hazard path",
        },
    ]
    for control in bundle["risk_path"]["controls"]:
        allowed.append(
            {
                "id": f"control:{control['id']}",
                "kind": f"approved control · {control['name']} · {control['hierarchy_code']}",
            }
        )
    if sources is None:
        sources = [
            (language, passage, str(passage["text"]))
            for language, groups in bundle["by_language"].items()
            for passages in groups.values()
            for passage in passages
        ]
    for language, passage, _text in sources:
        allowed.append(
            {
                "id": f"corpus:{passage['passage_id']}",
                "kind": f"{passage['source_type']} · {passage['section_role']} · {language} · {passage['citation_label']}",
            }
        )
    return {"allowed": allowed}


def build_prompt(
    bundle: dict[str, Any],
    *,
    incident_key: str,
    incident_narrative: str,
    previous_findings: list[dict[str, str]] | None = None,
    previous_missing: list[str] | None = None,
    token_budget: int | None = None,
    output_allowance: dict[str, int] | None = None,
) -> str:
    """Assemble the drafting prompt, trimmed to fit ``token_budget``.

    Without a budget every retrieved passage goes in, which is right for a
    large-context server and fatal for a small one. With one, the evidence is
    selected by value per character rather than truncated at the tail, so the
    requirement text a claim needs survives while a fourth background narrative
    does not.
    """
    path = bundle["risk_path"]
    if token_budget is None:
        sources = select_sources(bundle)
    else:
        shape = PromptBudget(
            context_tokens=0, prompt_tokens=token_budget,
            completion_tokens=0, source="",
        )
        sources = select_sources(
            bundle,
            char_budget=shape.source_char_budget,
            per_passage_chars=shape.per_passage_chars,
        )
    menu = _citation_menu(bundle, incident_key, sources)

    sources_text: list[str] = [
        f"[corpus:{passage['passage_id']}] ({language}/{passage['section_role']}) "
        f"{passage['citation_label']}\n{text}"
        for language, passage, text in sources
    ]

    parts = [
        "INCIDENT (source id: incident:%s)\n%s" % (incident_key, incident_narrative),
        "",
        "SITE HAZARD PATH (source id: site:%s)" % bundle["risk_assessment_id"],
        json.dumps(
            {
                "assessment_code": path["assessment_code"],
                "factory": path["factory_name"],
                "area": path["area_name"],
                "equipment": path["equipment_name"],
                "equipment_type": path["equipment_type"],
                "task": path["task_name"],
                "hazard": path["hazard_name"],
                "exposed_role": path["exposed_role"],
                "hazardous_situation": path["hazardous_situation"],
                "residual_level": path["residual_level"],
                "risk_accepted": path["risk_accepted"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        "",
        "APPROVED CONTROLS (control_id · name · hierarchy · performance requirement)",
        json.dumps(
            [
                {
                    "control_id": c["id"],
                    "name": c["name"],
                    "hierarchy": c["hierarchy_code"],
                    "performance_requirement": c["performance_requirement"],
                }
                for c in path["controls"]
            ],
            ensure_ascii=False,
            indent=2,
        ),
        "",
        "ALLOWED CITATIONS",
        json.dumps(menu["allowed"], ensure_ascii=False, indent=2),
        "",
        "SOURCE TEXT",
        "\n\n".join(sources_text),
    ]

    # G4 拒收缺段落的稿件，那么提示词就必须把这三条说出来。此前没说：模型
    # 交出两条引用干净的主张、一条 SITE_FACT 也没有，于是整份稿件被打回人工
    # 补写——不是因为它编造，而是因为没人告诉过它必须写哪几类。
    parts += [
        "",
        "REQUIRED COVERAGE (a draft missing any of these is not shipped; "
        "a person has to write it from scratch instead)",
        f"1. At least one INCIDENT_FACT claim citing \"incident:{incident_key}\".",
        f"2. At least one SITE_FACT claim citing \"site:{bundle['risk_assessment_id']}\" - "
        f"what this site's own hazard path records, from the block above.",
        "3. At least one REQUIREMENT claim citing a corpus id, or one CONTROL claim "
        "naming a control_id from APPROVED CONTROLS and citing that control.",
        "Put all three in briefing_claims. talk_claims restates the same ground in "
        "shop-floor words and may reuse the same citations.",
    ]

    if output_allowance:
        parts += [
            "",
            "OUTPUT BUDGET (the reply is cut off past this, so stay inside it)",
            f"At most {output_allowance['briefing']} briefing_claims, "
            f"{output_allowance['talk']} talk_claims, "
            f"{output_allowance['quiz']} quiz items, 3 unknowns. "
            f"Keep every sentence under 25 words. Four claims that survive the "
            f"citation check are worth more than ten that do not.",
        ]

    if previous_missing:
        # 缺段落和「主张被删」是两种失败，此前只回带后者：上一轮什么都没被删、
        # 只是少写了一类主张时，重生成收到的是一个空列表，等于没有反馈。
        parts += [
            "",
            "YOUR PREVIOUS ATTEMPT WAS REJECTED FOR MISSING REQUIRED CLAIM TYPES: "
            + ", ".join(previous_missing)
            + ". Write those this time; keep everything else short.",
        ]

    if previous_findings:
        # 只回带前几条：重生成的提示词必须比第一次更小，否则在一个刚好装得下
        # 第一次的窗口里，第二次注定溢出。
        parts += [
            "",
            "YOUR PREVIOUS ATTEMPT FAILED THESE CHECKS. Fix them or drop the claim.",
            json.dumps(previous_findings[:8], ensure_ascii=False, indent=2),
        ]
    return "\n".join(parts)


def _to_claims(models: list[DraftClaimModel]) -> list[Claim]:
    claims: list[Claim] = []
    for item in models:
        if item.claim_type not in CLAIM_TYPES:
            # An unknown type cannot be checked, so it cannot be shipped.
            continue
        claims.append(
            Claim(
                claim_id=item.claim_id,
                claim_type=item.claim_type,
                text_en=item.text_en.strip(),
                text_es=item.text_es.strip(),
                citations=list(item.citations),
                quote=item.quote,
                quote_citation=item.quote_citation,
                control_id=item.control_id,
            )
        )
    return claims


def generate_learning_draft(
    *,
    corpus_database: Path | str,
    factory_database: Path | str,
    incident_database: Path | str,
    risk_assessment_id: str,
    incident_key: str,
    incident_narrative: str,
    incident_title: str = "",
    generator: ClaimGenerator | None = None,
    index: GroundingIndex | None = None,
    top_k: int = 6,
    max_regenerations: int = 1,
) -> dict[str, Any]:
    """Run G1 -> G2/G3 -> G4 for one incident on one hazard path."""
    bundle = gather_evidence(
        corpus_database=corpus_database,
        factory_database=factory_database,
        risk_assessment_id=risk_assessment_id,
        incident_text=incident_narrative,
        top_k=top_k,
        index=index,
    )
    path = bundle["risk_path"]
    generator = generator or build_generator()

    draft = LearningDraft(
        draft_id=f"draft:{risk_assessment_id}:{incident_key}",
        risk_assessment_id=risk_assessment_id,
        assessment_code=path["assessment_code"],
        incident_citation=f"incident:{incident_key}",
        incident_title=incident_title or incident_key,
        equipment_name=path["equipment_name"],
        hazard_name=path["hazard_name"],
        task_name=path["task_name"],
        exposed_role=path["exposed_role"],
        generator_version=f"{GENERATOR_VERSION}+{PACK_VERSION}",
        model_id=getattr(generator, "model_id", ""),
    )

    reports: list[SelfCheckReport] = []
    findings_payload: list[dict[str, str]] | None = None
    missing_payload: list[str] | None = None

    # 生成器知道它连的那个端点有多大窗口；提示词得按那个数字来裁。
    # 用 getattr 而不是加进 Protocol，是因为测试里的假生成器不该被迫实现它。
    token_budget = getattr(generator, "prompt_token_budget", None)
    allowance = getattr(generator, "output_allowance", None)

    for attempt in range(max_regenerations + 1):
        attempt_budget = token_budget
        if attempt_budget is not None and findings_payload:
            # 重生成要额外塞进上一轮的失败清单，语料就得相应让位。
            attempt_budget -= estimate_tokens(
                json.dumps(findings_payload[:8], ensure_ascii=False)
            )
        prompt = build_prompt(
            bundle,
            incident_key=incident_key,
            incident_narrative=incident_narrative,
            previous_findings=findings_payload,
            previous_missing=missing_payload,
            token_budget=attempt_budget,
            output_allowance=allowance,
        )
        generated = generator(prompt)

        draft.briefing_claims = _to_claims(generated.briefing_claims)
        draft.talk_claims = _to_claims(generated.talk_claims)
        draft.quiz = [
            QuizItem(
                question_id=q.question_id,
                question_en=q.question_en,
                question_es=q.question_es,
                options_en=list(q.options_en),
                options_es=list(q.options_es),
                correct_index=q.correct_index,
                claim_id=q.claim_id,
            )
            for q in generated.quiz
        ]
        draft.unknowns = list(generated.unknowns)
        draft.regeneration_count = attempt

        report = run_self_check(
            draft,
            corpus_database=corpus_database,
            factory_database=factory_database,
            incident_database=incident_database,
            repair=True,
        )
        reports.append(report)

        if report.state != STATE_NEEDS_HUMAN_WRITER:
            break
        findings_payload = [f.to_dict() for f in report.findings]
        missing_payload = list(report.missing_required)

    if draft.state != STATE_NEEDS_HUMAN_WRITER:
        draft.state = STATE_PENDING_APPROVAL

    return {
        "draft": draft,
        "pack": render_pack(draft),
        "self_check_reports": [r.to_dict() for r in reports],
        "evidence_bundle": {
            "retrieval_version": bundle["retrieval_version"],
            "query_terms": bundle["query_terms"],
            "counts": {
                language: {group: len(items) for group, items in groups.items()}
                for language, groups in bundle["by_language"].items()
            },
        },
        "needs_human_writer": draft.state == STATE_NEEDS_HUMAN_WRITER,
    }


@tool
def draft_learning_pack(risk_assessment_id: str, incident_key: str) -> dict:
    """Draft a cited briefing and bilingual toolbox talk for one incident and hazard path."""
    import sqlite3

    from .config import load_settings

    settings = load_settings()
    corpus_database = settings.project_root / "data" / "corpus" / "grounding.db"

    connection = sqlite3.connect(str(settings.incident_database))
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT narrative, source_title FROM osha_sir_incidents WHERE incident_key = ?",
            (incident_key,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise ValueError(f"No such incident: {incident_key}")

    result = generate_learning_draft(
        corpus_database=corpus_database,
        factory_database=settings.factory_database,
        incident_database=settings.incident_database,
        risk_assessment_id=risk_assessment_id,
        incident_key=incident_key,
        incident_narrative=row["narrative"],
        incident_title=row["source_title"] or incident_key,
    )
    # The draft itself is not returned to the model: it goes to the review
    # queue. The model gets the audit summary so it can report what happened.
    return {
        "draft_id": result["draft"].draft_id,
        "state": result["draft"].state,
        "content_hash": result["pack"]["content_hash"],
        "needs_human_writer": result["needs_human_writer"],
        "self_check": result["self_check_reports"][-1],
    }


def main() -> int:
    import argparse
    import sqlite3

    from .config import load_settings

    parser = argparse.ArgumentParser(
        description="生成一条危险路径的证据链简报与双语班前会（G1→G2/G3→G4）。"
    )
    parser.add_argument("risk_assessment_id", help="工厂风险登记册中的路径 id")
    parser.add_argument("incident_key", help="incidents.db 中的 incident_key")
    parser.add_argument("--out", default=None, help="把完整 pack 写成 JSON 文件")
    args = parser.parse_args()

    settings = load_settings()
    connection = sqlite3.connect(str(settings.incident_database))
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT narrative, source_title FROM osha_sir_incidents WHERE incident_key = ?",
            (args.incident_key,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise SystemExit(f"找不到事故记录: {args.incident_key}")

    result = generate_learning_draft(
        corpus_database=settings.project_root / "data" / "corpus" / "grounding.db",
        factory_database=settings.factory_database,
        incident_database=settings.incident_database,
        risk_assessment_id=args.risk_assessment_id,
        incident_key=args.incident_key,
        incident_narrative=row["narrative"],
        incident_title=row["source_title"] or args.incident_key,
    )
    report = result["self_check_reports"][-1]
    print(
        json.dumps(
            {
                "state": result["draft"].state,
                "content_hash": result["pack"]["content_hash"],
                "needs_human_writer": result["needs_human_writer"],
                "checked_claims": report["checked_claims"],
                "removed_claim_ids": report["removed_claim_ids"],
                "findings": report["findings"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if args.out:
        Path(args.out).write_text(
            json.dumps(result["pack"], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"完整 pack 已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
