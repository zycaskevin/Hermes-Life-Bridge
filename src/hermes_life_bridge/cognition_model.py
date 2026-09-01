from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Any
import hashlib, json


def canonical_json(value: Any) -> str:
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CognitiveTaskEnvelope:
    task_id: str
    idempotency_key: str
    life_did: str
    event_id: str
    basis_state_sequence: int
    basis_state_hash: str
    purpose: str
    instruction: str
    projection_ref: str
    projection_hash: str
    risk_level: str
    created_at: str
    expires_at: str
    session_policy: str = "task_isolated"
    schema_version: str = "v0.2"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def request_hash(self) -> str:
        return sha256_text(canonical_json(self))


@dataclass(frozen=True)
class CognitiveReceipt:
    receipt_id: str
    task_id: str
    idempotency_key: str
    life_did: str
    status: str
    basis_state_sequence: int
    basis_state_hash: str
    projection_hash: str
    output_text: str
    output_hash: str
    hermes_session_id: str
    request_hash: str
    started_at: str
    completed_at: str
    duplicate: bool = False
    error: str = ""
    schema_version: str = "v0.2"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
