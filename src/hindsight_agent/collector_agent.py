"""Strands agent that can invoke the deterministic OSHA collector tool."""

from __future__ import annotations

import os

from strands import Agent
from strands.models.ollama import OllamaModel

from .tools.collect_osha import collect_osha_sir


SYSTEM_PROMPT = """
You are the Hindsight Accident Collection Agent.

Your only current responsibility is collecting the official OSHA Severe Injury
Report dataset. Use collect_osha_sir for that task. Never claim collection
succeeded unless the tool reports success. Clearly report source failures. Do not
analyze incidents, match factories, or generate training content.
""".strip()


def build_agent() -> Agent:
    model = OllamaModel(
        host=os.getenv("OLLAMA_HOST", "http://localhost:11434"),
        model_id=os.getenv(
            "OLLAMA_MODEL_ID",
            "qwen3:4b-instruct-2507-q4_K_M",
        ),
    )
    return Agent(model=model, tools=[collect_osha_sir], system_prompt=SYSTEM_PROMPT)


def main() -> int:
    agent = build_agent()
    agent(
        "Check the official OSHA Severe Injury Report source and collect the "
        "latest available full dataset. Report the collection status and evidence."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

