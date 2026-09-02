from __future__ import annotations

import time

from .config import BridgeConfig
from .correlation import stable_id
from .normalize import normalize_cli, normalize_gateway, to_runtime_percept
from .percept_delivery import PerceptReliabilityExecutor
from .routing import RouteStore, normalize_session_source
from .trace import BridgeTracer
from .transport import UnixSocketTransport


class HermesLifeBridge:
    def __init__(self, config: BridgeConfig | None = None, *, transport=None):
        self.config = config or BridgeConfig.from_env()
        self.trace = BridgeTracer(self.config.trace_path)
        self.transport = transport or UnixSocketTransport(self.config)
        self.percepts = PerceptReliabilityExecutor(
            self.config,
            transport=self.transport,
        )
        self.routes = RouteStore(self.config.route_path)

    def _deliver(self, percept, *, hook: str):
        trace_id = stable_id("trace", percept.bridge_event_id)
        self.trace.emit(
            trace_id=trace_id,
            stage="NORMALIZED",
            hook=hook,
            surface=percept.surface,
            platform=percept.platform,
            message_id=percept.source_message_id,
            idempotency_key=percept.idempotency_key,
            content_fingerprint=percept.content_fingerprint,
        )
        self.trace.emit(
            trace_id=trace_id,
            stage="DEDUPE_CHECK",
            hook=hook,
            idempotency_key=percept.idempotency_key,
        )
        runtime_event = to_runtime_percept(percept)
        before = time.monotonic()
        try:
            self.trace.emit(
                trace_id=trace_id,
                stage="SOCKET_CONNECT",
                hook=hook,
                socket=self.config.runtime_socket,
            )
            receipt = self.percepts.submit(runtime_event)
            elapsed = round((time.monotonic() - before) * 1000, 3)
            self.trace.emit(
                trace_id=trace_id,
                stage="EVENT_SENT",
                hook=hook,
                latency_ms=elapsed,
            )
            if not receipt.ok:
                self.trace.emit(
                    trace_id=trace_id,
                    stage="FAILED",
                    status="fail",
                    hook=hook,
                    error=receipt.error or "runtime_rejected",
                )
                return receipt
            self.trace.emit(
                trace_id=trace_id,
                stage="RUNTIME_ACK",
                hook=hook,
                duplicate=receipt.duplicate,
                persisted=receipt.persisted,
                state_sequence=receipt.state_sequence,
                state_hash=receipt.state_hash,
                decision_outcome=receipt.decision_outcome,
            )
            if receipt.persisted and not receipt.duplicate and receipt.state_sequence is not None:
                self.trace.emit(
                    trace_id=trace_id,
                    stage="STATE_ADVANCED",
                    hook=hook,
                    state_sequence=receipt.state_sequence,
                    duplicate=False,
                )
            else:
                self.trace.emit(
                    trace_id=trace_id,
                    stage="STATE_NOT_ADVANCED",
                    status="fail" if receipt.decision_outcome == "safe_stop" else "pass",
                    hook=hook,
                    state_sequence=receipt.state_sequence,
                    duplicate=receipt.duplicate,
                    persisted=receipt.persisted,
                    decision_outcome=receipt.decision_outcome,
                )
            return receipt
        except Exception as exc:
            self.trace.emit(
                trace_id=trace_id,
                stage="FAILED",
                status="fail",
                hook=hook,
                error=type(exc).__name__,
            )
            return None

    def gateway_message(self, event, *, session_ref: str = ""):
        route = normalize_session_source(getattr(event, "source", None))
        message_id = str(getattr(event, "message_id", "") or route.message_id or "")
        platform = route.platform or "gateway"
        trace_id = stable_id("hook", "pre_gateway_dispatch", platform, message_id)
        self.trace.emit(
            trace_id=trace_id,
            stage="HOOK_RECEIVED",
            hook="pre_gateway_dispatch",
            platform=platform,
            message_id=message_id,
            route_ready=bool(route.target),
        )
        if route.target:
            self.routes.save(route)
        percept = normalize_gateway(event, self.config, session_ref=session_ref)
        return self._deliver(percept, hook="pre_gateway_dispatch")

    def cli_turn(self, *, session_id: str, turn_id: str, user_message: str):
        trace_id = stable_id("hook", "pre_llm_call", session_id, turn_id)
        self.trace.emit(
            trace_id=trace_id,
            stage="HOOK_RECEIVED",
            hook="pre_llm_call",
            platform="cli",
            session_id=session_id,
            turn_id=turn_id,
        )
        percept = normalize_cli(
            session_id=session_id,
            turn_id=turn_id,
            user_message=user_message,
            config=self.config,
        )
        return self._deliver(percept, hook="pre_llm_call")

    def recover_percepts(self) -> dict[str, int]:
        return self.percepts.pump()
