"""Residual-path transfer assessment: decide whether an incident needs a human.

This module answers one question per factory hazard path:

    Given what this site has actually verified, does the hazard path from the
    external incident still survive here?

It is deterministic. No model call, no embedding, no threshold tuning. Every
decision is a fixed traversal over rows the EHS team already signed off on,
so the answer can be replayed, unit-tested and defended line by line.

A hazard path is one ``risk_assessments`` row: equipment x task x hazard x
exposed role. The controls on that path are ``risk_assessment_controls``. The
proof that a control still works is ``control_verifications``.

Decision precedence (first rule that fires wins):

  1. The site itself says the path is not controlled -- ``risk_accepted = 0``,
     ``status = 'action_required'``, or an open corrective action references
     the path.                                            -> ACTIONABLE
  2. No control is recorded on the path at all.           -> UNKNOWN
  3. At least one control is VERIFIED as of the cut-off.  -> NO_ACTIONABLE_TRANSFER
  4. At least one control has never been verified here.   -> UNKNOWN
  5. Everything left is expired, conditional or not
     implemented.                                         -> ACTIONABLE

Rule 1 outranks rule 3 on purpose: a verified guard cannot block a path the
site has already declared unaccepted. Rule 4 encodes the hard constraint that
UNKNOWN is never False -- absent evidence downgrades a claim, it never
promotes one.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field, asdict

from strands import tool
from datetime import date
from pathlib import Path
from typing import Any, Iterable

ASSESSMENT_VERSION = "residual-path-v1.0"

# --- control states -------------------------------------------------------
VERIFIED = "VERIFIED"
CONDITIONAL = "CONDITIONAL"
EXPIRED = "EXPIRED"
UNVERIFIED = "UNVERIFIED"
NOT_IMPLEMENTED = "NOT_IMPLEMENTED"

BLOCKING_STATES = frozenset({VERIFIED})

# --- decisions ------------------------------------------------------------
NO_ACTIONABLE_TRANSFER = "NO_ACTIONABLE_TRANSFER"
UNKNOWN = "UNKNOWN"
ACTIONABLE = "ACTIONABLE"

# --- reason codes ---------------------------------------------------------
R_SITE_DECLARES_UNCONTROLLED = "SITE_DECLARES_UNCONTROLLED"
R_NO_CONTROL_RECORDED = "NO_CONTROL_RECORDED"
R_BLOCKED_BY_VERIFIED_CONTROL = "BLOCKED_BY_VERIFIED_CONTROL"
R_CONTROL_NEVER_VERIFIED = "CONTROL_NEVER_VERIFIED"
R_ALL_CONTROLS_LAPSED = "ALL_CONTROLS_LAPSED"

#: Severity at or above which an UNKNOWN must reach a human rather than be
#: filed. Uses ``risk_assessments.initial_severity`` on the site's own 1-5
#: scale; the site sets the scale, this module only reads it.
HIGH_SEVERITY_FLOOR = 4


@dataclass(frozen=True)
class ControlEvidence:
    """One control on the path, plus the verification record that proves it."""

    safety_control_id: str
    control_name: str
    hierarchy_code: str
    implementation_status: str
    effectiveness_rating: int
    state: str
    verification_code: str | None = None
    verification_date: str | None = None
    verification_result: str | None = None
    verification_finding: str | None = None
    evidence_reference: str | None = None
    next_due_on: str | None = None

    @property
    def blocks(self) -> bool:
        return self.state in BLOCKING_STATES


@dataclass
class TransferCertificate:
    """Why this hazard path was surfaced, or why it was not.

    A ``NO_ACTIONABLE_TRANSFER`` certificate is a blocking-cut certificate: it
    names the controls that cut the path and the evidence that proves each one.
    It certifies that the obligation to check was met. It does not certify that
    the factory is safe.
    """

    risk_assessment_id: str
    assessment_code: str
    decision: str
    reason_code: str
    as_of: str
    equipment_id: str
    equipment_name: str | None
    task_name: str | None
    hazard_id: str
    hazard_name: str | None
    exposed_role: str | None
    hazardous_situation: str
    initial_severity: int
    initial_score: int
    initial_level: str
    residual_score: int
    residual_level: str
    risk_accepted: int
    site_status: str
    requires_review: bool
    blocking_cut: list[ControlEvidence] = field(default_factory=list)
    residual_controls: list[ControlEvidence] = field(default_factory=list)
    open_corrective_actions: list[dict[str, Any]] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    contradictions: list[str] = field(default_factory=list)
    missing_facts: list[str] = field(default_factory=list)
    assessment_version: str = ASSESSMENT_VERSION

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["certificate_hash"] = self.certificate_hash()
        return payload

    def certificate_hash(self) -> str:
        payload = asdict(self)
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def headline(self) -> str:
        if self.decision == NO_ACTIONABLE_TRANSFER:
            names = "、".join(c.control_name for c in self.blocking_cut)
            return f"{self.assessment_code} 已被 {names} 阻断"
        if self.decision == UNKNOWN:
            return f"{self.assessment_code} 事实不足，需要补充 {len(self.missing_facts)} 项"
        return f"{self.assessment_code} 残余路径成立，需要决定"


_PATH_SQL = """
SELECT ra.id, ra.assessment_code, ra.equipment_id, ra.hazard_id,
       ra.hazardous_situation, ra.initial_severity, ra.initial_score,
       ra.initial_level, ra.residual_score, ra.residual_level,
       ra.risk_accepted, ra.status AS site_status,
       e.name AS equipment_name, t.name AS task_name,
       h.name AS hazard_name, w.name AS exposed_role
FROM risk_assessments AS ra
LEFT JOIN equipment AS e ON e.id = ra.equipment_id
LEFT JOIN tasks AS t ON t.id = ra.task_id
LEFT JOIN hazards AS h ON h.id = ra.hazard_id
LEFT JOIN worker_roles AS w ON w.id = ra.exposed_role_id
"""

_CONTROL_SQL = """
SELECT rac.risk_assessment_id, rac.safety_control_id,
       rac.implementation_status, rac.effectiveness_rating,
       sc.name AS control_name, sc.hierarchy_code
FROM risk_assessment_controls AS rac
LEFT JOIN safety_controls AS sc ON sc.id = rac.safety_control_id
"""

#: Latest verification per (equipment, control). Ties on date break on the
#: verification code so the pick is stable across runs.
_VERIFICATION_SQL = """
SELECT equipment_id, safety_control_id, verification_code, verification_date,
       result, finding, evidence_reference, next_due_on
FROM control_verifications
ORDER BY equipment_id, safety_control_id, verification_date, verification_code
"""

_CORRECTIVE_SQL = """
SELECT risk_assessment_id, action_code, title, priority, owner_role_id,
       due_on, status
FROM corrective_actions
WHERE risk_assessment_id IS NOT NULL AND status NOT IN ('closed', 'completed')
"""


def _classify_control(row: dict[str, Any], verification: dict[str, Any] | None,
                      as_of: str) -> ControlEvidence:
    base = dict(
        safety_control_id=row["safety_control_id"],
        control_name=row["control_name"] or row["safety_control_id"],
        hierarchy_code=row["hierarchy_code"] or "unknown",
        implementation_status=row["implementation_status"],
        effectiveness_rating=row["effectiveness_rating"],
    )
    if row["implementation_status"] != "implemented":
        return ControlEvidence(state=NOT_IMPLEMENTED, **base)
    if verification is None:
        return ControlEvidence(state=UNVERIFIED, **base)

    proof = dict(
        verification_code=verification["verification_code"],
        verification_date=verification["verification_date"],
        verification_result=verification["result"],
        verification_finding=verification["finding"],
        evidence_reference=verification["evidence_reference"],
        next_due_on=verification["next_due_on"],
    )
    if verification["result"] != "pass":
        return ControlEvidence(state=CONDITIONAL, **base, **proof)
    # ISO date strings compare correctly as text.
    if verification["next_due_on"] < as_of:
        return ControlEvidence(state=EXPIRED, **base, **proof)
    return ControlEvidence(state=VERIFIED, **base, **proof)


def _decide(path: dict[str, Any], controls: list[ControlEvidence],
            actions: list[dict[str, Any]], as_of: str) -> TransferCertificate:
    assumptions: list[str] = []
    contradictions: list[str] = []
    missing: list[str] = []

    blocking = [c for c in controls if c.blocks]
    residual = [c for c in controls if not c.blocks]

    assumptions.append(
        "本判定只使用本厂已登记并签署的事实；未登记的控制措施一律不假定存在。"
    )
    assumptions.append(
        f"控制有效期以 control_verifications.next_due_on 与截止日 {as_of} 比较得出。"
    )
    if any(c.effectiveness_rating for c in controls):
        assumptions.append(
            "effectiveness_rating 已记录但不参与判定；本模块不自行设定有效性阈值。"
        )

    for c in residual:
        if c.state == EXPIRED:
            contradictions.append(
                f"{c.control_name} 上次验证 {c.verification_date} 通过，"
                f"但有效期已于 {c.next_due_on} 到期。"
            )
        elif c.state == CONDITIONAL:
            contradictions.append(
                f"{c.control_name} 验证结果为 {c.verification_result}："
                f"{c.verification_finding}"
            )
        elif c.state == UNVERIFIED:
            missing.append(
                f"{c.control_name}（{c.safety_control_id}）在设备 "
                f"{path['equipment_id']} 上没有任何验证记录，它最近一次测试是什么时候？"
            )
        elif c.state == NOT_IMPLEMENTED:
            contradictions.append(
                f"{c.control_name} 登记状态为 {c.implementation_status}，尚未实施。"
            )

    # Rule 1 -- the site's own record overrides any blocking cut.
    if actions or path["risk_accepted"] == 0 or path["site_status"] == "action_required":
        if path["risk_accepted"] == 0:
            contradictions.append("本厂风险登记册记录该路径剩余风险未被接受。")
        if path["site_status"] == "action_required":
            contradictions.append("本厂风险评估状态为 action_required。")
        for a in actions:
            contradictions.append(
                f"存在未闭环整改 {a['action_code']}（{a['status']}，"
                f"期限 {a['due_on']}）：{a['title']}"
            )
        decision, reason = ACTIONABLE, R_SITE_DECLARES_UNCONTROLLED
    # Rule 2
    elif not controls:
        missing.append(
            f"路径 {path['assessment_code']} 未登记任何控制措施。"
            "这是确实没有，还是尚未录入？强否定必须由人显式确认。"
        )
        decision, reason = UNKNOWN, R_NO_CONTROL_RECORDED
    # Rule 3
    elif blocking:
        decision, reason = NO_ACTIONABLE_TRANSFER, R_BLOCKED_BY_VERIFIED_CONTROL
    # Rule 4
    elif any(c.state == UNVERIFIED for c in residual):
        decision, reason = UNKNOWN, R_CONTROL_NEVER_VERIFIED
    # Rule 5
    else:
        decision, reason = ACTIONABLE, R_ALL_CONTROLS_LAPSED

    requires_review = decision == UNKNOWN and path["initial_severity"] >= HIGH_SEVERITY_FLOOR

    return TransferCertificate(
        risk_assessment_id=path["id"],
        assessment_code=path["assessment_code"],
        decision=decision,
        reason_code=reason,
        as_of=as_of,
        equipment_id=path["equipment_id"],
        equipment_name=path["equipment_name"],
        task_name=path["task_name"],
        hazard_id=path["hazard_id"],
        hazard_name=path["hazard_name"],
        exposed_role=path["exposed_role"],
        hazardous_situation=path["hazardous_situation"],
        initial_severity=path["initial_severity"],
        initial_score=path["initial_score"],
        initial_level=path["initial_level"],
        residual_score=path["residual_score"],
        residual_level=path["residual_level"],
        risk_accepted=path["risk_accepted"],
        site_status=path["site_status"],
        requires_review=requires_review,
        blocking_cut=blocking,
        residual_controls=residual,
        open_corrective_actions=actions,
        assumptions=assumptions,
        contradictions=contradictions,
        missing_facts=missing,
    )


def assess_paths(
    database_path: Path | str,
    *,
    as_of: str | None = None,
    risk_assessment_ids: Iterable[str] | None = None,
) -> list[TransferCertificate]:
    """Assess every factory hazard path, or only the ones named.

    ``as_of`` fixes the cut-off used for verification expiry. Pass it
    explicitly for demos and tests so results are reproducible; it defaults to
    today, which means results legitimately change as verifications lapse.
    """
    as_of = as_of or date.today().isoformat()
    wanted = set(risk_assessment_ids) if risk_assessment_ids is not None else None

    connection = sqlite3.connect(str(database_path))
    connection.row_factory = sqlite3.Row
    try:
        paths = [dict(r) for r in connection.execute(_PATH_SQL)]
        control_rows = [dict(r) for r in connection.execute(_CONTROL_SQL)]
        verification_rows = [dict(r) for r in connection.execute(_VERIFICATION_SQL)]
        action_rows = [dict(r) for r in connection.execute(_CORRECTIVE_SQL)]
    finally:
        connection.close()

    # Last row per (equipment, control) wins; _VERIFICATION_SQL orders ascending.
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in verification_rows:
        latest[(row["equipment_id"], row["safety_control_id"])] = row

    controls_by_path: dict[str, list[dict[str, Any]]] = {}
    for row in control_rows:
        controls_by_path.setdefault(row["risk_assessment_id"], []).append(row)

    actions_by_path: dict[str, list[dict[str, Any]]] = {}
    for row in action_rows:
        actions_by_path.setdefault(row["risk_assessment_id"], []).append(row)

    certificates: list[TransferCertificate] = []
    for path in paths:
        if wanted is not None and path["id"] not in wanted:
            continue
        evidence = [
            _classify_control(
                row, latest.get((path["equipment_id"], row["safety_control_id"])), as_of
            )
            for row in controls_by_path.get(path["id"], [])
        ]
        evidence.sort(key=lambda c: (not c.blocks, c.safety_control_id))
        certificates.append(
            _decide(path, evidence, actions_by_path.get(path["id"], []), as_of)
        )

    order = {ACTIONABLE: 0, UNKNOWN: 1, NO_ACTIONABLE_TRANSFER: 2}
    certificates.sort(
        key=lambda c: (order[c.decision], -c.initial_score, c.assessment_code)
    )
    return certificates


def summarize(certificates: list[TransferCertificate]) -> dict[str, Any]:
    """The one screen the console and the demo both show."""
    by_decision: dict[str, int] = {
        NO_ACTIONABLE_TRANSFER: 0, UNKNOWN: 0, ACTIONABLE: 0
    }
    by_reason: dict[str, int] = {}
    for certificate in certificates:
        by_decision[certificate.decision] += 1
        by_reason[certificate.reason_code] = by_reason.get(certificate.reason_code, 0) + 1
    return {
        "assessment_version": ASSESSMENT_VERSION,
        "as_of": certificates[0].as_of if certificates else None,
        "paths_assessed": len(certificates),
        "blocked": by_decision[NO_ACTIONABLE_TRANSFER],
        "unknown": by_decision[UNKNOWN],
        "actionable": by_decision[ACTIONABLE],
        "requires_review": sum(1 for c in certificates if c.requires_review),
        "by_reason_code": by_reason,
    }


# ==========================================================================
# V1 · 相关性分级路由
# ==========================================================================
# Maps the three-state transfer assessment onto the four levels the workflow
# spec routes on. The mapping is fixed, not tunable, because a threshold you
# can turn is a threshold somebody will turn until the alerts stop.
#
#   ACTIONABLE + 高严重          -> RED      逐条审批
#   ACTIONABLE + 其他            -> YELLOW   批量确认
#   UNKNOWN    + 任何严重度      -> YELLOW   请人过目
#   NO_ACTIONABLE_TRANSFER       -> GREEN    静默归档 + 周报一行
#   没有候选路径                 -> NOT_RELEVANT
#
# UNKNOWN never reaches RED. A high-severity path we cannot resolve is exactly
# the case where the system must not sound certain, so the spec's rule -
# 高严重 × 低置信强制降级为黄 - is implemented as a hard downgrade rather than
# as a confidence score somebody has to interpret.

LEVEL_RED = "RED"
LEVEL_YELLOW = "YELLOW"
LEVEL_GREEN = "GREEN"
LEVEL_NOT_RELEVANT = "NOT_RELEVANT"

#: Highest first. Used to collapse several candidate paths into one verdict.
LEVEL_ORDER = (LEVEL_RED, LEVEL_YELLOW, LEVEL_GREEN, LEVEL_NOT_RELEVANT)

#: Levels that stop at the archive. Everything else reaches a person.
SILENT_LEVELS = frozenset({LEVEL_GREEN, LEVEL_NOT_RELEVANT})

ROUTING_VERSION = "relevance-routing-v1.0"


@dataclass(frozen=True)
class RoutingDecision:
    """One hazard path's level, and the sentence that explains it."""

    level: str
    risk_assessment_id: str
    assessment_code: str
    reason_code: str
    initial_severity: int
    explanation: str
    requires_human: bool
    routing_version: str = ROUTING_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def route_path(certificate: TransferCertificate) -> RoutingDecision:
    """Level one assessed hazard path."""
    high_severity = certificate.initial_severity >= HIGH_SEVERITY_FLOOR

    if certificate.decision == ACTIONABLE:
        level = LEVEL_RED if high_severity else LEVEL_YELLOW
        if certificate.reason_code == R_SITE_DECLARES_UNCONTROLLED:
            explanation = (
                f"{certificate.assessment_code} 的剩余风险本厂尚未接受，"
                f"或存在未闭环整改；这条路径在你们自己的登记册里就是敞开的。"
            )
        else:
            names = "、".join(c.control_name for c in certificate.residual_controls) or "无"
            explanation = (
                f"{certificate.assessment_code} 上的控制（{names}）"
                f"已过期或仅为有条件通过，危险路径仍然成立。"
            )
    elif certificate.decision == UNKNOWN:
        # Deliberately never RED, however severe.
        level = LEVEL_YELLOW
        explanation = (
            f"{certificate.assessment_code} 缺 {len(certificate.missing_facts)} 项关键事实，"
            f"无法判定是否被阻断。未知不等于安全，需要你补充事实。"
        )
    else:
        level = LEVEL_GREEN
        names = "、".join(c.control_name for c in certificate.blocking_cut)
        explanation = (
            f"{certificate.assessment_code} 的危险路径已被 {names} 阻断，"
            f"验证在 {certificate.as_of} 仍然有效。已归档，不打扰。"
        )

    return RoutingDecision(
        level=level,
        risk_assessment_id=certificate.risk_assessment_id,
        assessment_code=certificate.assessment_code,
        reason_code=certificate.reason_code,
        initial_severity=certificate.initial_severity,
        explanation=explanation,
        requires_human=level not in SILENT_LEVELS,
    )


def route_incident(
    certificates: list[TransferCertificate],
) -> dict[str, Any]:
    """Collapse every candidate path for one incident into a single verdict.

    An incident is as urgent as its worst surviving path, so the incident level
    is the highest level among its paths. The other paths are kept, because the
    reviewer needs to see what was checked and cleared, not only what failed.
    """
    if not certificates:
        return {
            "level": LEVEL_NOT_RELEVANT,
            "routing_version": ROUTING_VERSION,
            "requires_human": False,
            "explanation": "没有任何本厂危险路径与这起事故匹配。已归档，周报计入一行。",
            "paths": [],
        }

    decisions = [route_path(certificate) for certificate in certificates]
    rank = {level: index for index, level in enumerate(LEVEL_ORDER)}
    decisions.sort(key=lambda d: (rank[d.level], -d.initial_severity, d.assessment_code))
    top = decisions[0]

    return {
        "level": top.level,
        "routing_version": ROUTING_VERSION,
        "requires_human": top.requires_human,
        "explanation": top.explanation,
        "driving_path": top.risk_assessment_id,
        "paths": [d.to_dict() for d in decisions],
        "counts": {
            level: sum(1 for d in decisions if d.level == level) for level in LEVEL_ORDER
        },
    }


def route_all_paths(
    database_path: Path | str,
    *,
    as_of: str | None = None,
    risk_assessment_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Assess and level every hazard path. This is the daily-run screen."""
    certificates = assess_paths(
        database_path, as_of=as_of, risk_assessment_ids=risk_assessment_ids
    )
    decisions = [route_path(certificate) for certificate in certificates]
    counts = {level: sum(1 for d in decisions if d.level == level) for level in LEVEL_ORDER}
    return {
        "routing_version": ROUTING_VERSION,
        "assessment_version": ASSESSMENT_VERSION,
        "as_of": certificates[0].as_of if certificates else as_of,
        "paths_assessed": len(certificates),
        "counts": counts,
        "requires_human": counts[LEVEL_RED] + counts[LEVEL_YELLOW],
        "silently_archived": counts[LEVEL_GREEN] + counts[LEVEL_NOT_RELEVANT],
        "decisions": [d.to_dict() for d in decisions],
        "certificates": {c.risk_assessment_id: c.to_dict() for c in certificates},
    }


def weekly_digest(routing: dict[str, Any]) -> list[str]:
    """W6 的周报：被静默归档的那些，每条一行，证明系统确实在看。"""
    lines = []
    for decision in routing["decisions"]:
        if decision["level"] in SILENT_LEVELS:
            lines.append(f"[{decision['level']}] {decision['explanation']}")
    return lines


@tool
def screen_factory_risk_paths(as_of: str = "") -> dict:
    """Screen every factory hazard path and return the four-level routing counts."""
    from .config import load_settings

    settings = load_settings()
    routing = route_all_paths(settings.factory_database, as_of=as_of or None)
    # The certificates stay out of the model's context: they are the reviewer's
    # evidence, and they are large. The model gets the screen, not the file.
    return {
        "routing_version": routing["routing_version"],
        "as_of": routing["as_of"],
        "paths_assessed": routing["paths_assessed"],
        "counts": routing["counts"],
        "requires_human": routing["requires_human"],
        "silently_archived": routing["silently_archived"],
        "needs_attention": [
            d for d in routing["decisions"] if d["level"] in (LEVEL_RED, LEVEL_YELLOW)
        ],
    }


def main() -> int:
    import argparse

    from .config import load_settings

    parser = argparse.ArgumentParser(
        description="残余路径验证与 V1 四级路由：判断哪些危险路径真的需要人来决定。"
    )
    parser.add_argument("--as-of", default=None, help="有效期截止日 YYYY-MM-DD，默认今天")
    parser.add_argument("--digest", action="store_true", help="打印被静默归档的周报行")
    parser.add_argument("--out", default=None, help="把完整证书写成 JSON")
    args = parser.parse_args()

    settings = load_settings()
    routing = route_all_paths(settings.factory_database, as_of=args.as_of)
    counts = routing["counts"]
    print(f"截止日 {routing['as_of']} · 共 {routing['paths_assessed']} 条危险路径")
    print(
        f"  红 {counts[LEVEL_RED]}  黄 {counts[LEVEL_YELLOW]}  "
        f"绿 {counts[LEVEL_GREEN]}  无关 {counts[LEVEL_NOT_RELEVANT]}"
    )
    print(f"  需要人看 {routing['requires_human']} 条 · 静默归档 {routing['silently_archived']} 条")
    for decision in routing["decisions"]:
        if decision["level"] in (LEVEL_RED, LEVEL_YELLOW):
            print(f"  [{decision['level']:6s}] sev{decision['initial_severity']} {decision['explanation']}")
    if args.digest:
        print("\n周报（静默归档）:")
        for line in weekly_digest(routing):
            print("  " + line)
    if args.out:
        Path(args.out).write_text(
            json.dumps(routing, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n完整证书已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
