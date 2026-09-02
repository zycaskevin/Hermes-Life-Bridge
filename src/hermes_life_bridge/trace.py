from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
import json
from typing import Any
from .representation import canonicalize_operational_value

STAGES = (
    "HOOK_RECEIVED",
    "NORMALIZED",
    "DEDUPE_CHECK",
    "SOCKET_CONNECT",
    "EVENT_SENT",
    "RUNTIME_ACK",
    "STATE_ADVANCED",
    "STATE_NOT_ADVANCED",
    "FAILED",
    "CONTACT_REQUEST_RECEIVED",
    "CONTACT_DEDUPE_HIT",
    "CONTACT_DRY_RUN",
    "HERMES_SEND_START",
    "HERMES_SEND_SUCCESS",
    "DELIVERY_RECEIPT_SENT",
    "CONTACT_FAILED",
    "CONTACT_FAILED_SAFE",
    "CONTACT_DELIVERY_UNKNOWN",
    "CONTACT_RECONCILIATION_FAILED",
    "COGNITION_TASK_RECEIVED",
    "COGNITION_DEDUPE_HIT",
    "HERMES_API_CONNECT",
    "HERMES_API_RESPONSE",
    "COGNITIVE_RECEIPT_SENT",
    "COGNITION_FAILED",
)

class BridgeTracer:
    def __init__(self, path: str):
        self.path = Path(path)

    def emit(self, *, trace_id: str, stage: str, status: str = "pass", **fields: Any) -> None:
        if stage not in STAGES:
            raise ValueError(f"invalid trace stage: {stage}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "trace_id": trace_id,
            "stage": stage,
            "status": status,
            **fields,
        }
        # Privacy guard: never accept raw message text.
        forbidden = {
            "text", "message", "user_message", "raw_message", "content",
            "target", "canonical_target", "reply_target", "chat_id", "thread_id",
        }
        for key in tuple(record):
            if key.lower() in forbidden:
                record.pop(key, None)
        record = canonicalize_operational_value(record)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")

    def tail(self, n: int = 20) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        out = []
        for line in lines[-n:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out
