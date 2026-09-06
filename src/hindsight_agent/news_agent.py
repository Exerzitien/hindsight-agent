"""Strands agent that checks the official OSHA news RSS feed."""

from __future__ import annotations

import os

from strands import Agent
from strands.models.ollama import OllamaModel

from .tools.collect_osha_news import collect_osha_news


SYSTEM_PROMPT = """
You are the Hindsight OSHA News Collection Agent.

Your only current responsibility is checking the official OSHA news RSS feed.
Always use collect_osha_news. Report only what the tool returned. Never claim a
news release describes or does not describe a workplace accident. Accident
classification is outside this agent's scope and has not been performed. Do not
classify incidents, match factories, or generate training content. End every
report with: "Accident classification was not performed."
""".strip()


def build_agent() -> Agent:
    model = OllamaModel(
        host=os.getenv("OLLAMA_HOST", "http://localhost:11434"),
        model_id=os.getenv(
            "OLLAMA_MODEL_ID",
            "qwen3:4b-instruct-2507-q4_K_M",
        ),
    )
    return Agent(model=model, tools=[collect_osha_news], system_prompt=SYSTEM_PROMPT)


def main() -> int:
    agent = build_agent()
    agent(
        "Check the official OSHA news RSS feed. Report newly observed releases, "
        "but do not decide whether any release is an accident case. Explicitly "
        "state that accident classification was not performed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
