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
from pathlib import Path
from typing import Any, Callable, Protocol

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
    """Structured generation through the project's configured Strands model."""

    def __init__(self, model: Any | None = None, model_id: str | None = None) -> None:
        from strands import Agent

        if model is None:
            from .rag_agent import _model

            model = _model()
        self._agent = Agent(model=model, system_prompt=SYSTEM_PROMPT, name="HindsightContentDrafter")
        self.model_id = model_id or os.getenv("OLLAMA_MODEL_ID", "") or os.getenv("BEDROCK_MODEL_ID", "")

    def __call__(self, prompt: str) -> GeneratedDraftModel:
        return self._agent.structured_output(GeneratedDraftModel, prompt)


def _citation_menu(bundle: dict[str, Any], incident_key: str) -> dict[str, Any]:
    """The closed world the model is allowed to cite from."""
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
    for language, groups in bundle["by_language"].items():
        for group_name, passages in groups.items():
            for passage in passages:
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
) -> str:
    path = bundle["risk_path"]
    menu = _citation_menu(bundle, incident_key)

    sources_text: list[str] = []
    for language, groups in bundle["by_language"].items():
        for group_name, passages in groups.items():
            for passage in passages:
                sources_text.append(
                    f"[corpus:{passage['passage_id']}] ({language}/{passage['section_role']}) "
                    f"{passage['citation_label']}\n{passage['text'][:1200]}"
                )

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

    if previous_findings:
        parts += [
            "",
            "YOUR PREVIOUS ATTEMPT FAILED THESE CHECKS. Fix them or drop the claim.",
            json.dumps(previous_findings, ensure_ascii=False, indent=2),
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
    generator = generator or StrandsClaimGenerator()

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

    for attempt in range(max_regenerations + 1):
        prompt = build_prompt(
            bundle,
            incident_key=incident_key,
            incident_narrative=incident_narrative,
            previous_findings=findings_payload,
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
