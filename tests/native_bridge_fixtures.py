"""SYNTHETIC_TEST ports only; never imported by the production package."""
import copy
from datetime import datetime, timedelta, timezone
import importlib

import pytest

from hermes_life_bridge.runtime_adapter import (
    HermesRuntimeAdapter, BoundResearchBridge, register_research_bridge,
)


class FixtureNative:
    runtime_id = "hermes"
    implementation_id = "hermes.fixture.native-web.v1"
    origin = "SYNTHETIC_TEST"

    def __init__(self, contract):
        self.contract, self.calls, self.cancellations = contract, [], []
        self.fail = False
        self.missing_read = False
        self.bad_event = False
        self.swallow_bad_event = False
        self.usage = None
        self.combined = False

    async def invoke(self, request, emit):
        self.calls.append(request)
        if self.fail:
            raise RuntimeError("PRIVATE_RUNTIME_ERROR_SENTINEL")
        names = ["research_workflow"] if self.combined else ["web_search", "web_extract"]
        if self.missing_read:
            names = ["web_search"]
        for i, name in enumerate(names):
            event = {"kind": "call", "invocationId": request.invocation_id,
                "nativeToolName": name, "toolCallId": "fixture-call-" + str(i),
                "args": "PRIVATE_TOOL_ARGS_SENTINEL"}
            emit(event)
            if self.bad_event:
                bad = dict(event, kind="result", toolCallId="wrong-call")
                try:
                    emit(bad)
                except ValueError:
                    if not self.swallow_bad_event:
                        raise
            emit(dict(event, kind="result", evidenceRefs=[self.contract.digest({"fixture": i})],
                      result="PRIVATE_TOOL_RESULT_SENTINEL"))
        return {"invocationId": request.invocation_id, "runtime": "hermes",
            "implementationId": self.implementation_id, "status": "completed",
            "usage": self.usage, "output": "PRIVATE_NATIVE_OUTPUT_SENTINEL"}

    async def cancel(self, invocation_id):
        self.cancellations.append(invocation_id)
        return True


class FixtureAdmission:
    def __init__(self):
        self.calls = []
        self.deny = False
        self.return_boolean = False

    async def admit(self, intent, selection, descriptor, native):
        self.calls.append(copy.deepcopy((intent, selection, descriptor)))
        if self.deny:
            raise PermissionError("PRIVATE_AUTH_ERROR_SENTINEL")
        if self.return_boolean:
            return True
        return None


def modules():
    native = pytest.importorskip("digital_life_native_capability",
        reason="DLS canonical source must be explicitly installed/supplied; not a live test")
    return native, importlib.import_module("digital_life_native_projection")


def make_adapter(config, *, tenant="tenant-test", combined=False):
    c, _ = modules()
    native, admission = FixtureNative(c), FixtureAdmission()
    native.combined = combined
    descriptor = {"schema": c.DESCRIPTOR_SCHEMA, "capability": "public_research.v1",
        "runtime": "hermes", "implementationId": native.implementation_id,
        "nativeTools": ({"research_workflow": ["discover", "read"]} if combined else
            {"web_search": ["discover"], "web_extract": ["read"]}),
        "requiredEvidenceRoles": ["discover", "read"], "availability": "available"}
    adapter = HermesRuntimeAdapter(config, tenant_id=tenant, descriptor=descriptor,
        native=native, admission=admission)
    return adapter, native, admission


def intent_and_selection(adapter, request="Public fixture topic", budget=None):
    c = adapter.contract
    intent_id = "fixture-intent-" + c.digest(
        {"request": request, "budget": budget or {}}
    )[7:23]
    intent = {"schema": c.INTENT_SCHEMA, "intentId": intent_id,
        "tenantId": adapter.tenant_id, "lifeDid": adapter.config.life_did,
        "capability": "public_research.v1", "residentRuntime": "hermes", "request": request,
        "contextBinding": {k: c.digest({"fixture-context": k}) for k in c.CONTEXT_KEYS},
        "authorizationRef": c.digest("fixture-authorization"),
        "budget": budget or {"maxRuntimeMs": 1000, "maxTokens": 1000,
            "maxCostUsd": "0.05", "maxNativeCalls": 2}}
    selection = {"schema": c.SELECTION_SCHEMA,
        "selectionRef": c.digest({"fixture-selection": intent_id}),
        "authorizationRef": intent["authorizationRef"], "intentDigest": c.digest(intent),
        "tenantId": intent["tenantId"], "lifeDid": intent["lifeDid"],
        "selectedRuntime": "hermes", "implementationId": adapter.capabilities()["implementationId"],
        "capability": intent["capability"],
        "expiresAt": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "previousInvocationId": None}
    return intent, selection


def bind_tools(tools, monkeypatch):
    from hermes_life_bridge import runtime_adapter as module
    # Clear only test-owned process-local registry; pytest restores it afterward.
    monkeypatch.setattr(module, "_BINDINGS", {})
    adapter, native, admission = make_adapter(tools.test_config)
    async def prepare(*, request, session_ref, budget):
        return intent_and_selection(adapter, request, budget)
    async def answer(receipt):
        return {"invocationId": receipt["invocationId"],
            "receiptDigest": adapter.contract.digest(receipt),
            "text": "OWNER_ANSWER_SENTINEL https://example.com/source",
            "taskAccepted": True}
    bridge = BoundResearchBridge(adapter=adapter, manifest_hash=tools.manifest["manifestHash"],
        prepare=prepare, read_answer=answer)
    register_research_bridge(bridge)
    return bridge, native, admission
