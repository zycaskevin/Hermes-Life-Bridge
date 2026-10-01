"""Hermes-side composition of the DLS canonical native-capability contract.

No subprocess, model/credential selection, research implementation or implicit
host is supplied. The deployment binds an already-contained Hermes native port
and the EXISTING AF/Self Gateway admission port. DLS owns receipt normalization.
Contact delivery continues through the existing durable ContactService.
"""
from __future__ import annotations

import asyncio
import copy
import importlib
import json
import re
import threading
from decimal import Decimal
from typing import Any, Callable

from .config import BridgeConfig

PUBLIC_RESEARCH = "public_research.v1"
RECEIPT_SCHEMA = "digital-life-stack.runtime-execution-receipt.v1"


class RuntimeBridgeError(RuntimeError):
    """Stable code only; private underlying exception messages are not exported."""


def _contracts():
    try:
        native = importlib.import_module("digital_life_native_capability")
        projection = importlib.import_module("digital_life_native_projection")
    except ImportError:
        raise RuntimeBridgeError("dls_native_contract_not_installed") from None
    if native.RECEIPT_SCHEMA != RECEIPT_SCHEMA:
        raise RuntimeBridgeError("dls_native_contract_version_mismatch")
    return native, projection


class HermesRuntimeAdapter:
    """Two thin paths: canonical governed invocation and durable Contact.

    Dependencies are trusted deployment objects, never model tool arguments.
    Receipt validation is not authentication: admission/native transport must
    authenticate their sources, context, effect grants and hard budgets.
    """
    runtime_id = "hermes"

    def __init__(self, config: BridgeConfig, *, tenant_id: str, descriptor: dict,
                 native, admission):
        if not isinstance(tenant_id, str) or not tenant_id:
            raise RuntimeBridgeError("tenant_binding_required")
        self.config, self.tenant_id = config, tenant_id
        self.contract, self.projection = _contracts()
        self._adapter = self.contract.HermesRuntimeAdapter(
            descriptor, native=native, admission=admission)

    def capabilities(self) -> dict:
        return self._adapter.capabilities()

    async def invoke(self, intent: dict, selection: dict) -> dict:
        # A resident Hermes is not itself an AF selection or a grant.
        if not isinstance(intent, dict) or (
            intent.get("lifeDid") != self.config.life_did
            or intent.get("tenantId") != self.tenant_id
        ):
            raise RuntimeBridgeError("native_life_scope_mismatch")
        if not isinstance(selection, dict) or selection.get("selectedRuntime") != "hermes":
            raise RuntimeBridgeError("hermes_runtime_mismatch")
        receipt = await self._adapter.invoke(copy.deepcopy(intent), copy.deepcopy(selection))
        return self.projection.validate_receipt(
            receipt, self.capabilities(), tenant_id=self.tenant_id,
            life_did=self.config.life_did)

    def project_receipt(self, receipt: dict) -> list[dict]:
        return self.projection.project_native_receipt(
            receipt, self.capabilities(), tenant_id=self.tenant_id,
            life_did=self.config.life_did)

    def deliver_contact(self, *, selected_runtime: str, intent, decision, service):
        from .runtime_contact import HermesContactBridge
        return HermesContactBridge(self.config).deliver(selected_runtime=selected_runtime,
            intent=intent, decision=decision, service=service)


class BoundResearchBridge:
    """Intent-only Hermes tool integration, bound by the trusted deployment.

    prepare(request, session_ref, budget) returns DLS (intent, AF selection).
    It must use existing authority; this bridge never manufactures a grant.
    read_answer(receipt) optionally resolves a scoped public answer via the
    host's existing output boundary. Answers never enter canonical receipts or
    operational ledgers. No production binding is installed by import.
    """
    def __init__(self, *, adapter: HermesRuntimeAdapter, manifest_hash: str,
                 prepare: Callable, read_answer: Callable | None = None,
                 host_loop: asyncio.AbstractEventLoop | None = None):
        if not isinstance(adapter, HermesRuntimeAdapter) or not callable(prepare):
            raise RuntimeBridgeError("governed_native_binding_required")
        if not isinstance(manifest_hash, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", manifest_hash):
            raise RuntimeBridgeError("manifest_binding_required")
        if read_answer is not None and not callable(read_answer):
            raise RuntimeBridgeError("answer_port_invalid")
        self.adapter, self.manifest_hash = adapter, manifest_hash
        if host_loop is not None and not isinstance(host_loop, asyncio.AbstractEventLoop):
            raise RuntimeBridgeError("native_host_loop_invalid")
        self._prepare, self._read_answer = prepare, read_answer
        self.host_loop = host_loop

    async def dispatch(self, *, query: str, session: str, max_age_days: int | None,
                       timeout_seconds: int) -> dict:
        # The unchanged freshness requirement is part of the exact request bound
        # by AF. Passing the constraint is NOT certifying that it was satisfied.
        request = json.dumps({"objective": query, "maxAgeDays": max_age_days},
                             sort_keys=True, ensure_ascii=False)
        ceiling = {"maxRuntimeMs": timeout_seconds * 1000, "maxTokens": 1000,
                   "maxCostUsd": "0.05", "maxNativeCalls": 2}
        try:
            intent, selection = await self._prepare(
                request=request, session_ref=session, budget=copy.deepcopy(ceiling))
            intent = self.adapter.contract.validate_intent(intent)
            if intent["request"] != request or intent["capability"] != PUBLIC_RESEARCH:
                raise RuntimeBridgeError("native_request_binding_mismatch")
            budget = intent["budget"]
            if any(budget[k] > ceiling[k] for k in ("maxRuntimeMs", "maxTokens", "maxNativeCalls")) or (
                Decimal(budget["maxCostUsd"]) > Decimal(ceiling["maxCostUsd"])
            ):
                raise RuntimeBridgeError("native_budget_expansion_denied")
            receipt = await self.adapter.invoke(intent, selection)
        except RuntimeBridgeError:
            raise
        except Exception:
            raise RuntimeBridgeError("native_governance_or_contract_failed") from None
        result = {"executionReceipt": receipt, "taskAccepted": False,
                  "answerAvailable": False, "summary": None}
        # Never turn invocation failure into a native execution claim or fallback.
        if receipt["status"] != "completed" or self._read_answer is None:
            return result
        try:
            answer = await self._read_answer(copy.deepcopy(receipt))
            if (not isinstance(answer, dict)
                    or set(answer) != {"invocationId", "receiptDigest", "text", "taskAccepted"}
                    or answer["invocationId"] != receipt["invocationId"]
                    or answer["receiptDigest"] != self.adapter.contract.digest(receipt)
                    or type(answer["taskAccepted"]) is not bool
                    or not isinstance(answer["text"], str)
                    or not answer["text"].strip() or len(answer["text"]) > 24000
                    or "\x00" in answer["text"]):
                raise RuntimeBridgeError("native_answer_binding_mismatch")
            result.update(taskAccepted=answer["taskAccepted"], answerAvailable=True,
                          summary=answer["text"])
        except Exception:
            # Native evidence remains true even when task-result acceptance fails.
            result["answerError"] = "native_answer_unavailable"
        return result


_BINDINGS: dict[tuple[str, str, str], BoundResearchBridge] = {}
_LOCK = threading.RLock()


def register_research_bridge(bridge: BoundResearchBridge) -> None:
    """Trusted bootstrap API, deliberately absent from Hermes tool schemas."""
    if not isinstance(bridge, BoundResearchBridge):
        raise RuntimeBridgeError("governed_native_binding_required")
    key = (bridge.adapter.config.life_did, bridge.adapter.tenant_id, bridge.manifest_hash)
    with _LOCK:
        prior = _BINDINGS.get(key)
        if prior is not None and prior is not bridge:
            raise RuntimeBridgeError("native_binding_already_registered")
        if prior is None and len(_BINDINGS) >= 64:
            raise RuntimeBridgeError("native_binding_capacity")
        _BINDINGS[key] = bridge


def get_research_bridge(*, life_did: str, tenant_id: str, manifest_hash: str) -> BoundResearchBridge | None:
    with _LOCK:
        return _BINDINGS.get((life_did, tenant_id, manifest_hash))


def check_sync_dispatch(bridge: BoundResearchBridge) -> None:
    """Reject an incompatible caller BEFORE reserving a durable invocation."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeBridgeError("native_bridge_requires_host_async_dispatch")
    loop = bridge.host_loop
    if loop is not None and (loop.is_closed() or not loop.is_running()):
        raise RuntimeBridgeError("native_host_loop_unavailable")


def dispatch_sync(bridge: BoundResearchBridge, **kwargs: Any) -> dict:
    check_sync_dispatch(bridge)
    if bridge.host_loop is None:
        return asyncio.run(bridge.dispatch(**kwargs))
    # The host owns this already-running loop. Do not create a hidden worker or
    # recreate loop-bound transports per tool call. A timeout leaves the durable
    # request unresolved until authoritative cancellation/reconciliation.
    future = asyncio.run_coroutine_threadsafe(bridge.dispatch(**kwargs), bridge.host_loop)
    try:
        return future.result(timeout=kwargs["timeout_seconds"] + 5)
    except TimeoutError:
        future.cancel()
        raise RuntimeBridgeError("native_host_dispatch_outcome_unknown") from None
