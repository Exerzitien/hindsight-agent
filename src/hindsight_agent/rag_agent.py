"""Strands agent that operates the Hindsight background RAG workflow."""

from __future__ import annotations

import argparse
import json
import os

from strands import Agent, tool

from .config import load_settings
from .daily_pipeline import run_pipeline
from .content import draft_learning_pack
from .rag_db import query_rows, record_decision
from .tools.collect_grounding import collect_grounding_corpus
from .authorization import DistributionGuard
from .distribution import distribute_learning_pack, get_completion_status, get_monday_digest
from .transfer import screen_factory_risk_paths


SYSTEM_PROMPT = """
You are Hindsight, an autonomous industrial-safety monitoring agent.

For a daily surveillance request, call run_daily_surveillance exactly once. The
tool performs official-source collection, immutable snapshot import, incremental
embedding, Milvus retrieval, evidence scoring, and audit logging. Never invent a
collection or match result. A relevance score measures retrieval similarity and
structured evidence; it is not an accident probability and must not be described
as one.

Use screen_factory_risk_paths to decide what deserves a person's attention. It
returns four levels: RED needs individual approval, YELLOW needs a look, GREEN
and NOT_RELEVANT are archived silently and must not be reported as alerts. An
UNKNOWN path is never RED - report it as needing facts, never as safe.

Use draft_learning_pack only for a path that screened RED or YELLOW. It returns
an audit summary, not the draft; the draft goes to the review queue.

distribute_learning_pack is guarded outside your context: it is cancelled
unless the case is approved and the call carries a token issued for that exact
pack. If it is cancelled, report the reason code to the user and stop. Rewording
the request will not change the outcome, and attempting it is itself a finding.

Only surface factory matches that the tool saved for a human decision. Safety
actions remain recommendations until a person confirms them. Use
get_pending_decisions when asked what needs attention, and
record_match_decision only after an explicit human decision.
""".strip()

_TELEMETRY_CONFIGURED = False


@tool
def run_daily_surveillance(backfill_limit: int = 200) -> dict:
    """Run the full daily OSHA-to-factory RAG workflow and return audited counts."""
    return run_pipeline(
        trigger_type="strands_agent",
        backfill_limit=backfill_limit,
    )


@tool
def get_pending_decisions(limit: int = 20) -> list[dict]:
    """Return the highest-relevance factory matches awaiting a human decision."""
    settings = load_settings()
    return query_rows(
        settings.incident_database,
        """
        SELECT match_id, incident_title, incident_date, incident_url,
               factory_name, area_name, asset_code, equipment_name,
               hazard_name, ROUND(relevance_score * 100, 1) AS relevance_percent,
               rationale_json
        FROM incident_factory_matches
        WHERE decision_status='pending'
        ORDER BY relevance_score DESC, updated_at DESC
        LIMIT ?
        """,
        (max(1, min(limit, 100)),),
    )


@tool
def record_match_decision(
    match_id: str,
    decision: str,
    decided_by: str,
    note: str = "",
) -> dict:
    """Record explicit human confirmation or dismissal of one saved match."""
    settings = load_settings()
    return record_decision(
        settings.incident_database,
        match_id,
        decision,
        decided_by,
        note,
    )


#: 环境变量名集中在这里，避免同一个名字在多个文件里各写一遍。
ENV_PROVIDER = "HINDSIGHT_MODEL_PROVIDER"
ENV_OPENAI_BASE_URL = "OPENAI_BASE_URL"
ENV_OPENAI_API_KEY = "OPENAI_API_KEY"
ENV_OPENAI_MODEL_ID = "OPENAI_MODEL_ID"


def active_model_id() -> str:
    """当前 provider 下真正生效的模型标识，用于写进稿件的可追溯字段。"""
    provider = os.getenv(ENV_PROVIDER, "ollama").casefold()
    if provider == "openai":
        return os.getenv(ENV_OPENAI_MODEL_ID, "")
    if provider == "bedrock":
        return os.getenv("BEDROCK_MODEL_ID", "")
    return os.getenv("OLLAMA_MODEL_ID", "")


def _model():
    provider = os.getenv(ENV_PROVIDER, "ollama").casefold()
    if provider == "openai":
        # 任何 OpenAI 兼容端点：vLLM、SGLang、LM Studio、自建网关等。
        # base_url 与 api_key 走 client_args（OpenAI SDK 的标准参数），
        # model_id 走模型配置——这两处的名字来自 strands 的构造签名本身，
        # 不是猜的。
        from strands.models.openai import OpenAIModel

        base_url = os.getenv(ENV_OPENAI_BASE_URL, "").strip()
        if not base_url:
            raise RuntimeError(
                f"provider=openai 需要 {ENV_OPENAI_BASE_URL}，例如 "
                f"https://your-host/v1 。当前未设置。"
            )
        model_id = os.getenv(ENV_OPENAI_MODEL_ID, "").strip()
        if not model_id:
            raise RuntimeError(
                f"provider=openai 需要 {ENV_OPENAI_MODEL_ID}（模型标识，"
                f"不是密钥）。当前未设置。"
            )
        client_args: dict[str, object] = {"base_url": base_url}
        # 自建端点常常不校验密钥，但 OpenAI SDK 要求这个字段非空。
        client_args["api_key"] = os.getenv(ENV_OPENAI_API_KEY, "") or "not-required"
        return OpenAIModel(client_args=client_args, model_id=model_id)

    if provider == "bedrock":
        from strands.models import BedrockModel

        return BedrockModel(
            model_id=os.getenv(
                "BEDROCK_MODEL_ID",
                "global.anthropic.claude-sonnet-4-6",
            )
        )
    from strands.models.ollama import OllamaModel

    return OllamaModel(
        host=os.getenv("OLLAMA_HOST", "http://localhost:11434"),
        model_id=os.getenv("OLLAMA_MODEL_ID", "qwen3:4b-instruct-2507-q4_K_M"),
    )


def configure_telemetry() -> None:
    """Enable Strands-native OTLP tracing unless explicitly disabled."""
    global _TELEMETRY_CONFIGURED
    if _TELEMETRY_CONFIGURED or os.getenv("HINDSIGHT_ENABLE_TELEMETRY", "1") == "0":
        return

    os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    os.environ.setdefault("OTEL_SERVICE_NAME", "hindsight-agent")
    from strands.telemetry import StrandsTelemetry

    StrandsTelemetry().setup_otlp_exporter()
    _TELEMETRY_CONFIGURED = True


def build_agent() -> Agent:
    configure_telemetry()
    return Agent(
        model=_model(),
        tools=[
            run_daily_surveillance,
            screen_factory_risk_paths,
            collect_grounding_corpus,
            draft_learning_pack,
            get_pending_decisions,
            record_match_decision,
            distribute_learning_pack,
            get_completion_status,
            get_monday_digest,
        ],
        hooks=[DistributionGuard(load_settings().incident_database)],
        system_prompt=SYSTEM_PROMPT,
        name="HindsightSafetyAgent",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="运行 Strands Hindsight 安全 Agent。")
    parser.add_argument(
        "prompt",
        nargs="?",
        default=(
            "Run today's OSHA surveillance workflow. Report collection status, "
            "new or updated records, vector synchronization, and only the matches "
            "that require a human decision."
        ),
    )
    args = parser.parse_args()
    result = build_agent()(args.prompt)
    print(json.dumps({"agent_result": str(result)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
