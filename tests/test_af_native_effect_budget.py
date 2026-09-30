"""HLB host-budget port against the real AF broker/ledger.

All authority, model pricing and tool pricing are synthetic fixtures. No provider,
Hermes gateway, network tool, or production policy is contacted or enabled.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("agent_factory.production.native_effect_budget_broker")

from agent_factory._validation import wire_sha256
from agent_factory.contracts import (
    AuthorizationOutcome,
    ContextProjection,
    ExecutionAuthorization,
    RiskLevel,
    SideEffectPolicy,
)
from agent_factory.contracts.runtime_invocation import (
    CapabilityIntent,
    GovernedInvocation,
)
from agent_factory.policy.execution import ExecutionAuthorityVerification
from agent_factory.production.native_effect_budget import fingerprint
from agent_factory.production.native_effect_budget_broker import (
    NativeEffectBudgetBrokerClient,
    NativeEffectBudgetBrokerError,
)

from hermes_life_bridge.af_native_effect_budget import AFNativeEffectBudgetPort
from hermes_life_bridge.hermes_native_events import digest
from hermes_life_bridge.host_budget_admission import REQUIRED_COVERAGE
from hermes_life_bridge.runtime_adapter import RuntimeBridgeError


class FixtureAuthority:
    def __init__(self):
        self.calls = 0

    def verify(self, authorization, workspace_grant, *, at):
        self.calls += 1
        return ExecutionAuthorityVerification(
            verification_ref="authority-verification://synthetic-budget-port",
            verifier_id="synthetic-only",
            authorization_hash=wire_sha256(authorization.to_dict()),
            workspace_grant_hash=None,
            verified_at=at,
            expiry=authorization.expiry,
        )


def governed(*, tokens=10_000, cost="1.0", native_calls=8,
             task_id="task:host-budget-fixture",
             invocation_id="invocation:host-budget-fixture"):
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(minutes=2)
    intent = CapabilityIntent(
        task_id,
        "did:arthurverse:host-budget-fixture",
        "fixture-life",
        "public_research",
        "PUBLIC_FIXTURE_OBJECTIVE",
        ("public_web",),
        RiskLevel.L0,
        tokens,
        Decimal(cost),
        expiry,
        json.dumps({
            "maxNativeCalls": native_calls,
            "maxRuntimeMs": 120_000,
        }),
    )
    projection = ContextProjection(
        "fixture-projection",
        intent.life_did,
        "fixture-self-gateway",
        "synthetic",
        now - timedelta(seconds=1),
        expiry,
        project_state={"capability_intent": intent.to_dict()},
        source_hashes=(fingerprint("fixture-source"),),
    )
    auth = ExecutionAuthorization(
        "fixture-authorization",
        intent.request_id,
        intent.life_did,
        projection.projection_hash(),
        AuthorizationOutcome.ALLOW,
        frozenset({"public_research"}),
        frozenset({"public_web"}),
        RiskLevel.L0,
        SideEffectPolicy.NONE,
        tokens,
        Decimal(cost),
        "fixture-self-gateway",
        now - timedelta(seconds=1),
        expiry,
        "SYNTHETIC_SIGNATURE_NOT_PRODUCTION",
    )
    return GovernedInvocation(
        invocation_id,
        intent,
        "hermes",
        "hermes.native.fixture",
        1,
        tokens,
        Decimal(cost),
        projection,
        auth,
    )


def model_bounds():
    return {
        "fixture-main": {
            "provider": "fixture-provider",
            "model": "fixture-model",
            "maxInputUsdPerMillion": "1.5",
            "maxOutputUsdPerMillion": "4.0",
            "maxRequestUsd": "0.0001",
            "maxOutputTokens": 256,
            "protocolOverheadTokens": 64,
            "quoteRef": digest("synthetic-official-model-bound"),
        }
    }


def tool_bounds():
    return {
        "web_search": {
            "tokens": 0,
            "costUsd": "0.002",
            "quoteRef": digest("synthetic-web-search-bound"),
        },
        "web_extract": {
            "tokens": 0,
            "costUsd": "0.001",
            "quoteRef": digest("synthetic-web-extract-bound"),
        },
    }


def binding(g):
    return {
        "schema": "rlb.host-budget-binding.v1",
        "tenantId": "tenant-test",
        "lifeDid": g.intent.life_did,
        "requestId": g.intent.request_id,
        "afInvocationDigest": g.digest(),
        "deadline": g.intent.deadline.isoformat(),
        "modelDigest": digest({"provider": "fixture-provider", "model": "fixture-model"}),
        "sessionRef": digest("fixture-session"),
        "endpointRef": digest("http://127.0.0.1:8642"),
        "limits": {
            "maxTokens": g.token_budget,
            "maxCostUsd": str(g.cost_budget),
            "maxNativeCalls": 8,
            "maxRuntimeMs": 120_000,
        },
    }


def make_port(root: Path, authority: FixtureAuthority, *, capacity: int = 128):
    return AFNativeEffectBudgetPort(
        socket_path=root / "budget.sock",
        ledger_path=root / "budget.sqlite3",
        authority_verifier=authority,
        allowed_uids=frozenset({os.geteuid()}),
        origin="SYNTHETIC_TEST",
        runtime_isolation_ref=digest("synthetic-runtime-isolation-evidence"),
        model_bounds=model_bounds(),
        tool_bounds=tool_bounds(),
        tool_route_pins={
            "web_search": "fixture-search",
            "web_extract": "fixture-extract",
        },
        capacity=capacity,
    )


def test_real_af_broker_computes_quote_and_holds_model_and_tool_effects(tmp_path):
    tmp_path.chmod(0o700)
    authority = FixtureAuthority()
    g = governed()
    port = make_port(tmp_path, authority)
    port.start()
    try:
        ctx = __import__("asyncio").run(
            port.prepare_execution_context(binding(g), g)
        )
        assert ctx["schema"] == "af.native-effect-budget-context.v1"
        assert set(ctx) == {"schema", "socketPath", "invocationRef", "quotePolicyRef"}
        assert ctx["quotePolicyRef"].startswith("sha256:")
        assert "modelBounds" not in ctx and "toolBounds" not in ctx
        assert "toolRoutePins" not in ctx

        client = NativeEffectBudgetBrokerClient(ctx["socketPath"])
        model_request = {
            "messages": [{"role": "user", "content": "public fixture"}],
            "max_tokens": 32,
        }
        reserved = client.request({
            "action": "reserve",
            "invocationRef": ctx["invocationRef"],
            "effectId": "model:fixture",
            "kind": "model",
            "request": model_request,
            "effectMeta": {
                "provider": "fixture-provider",
                "model": "fixture-model",
            },
            # A hostile Runtime-supplied quote is intentionally ignored. The
            # broker must calculate its own ceiling from the bound host policy.
            "quote": {
                "tokens": 0,
                "costUsd": "0",
                "quoteRef": digest("hostile-low-quote"),
                "requestDigest": digest(model_request),
            },
        })
        assert reserved["tokenCeiling"] > 32
        assert Decimal(reserved["costCeilingUsd"]) > 0
        assert reserved["quoteRef"].startswith("sha256:")
        client.request({
            "action": "finish",
            "invocationRef": ctx["invocationRef"],
            "effectId": "model:fixture",
            "effectOutcome": "completed",
        })

        client.request({
            "action": "reserve",
            "invocationRef": ctx["invocationRef"],
            "effectId": "tool:fixture",
            "kind": "tool",
            "request": {"tool": "web_search", "args": {"query": "public"}},
            "effectMeta": {"tool": "web_search", "route": "fixture-search"},
        })
        finished = client.request({
            "action": "finish",
            "invocationRef": ctx["invocationRef"],
            "effectId": "tool:fixture",
            "effectOutcome": "completed",
        })
        snap = finished["snapshot"]
        assert snap["effectStates"] == {"completed_held": 2}
        assert snap["nativeCalls"] == 1
        assert Decimal(snap["heldCostCeilingsUsd"]) >= Decimal("0.002")

        proof_binding = binding(g)
        proof_binding["submissionDigest"] = digest({
            "input": "fixture", "execution_context": ctx})
        proof = __import__("asyncio").run(
            port.verify_existing_reservation(proof_binding)
        )
        assert proof.coverage == REQUIRED_COVERAGE
        assert proof.reservation_ref == ctx["invocationRef"]
        assert authority.calls >= 3
    finally:
        port.close()


def test_retired_invocation_frees_live_broker_capacity_without_refund_or_replay(tmp_path):
    tmp_path.chmod(0o700)
    authority = FixtureAuthority()
    first = governed(
        task_id="life-action:first",
        invocation_id="native:first",
    )
    second = governed(
        task_id="life-action:second",
        invocation_id="native:second",
    )
    port = make_port(tmp_path, authority, capacity=1)
    port.start()
    try:
        first_binding = binding(first)
        first_ctx = port.bind_execution_context(first_binding, first)
        retire_binding = dict(first_binding)
        retire_binding["submissionDigest"] = digest({
            "fixture": "exact-submission-after-existing-host-verification",
            "execution_context": first_ctx,
        })
        port.retire_execution_context(retire_binding)

        assert port.diagnostics()["boundInvocations"] == 0
        with pytest.raises(
            RuntimeBridgeError, match="native_effect_budget_bind_denied"
        ):
            port.bind_execution_context(binding(first), first)

        second_ctx = port.bind_execution_context(binding(second), second)
        assert second_ctx["invocationRef"] != first_ctx["invocationRef"]
        assert port.diagnostics()["boundInvocations"] == 1

        # Closing never refunded the first ledger row; it only freed live
        # broker correlation and permanently tombstoned that invocation.
        import sqlite3
        db = sqlite3.connect(tmp_path / "budget.sqlite3")
        try:
            rows = db.execute(
                "select invocation_ref,closed,held_tokens,held_cost_nanos "
                "from invocations order by invocation_ref"
            ).fetchall()
        finally:
            db.close()
        first_row = next(row for row in rows if row[0] == first_ctx["invocationRef"])
        assert first_row[1] == 1
        assert first_row[2] == 0 and first_row[3] == 0
    finally:
        port.close()


def test_same_original_task_cannot_allocate_second_budget_via_new_invocation(tmp_path):
    tmp_path.chmod(0o700)
    authority = FixtureAuthority()
    first = governed(task_id="life-action:stable", invocation_id="native:attempt-one")
    second = governed(task_id="life-action:stable", invocation_id="native:attempt-two")
    port = make_port(tmp_path, authority)
    port.start()
    try:
        __import__("asyncio").run(
            port.prepare_execution_context(binding(first), first)
        )
        with pytest.raises(Exception, match="native_effect_budget_bind_denied"):
            __import__("asyncio").run(
                port.prepare_execution_context(binding(second), second)
            )
    finally:
        port.close()



def test_unapproved_tool_route_is_denied_by_af_before_effect(tmp_path):
    tmp_path.chmod(0o700)
    port = make_port(tmp_path, FixtureAuthority())
    g = governed()
    port.start()
    try:
        ctx = __import__("asyncio").run(
            port.prepare_execution_context(binding(g), g)
        )
        client = NativeEffectBudgetBrokerClient(ctx["socketPath"])
        with pytest.raises(
            NativeEffectBudgetBrokerError,
            match="effect_quote_tool_route_unapproved",
        ):
            client.request({
                "action": "reserve",
                "invocationRef": ctx["invocationRef"],
                "effectId": "tool:wrong-route",
                "kind": "tool",
                "request": {"tool": "web_search", "args": {"query": "public"}},
                "effectMeta": {
                    "tool": "web_search",
                    "route": "unexpected-route",
                },
            })
        snap = client.request({
            "action": "snapshot",
            "invocationRef": ctx["invocationRef"],
        })["snapshot"]
        assert snap["heldTokenCeilings"] == 0
        assert snap["effectStates"] == {}
    finally:
        port.close()

def test_unknown_model_route_is_denied_by_af_before_effect(tmp_path):
    tmp_path.chmod(0o700)
    port = make_port(tmp_path, FixtureAuthority())
    g = governed()
    port.start()
    try:
        ctx = __import__("asyncio").run(
            port.prepare_execution_context(binding(g), g)
        )
        client = NativeEffectBudgetBrokerClient(ctx["socketPath"])
        with pytest.raises(
            NativeEffectBudgetBrokerError,
            match="effect_quote_model_route_unapproved",
        ):
            client.request({
                "action": "reserve",
                "invocationRef": ctx["invocationRef"],
                "effectId": "model:unknown",
                "kind": "model",
                "request": {"messages": [], "max_tokens": 1},
                "effectMeta": {
                    "provider": "fallback-provider",
                    "model": "unapproved-model",
                },
            })
        snap = client.request({
            "action": "snapshot",
            "invocationRef": ctx["invocationRef"],
        })["snapshot"]
        assert snap["heldTokenCeilings"] == 0
        assert snap["effectStates"] == {}
    finally:
        port.close()


def test_broker_restart_converts_inflight_reservation_to_unknown_and_halts(tmp_path):
    tmp_path.chmod(0o700)
    g = governed()
    first = make_port(tmp_path, FixtureAuthority())
    first.start()
    ctx = __import__("asyncio").run(
        first.prepare_execution_context(binding(g), g)
    )
    client = NativeEffectBudgetBrokerClient(ctx["socketPath"])
    client.request({
        "action": "reserve",
        "invocationRef": ctx["invocationRef"],
        "effectId": "model:crash",
        "kind": "model",
        "request": {"messages": [], "max_tokens": 1},
        "effectMeta": {
            "provider": "fixture-provider",
            "model": "fixture-model",
        },
    })
    first.close()

    second = make_port(tmp_path, FixtureAuthority())
    second.start()
    try:
        second_ctx = __import__("asyncio").run(
            second.prepare_execution_context(binding(g), g)
        )
        client2 = NativeEffectBudgetBrokerClient(second_ctx["socketPath"])
        snap = client2.request({
            "action": "snapshot",
            "invocationRef": second_ctx["invocationRef"],
        })["snapshot"]
        assert snap["effectStates"] == {"unknown_held": 1}
        assert snap["reconciliationRequired"] is True
        with pytest.raises(
            NativeEffectBudgetBrokerError,
            match="effect_unknown_requires_reconciliation",
        ):
            client2.request({
                "action": "reserve",
                "invocationRef": second_ctx["invocationRef"],
                "effectId": "model:after-crash",
                "kind": "model",
                "request": {"messages": [], "max_tokens": 1},
                "effectMeta": {
                    "provider": "fixture-provider",
                    "model": "fixture-model",
                },
            })
    finally:
        second.close()
