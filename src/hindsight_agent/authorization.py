"""A1: distribution is blocked in code, not by asking the model nicely.

The first iron rule is 未批不触达. A system prompt that says so is a request; a
hook that cancels the tool call is a control. This module is the control.

Two pieces:

``ApprovalToken``
    An HMAC over the exact thing being released: case, pack content hash,
    channel, audience and expiry. Edit the pack and its content hash changes, so
    the token stops verifying - approval is for one artefact, not for a topic.

``DistributionGuard``
    A Strands ``HookProvider`` on ``BeforeToolCallEvent``. Any tool in
    ``PROTECTED_TOOLS`` is cancelled unless the call carries a token that
    verifies, has not expired, and matches the case and pack it claims. The
    agent cannot route around it by rewording the request, because the check
    never looks at the request - only at the signature.

The guard fails closed. A missing secret, an unparseable token, an unknown case:
all cancel. The one thing it will not do is let a call through because it could
not tell.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from .workflow_db import connect, ensure_workflow_schema, utc_now

AUTHORIZATION_VERSION = "authorization-v1.0"

#: Tools that can reach a worker. Anything added here is guarded by default;
#: the list is deny-by-omission for reads and allow-by-omission for nothing.
PROTECTED_TOOLS = frozenset({
    "publish_learning_pack",
    "distribute_learning_pack",
    "send_reminder",
    "create_action_item",
})

DEFAULT_TTL_MINUTES = 240

E_NO_TOKEN = "POLICY_NO_APPROVAL_TOKEN"
E_BAD_SIGNATURE = "POLICY_TOKEN_SIGNATURE_INVALID"
E_EXPIRED = "POLICY_TOKEN_EXPIRED"
E_CASE_MISMATCH = "POLICY_TOKEN_CASE_MISMATCH"
E_PACK_MISMATCH = "POLICY_TOKEN_PACK_MISMATCH"
E_CHANNEL_MISMATCH = "POLICY_TOKEN_CHANNEL_MISMATCH"
E_CASE_NOT_APPROVED = "POLICY_CASE_NOT_APPROVED"


class AuthorizationError(RuntimeError):
    """Raised when a token cannot be issued. Verification never raises; it denies."""


@dataclass(frozen=True)
class ApprovalToken:
    case_id: str
    pack_content_hash: str
    channel_id: str
    audience_size: int
    approver: str
    issued_at: str
    expires_at: str
    nonce: str

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def _secret(database_path: Path | str) -> bytes:
    """Signing key from the environment, or a per-install key stored once."""
    from_env = os.getenv("HINDSIGHT_APPROVAL_SECRET", "").strip()
    if from_env:
        return from_env.encode("utf-8")

    ensure_workflow_schema(database_path)
    connection = connect(database_path)
    try:
        row = connection.execute(
            "SELECT value FROM workflow_metadata WHERE key='approval_secret'"
        ).fetchone()
        if row is not None:
            return str(row["value"]).encode("utf-8")
        generated = secrets.token_urlsafe(32)
        with connection:
            connection.execute(
                "INSERT INTO workflow_metadata(key, value) VALUES('approval_secret', ?)",
                (generated,),
            )
        return generated.encode("utf-8")
    finally:
        connection.close()


def _sign(secret: bytes, payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hmac.new(secret, encoded.encode("utf-8"), sha256).digest()
    body = base64.urlsafe_b64encode(encoded.encode("utf-8")).decode("ascii").rstrip("=")
    signature = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"{body}.{signature}"


def issue_token(
    database_path: Path | str,
    *,
    case_id: str,
    pack_content_hash: str,
    channel_id: str,
    audience_size: int,
    approver: str,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
) -> str:
    """Issue a token for one pack on one channel. Only after a human approval."""
    if not approver.strip():
        raise AuthorizationError("必须记录批准人；不接受匿名批准")
    if not pack_content_hash:
        raise AuthorizationError("必须绑定 pack 内容哈希；否则批准的是话题不是稿件")

    now = datetime.now(timezone.utc)
    token = ApprovalToken(
        case_id=case_id,
        pack_content_hash=pack_content_hash,
        channel_id=channel_id,
        audience_size=audience_size,
        approver=approver,
        issued_at=now.isoformat(timespec="seconds"),
        expires_at=(now + timedelta(minutes=ttl_minutes)).isoformat(timespec="seconds"),
        nonce=secrets.token_hex(8),
    )
    return _sign(_secret(database_path), token.payload())


def verify_token(
    database_path: Path | str,
    token: str,
    *,
    case_id: str | None = None,
    pack_content_hash: str | None = None,
    channel_id: str | None = None,
) -> dict[str, Any]:
    """Verify without raising. Returns ``{"ok": bool, "error": code, ...}``."""
    if not token:
        return {"ok": False, "error": E_NO_TOKEN, "detail": "调用未携带审批 token"}

    try:
        body, signature = token.split(".", 1)
        padded = body + "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except Exception:
        return {"ok": False, "error": E_BAD_SIGNATURE, "detail": "token 无法解析"}

    expected = _sign(_secret(database_path), payload)
    if not hmac.compare_digest(expected, token):
        return {"ok": False, "error": E_BAD_SIGNATURE, "detail": "签名不匹配"}

    if payload.get("expires_at", "") < utc_now():
        return {"ok": False, "error": E_EXPIRED,
                "detail": f"token 已于 {payload.get('expires_at')} 过期"}

    if case_id is not None and payload.get("case_id") != case_id:
        return {"ok": False, "error": E_CASE_MISMATCH,
                "detail": f"token 属于 {payload.get('case_id')}，不是 {case_id}"}
    if pack_content_hash is not None and payload.get("pack_content_hash") != pack_content_hash:
        return {"ok": False, "error": E_PACK_MISMATCH,
                "detail": "稿件内容已改变，原批准失效；需重新审批"}
    if channel_id is not None and payload.get("channel_id") != channel_id:
        return {"ok": False, "error": E_CHANNEL_MISMATCH,
                "detail": f"token 授权的是渠道 {payload.get('channel_id')}"}

    return {"ok": True, "token": payload}


def case_is_approved(database_path: Path | str, case_id: str) -> bool:
    connection = connect(database_path)
    try:
        row = connection.execute(
            "SELECT state FROM cases WHERE case_id=?", (case_id,)
        ).fetchone()
    finally:
        connection.close()
    return row is not None and row["state"] in {"APPROVED", "DISTRIBUTED", "TRACKING"}


@dataclass
class PolicyDenial:
    """A typed refusal. The agent gets a reason code, never a way around it."""

    tool_name: str
    error: str
    detail: str
    policy_version: str = AUTHORIZATION_VERSION

    def as_message(self) -> str:
        return (
            f"[{self.error}] {self.detail}　"
            f"（policy {self.policy_version}；请先取得人工批准并携带对应 token，"
            f"重述请求不会改变结果）"
        )


class DistributionGuard:
    """Strands hook provider that cancels unapproved distribution calls.

    Registered as ``Agent(hooks=[DistributionGuard(db)])``. Because the check
    lives outside the model's context, the model cannot be prompted past it.
    """

    def __init__(self, database_path: Path | str, *, protected: frozenset[str] | None = None) -> None:
        self.database_path = Path(database_path)
        self.protected = protected or PROTECTED_TOOLS
        self.denials: list[PolicyDenial] = []
        self.allowed: list[str] = []

    def register_hooks(self, registry: Any, **_: Any) -> None:
        from strands.hooks import BeforeToolCallEvent

        registry.add_callback(BeforeToolCallEvent, self._before_tool_call)

    # -- the check -----------------------------------------------------------
    def evaluate(self, tool_name: str, tool_input: dict[str, Any]) -> PolicyDenial | None:
        if tool_name not in self.protected:
            return None

        case_id = tool_input.get("case_id")
        token = tool_input.get("approval_token", "")
        pack_hash = tool_input.get("pack_content_hash")
        channel_id = tool_input.get("channel_id")

        if not case_id:
            return PolicyDenial(tool_name, E_CASE_MISMATCH, "调用未指明 case_id")
        if not case_is_approved(self.database_path, str(case_id)):
            return PolicyDenial(
                tool_name, E_CASE_NOT_APPROVED,
                f"案例 {case_id} 尚未经安全经理批准，不允许触达工人",
            )

        result = verify_token(
            self.database_path, str(token), case_id=str(case_id),
            pack_content_hash=pack_hash, channel_id=channel_id,
        )
        if not result["ok"]:
            return PolicyDenial(tool_name, result["error"], result["detail"])
        return None

    def _before_tool_call(self, event: Any) -> None:
        tool_use = getattr(event, "tool_use", None) or {}
        tool_name = tool_use.get("name", "")
        tool_input = tool_use.get("input", {}) or {}
        denial = self.evaluate(tool_name, tool_input)
        if denial is None:
            if tool_name in self.protected:
                self.allowed.append(tool_name)
            return
        self.denials.append(denial)
        # cancel_tool accepts a string; the model sees the reason, not a crash.
        event.cancel_tool = denial.as_message()
