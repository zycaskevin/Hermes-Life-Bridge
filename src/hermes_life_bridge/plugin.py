from __future__ import annotations

from collections import OrderedDict
import json
import threading
import time

from .affect_context import AffectContextProjector
from .bridge import HermesLifeBridge
from .compatibility import CompatibilityEvidenceStore
from .codex_decision import (
    CODEX_DECISION_TOOL_NAME,
    CODEX_DECISION_TOOL_SCHEMA,
    CODEX_DECISION_TOOLSET,
    CodexDecisionError,
    CodexDecisionRouter,
)
from .config import BridgeConfig
from .development_context import (
    DevelopmentContextProjector,
    safe_development_context,
)
from .interest_producer import HermesInterestProducer, create_interest_producer
from .routing import HermesRoute, is_delivery_route, normalize_session_source
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
_INTEREST_PRODUCER: HermesInterestProducer | None = None
_DEVELOPMENT_CONTEXT_PROJECTOR: DevelopmentContextProjector | None = None
_AFFECT_CONTEXT_PROJECTOR: AffectContextProjector | None = None
_CODEX_DECISION_ROUTER: CodexDecisionRouter | None = None
_SESSION_ROUTE_TARGETS: OrderedDict[str, tuple[str, float]] = OrderedDict()
_SESSION_ROUTE_LOCK = threading.RLock()
_MAX_SESSION_ROUTES = 256
_SESSION_ROUTE_TTL_SECONDS = 300.0


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


def _codex_decision_enabled() -> bool:
    try:
        return BridgeConfig.from_env().codex_decision_enabled
    except Exception:
        return False


def _codex_decision_router() -> CodexDecisionRouter | None:
    global _CODEX_DECISION_ROUTER
    try:
        config = BridgeConfig.from_env()
        if not config.codex_decision_enabled:
            return None
        if _CODEX_DECISION_ROUTER is None:
            _CODEX_DECISION_ROUTER = CodexDecisionRouter(config)
        return _CODEX_DECISION_ROUTER
    except Exception:
        return None

def _remember_session_route(session_ref: str, event) -> None:
    if not session_ref:
        return
    source = getattr(event, "source", None)
    route = normalize_session_source(source)
    if not is_delivery_route(route):
        platform = route.platform
        if not platform and isinstance(source, str):
            platform = source.strip().lower()
        route = HermesRoute(
            platform=platform,
            chat_id=str(getattr(event, "chat_id", "") or "").strip(),
            thread_id=str(getattr(event, "thread_id", "") or "").strip(),
            message_id=str(getattr(event, "message_id", "") or "").strip(),
        )
    if not is_delivery_route(route):
        return
    with _SESSION_ROUTE_LOCK:
        _SESSION_ROUTE_TARGETS.pop(session_ref, None)
        _SESSION_ROUTE_TARGETS[session_ref] = (route.target, time.monotonic())
        while len(_SESSION_ROUTE_TARGETS) > _MAX_SESSION_ROUTES:
            _SESSION_ROUTE_TARGETS.popitem(last=False)

def _session_route_target(session_ref: str) -> str:
    if not session_ref:
        return ""
    with _SESSION_ROUTE_LOCK:
        stored = _SESSION_ROUTE_TARGETS.get(session_ref)
        if stored is None:
            return ""
        target, observed_mono = stored
        if time.monotonic() - observed_mono > _SESSION_ROUTE_TTL_SECONDS:
            _SESSION_ROUTE_TARGETS.pop(session_ref, None)
            return ""
        _SESSION_ROUTE_TARGETS.move_to_end(session_ref)
        return target

def resolve_codex_approval_handler(args: dict, **kwargs) -> str:
    if not isinstance(args, dict) or set(args) != {"decision"}:
        return json.dumps({"error": "invalid_codex_decision"}, sort_keys=True, separators=(",", ":"))
    decision = args.get("decision")
    if decision not in {"accept", "acceptForSession", "decline", "cancel"}:
        return json.dumps({"error": "invalid_codex_decision"}, sort_keys=True, separators=(",", ":"))
    session_ref = str(kwargs.get("session_id") or "")
    target = _session_route_target(session_ref)
    if not target:
        return json.dumps({"error": "no_current_delivery_route"}, sort_keys=True, separators=(",", ":"))
    router = _codex_decision_router()
    if router is None:
        return json.dumps({"error": "codex_decision_routing_disabled"}, sort_keys=True, separators=(",", ":"))
    try:
        result = router.resolve(target=target, decision=decision)
    except CodexDecisionError as exc:
        return json.dumps({"error": str(exc)[:128]}, sort_keys=True, separators=(",", ":"))
    return json.dumps(result, sort_keys=True, separators=(",", ":"))


def _interest_producer() -> HermesInterestProducer | None:
    global _INTEREST_PRODUCER
    try:
        config = BridgeConfig.from_env()
        if not config.ambient_interest_enabled:
            return None
        if _INTEREST_PRODUCER is None:
            _INTEREST_PRODUCER = create_interest_producer(config)
        return _INTEREST_PRODUCER
    except Exception:
        # Interest refresh is advisory and must never break a user turn.
        return None


def _development_context() -> str:
    global _DEVELOPMENT_CONTEXT_PROJECTOR
    try:
        config = BridgeConfig.from_env()
        if not config.development_context_enabled:
            return ""
        if not config.development_projection_file:
            return safe_development_context()
        if _DEVELOPMENT_CONTEXT_PROJECTOR is None:
            _DEVELOPMENT_CONTEXT_PROJECTOR = DevelopmentContextProjector(
                life_did=config.life_did,
                projection_file=config.development_projection_file,
            )
        return _DEVELOPMENT_CONTEXT_PROJECTOR.context()
    except Exception:
        # If the expression gate is configured but unreadable, fail safe toward
        # developmental humility rather than silently reverting to mature-agent behavior.
        return safe_development_context()


def _affect_context() -> str:
    global _AFFECT_CONTEXT_PROJECTOR
    try:
        config = BridgeConfig.from_env()
        if not config.affect_context_enabled or not config.affect_state_file:
            return ""
        if _AFFECT_CONTEXT_PROJECTOR is None:
            _AFFECT_CONTEXT_PROJECTOR = AffectContextProjector(
                life_did=config.life_did,
                state_file=config.affect_state_file,
            )
        return _AFFECT_CONTEXT_PROJECTOR.context()
    except Exception:
        # Affect is advisory. Never fabricate a transient state when the file is invalid.
        return ""


def _turn_context() -> str:
    parts = [value for value in (_development_context(), _affect_context()) if value]
    return "\n\n".join(parts)


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
        _remember_session_route(session_ref, event)
        producer = _work_producer()
        if producer is not None:
            producer.note_context_activity(session_ref)
        interest = _interest_producer()
        if interest is not None:
            interest.observe_owner_discussion(
                str(getattr(event, "text", "") or ""),
                event_ref=str(getattr(event, "message_id", "") or session_ref),
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
    turn_context = _turn_context()
    # Gateway messages are already authoritatively observed by pre_gateway_dispatch.
    # For gateway turns this hook is context-only, avoiding double ingestion while
    # still applying the DLD Developmental Expression Gate to every LLM call.
    if platform not in ("", "cli", None):
        return {"context": turn_context} if turn_context else None
    try:
        producer = _work_producer()
        if producer is not None:
            producer.note_context_activity(session_id or "")
        interest = _interest_producer()
        if interest is not None:
            interest.observe_owner_discussion(
                user_message or "",
                event_ref=turn_id or session_id or "cli",
            )
        _bridge().cli_turn(
            session_id=session_id or "",
            turn_id=turn_id or str(kwargs.get("turn_id") or ""),
            user_message=user_message or "",
        )
    except Exception:
        pass
    return {"context": turn_context} if turn_context else None


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
        register_tool(
            name=CODEX_DECISION_TOOL_NAME,
            toolset=CODEX_DECISION_TOOLSET,
            schema=CODEX_DECISION_TOOL_SCHEMA,
            handler=resolve_codex_approval_handler,
            check_fn=_codex_decision_enabled,
            description=CODEX_DECISION_TOOL_SCHEMA["function"]["description"],
        )
    try:
        _evidence_store().record_registration(plugin_api_version="register_hook")
    except Exception:
        # Compatibility evidence must never prevent Hermes from loading HLB.
        pass
