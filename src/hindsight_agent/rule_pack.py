"""S3 + R1–R4: the versioned rule pack and the evolution loop.

A rule pack holds the parts of the screen a site is allowed to tune: which
industries count as near-neighbours, which hazard classes it has muted, how
many alerts a week it will accept, and how content is presented. Every version
is immutable, signed off by a person, and reversible.

The loop is deliberately slow. Feedback is collected continuously (R1) but only
turned into proposals on demand (R2); a proposal changes nothing until a person
accepts it (R3); and every activation keeps the previous version intact so it
can be rolled back in one call (R4).

What a diff may **not** touch is enforced here, in code:

    MUTABLE_KEYS      what a proposal is allowed to change
    IMMUTABLE_KEYS    the safety kernel - evidence requirements, approval
                      requirements, cross-site isolation, the routing rule that
                      keeps UNKNOWN out of RED

A proposal that reaches into the second set is rejected at creation time, so no
sequence of accepted diffs can erode the safety properties the rest of the
system relies on. This is the difference between a system that learns and a
system that drifts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .workflow_db import (
    connect,
    ensure_workflow_schema,
    new_id,
    payload_hash,
    utc_now,
)

RULE_PACK_KIND = "rule-pack-v1"

#: A proposal may only touch these. Everything is additive or a small nudge.
MUTABLE_KEYS = frozenset({
    "naics_neighbours",      # 视为邻近行业的 NAICS 前缀
    "muted_hazards",         # 本厂选择不再接收的危险类别
    "muted_equipment",       # 本厂选择不再接收的设备
    "boosted_hazards",       # 提权的危险类别
    "weekly_alert_budget",   # 每周愿意接收的提醒上限
    "route_to_review",       # 强制走人审的危险类别
    "ask_site_fact",         # 遇到这些类别时优先追问的事实
    "presentation",          # 语言、排版偏好
})

#: The safety kernel. No accepted diff can reach any of these.
IMMUTABLE_KEYS = frozenset({
    "require_evidence_for_every_claim",
    "require_human_approval_before_distribution",
    "unknown_never_routes_red",
    "cross_site_isolation",
    "numerals_must_be_sourced",
})

DEFAULT_PAYLOAD: dict[str, Any] = {
    "kind": RULE_PACK_KIND,
    # --- 安全内核，不可变 ---
    "require_evidence_for_every_claim": True,
    "require_human_approval_before_distribution": True,
    "unknown_never_routes_red": True,
    "cross_site_isolation": True,
    "numerals_must_be_sourced": True,
    # --- 可调，从保守默认起步 ---
    "naics_neighbours": ["332", "331", "333"],
    "muted_hazards": [],
    "muted_equipment": [],
    "boosted_hazards": [],
    "weekly_alert_budget": 5,
    "route_to_review": [],
    "ask_site_fact": [],
    "presentation": {"primary_language": "en", "second_language": "es"},
}


class RulePackError(RuntimeError):
    """Raised when a change would breach the safety kernel or the version chain."""


@dataclass(frozen=True)
class RulePack:
    version: str
    payload: dict[str, Any]
    status: str
    parent_version: str | None
    approved_by: str | None

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)


def _validate(payload: dict[str, Any]) -> None:
    for key in IMMUTABLE_KEYS:
        if payload.get(key) is not True:
            raise RulePackError(
                f"安全内核项 {key} 必须为 True；规则包不得关闭它。"
            )


def initialize(
    database_path: Path | str,
    *,
    approved_by: str = "system",
    payload: dict[str, Any] | None = None,
) -> RulePack:
    """S3: create and activate v1 if the site has no rule pack yet."""
    ensure_workflow_schema(database_path)
    existing = active_pack(database_path, required=False)
    if existing is not None:
        return existing

    body = dict(payload or DEFAULT_PAYLOAD)
    _validate(body)
    version = "rp-v1"
    connection = connect(database_path)
    try:
        with connection:
            connection.execute(
                "INSERT INTO rule_packs(version, parent_version, payload_json, "
                "payload_hash, status, approved_by, approved_at, created_at, note) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (version, None, json.dumps(body, ensure_ascii=False),
                 payload_hash(body), "active", approved_by, utc_now(), utc_now(),
                 "初始保守默认"),
            )
    finally:
        connection.close()
    return RulePack(version, body, "active", None, approved_by)


def active_pack(database_path: Path | str, *, required: bool = True) -> RulePack | None:
    connection = connect(database_path)
    try:
        row = connection.execute(
            "SELECT * FROM rule_packs WHERE status='active' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    except Exception:
        row = None
    finally:
        connection.close()
    if row is None:
        if required:
            raise RulePackError("没有生效的规则包；先运行 rule_pack.initialize()")
        return None
    return RulePack(
        row["version"], json.loads(row["payload_json"]), row["status"],
        row["parent_version"], row["approved_by"],
    )


# --------------------------------------------------------------------------
# R2 · diff 提案
# --------------------------------------------------------------------------

#: A hazard class rejected this many times in a row is worth proposing a mute.
#: Two is the spec's own example; it is low on purpose, because a proposal is
#: cheap and a person still has to accept it.
REJECTION_STREAK = 2


def propose_diffs(
    database_path: Path | str,
    *,
    lookback: int = 200,
) -> list[dict[str, Any]]:
    """R2: turn accumulated feedback into small, evidence-carrying proposals.

    One proposal changes one thing. Each carries the signals that justify it, so
    the reviewer in R3 is reading evidence rather than trusting a summary.
    """
    pack = active_pack(database_path)
    connection = connect(database_path)
    try:
        signals = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM feedback_signals ORDER BY created_at DESC LIMIT ?",
                (lookback,),
            )
        ]
    finally:
        connection.close()

    rejections: dict[str, list[dict[str, Any]]] = {}
    unknown_hits: dict[str, list[dict[str, Any]]] = {}
    for signal in signals:
        feature = json.loads(signal["feature_json"])
        hazard = feature.get("hazard_id")
        if not hazard:
            continue
        if signal["signal_kind"] == "REJECTED":
            rejections.setdefault(hazard, []).append(signal)
        elif signal["signal_kind"] == "UNKNOWN_BLOCKED":
            unknown_hits.setdefault(hazard, []).append(signal)

    proposals: list[dict[str, Any]] = []
    muted = set(pack.get("muted_hazards", []))
    asking = set(pack.get("ask_site_fact", []))

    for hazard, hits in sorted(rejections.items()):
        if len(hits) >= REJECTION_STREAK and hazard not in muted:
            proposals.append(
                _create_diff(
                    database_path, pack,
                    change_kind="mute_hazard",
                    change={"key": "muted_hazards", "add": hazard},
                    evidence={
                        "reason": f"{hazard} 连续被驳回 {len(hits)} 次",
                        "signal_ids": [h["signal_id"] for h in hits[:5]],
                        "reason_codes": sorted({h["reason_code"] or "" for h in hits}),
                    },
                )
            )

    for hazard, hits in sorted(unknown_hits.items()):
        if len(hits) >= REJECTION_STREAK and hazard not in asking:
            proposals.append(
                _create_diff(
                    database_path, pack,
                    change_kind="ask_site_fact",
                    change={"key": "ask_site_fact", "add": hazard},
                    evidence={
                        "reason": f"{hazard} 有 {len(hits)} 次因事实不足无法判定",
                        "signal_ids": [h["signal_id"] for h in hits[:5]],
                    },
                )
            )
    return proposals


def _create_diff(
    database_path: Path | str,
    pack: RulePack,
    *,
    change_kind: str,
    change: dict[str, Any],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    key = change.get("key")
    if key not in MUTABLE_KEYS:
        raise RulePackError(
            f"提案试图修改 {key}，它不在可调项内。安全内核与权限规则不可通过 diff 修改。"
        )
    diff_id = new_id("diff")
    proposed_version = f"{pack.version}+{diff_id[-6:]}"
    connection = connect(database_path)
    try:
        with connection:
            connection.execute(
                "INSERT INTO rule_pack_diffs(diff_id, from_version, proposed_version, "
                "change_kind, change_json, evidence_json, status, created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (diff_id, pack.version, proposed_version, change_kind,
                 json.dumps(change, ensure_ascii=False),
                 json.dumps(evidence, ensure_ascii=False), "proposed", utc_now()),
            )
    finally:
        connection.close()
    return {
        "diff_id": diff_id, "from_version": pack.version,
        "proposed_version": proposed_version, "change_kind": change_kind,
        "change": change, "evidence": evidence, "status": "proposed",
    }


def pending_diffs(database_path: Path | str) -> list[dict[str, Any]]:
    connection = connect(database_path)
    try:
        rows = connection.execute(
            "SELECT * FROM rule_pack_diffs WHERE status='proposed' ORDER BY created_at"
        ).fetchall()
    finally:
        connection.close()
    return [
        {**dict(row),
         "change": json.loads(row["change_json"]),
         "evidence": json.loads(row["evidence_json"])}
        for row in rows
    ]


# --------------------------------------------------------------------------
# R3 · 人工批准　R4 · 版本化与回滚
# --------------------------------------------------------------------------

def decide_diff(
    database_path: Path | str,
    diff_id: str,
    *,
    decision: str,
    decided_by: str,
) -> dict[str, Any]:
    """R3: a person accepts or rejects one proposal. Accepting activates a new
    version; the previous one is retired, not deleted."""
    if decision not in {"accepted", "rejected"}:
        raise RulePackError("decision 只能是 accepted 或 rejected")
    if not decided_by.strip():
        raise RulePackError("必须记录批准人；规则包变更不接受匿名决定")

    connection = connect(database_path)
    try:
        row = connection.execute(
            "SELECT * FROM rule_pack_diffs WHERE diff_id=?", (diff_id,)
        ).fetchone()
        if row is None:
            raise RulePackError(f"提案不存在: {diff_id}")
        if row["status"] != "proposed":
            raise RulePackError(f"提案 {diff_id} 已被处理为 {row['status']}")
        change = json.loads(row["change_json"])
    finally:
        connection.close()

    if decision == "rejected":
        _close_diff(database_path, diff_id, "rejected", decided_by)
        return {"diff_id": diff_id, "status": "rejected", "active_version": active_pack(database_path).version}

    pack = active_pack(database_path)
    payload = json.loads(json.dumps(pack.payload))  # deep copy
    key = change["key"]
    if key not in MUTABLE_KEYS:
        raise RulePackError(f"拒绝应用：{key} 不可调")
    if "add" in change:
        current = list(payload.get(key, []))
        if change["add"] not in current:
            current.append(change["add"])
        payload[key] = sorted(current)
    elif "set" in change:
        payload[key] = change["set"]
    _validate(payload)

    version = row["proposed_version"]
    connection = connect(database_path)
    try:
        with connection:
            connection.execute("UPDATE rule_packs SET status='retired' WHERE status='active'")
            connection.execute(
                "INSERT INTO rule_packs(version, parent_version, payload_json, "
                "payload_hash, status, approved_by, approved_at, created_at, note) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (version, pack.version, json.dumps(payload, ensure_ascii=False),
                 payload_hash(payload), "active", decided_by, utc_now(), utc_now(),
                 f"接受提案 {diff_id}"),
            )
            connection.execute(
                "UPDATE rule_pack_diffs SET status='accepted', decided_by=?, decided_at=? "
                "WHERE diff_id=?",
                (decided_by, utc_now(), diff_id),
            )
    finally:
        connection.close()
    return {"diff_id": diff_id, "status": "accepted", "active_version": version}


def _close_diff(database_path: Path | str, diff_id: str, status: str, decided_by: str) -> None:
    connection = connect(database_path)
    try:
        with connection:
            connection.execute(
                "UPDATE rule_pack_diffs SET status=?, decided_by=?, decided_at=? WHERE diff_id=?",
                (status, decided_by, utc_now(), diff_id),
            )
    finally:
        connection.close()


def rollback(database_path: Path | str, *, to_version: str, decided_by: str) -> RulePack:
    """R4: reactivate an earlier version in one call. History is never deleted."""
    connection = connect(database_path)
    try:
        row = connection.execute(
            "SELECT * FROM rule_packs WHERE version=?", (to_version,)
        ).fetchone()
        if row is None:
            raise RulePackError(f"没有这个版本: {to_version}")
        with connection:
            connection.execute("UPDATE rule_packs SET status='retired' WHERE status='active'")
            connection.execute(
                "UPDATE rule_packs SET status='active', approved_by=?, approved_at=? "
                "WHERE version=?",
                (decided_by, utc_now(), to_version),
            )
    finally:
        connection.close()
    return active_pack(database_path)


def version_history(database_path: Path | str) -> list[dict[str, Any]]:
    connection = connect(database_path)
    try:
        rows = connection.execute(
            "SELECT version, parent_version, status, approved_by, approved_at, "
            "payload_hash, note, created_at FROM rule_packs ORDER BY created_at"
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]
