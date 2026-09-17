from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import stat
from typing import Any, Mapping

from .config import BridgeConfig
from .contact_store import ContactStore


CODEX_DECISION_TOOL_NAME = "hlb_resolve_codex_approval"
CODEX_DECISION_TOOLSET = "safe"
CODEX_DECISION_SCHEMA_VERSION = "clb-owner-decision.v0.1"
CODEX_DECISION_RESPONSE_SCHEMA_VERSION = "clb-owner-decision-response.v0.1"
MAX_RESPONSE_BYTES = 16 * 1024
_DECISIONS = ("accept", "acceptForSession", "decline", "cancel")


CODEX_DECISION_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": CODEX_DECISION_TOOL_NAME,
        "description": (
            "Resolve the Codex approval request tied to the most recent delivered work-contact "
            "in this exact current Hermes session. Use only when the owner clearly answers that "
            "Codex approval request. Never infer consent from silence, unrelated agreement, or a "
            "different conversation. Use acceptForSession only when the owner explicitly grants "
            "session-wide approval. The tool does not accept event IDs, request IDs, commands, "
            "paths, diffs, or raw execution content."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "decision": {
                    "type": "string",
                    "enum": list(_DECISIONS),
                }
            },
            "required": ["decision"],
        },
    },
}


class CodexDecisionError(RuntimeError):
    pass


class CodexDecisionRouter:
    def __init__(
        self,
        config: BridgeConfig | None = None,
        *,
        contact_store: ContactStore | None = None,
    ):
        self.config = config or BridgeConfig.from_env()
        self.store = contact_store or ContactStore(self.config.contact_db)
        self._owns_store = contact_store is None

    def resolve(self, *, target: str, decision: str) -> dict[str, Any]:
        if decision not in _DECISIONS:
            raise CodexDecisionError("unsupported_codex_decision")
        if not target:
            raise CodexDecisionError("current_delivery_route_required")
        work_event_id = self.store.latest_delivered_work_event_for_target(target)
        if work_event_id is None:
            raise CodexDecisionError("no_delivered_work_contact_for_route")

        response = _send_decision(
            self.config.codex_decision_socket,
            work_event_id=work_event_id,
            decision=decision,
            timeout_seconds=self.config.codex_decision_timeout_seconds,
        )
        status = response.get("status")
        if status != "accepted":
            error = str(response.get("error") or "codex_decision_rejected")[:128]
            if error == "work_event_not_found":
                raise CodexDecisionError("no_live_codex_approval_for_latest_work_contact")
            raise CodexDecisionError(error)
        return {
            "ok": True,
            "status": "accepted",
            "decision": decision,
        }

    def close(self) -> None:
        if self._owns_store:
            try:
                self.store.conn.close()
            except Exception:
                pass


def _send_decision(
    socket_path: str,
    *,
    work_event_id: str,
    decision: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    path = Path(socket_path).expanduser().resolve(strict=True)
    metadata = path.lstat()
    if not stat.S_ISSOCK(metadata.st_mode):
        raise CodexDecisionError("codex_decision_path_must_be_socket")
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise CodexDecisionError("codex_decision_socket_wrong_owner")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise CodexDecisionError("codex_decision_socket_must_be_owner_only")

    body = json.dumps(
        {
            "schema_version": CODEX_DECISION_SCHEMA_VERSION,
            "work_event_id": work_event_id,
            "decision": decision,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout_seconds)
            client.connect(str(path))
            client.sendall(body)
            raw = b""
            while b"\n" not in raw:
                chunk = client.recv(4096)
                if not chunk:
                    break
                raw += chunk
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise CodexDecisionError("codex_decision_response_too_large")
    except (OSError, TimeoutError) as exc:
        raise CodexDecisionError("codex_decision_transport_failure") from exc

    line = raw.split(b"\n", 1)[0]
    try:
        parsed = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CodexDecisionError("codex_decision_response_invalid_json") from exc
    if not isinstance(parsed, Mapping):
        raise CodexDecisionError("codex_decision_response_invalid_shape")
    if parsed.get("schema_version") != CODEX_DECISION_RESPONSE_SCHEMA_VERSION:
        raise CodexDecisionError("codex_decision_response_schema_unsupported")
    return dict(parsed)
