"""The claim contract shared by G2, G3 and G4.

Nothing in this project emits prose that a model wrote freehand. A draft is a
list of typed claims, each carrying its own citations, and the English and
Spanish renderings are two views of the *same* claim list. That is what makes
the fourth iron rule mechanically checkable: G4 verifies claims, and the
renderers can only assemble sentences out of claims that survived.

Citation namespaces:

``corpus:<passage_id>``   a passage in the frozen grounding corpus (G1)
``incident:<key>``        one row of ``osha_sir_incidents``
``site:<risk_id>``        one hazard path in the factory risk register
``control:<control_id>``  one approved control in ``safety_controls``

Bilingual policy: Spanish is rendered from the same claims as English and
carries the same citation set. Where OSHA publishes a Spanish edition, a claim
cites it directly. Where it does not - which is the common case for 29 CFR -
the Spanish sentence still cites the English source and the pack is stamped
``translated_from_en``. A translated sentence is still a sourced sentence; an
unsourced one is deleted either way.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

PACK_VERSION = "learning-pack-v1.0"

CLAIM_INCIDENT_FACT = "INCIDENT_FACT"
CLAIM_SITE_FACT = "SITE_FACT"
CLAIM_REQUIREMENT = "REQUIREMENT"
CLAIM_CONTROL = "CONTROL"
CLAIM_UNKNOWN = "UNKNOWN"
CLAIM_TYPES = (
    CLAIM_INCIDENT_FACT,
    CLAIM_SITE_FACT,
    CLAIM_REQUIREMENT,
    CLAIM_CONTROL,
    CLAIM_UNKNOWN,
)

#: Claim types that may appear as something the site should *do*. A narrative
#: claim explains what happened elsewhere; it can never become an instruction.
ACTION_CLAIM_TYPES = frozenset({CLAIM_REQUIREMENT, CLAIM_CONTROL})

STATE_DRAFTED = "DRAFTED"
STATE_SELF_CHECK_OK = "SELF_CHECK_OK"
STATE_SELF_CHECK_FAIL = "SELF_CHECK_FAIL"
STATE_NEEDS_HUMAN_WRITER = "NEEDS_HUMAN_WRITER"
STATE_PENDING_APPROVAL = "PENDING_APPROVAL"

ADVISORY_EN = (
    "Advisory only. This material was assembled automatically from public "
    "incident records and published safety standards, and reviewed by a person "
    "before release. It is not a legal determination, not a site inspection, "
    "and not a substitute for your own risk assessment."
)
ADVISORY_ES = (
    "Solo con carácter informativo. Este material se elaboró automáticamente a "
    "partir de registros públicos de incidentes y normas de seguridad "
    "publicadas, y fue revisado por una persona antes de su distribución. No "
    "constituye una determinación legal, ni una inspección del sitio, ni "
    "sustituye su propia evaluación de riesgos."
)

#: Numbers and measurements. Any of these appearing in a claim must also appear
#: in one of that claim's sources - this is the check that stops a model
#: inventing a clearance distance, an interval or a torque.
#:
#: Only unambiguous multi-character units are recognised. Single-letter units
#: were tried and removed: they turn "press 01 in the stamping area" into the
#: measurement "01in" and Spanish "5 veces" into "5V", which flags correct
#: sentences as unsourced. A bare number is still compared, so an invented
#: "250 mm" is caught by its 250 either way.
_NUMERAL_UNIT = r"(?:%|mm|cm|km|ft|kg|lb|nm|min|hz|psi|rpm|°c|°f)"
NUMERAL = re.compile(
    rf"\d+(?:[.,]\d+)*\s?{_NUMERAL_UNIT}\b|\d+(?:[.,]\d+)*",
    re.IGNORECASE,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_numerals(text: str) -> list[str]:
    """Numerals in comparable form, so 1,000 and 1000 do not look different."""
    out = []
    for match in NUMERAL.finditer(text or ""):
        token = match.group(0).casefold().replace(",", "").replace(" ", "")
        out.append(token)
    return sorted(set(out))


@dataclass
class Claim:
    claim_id: str
    claim_type: str
    text_en: str
    text_es: str
    citations: list[str] = field(default_factory=list)
    quote: str | None = None
    quote_citation: str | None = None
    control_id: str | None = None
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.claim_type not in CLAIM_TYPES:
            raise ValueError(f"Unknown claim type: {self.claim_type}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class QuizItem:
    question_id: str
    question_en: str
    question_es: str
    options_en: list[str]
    options_es: list[str]
    correct_index: int
    claim_id: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LearningDraft:
    """One incident, one hazard path, one reviewable package."""

    draft_id: str
    risk_assessment_id: str
    assessment_code: str
    incident_citation: str
    incident_title: str
    equipment_name: str
    hazard_name: str
    task_name: str
    exposed_role: str
    briefing_claims: list[Claim] = field(default_factory=list)
    talk_claims: list[Claim] = field(default_factory=list)
    quiz: list[QuizItem] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    state: str = STATE_DRAFTED
    generator_version: str = PACK_VERSION
    model_id: str = ""
    translated_from_en: bool = True
    created_at: str = field(default_factory=utc_now)
    regeneration_count: int = 0

    def all_claims(self) -> list[Claim]:
        return [*self.briefing_claims, *self.talk_claims]

    def claim_by_id(self, claim_id: str) -> Claim | None:
        for claim in self.all_claims():
            if claim.claim_id == claim_id:
                return claim
        return None

    def content_hash(self) -> str:
        payload = {
            "risk_assessment_id": self.risk_assessment_id,
            "incident_citation": self.incident_citation,
            "briefing": [c.to_dict() for c in self.briefing_claims],
            "talk": [c.to_dict() for c in self.talk_claims],
            "quiz": [q.to_dict() for q in self.quiz],
            "unknowns": self.unknowns,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["content_hash"] = self.content_hash()
        return data


# --------------------------------------------------------------------------
# G2 - the manager's briefing, in the Fatal Facts shape
# --------------------------------------------------------------------------

_BRIEFING_SECTIONS_EN = [
    ("WHAT HAPPENED", (CLAIM_INCIDENT_FACT,)),
    ("WHY IT APPLIES TO THIS SITE", (CLAIM_SITE_FACT,)),
    ("WHAT THE STANDARD REQUIRES", (CLAIM_REQUIREMENT,)),
    ("RECOMMENDED ACTION", (CLAIM_CONTROL,)),
    ("NOT ESTABLISHED", (CLAIM_UNKNOWN,)),
]
_BRIEFING_SECTIONS_ES = [
    ("QUÉ OCURRIÓ", (CLAIM_INCIDENT_FACT,)),
    ("POR QUÉ APLICA A ESTA PLANTA", (CLAIM_SITE_FACT,)),
    ("QUÉ EXIGE LA NORMA", (CLAIM_REQUIREMENT,)),
    ("ACCIÓN RECOMENDADA", (CLAIM_CONTROL,)),
    ("NO ESTABLECIDO", (CLAIM_UNKNOWN,)),
]


def render_briefing(draft: LearningDraft, language: str = "en") -> dict[str, Any]:
    """G2 output. Every line carries the citations of the claim behind it."""
    spanish = language == "es"
    sections_spec = _BRIEFING_SECTIONS_ES if spanish else _BRIEFING_SECTIONS_EN
    sections: list[dict[str, Any]] = []

    for heading, types in sections_spec:
        lines = [
            {
                "claim_id": claim.claim_id,
                "text": claim.text_es if spanish else claim.text_en,
                "citations": claim.citations,
                "quote": claim.quote,
            }
            for claim in draft.briefing_claims
            if claim.claim_type in types
        ]
        if lines:
            sections.append({"heading": heading, "lines": lines})

    return {
        "kind": "briefing",
        "language": language,
        "title": (
            f"{draft.equipment_name} · {draft.hazard_name} · {draft.assessment_code}"
        ),
        "incident_citation": draft.incident_citation,
        "sections": sections,
        "advisory": ADVISORY_ES if spanish else ADVISORY_EN,
        "translated_from_en": spanish and draft.translated_from_en,
        "content_hash": draft.content_hash(),
    }


# --------------------------------------------------------------------------
# G3 - the five-minute toolbox talk and its quiz
# --------------------------------------------------------------------------

_TALK_HEADINGS_EN = {
    CLAIM_INCIDENT_FACT: "What happened somewhere else",
    CLAIM_SITE_FACT: "Where we do the same job",
    CLAIM_REQUIREMENT: "What the rule says",
    CLAIM_CONTROL: "What we check before the next shift",
    CLAIM_UNKNOWN: "What we do not know yet",
}
_TALK_HEADINGS_ES = {
    CLAIM_INCIDENT_FACT: "Qué ocurrió en otro lugar",
    CLAIM_SITE_FACT: "Dónde hacemos el mismo trabajo",
    CLAIM_REQUIREMENT: "Qué dice la norma",
    CLAIM_CONTROL: "Qué revisamos antes del próximo turno",
    CLAIM_UNKNOWN: "Qué aún no sabemos",
}


def render_toolbox_talk(draft: LearningDraft, language: str = "en") -> dict[str, Any]:
    """G3 output: a five-minute script plus its quiz, both claim-backed."""
    spanish = language == "es"
    headings = _TALK_HEADINGS_ES if spanish else _TALK_HEADINGS_EN

    blocks: list[dict[str, Any]] = []
    for claim_type in (
        CLAIM_INCIDENT_FACT, CLAIM_SITE_FACT, CLAIM_REQUIREMENT,
        CLAIM_CONTROL, CLAIM_UNKNOWN,
    ):
        lines = [
            {
                "claim_id": claim.claim_id,
                "text": claim.text_es if spanish else claim.text_en,
                "citations": claim.citations,
            }
            for claim in draft.talk_claims
            if claim.claim_type == claim_type
        ]
        if lines:
            blocks.append({"heading": headings[claim_type], "lines": lines})

    quiz = [
        {
            "question_id": item.question_id,
            "question": item.question_es if spanish else item.question_en,
            "options": item.options_es if spanish else item.options_en,
            "correct_index": item.correct_index,
            "claim_id": item.claim_id,
        }
        for item in draft.quiz
    ]

    word_count = sum(
        len((line["text"] or "").split())
        for block in blocks
        for line in block["lines"]
    )
    return {
        "kind": "toolbox_talk",
        "language": language,
        "title": f"{draft.equipment_name} · {draft.task_name}",
        "audience_role": draft.exposed_role,
        "blocks": blocks,
        "quiz": quiz,
        "estimated_minutes": round(max(1.0, word_count / 130), 1),
        "advisory": ADVISORY_ES if spanish else ADVISORY_EN,
        "translated_from_en": spanish and draft.translated_from_en,
        "content_hash": draft.content_hash(),
    }


def render_pack(draft: LearningDraft) -> dict[str, Any]:
    """Everything a reviewer sees, in both languages, from one claim list."""
    return {
        "pack_version": PACK_VERSION,
        "draft_id": draft.draft_id,
        "state": draft.state,
        "risk_assessment_id": draft.risk_assessment_id,
        "assessment_code": draft.assessment_code,
        "incident_citation": draft.incident_citation,
        "incident_title": draft.incident_title,
        "model_id": draft.model_id,
        "created_at": draft.created_at,
        "regeneration_count": draft.regeneration_count,
        "content_hash": draft.content_hash(),
        "unknowns": draft.unknowns,
        "briefing": {
            "en": render_briefing(draft, "en"),
            "es": render_briefing(draft, "es"),
        },
        "toolbox_talk": {
            "en": render_toolbox_talk(draft, "en"),
            "es": render_toolbox_talk(draft, "es"),
        },
    }
