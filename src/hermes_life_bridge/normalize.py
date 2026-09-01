from __future__ import annotations
from datetime import datetime, timezone
from .config import BridgeConfig
from .correlation import (
    cli_idempotency,
    fingerprint,
    gateway_idempotency,
    stable_id,
)
from .model import BridgePercept
from .routing import normalize_session_source

def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def normalize_gateway(event, config: BridgeConfig, *, session_ref: str = "") -> BridgePercept:
    text = getattr(event, "text", "") or ""
    route = normalize_session_source(getattr(event, "source", None))
    event_message_id = str(getattr(event, "message_id", "") or "")
    message_id = event_message_id or route.message_id
    platform = route.platform or "gateway"
    fp = fingerprint(text)
    idem = gateway_idempotency(platform, message_id, session_ref, fp)
    bridge_event_id = stable_id("gateway-percept", idem)
    payload_ref = f"hermes://gateway/{platform}/message/{message_id or bridge_event_id}"
    return BridgePercept(
        bridge_event_id=bridge_event_id,
        idempotency_key=idem,
        life_did=config.life_did,
        surface="gateway",
        platform=platform,
        source_message_id=message_id,
        session_ref=session_ref,
        turn_ref="",
        payload_ref=payload_ref,
        content_fingerprint=f"sha256:{fp}",
        observed_at=_now(),
    )


def normalize_cli(*, session_id: str, turn_id: str, user_message: str, config: BridgeConfig) -> BridgePercept:
    fp = fingerprint(user_message)
    idem = cli_idempotency(session_id, turn_id, fp)
    bridge_event_id = stable_id("cli-percept", idem)
    return BridgePercept(
        bridge_event_id=bridge_event_id,
        idempotency_key=idem,
        life_did=config.life_did,
        surface="cli",
        platform="cli",
        source_message_id="",
        session_ref=session_id,
        turn_ref=turn_id,
        payload_ref=f"hermes://cli/{session_id}/turn/{turn_id or bridge_event_id}",
        content_fingerprint=f"sha256:{fp}",
        observed_at=_now(),
    )

def to_runtime_percept(percept: BridgePercept) -> dict:
    # HLB-001 intentionally avoids semantic interpretation.
    # transport_priority_hint is mapped to legacy Life Runtime salience_hint only for v0.1 compatibility.
    return {
        "event_id": percept.bridge_event_id,
        "life_did": percept.life_did,
        "source_body_id": f"hermes-{percept.surface}:{percept.platform}",
        "modality": "system",
        "observed_at": percept.observed_at,
        "payload_ref": percept.payload_ref,
        "salience_hint": 0.5,
        "idempotency_key": percept.idempotency_key,
        "schema_version": "v0.1",
    }
