"""Trusted host composition for AF's durable native-effect budget.

This module does not issue capability authority.  Its caller supplies the existing
AF/Self-Gateway authority verifier and already-verified Runtime isolation/tool
pricing evidence.  The real AF ledger and Unix broker live on the host; Hermes
receives only an invocation reference, private socket path, and conservative
quote policy metadata.
"""
from __future__ import annotations

import copy
import json
import os
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .hermes_native_events import digest
from .host_budget_admission import REQUIRED_COVERAGE, VerifiedHostBudget
from .runtime_adapter import RuntimeBridgeError

_SCHEMA = "af.native-effect-budget-context.v1"
_SHA_PREFIX = "sha256:"
_ALLOWED_TOOLS = frozenset({"web_search", "web_extract"})


def _tool_route_pins(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != _ALLOWED_TOOLS:
        raise RuntimeBridgeError("native_effect_tool_route_pins_required")
    result: dict[str, str] = {}
    for name in sorted(_ALLOWED_TOOLS):
        route = value.get(name)
        if (not isinstance(route, str) or not route or len(route) > 128
                or any(ord(ch) < 33 or ord(ch) > 126 for ch in route)):
            raise RuntimeBridgeError("native_effect_tool_route_pin_invalid")
        result[name] = route
    return result


def _sha(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value.startswith(_SHA_PREFIX) or len(value) != 71:
        raise RuntimeBridgeError(code)
    try:
        int(value[7:], 16)
    except ValueError:
        raise RuntimeBridgeError(code) from None
    return value


def _model_bounds(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict) or not value or len(value) > 16:
        raise RuntimeBridgeError("native_effect_model_bounds_required")
    result: dict[str, dict[str, Any]] = {}
    for route_key, item in value.items():
        if (not isinstance(route_key, str) or not route_key or len(route_key) > 256
                or not isinstance(item, dict)):
            raise RuntimeBridgeError("native_effect_model_bound_invalid")
        provider, model = item.get("provider"), item.get("model")
        if (not isinstance(provider, str) or not provider or len(provider) > 128
                or not isinstance(model, str) or not model or len(model) > 256):
            raise RuntimeBridgeError("native_effect_model_bound_invalid")
        try:
            input_rate = Decimal(str(item.get("maxInputUsdPerMillion")))
            output_rate = Decimal(str(item.get("maxOutputUsdPerMillion")))
            request_cost = Decimal(str(item.get("maxRequestUsd", "0")))
        except (InvalidOperation, ValueError, TypeError):
            raise RuntimeBridgeError("native_effect_model_bound_invalid") from None
        if any(not amount.is_finite() or amount < 0 or amount > Decimal("10000")
               for amount in (input_rate, output_rate, request_cost)):
            raise RuntimeBridgeError("native_effect_model_bound_invalid")
        max_output = item.get("maxOutputTokens")
        overhead = item.get("protocolOverheadTokens")
        if (type(max_output) is not int or not 1 <= max_output <= 10**7
                or type(overhead) is not int or not 0 <= overhead <= 10**7):
            raise RuntimeBridgeError("native_effect_model_bound_invalid")
        result[route_key] = {
            "provider": provider,
            "model": model,
            "maxInputUsdPerMillion": format(input_rate, "f"),
            "maxOutputUsdPerMillion": format(output_rate, "f"),
            "maxRequestUsd": format(request_cost, "f"),
            "maxOutputTokens": max_output,
            "protocolOverheadTokens": overhead,
            "quoteRef": _sha(item.get("quoteRef"), "native_effect_model_bound_invalid"),
        }
    return result


def _tool_bounds(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict) or set(value) != _ALLOWED_TOOLS:
        raise RuntimeBridgeError("native_effect_tool_bounds_required")
    result: dict[str, dict[str, Any]] = {}
    for name in sorted(_ALLOWED_TOOLS):
        item = value.get(name)
        if not isinstance(item, dict):
            raise RuntimeBridgeError("native_effect_tool_bound_invalid")
        tokens = item.get("tokens")
        if type(tokens) is not int or not 0 <= tokens <= 10**9:
            raise RuntimeBridgeError("native_effect_tool_bound_invalid")
        try:
            cost = Decimal(str(item.get("costUsd")))
        except (InvalidOperation, ValueError, TypeError):
            raise RuntimeBridgeError("native_effect_tool_bound_invalid") from None
        if not cost.is_finite() or cost < 0 or cost > Decimal("1000"):
            raise RuntimeBridgeError("native_effect_tool_bound_invalid")
        quote_ref = _sha(item.get("quoteRef"), "native_effect_tool_bound_invalid")
        result[name] = {
            "tokens": tokens,
            "costUsd": format(cost, "f"),
            "quoteRef": quote_ref,
        }
    return result


class AFNativeEffectBudgetPort:
    """ExistingHostBudgetPort backed by AF NativeEffectBudget + AF_UNIX broker."""

    def __init__(
        self,
        *,
        socket_path: str | Path,
        ledger_path: str | Path,
        authority_verifier,
        allowed_uids: frozenset[int],
        origin: str,
        runtime_isolation_ref: str,
        model_bounds: dict[str, dict[str, Any]],
        tool_bounds: dict[str, dict[str, Any]],
        tool_route_pins: dict[str, str],
        capacity: int = 128,
    ) -> None:
        if origin not in {"SYNTHETIC_TEST", "LIVE_RUNTIME_REPORTED"}:
            raise RuntimeBridgeError("native_effect_budget_origin_invalid")
        if not allowed_uids or any(type(uid) is not int or uid < 0 for uid in allowed_uids):
            raise RuntimeBridgeError("native_effect_budget_uid_policy_invalid")
        self.origin = origin
        self.runtime_isolation_ref = _sha(
            runtime_isolation_ref, "native_effect_runtime_isolation_evidence_required")
        self.model_bounds = _model_bounds(model_bounds)
        self.tool_bounds = _tool_bounds(tool_bounds)
        self.tool_route_pins = _tool_route_pins(tool_route_pins)
        try:
            from agent_factory.production.native_effect_budget_broker import (
                NativeEffectBudgetBrokerServer,
                NativeEffectBudgetExecutionContext,
                NativeEffectQuotePolicy,
            )
            broker_tool_bounds = copy.deepcopy(self.tool_bounds)
            for name, route in self.tool_route_pins.items():
                broker_tool_bounds[name]["route"] = route
            self._quote_policy = NativeEffectQuotePolicy(
                model_bounds=self.model_bounds, tool_bounds=broker_tool_bounds)
            self._server = NativeEffectBudgetBrokerServer(
                socket_path,
                ledger_path,
                authority_verifier=authority_verifier,
                allowed_uids=allowed_uids,
                capacity=capacity,
                recover_inflight=True,
            )
        except Exception:
            raise RuntimeBridgeError("native_effect_budget_broker_unavailable") from None
        self.socket_path = str(self._server.socket_path)
        self._execution_context = NativeEffectBudgetExecutionContext(
            self._server, self._quote_policy)
        self._thread: threading.Thread | None = None
        self._thread_error: BaseException | None = None
        self._bound: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def start(self, *, timeout_seconds: float = 2.0) -> None:
        if self._thread is not None:
            if self._thread.is_alive() and Path(self.socket_path).is_socket():
                return
            raise RuntimeBridgeError("native_effect_budget_broker_unavailable")

        def serve() -> None:
            try:
                self._server.serve()
            except BaseException as exc:
                self._thread_error = exc

        self._thread = threading.Thread(
            target=serve, name="af-native-effect-budget-broker", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self._thread_error is not None:
                raise RuntimeBridgeError("native_effect_budget_broker_unavailable")
            if Path(self.socket_path).is_socket():
                return
            if not self._thread.is_alive():
                break
            time.sleep(0.01)
        raise RuntimeBridgeError("native_effect_budget_broker_unavailable")

    def close(self) -> None:
        self._server.stop()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._server.close()

    def bind_execution_context(self, binding: dict, governed) -> dict:
        """Bind the exact governed invocation through AF's canonical context port."""
        if not isinstance(binding, dict) or "submissionDigest" in binding:
            raise RuntimeBridgeError("native_effect_budget_binding_invalid")
        try:
            context = self._execution_context.bind(governed)
        except Exception:
            raise RuntimeBridgeError("native_effect_budget_bind_denied") from None
        invocation_ref = _sha(
            context.get("invocationRef"), "native_effect_budget_binding_invalid")
        if (context.get("schema") != _SCHEMA
                or context.get("socketPath") != self.socket_path
                or context.get("quotePolicyRef") != self._quote_policy.policy_ref):
            raise RuntimeBridgeError("native_effect_budget_binding_invalid")
        key = binding.get("afInvocationDigest")
        _sha(key, "native_effect_budget_binding_invalid")
        stable = copy.deepcopy(binding)
        record = {
            "stableBinding": stable,
            "invocationRef": invocation_ref,
            "deadline": stable.get("deadline"),
        }
        with self._lock:
            existing = self._bound.get(key)
            if existing is not None and existing != record:
                raise RuntimeBridgeError("native_effect_budget_rebind_denied")
            self._bound[key] = record
        return copy.deepcopy(context)

    async def prepare_execution_context(self, binding: dict, governed) -> dict:
        """Compatibility wrapper; AF production calls bind_execution_context."""
        return await __import__("asyncio").to_thread(
            self.bind_execution_context, binding, governed)

    async def verify_existing_reservation(self, binding: dict) -> VerifiedHostBudget:
        if not isinstance(binding, dict):
            raise RuntimeBridgeError("native_effect_budget_binding_invalid")
        key = binding.get("afInvocationDigest")
        _sha(key, "native_effect_budget_binding_invalid")
        with self._lock:
            record = copy.deepcopy(self._bound.get(key))
        if record is None:
            raise RuntimeBridgeError("native_effect_budget_not_bound")
        stable = copy.deepcopy(binding)
        submission_digest = stable.pop("submissionDigest", None)
        _sha(submission_digest, "native_effect_budget_submission_invalid")
        if stable != record["stableBinding"]:
            raise RuntimeBridgeError("native_effect_budget_binding_drift")
        try:
            snapshot = await __import__("asyncio").to_thread(
                self._server.snapshot, record["invocationRef"])
        except Exception:
            raise RuntimeBridgeError("native_effect_budget_snapshot_unavailable") from None
        if (snapshot.get("invocationRef") != record["invocationRef"]
                or snapshot.get("quotePolicyRef") != self._quote_policy.policy_ref):
            raise RuntimeBridgeError("native_effect_budget_snapshot_mismatch")
        deadline = datetime.fromisoformat(str(binding["deadline"]).replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        if deadline.tzinfo is None or now >= deadline:
            raise RuntimeBridgeError("native_effect_budget_deadline_expired")
        verification_ref = digest({
            "snapshot": snapshot,
            "runtimeIsolationRef": self.runtime_isolation_ref,
            "modelBounds": self.model_bounds,
            "toolBounds": self.tool_bounds,
            "toolRoutePins": self.tool_route_pins,
            "submissionDigest": submission_digest,
        })
        return VerifiedHostBudget(
            binding_digest=digest(binding),
            reservation_ref=record["invocationRef"],
            verification_ref=verification_ref,
            origin=self.origin,
            issued_at=now,
            expires_at=deadline,
            coverage=REQUIRED_COVERAGE,
        )

    def retire_execution_context(self, binding: dict) -> None:
        """Close reconciled AF broker binding; retain all durable held ceilings."""
        if not isinstance(binding, dict):
            raise RuntimeBridgeError("native_effect_budget_binding_invalid")
        key = binding.get("afInvocationDigest")
        _sha(key, "native_effect_budget_binding_invalid")
        with self._lock:
            record = copy.deepcopy(self._bound.get(key))
        if record is None:
            raise RuntimeBridgeError("native_effect_budget_not_bound")
        stable = copy.deepcopy(binding)
        submission_digest = stable.pop("submissionDigest", None)
        _sha(submission_digest, "native_effect_budget_submission_invalid")
        if stable != record["stableBinding"]:
            raise RuntimeBridgeError("native_effect_budget_binding_drift")
        try:
            snapshot = self._server.retire(record["invocationRef"])
        except Exception:
            raise RuntimeBridgeError(
                "native_effect_budget_retirement_denied") from None
        if (
            snapshot.get("invocationRef") != record["invocationRef"]
            or snapshot.get("quotePolicyRef") != self._quote_policy.policy_ref
            or snapshot.get("closed") is not True
            or snapshot.get("reconciliationRequired") is not False
            or snapshot.get("refundPerformed") is not False
        ):
            raise RuntimeBridgeError("native_effect_budget_retirement_invalid")
        with self._lock:
            if self._bound.get(key) != record:
                raise RuntimeBridgeError("native_effect_budget_retirement_race")
            self._bound.pop(key, None)

    def diagnostics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "boundInvocations": len(self._bound),
                "brokerSocketRef": digest(self.socket_path),
                "runtimeIsolationRef": self.runtime_isolation_ref,
                "modelBoundsRef": digest(self.model_bounds),
                "toolBoundsRef": digest(self.tool_bounds),
                "toolRoutePinsRef": digest(self.tool_route_pins),
                "origin": self.origin,
            }
