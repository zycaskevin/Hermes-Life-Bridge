"""Real AF broker + real Hermes execution middleware integration.

All authority/pricing/provider/tool callbacks are synthetic. No network provider,
live gateway, production grant, or real research task is contacted.
"""
from __future__ import annotations

import asyncio
import os

import pytest

pytest.importorskip("agent.execution_context")
pytest.importorskip("hermes_cli.middleware")

from agent.execution_context import scoped_execution_context
from agent_factory.production.native_effect_budget_broker import NativeEffectBudgetBrokerClient
from hermes_cli import plugins
from hermes_cli.middleware import (
    ExecutionMiddlewareDenied,
    LLM_EXECUTION_MIDDLEWARE,
    TOOL_EXECUTION_MIDDLEWARE,
    run_llm_execution_middleware,
    run_tool_execution_middleware,
)
from hermes_cli.plugins import PluginManager

from hermes_life_bridge import native_effect_budget_middleware as budget_mw
from test_af_native_effect_budget import (
    FixtureAuthority,
    binding,
    governed,
    make_port,
)


@pytest.fixture(autouse=True)
def clear_local_halts():
    budget_mw.reset_local_halts_for_tests()
    yield
    budget_mw.reset_local_halts_for_tests()


@pytest.fixture
def real_execution_middleware(monkeypatch):
    manager = PluginManager()
    manager._discovered = True
    manager._middleware.setdefault(LLM_EXECUTION_MIDDLEWARE, []).append(
        budget_mw.on_llm_execution
    )
    manager._middleware.setdefault(TOOL_EXECUTION_MIDDLEWARE, []).append(
        budget_mw.on_tool_execution
    )
    monkeypatch.setattr(plugins, "_plugin_manager", manager)
    monkeypatch.setattr(
        budget_mw,
        "_tool_route",
        lambda name: {
            "web_search": "fixture-search",
            "web_extract": "fixture-extract",
        }[name],
    )
    return manager


def _start_port(tmp_path, *, tokens=10_000, cost="1.0", native_calls=8):
    tmp_path.chmod(0o700)
    authority = FixtureAuthority()
    g = governed(tokens=tokens, cost=cost, native_calls=native_calls)
    port = make_port(tmp_path, authority)
    port.start()
    ctx = asyncio.run(port.prepare_execution_context(binding(g), g))
    return port, g, ctx


def test_real_broker_reserves_model_and_tool_before_callbacks(
    tmp_path, real_execution_middleware
):
    port, _g, ctx = _start_port(tmp_path)
    client = NativeEffectBudgetBrokerClient(ctx["socketPath"])
    sequence = []
    try:
        with scoped_execution_context(ctx):
            model_result = run_llm_execution_middleware(
                {
                    "messages": [{"role": "user", "content": "public fixture"}],
                    "max_tokens": 32,
                },
                lambda request: sequence.append(("model", request)) or "model-ok",
                provider="fixture-provider",
                model="fixture-model",
                api_request_id="main-1",
                api_call_count=1,
            )
            tool_result = run_tool_execution_middleware(
                "web_search",
                {"query": "public fixture"},
                lambda args: sequence.append(("tool", args)) or "tool-ok",
                tool_call_id="tool-1",
                task_id="fixture-task",
            )

        assert model_result == "model-ok"
        assert tool_result == "tool-ok"
        assert [kind for kind, _ in sequence] == ["model", "tool"]

        snap = client.request({
            "action": "snapshot",
            "invocationRef": ctx["invocationRef"],
        })["snapshot"]
        assert snap["effectStates"] == {"completed_held": 2}
        assert snap["heldTokenCeilings"] > 32
        assert snap["nativeCalls"] == 1
        assert snap["heldCostCeilingsUsd"] != "0"
        assert snap["refundPerformed"] is False
    finally:
        port.close()


def test_cumulative_model_budget_denial_never_calls_provider(
    tmp_path, real_execution_middleware
):
    port, _g, ctx = _start_port(tmp_path, tokens=100, cost="1.0")
    called = []
    try:
        with scoped_execution_context(ctx):
            with pytest.raises(
                ExecutionMiddlewareDenied, match="effect_cumulative_budget_exceeded"
            ):
                run_llm_execution_middleware(
                    {
                        "messages": [{"role": "user", "content": "public fixture"}],
                        "max_tokens": 80,
                    },
                    lambda request: called.append(request) or "must-not-run",
                    provider="fixture-provider",
                    model="fixture-model",
                    api_request_id="over-budget",
                    api_call_count=1,
                )
        assert called == []
        snap = NativeEffectBudgetBrokerClient(ctx["socketPath"]).request({
            "action": "snapshot",
            "invocationRef": ctx["invocationRef"],
        })["snapshot"]
        assert snap["effectStates"] == {}
        assert snap["heldTokenCeilings"] == 0
    finally:
        port.close()


def test_failed_provider_holds_unknown_and_halts_followup_effect(
    tmp_path, real_execution_middleware
):
    port, _g, ctx = _start_port(tmp_path)
    second_called = []
    try:
        def fail(_request):
            raise RuntimeError("synthetic provider failure")

        with scoped_execution_context(ctx):
            with pytest.raises(RuntimeError, match="synthetic provider failure"):
                run_llm_execution_middleware(
                    {"messages": [], "max_tokens": 8},
                    fail,
                    provider="fixture-provider",
                    model="fixture-model",
                    api_request_id="provider-fails",
                    api_call_count=1,
                )
            with pytest.raises(
                ExecutionMiddlewareDenied,
                match="native_effect_invocation_locally_halted|effect_unknown_requires_reconciliation",
            ):
                run_llm_execution_middleware(
                    {"messages": [], "max_tokens": 8},
                    lambda request: second_called.append(request) or "must-not-run",
                    provider="fixture-provider",
                    model="fixture-model",
                    api_request_id="after-failure",
                    api_call_count=2,
                )

        assert second_called == []
        snap = NativeEffectBudgetBrokerClient(ctx["socketPath"]).request({
            "action": "snapshot",
            "invocationRef": ctx["invocationRef"],
        })["snapshot"]
        assert snap["effectStates"] == {"unknown_held": 1}
        assert snap["reconciliationRequired"] is True
    finally:
        port.close()
