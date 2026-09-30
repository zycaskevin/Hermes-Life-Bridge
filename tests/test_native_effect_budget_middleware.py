from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("agent.execution_context")
pytest.importorskip("hermes_cli.middleware")

from agent.execution_context import scoped_execution_context
from hermes_cli.middleware import ExecutionMiddlewareDenied

from hermes_life_bridge import native_effect_budget_middleware as m


def context():
    return {
        "schema": "af.native-effect-budget-context.v1",
        "socketPath": "/tmp/synthetic-not-used.sock",
        "invocationRef": "sha256:" + "1" * 64,
        "quotePolicyRef": "sha256:" + "4" * 64,
    }


@pytest.fixture(autouse=True)
def clear_halts():
    m.reset_local_halts_for_tests()
    yield
    m.reset_local_halts_for_tests()


def test_governed_context_requires_exact_canonical_shape(monkeypatch):
    monkeypatch.setattr(
        m, "_broker", lambda *_a, **_k: pytest.fail("broker must not run")
    )
    mutations = [
        lambda ctx: ctx.pop("quotePolicyRef"),
        lambda ctx: ctx.__setitem__("extra", "not-canonical"),
        lambda ctx: ctx.__setitem__("invocationRef", "not-a-sha"),
        lambda ctx: ctx.__setitem__("socketPath", ""),
    ]
    for mutate in mutations:
        ctx = context()
        mutate(ctx)
        with scoped_execution_context(ctx):
            with pytest.raises(ExecutionMiddlewareDenied):
                m.on_llm_execution(
                    {"messages": [], "max_tokens": 1},
                    lambda _request: pytest.fail("provider must not run"),
                    provider="fixture-provider",
                    model="fixture-model",
                )


def test_ordinary_turn_is_untouched(monkeypatch):
    monkeypatch.setattr(m, "_broker", lambda *_a, **_k: pytest.fail("broker must not run"))
    called = []
    assert m.on_llm_execution({"model": "ordinary"}, lambda request: called.append(request) or "ok",
                              provider="p", model="m") == "ok"
    assert called == [{"model": "ordinary"}]


def test_main_model_reserves_before_callback_and_finishes_after(monkeypatch):
    sequence = []
    messages = []

    def broker(ctx, message):
        messages.append(message)
        sequence.append(message["action"])
        if message["action"] == "reserve":
            return {"outcome": "completed", "quotePolicyRef": ctx["quotePolicyRef"]}
        return {
            "outcome": "completed",
            "snapshot": {"quotePolicyRef": ctx["quotePolicyRef"]},
        }

    monkeypatch.setattr(m, "_broker", broker)
    request = {"messages": [{"role": "user", "content": "public fixture"}], "max_tokens": 12}

    def provider(value):
        assert sequence == ["reserve"]
        sequence.append("provider")
        assert value is request
        return "answer"

    with scoped_execution_context(context()):
        result = m.on_llm_execution(
            request, provider, provider="fixture-provider", model="fixture-model",
            api_request_id="req-1", api_call_count=1,
        )

    assert result == "answer"
    assert sequence == ["reserve", "provider", "finish"]
    reserve = messages[0]
    assert reserve["kind"] == "model"
    assert reserve["request"] == request
    assert reserve["effectMeta"] == {
        "provider": "fixture-provider", "model": "fixture-model"}
    assert "quote" not in reserve


def test_auxiliary_awaitable_reserves_before_physical_dispatch(monkeypatch):
    sequence = []
    messages = []

    def broker(ctx, message):
        messages.append(message)
        sequence.append(message["action"])
        if message["action"] == "reserve":
            return {"outcome": "completed"}
        return {
            "outcome": "completed",
            "snapshot": {"quotePolicyRef": ctx["quotePolicyRef"]},
        }

    monkeypatch.setattr(m, "_broker", broker)

    async def provider(_request):
        assert sequence == ["reserve"]
        sequence.append("provider")
        await asyncio.sleep(0)
        return "compressed"

    async def run():
        with scoped_execution_context(context()):
            pending = m.on_llm_execution(
                {"messages": [], "max_tokens": 8},
                provider,
                provider="fixture-provider",
                model="fixture-aux",
                call_role="auxiliary:compression",
                auxiliary_task="compression",
                auxiliary_physical_attempt=2,
                retry_count=1,
            )
            assert sequence == ["reserve"]
            return await pending

    assert asyncio.run(run()) == "compressed"
    assert sequence == ["reserve", "provider", "finish"]
    assert messages[0]["kind"] == "auxiliary_model"
    assert messages[0]["effectId"].startswith("aux:")


def test_unapproved_tool_is_denied_before_callback(monkeypatch):
    monkeypatch.setattr(m, "_broker", lambda *_a, **_k: pytest.fail("broker must not run"))
    called = []
    with scoped_execution_context(context()):
        with pytest.raises(ExecutionMiddlewareDenied, match="native_effect_tool_unapproved"):
            m.on_tool_execution(
                "terminal", {"command": "echo no"},
                lambda args: called.append(args) or "bad",
                tool_call_id="tool-1",
            )
    assert called == []


def test_budget_denial_is_before_provider_callback(monkeypatch):
    def deny(_ctx, message):
        assert message["action"] == "reserve"
        raise m.NativeEffectBudgetMiddlewareError("effect_cumulative_budget_exceeded")

    monkeypatch.setattr(m, "_broker", deny)
    called = []
    with scoped_execution_context(context()):
        with pytest.raises(ExecutionMiddlewareDenied, match="effect_cumulative_budget_exceeded"):
            m.on_llm_execution(
                {"messages": [], "max_tokens": 10},
                lambda req: called.append(req),
                provider="fixture-provider", model="fixture-model",
                api_request_id="req-over",
            )
    assert called == []


def test_sync_stream_settles_only_after_clean_exhaustion(monkeypatch):
    calls = []

    def broker(ctx, message):
        calls.append(message)
        if message["action"] == "reserve":
            return {"outcome": "completed", "quotePolicyRef": ctx["quotePolicyRef"]}
        assert message["effectOutcome"] == "completed"
        return {
            "outcome": "completed",
            "snapshot": {"quotePolicyRef": ctx["quotePolicyRef"]},
        }

    monkeypatch.setattr(m, "_broker", broker)

    def provider(_request):
        def stream():
            yield "one"
            yield "two"
        return stream()

    with scoped_execution_context(context()):
        stream = m.on_llm_execution(
            {"messages": [], "max_tokens": 4}, provider,
            provider="fixture-provider", model="fixture-model",
            api_request_id="stream-clean", streaming=True,
        )
        assert [call["action"] for call in calls] == ["reserve"]
        assert list(stream) == ["one", "two"]
    assert [call["action"] for call in calls] == ["reserve", "finish"]
    assert calls[-1]["effectOutcome"] == "completed"



def test_streaming_iterable_with_separate_iterator_settles_on_exhaustion(monkeypatch):
    calls = []

    def broker(ctx, message):
        calls.append(message)
        if message["action"] == "reserve":
            return {
                "outcome": "completed",
                "quotePolicyRef": ctx["quotePolicyRef"],
            }
        return {
            "outcome": "completed",
            "snapshot": {"quotePolicyRef": ctx["quotePolicyRef"]},
        }

    class IterableStream:
        marker = "owner-attribute"

        def __iter__(self):
            return iter(["one", "two"])

    monkeypatch.setattr(m, "_broker", broker)
    owner = IterableStream()
    with scoped_execution_context(context()):
        stream = m.on_llm_execution(
            {"messages": [], "max_tokens": 4},
            lambda _request: owner,
            provider="fixture-provider",
            model="fixture-model",
            api_request_id="separate-iterator",
            streaming=True,
        )
        assert [call["action"] for call in calls] == ["reserve"]
        assert stream.marker == "owner-attribute"
        assert list(stream) == ["one", "two"]
    assert [call["action"] for call in calls] == ["reserve", "finish"]
    assert calls[-1]["effectOutcome"] == "completed"

def test_sync_stream_failure_preserves_error_and_marks_unknown(monkeypatch):
    calls = []

    def broker(ctx, message):
        calls.append(message)
        if message["action"] == "reserve":
            return {"outcome": "completed", "quotePolicyRef": ctx["quotePolicyRef"]}
        assert message["effectOutcome"] == "unknown"
        return {
            "outcome": "completed",
            "snapshot": {"quotePolicyRef": ctx["quotePolicyRef"]},
        }

    monkeypatch.setattr(m, "_broker", broker)
    original = RuntimeError("synthetic stream failure")

    def provider(_request):
        def stream():
            yield "partial"
            raise original
        return stream()

    with scoped_execution_context(context()):
        stream = m.on_llm_execution(
            {"messages": [], "max_tokens": 4}, provider,
            provider="fixture-provider", model="fixture-model",
            api_request_id="stream-fails", streaming=True,
        )
        assert next(stream) == "partial"
        with pytest.raises(RuntimeError, match="synthetic stream failure") as caught:
            next(stream)
    assert caught.value is original
    assert [call["action"] for call in calls] == ["reserve", "finish"]
    assert calls[-1]["effectOutcome"] == "unknown"


def test_sync_stream_close_is_conservatively_unknown(monkeypatch):
    calls = []

    def broker(ctx, message):
        calls.append(message)
        if message["action"] == "reserve":
            return {"outcome": "completed", "quotePolicyRef": ctx["quotePolicyRef"]}
        assert message["effectOutcome"] == "unknown"
        return {
            "outcome": "completed",
            "snapshot": {"quotePolicyRef": ctx["quotePolicyRef"]},
        }

    monkeypatch.setattr(m, "_broker", broker)

    def provider(_request):
        def stream():
            yield "partial"
            yield "unread"
        return stream()

    with scoped_execution_context(context()):
        stream = m.on_llm_execution(
            {"messages": [], "max_tokens": 4}, provider,
            provider="fixture-provider", model="fixture-model",
            api_request_id="stream-closed", streaming=True,
        )
        assert next(stream) == "partial"
        stream.close()
    assert [call["action"] for call in calls] == ["reserve", "finish"]
    assert calls[-1]["effectOutcome"] == "unknown"


def test_streaming_flag_with_completed_response_settles_immediately(monkeypatch):
    calls = []

    def broker(ctx, message):
        calls.append(message)
        if message["action"] == "reserve":
            return {"outcome": "completed", "quotePolicyRef": ctx["quotePolicyRef"]}
        return {
            "outcome": "completed",
            "snapshot": {"quotePolicyRef": ctx["quotePolicyRef"]},
        }

    monkeypatch.setattr(m, "_broker", broker)
    response = object()
    with scoped_execution_context(context()):
        assert m.on_llm_execution(
            {"messages": [], "max_tokens": 4}, lambda _request: response,
            provider="fixture-provider", model="fixture-model",
            api_request_id="stream-completed-response", streaming=True,
        ) is response
    assert [call["action"] for call in calls] == ["reserve", "finish"]
    assert calls[-1]["effectOutcome"] == "completed"


def test_sync_downstream_failure_preserves_original_error_and_marks_unknown(monkeypatch):
    calls = []

    def broker(ctx, message):
        calls.append(message)
        if message["action"] == "reserve":
            return {"outcome": "completed", "quotePolicyRef": ctx["quotePolicyRef"]}
        assert message["action"] == "finish"
        assert message["effectOutcome"] == "unknown"
        return {
            "outcome": "completed",
            "snapshot": {"quotePolicyRef": ctx["quotePolicyRef"]},
        }

    monkeypatch.setattr(m, "_broker", broker)
    original = RuntimeError("synthetic provider failure")
    with scoped_execution_context(context()):
        with pytest.raises(RuntimeError, match="synthetic provider failure") as caught:
            m.on_llm_execution(
                {"messages": [], "max_tokens": 4},
                lambda _req: (_ for _ in ()).throw(original),
                provider="fixture-provider", model="fixture-model",
                api_request_id="provider-error",
            )
    assert caught.value is original
    assert [call["action"] for call in calls] == ["reserve", "finish"]



def test_tool_route_is_broker_verified_and_pinned_for_callback(monkeypatch):
    calls = []

    def broker(ctx, message):
        calls.append(message)
        if message["action"] == "reserve":
            return {
                "outcome": "completed",
                "quotePolicyRef": ctx["quotePolicyRef"],
            }
        return {
            "outcome": "completed",
            "snapshot": {"quotePolicyRef": ctx["quotePolicyRef"]},
        }

    monkeypatch.setattr(m, "_broker", broker)
    monkeypatch.setattr(m, "_tool_route", lambda _name: "fixture-search")

    from agent.execution_context import current_tool_route_pin

    def tool(args):
        assert current_tool_route_pin("web_search") == "fixture-search"
        return {"ok": args["query"]}

    with scoped_execution_context(context()):
        result = m.on_tool_execution(
            "web_search", {"query": "fixture"}, tool, tool_call_id="tool-pinned"
        )
    assert result == {"ok": "fixture"}
    assert calls[0]["effectMeta"] == {
        "tool": "web_search",
        "route": "fixture-search",
    }
    assert current_tool_route_pin("web_search") is None

def test_sync_tool_failure_preserves_original_error_and_marks_unknown(monkeypatch):
    calls = []

    def broker(ctx, message):
        calls.append(message)
        if message["action"] == "reserve":
            return {"outcome": "completed", "quotePolicyRef": ctx["quotePolicyRef"]}
        assert message["effectOutcome"] == "unknown"
        return {
            "outcome": "completed",
            "snapshot": {"quotePolicyRef": ctx["quotePolicyRef"]},
        }

    monkeypatch.setattr(m, "_broker", broker)
    monkeypatch.setattr(m, "_tool_route", lambda _name: "fixture-search")
    original = OSError("synthetic tool failure")
    with scoped_execution_context(context()):
        with pytest.raises(OSError, match="synthetic tool failure") as caught:
            m.on_tool_execution(
                "web_search", {"query": "fixture"},
                lambda _args: (_ for _ in ()).throw(original),
                tool_call_id="tool-error",
            )
    assert caught.value is original
    assert [call["action"] for call in calls] == ["reserve", "finish"]


def test_unconfirmed_finish_locally_halts_followup_effect(monkeypatch):
    calls = []

    def broker(ctx, message):
        calls.append(message["action"])
        if message["action"] == "reserve":
            return {"outcome": "completed"}
        raise m.NativeEffectBudgetMiddlewareError("native_effect_broker_unavailable")

    monkeypatch.setattr(m, "_broker", broker)
    ctx = context()
    with scoped_execution_context(ctx):
        with pytest.raises(ExecutionMiddlewareDenied, match="native_effect_broker_unavailable"):
            m.on_llm_execution(
                {"messages": [], "max_tokens": 4}, lambda _req: "done",
                provider="fixture-provider", model="fixture-model",
                api_request_id="first",
            )
        with pytest.raises(
            ExecutionMiddlewareDenied, match="native_effect_invocation_locally_halted"
        ):
            m.on_llm_execution(
                {"messages": [], "max_tokens": 4}, lambda _req: pytest.fail("must not run"),
                provider="fixture-provider", model="fixture-model",
                api_request_id="second",
            )
    assert calls == ["reserve", "finish"]
