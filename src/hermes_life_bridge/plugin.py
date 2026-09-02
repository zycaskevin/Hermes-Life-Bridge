from __future__ import annotations

from .bridge import HermesLifeBridge
from .compatibility import CompatibilityEvidenceStore
from .config import BridgeConfig

_BRIDGE: HermesLifeBridge | None = None


def _bridge() -> HermesLifeBridge:
    global _BRIDGE
    if _BRIDGE is None:
        _BRIDGE = HermesLifeBridge()
    return _BRIDGE


def _evidence_store() -> CompatibilityEvidenceStore:
    config = BridgeConfig.from_env()
    return CompatibilityEvidenceStore(config.compatibility_evidence_path)


def on_pre_gateway_dispatch(event, gateway=None, session_store=None, **kwargs):
    # Observer only. Never break or rewrite Hermes' normal user-message flow.
    try:
        _evidence_store().record_gateway_event(getattr(event, "source", None))
    except Exception:
        pass
    try:
        session_ref = str(
            kwargs.get("session_id")
            or getattr(event, "chat_id", "")
            or getattr(event, "sender_id", "")
            or ""
        )
        _bridge().gateway_message(event, session_ref=session_ref)
    except Exception:
        pass
    return {"action": "allow"}


def on_pre_llm_call(
    session_id: str,
    user_message: str,
    platform: str = "cli",
    turn_id: str = "",
    **kwargs,
):
    # Gateway messages are already authoritatively observed by pre_gateway_dispatch.
    # pre_llm_call is retained for CLI only to avoid double ingestion.
    if platform not in ("", "cli", None):
        return None
    try:
        _bridge().cli_turn(
            session_id=session_id or "",
            turn_id=turn_id or str(kwargs.get("turn_id") or ""),
            user_message=user_message or "",
        )
    except Exception:
        pass
    return None


def register(ctx):
    ctx.register_hook("pre_gateway_dispatch", on_pre_gateway_dispatch)
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    try:
        _evidence_store().record_registration(plugin_api_version="register_hook")
    except Exception:
        # Compatibility evidence must never prevent Hermes from loading HLB.
        pass
