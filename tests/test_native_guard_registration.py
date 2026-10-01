"""The HLB plugin's actual registrations cover both required effect stages."""

import pytest

pytest.importorskip("hermes_cli.middleware")

from agent.execution_context import scoped_execution_context
from hermes_cli import plugins
from hermes_cli.middleware import (
    ExecutionMiddlewareDenied,
    execution_context_guard_capabilities,
    run_llm_execution_middleware,
)
from hermes_life_bridge import plugin
from hermes_life_bridge import native_effect_budget_middleware as budget


def test_plugin_registers_real_budget_callbacks_and_missing_stage_denies(monkeypatch):
    manager = plugins.PluginManager()
    manager._discovered = True
    monkeypatch.setattr(plugins, "_plugin_manager", manager)

    class Context:
        def register_hook(self, *_args, **_kwargs):
            pass

        def register_tool(self, *_args, **_kwargs):
            pass

        def register_middleware(self, kind, callback):
            manager._middleware.setdefault(kind, []).append(callback)

    plugin.register(Context())
    evidence = execution_context_guard_capabilities()
    assert evidence["supported_context_schemas"] == ["af.native-effect-budget-context.v1"]
    assert manager._middleware["llm_execution"] == [budget.on_llm_execution]
    assert manager._middleware["tool_execution"] == [budget.on_tool_execution]
    assert {row["callback_id"] for row in evidence["guards"]} == {
        f"{budget.__name__}.on_llm_execution",
        f"{budget.__name__}.on_tool_execution",
    }
    assert all(len(row["module_source_sha256"]) == 64 for row in evidence["guards"])

    effects = []
    with scoped_execution_context({"schema": "af.native-effect-budget-context.v1"}):
        with pytest.raises(ExecutionMiddlewareDenied):
            run_llm_execution_middleware({}, lambda request: effects.append(request))
    assert effects == []

    del manager._middleware["tool_execution"]
    assert execution_context_guard_capabilities()["supported_context_schemas"] == []
    with scoped_execution_context({"schema": "af.native-effect-budget-context.v1"}):
        with pytest.raises(ExecutionMiddlewareDenied, match="guard_missing:tool_execution"):
            run_llm_execution_middleware({}, lambda request: effects.append(request))
    assert effects == []
