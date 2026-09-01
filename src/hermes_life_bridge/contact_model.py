from __future__ import annotations
from dataclasses import asdict, dataclass
from typing import Any
import hashlib, json

def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

@dataclass(frozen=True)
class ContactIntentEnvelope:
    intent_id: str
    idempotency_key: str
    life_did: str
    basis_state_sequence: int
    basis_state_hash: str
    source_event_id: str
    cognitive_receipt_id: str
    target: str
    message_text: str
    message_hash: str
    utility: float
    urgency: float
    evidence_refs: list[str]
    created_at: str
    expires_at: str
    schema_version: str = "v0.3"

    def to_dict(self) -> dict[str, Any]: return asdict(self)

@dataclass(frozen=True)
class ContactDecisionEnvelope:
    decision_id: str
    intent_id: str
    outcome: str
    contact_utility: float
    interruption_cost: float
    reason_codes: list[str]
    decided_at: str
    schema_version: str = "v0.3"

    def to_dict(self) -> dict[str, Any]: return asdict(self)

@dataclass(frozen=True)
class DeliveryReceipt:
    receipt_id: str
    intent_id: str
    idempotency_key: str
    life_did: str
    target: str
    status: str
    message_hash: str
    provider_message_id: str
    delivered_at: str
    duplicate: bool = False
    error: str = ""
    schema_version: str = "v0.3"

    def to_dict(self) -> dict[str, Any]: return asdict(self)
