from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Any

@dataclass(frozen=True)
class BridgePercept:
    bridge_event_id: str
    idempotency_key: str
    life_did: str
    surface: str
    platform: str
    source_message_id: str
    session_ref: str
    turn_ref: str
    payload_ref: str
    content_fingerprint: str
    observed_at: str
    schema_version: str = "v0.1"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

@dataclass(frozen=True)
class RuntimeReceipt:
    ok: bool
    duplicate: bool | None = None
    persisted: bool | None = None
    state_sequence: int | None = None
    state_hash: str | None = None
    decision_outcome: str | None = None
    error: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuntimeReceipt":
        decision = data.get("decision") or {}
        return cls(
            ok=bool(data.get("ok", False)),
            duplicate=data.get("duplicate"),
            persisted=data.get("persisted"),
            state_sequence=data.get("state_sequence"),
            state_hash=data.get("state_hash"),
            decision_outcome=decision.get("outcome"),
            error=data.get("error"),
        )
