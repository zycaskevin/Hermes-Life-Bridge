from __future__ import annotations
from dataclasses import dataclass, replace
import uuid
from .bridge import HermesLifeBridge
from .config import BridgeConfig

@dataclass
class _FakeEvent:
    text: str
    source: str
    message_id: str

def run_selftest(config: BridgeConfig | None = None) -> dict:
    base_config = config or BridgeConfig.from_env()
    # Synthetic self-test traffic must never overwrite a real user delivery route.
    bridge = HermesLifeBridge(replace(base_config, route_path=""))
    event = _FakeEvent(
        text="HLB self-test payload; raw content must not persist",
        source="hlb-selftest",
        message_id=f"hlb-selftest-{uuid.uuid4()}",
    )
    receipt = bridge.gateway_message(event, session_ref="hlb-selftest-session")
    if receipt is None:
        return {"ok": False, "error": "bridge_delivery_exception"}
    healthy = bool(receipt.ok and receipt.persisted and receipt.decision_outcome != "safe_stop")
    return {
        "ok": healthy,
        "transport_ok": receipt.ok,
        "duplicate": receipt.duplicate,
        "persisted": receipt.persisted,
        "state_sequence": receipt.state_sequence,
        "state_hash": receipt.state_hash,
        "decision_outcome": receipt.decision_outcome,
        "error": receipt.error,
    }
