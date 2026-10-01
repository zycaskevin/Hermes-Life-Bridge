from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from hermes_life_bridge.runtime_adapter import HermesRuntimeAdapter, RuntimeBridgeError


class FakeNativeAdapter:
    def __init__(self):
        self.calls = []

    def capabilities(self):
        return {"runtime": "hermes", "capability": "public_research.v1"}

    async def invoke_prepared(self, intent, prepare):
        self.calls.append((intent, prepare))
        return {"receipt": "raw"}


class FakeProjection:
    def __init__(self):
        self.calls = []

    def validate_receipt(self, receipt, descriptor, *, tenant_id, life_did):
        self.calls.append((receipt, descriptor, tenant_id, life_did))
        return {"validated": True, "receipt": receipt}


def adapter():
    value = HermesRuntimeAdapter.__new__(HermesRuntimeAdapter)
    value.config = SimpleNamespace(life_did="did:arthurverse:nancy")
    value.tenant_id = "tenant-arthur"
    value._adapter = FakeNativeAdapter()
    value.projection = FakeProjection()
    return value


def valid_intent():
    return {"lifeDid": "did:arthurverse:nancy", "tenantId": "tenant-arthur", "schema": "fixture"}


def test_invoke_prepared_delegates_selection_to_canonical_dls_adapter_and_validates_receipt():
    value = adapter()
    prepare = lambda *_args: None
    result = asyncio.run(value.invoke_prepared(valid_intent(), prepare))
    assert result == {"validated": True, "receipt": {"receipt": "raw"}}
    assert value._adapter.calls == [(valid_intent(), prepare)]
    assert value.projection.calls == [(
        {"receipt": "raw"},
        {"runtime": "hermes", "capability": "public_research.v1"},
        "tenant-arthur",
        "did:arthurverse:nancy",
    )]


def test_invoke_prepared_rejects_cross_life_before_preparation():
    value = adapter()
    intent = valid_intent()
    intent["lifeDid"] = "did:arthurverse:lily"
    with pytest.raises(RuntimeBridgeError, match="native_life_scope_mismatch"):
        asyncio.run(value.invoke_prepared(intent, lambda *_args: None))
    assert value._adapter.calls == []


def test_invoke_prepared_requires_trusted_callable():
    value = adapter()
    with pytest.raises(RuntimeBridgeError, match="trusted_preparation_port_required"):
        asyncio.run(value.invoke_prepared(valid_intent(), None))
