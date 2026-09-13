from __future__ import annotations

from .bridge import HermesLifeBridge
from .compatibility import CompatibilityEvidenceStore
from .config import BridgeConfig
from .work_producer import (
    REPORT_TOOL_NAME,
    REPORT_TOOL_SCHEMA,
    REPORT_TOOLSET,
    HermesWorkProducer,
    create_work_producer,
    report_work_event_handler,
)

_BRIDGE: HermesLifeBridge | None = None
_WORK_PRODUCER: HermesWorkProducer | None = None


def _bridge() -> HermesLifeBridge:
    global _BRIDGE
    if _BRIDGE is None:
        _BRIDGE = HermesLifeBridge()
    return _BRIDGE


def _evidence_store() -> CompatibilityEvidenceStore:
    config = BridgeConfig.from_env()
    return CompatibilityEvidenceStore(config.compatibility_evidence_path)


def _work_producer_enabled() -> bool:
    try:
        return BridgeConfig.from_env().work_producer_enabled
    except Exception:
        return False


def _work_producer() -> HermesWorkProducer | None:
    global _WORK_PRODUCER
    try:
        config = BridgeConfig.from_env()
        if not config.work_producer_enabled:
            return None
        if _WORK_PRODUCER is None:
            _WORK_PRODUCER = create_work_producer(config)
        return _WORK_PRODUCER
    except Exception:
        # Work-shadow observation must never break Hermes user turns.
        return None


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
        producer = _work_producer()
        if producer is not None:
            producer.note_context_activity(session_ref)
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
        producer = _work_producer()
        if producer is not None:
            producer.note_context_activity(session_id or "")
        _bridge().cli_turn(
            session_id=session_id or "",
            turn_id=turn_id or str(kwargs.get("turn_id") or ""),
            user_message=user_message or "",
        )
    except Exception:
        pass
    return None


def on_post_tool_call(
    tool_name: str,
    args,
    result=None,
    session_id: str = "",
    turn_id: str = "",
    tool_call_id: str = "",
    status: str | None = None,
    **kwargs,
):
    # Never inspect or persist raw args/results for ordinary tools. The producer
    # receives only terminal metadata; declaration args are parsed only when the
    # dedicated report tool itself completes successfully.
    try:
        producer = _work_producer()
        if producer is not None:
            producer.observe_post_tool_call(
                tool_name=tool_name or "",
                args=args if tool_name == REPORT_TOOL_NAME else None,
                status=status,
                session_id=session_id or "",
                turn_id=turn_id or "",
                tool_call_id=tool_call_id or "",
            )
    except Exception:
        pass
    return None


def register(ctx):
    ctx.register_hook("pre_gateway_dispatch", on_pre_gateway_dispatch)
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    # Real modern PluginContext has register_tool(). Keep older/minimal hook-only
    # contexts loadable rather than failing the entire bridge during compatibility
    # probes. On modern Hermes the observer/tool contract is stable; while the
    # producer is disabled the tool is hidden by check_fn and the hook is a no-op.
    register_tool = getattr(ctx, "register_tool", None)
    if callable(register_tool):
        ctx.register_hook("post_tool_call", on_post_tool_call)
        register_tool(
            name=REPORT_TOOL_NAME,
            toolset=REPORT_TOOLSET,
            schema=REPORT_TOOL_SCHEMA,
            handler=report_work_event_handler,
            check_fn=_work_producer_enabled,
            description=REPORT_TOOL_SCHEMA["function"]["description"],
        )
    try:
        _evidence_store().record_registration(plugin_api_version="register_hook")
    except Exception:
        # Compatibility evidence must never prevent Hermes from loading HLB.
        pass
