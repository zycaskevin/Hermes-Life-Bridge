from __future__ import annotations
import hashlib
import uuid

NS = uuid.UUID("bd6e6b71-c60b-4984-a07d-4d4fe2a99e5e")

def stable_id(kind: str, *parts: object) -> str:
    return str(uuid.uuid5(NS, "|".join([kind, *[str(p) for p in parts]])))

def fingerprint(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()

def gateway_idempotency(platform: str, message_id: str, session_ref: str, content_fp: str) -> str:
    if message_id:
        return f"hermes:gateway:{platform}:{message_id}"
    return f"hermes:gateway:{platform}:{stable_id('fallback', session_ref, content_fp)}"

def cli_idempotency(session_id: str, turn_id: str, content_fp: str) -> str:
    if turn_id:
        return f"hermes:cli:{session_id}:{turn_id}"
    return f"hermes:cli:{session_id}:{stable_id('fallback-cli', content_fp)}"
