from hermes_life_bridge import plugin
from hermes_life_bridge.config import BridgeConfig
from hermes_life_bridge.codex_decision import CODEX_DECISION_TOOL_NAME
from hermes_life_bridge.work_producer import REPORT_TOOL_NAME


class Ctx:
    def __init__(self):
        self.hooks = {}
        self.tools = {}

    def register_hook(self, name, cb):
        self.hooks[name] = cb

    def register_tool(self, name, **kwargs):
        self.tools[name] = kwargs


def _config(
    work_producer_enabled: bool = False,
    codex_decision_enabled: bool = False,
) -> BridgeConfig:
    return BridgeConfig(
        life_did="did:example:life",
        runtime_socket="/tmp/runtime.sock",
        trace_path="/tmp/trace.jsonl",
        work_producer_enabled=work_producer_enabled,
        codex_decision_enabled=codex_decision_enabled,
    )


def test_registers_stable_work_contract_but_hides_tool_when_disabled(monkeypatch):
    monkeypatch.setattr(plugin.BridgeConfig, "from_env", staticmethod(lambda: _config(False)))
    ctx = Ctx()
    plugin.register(ctx)
    assert set(ctx.hooks) == {
        "pre_gateway_dispatch",
        "pre_llm_call",
        "post_tool_call",
    }
    assert set(ctx.tools) == {REPORT_TOOL_NAME, CODEX_DECISION_TOOL_NAME}
    assert ctx.tools[REPORT_TOOL_NAME]["check_fn"]() is False
    assert ctx.tools[CODEX_DECISION_TOOL_NAME]["check_fn"]() is False


def test_registers_work_observer_and_report_tool_when_enabled(monkeypatch):
    monkeypatch.setattr(plugin.BridgeConfig, "from_env", staticmethod(lambda: _config(True)))
    ctx = Ctx()
    plugin.register(ctx)
    assert set(ctx.hooks) == {
        "pre_gateway_dispatch",
        "pre_llm_call",
        "post_tool_call",
    }
    assert set(ctx.tools) == {REPORT_TOOL_NAME, CODEX_DECISION_TOOL_NAME}
    assert ctx.tools[REPORT_TOOL_NAME]["toolset"] == "safe"
    assert ctx.tools[REPORT_TOOL_NAME]["schema"]["function"]["name"] == REPORT_TOOL_NAME
    assert ctx.tools[REPORT_TOOL_NAME]["check_fn"]() is True


def test_registers_codex_decision_tool_only_when_enabled(monkeypatch):
    monkeypatch.setattr(
        plugin.BridgeConfig,
        "from_env",
        staticmethod(lambda: _config(False, True)),
    )
    ctx = Ctx()
    plugin.register(ctx)
    tool = ctx.tools[CODEX_DECISION_TOOL_NAME]
    assert tool["toolset"] == "safe"
    assert tool["schema"]["function"]["name"] == CODEX_DECISION_TOOL_NAME
    assert set(tool["schema"]["function"]["parameters"]["properties"]) == {"decision"}
    assert tool["check_fn"]() is True
    assert ctx.tools[REPORT_TOOL_NAME]["check_fn"]() is False


def test_gateway_hook_always_allows(monkeypatch):
    class DummyBridge:
        def gateway_message(self, *a, **k):
            raise RuntimeError("bridge failure")

    monkeypatch.setattr(plugin, "_BRIDGE", DummyBridge())
    monkeypatch.setattr(plugin, "_work_producer", lambda: None)

    class E:
        source = "telegram"
        message_id = "m1"
        chat_id = "c1"

    assert plugin.on_pre_gateway_dispatch(E()) == {"action": "allow"}


def test_gateway_hook_emits_context_activity_without_exposing_route_to_producer(monkeypatch):
    seen = []

    class DummyProducer:
        def note_context_activity(self, session_ref):
            seen.append(session_ref)

    class DummyBridge:
        def gateway_message(self, *a, **k):
            return None

    monkeypatch.setattr(plugin, "_work_producer", lambda: DummyProducer())
    monkeypatch.setattr(plugin, "_BRIDGE", DummyBridge())

    class E:
        source = "telegram"
        message_id = "m1"
        chat_id = "c1"

    assert plugin.on_pre_gateway_dispatch(E(), session_id="session-opaque") == {
        "action": "allow"
    }
    assert seen == ["session-opaque"]


def test_gateway_hook_refreshes_recent_interest_without_persisting_or_rewriting_turn(monkeypatch):
    seen = []

    class DummyInterestProducer:
        def observe_owner_discussion(self, text, **kwargs):
            seen.append((text, kwargs))

    class DummyBridge:
        def gateway_message(self, *a, **k):
            return None

    monkeypatch.setattr(plugin, "_work_producer", lambda: None)
    monkeypatch.setattr(plugin, "_interest_producer", lambda: DummyInterestProducer())
    monkeypatch.setattr(plugin, "_BRIDGE", DummyBridge())

    class E:
        source = "feishu"
        message_id = "m-interest-1"
        chat_id = "c1"
        text = "我们继续聊 Agent Runtime"

    assert plugin.on_pre_gateway_dispatch(E()) == {"action": "allow"}
    assert seen == [
        ("我们继续聊 Agent Runtime", {"event_ref": "m-interest-1"})
    ]


def test_cli_turn_refreshes_recent_interest(monkeypatch):
    seen = []

    class DummyInterestProducer:
        def observe_owner_discussion(self, text, **kwargs):
            seen.append((text, kwargs))

    class DummyBridge:
        def cli_turn(self, **kwargs):
            return None

    monkeypatch.setattr(plugin, "_work_producer", lambda: None)
    monkeypatch.setattr(plugin, "_interest_producer", lambda: DummyInterestProducer())
    monkeypatch.setattr(plugin, "_BRIDGE", DummyBridge())
    plugin.on_pre_llm_call(
        session_id="s",
        user_message="persistent agent 最近如何",
        platform="cli",
        turn_id="t-interest",
    )
    assert seen == [
        ("persistent agent 最近如何", {"event_ref": "t-interest"})
    ]


def test_pre_llm_call_skips_gateway_platform(monkeypatch):
    called = []

    class DummyBridge:
        def cli_turn(self, **kwargs):
            called.append(kwargs)

    monkeypatch.setattr(plugin, "_BRIDGE", DummyBridge())
    plugin.on_pre_llm_call(
        session_id="s", user_message="x", platform="telegram", turn_id="t"
    )
    assert called == []


def test_post_tool_hook_discards_raw_args_and_result_for_ordinary_tools(monkeypatch):
    calls = []

    class DummyProducer:
        def observe_post_tool_call(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(plugin, "_work_producer", lambda: DummyProducer())
    plugin.on_post_tool_call(
        tool_name="terminal",
        args={"command": "PRIVATE COMMAND"},
        result="PRIVATE RAW TOOL OUTPUT",
        session_id="s",
        turn_id="t",
        tool_call_id="c",
        status="ok",
    )
    assert len(calls) == 1
    assert calls[0]["tool_name"] == "terminal"
    assert calls[0]["args"] is None
    assert "result" not in calls[0]


def test_post_tool_hook_passes_only_declaration_args_for_report_tool(monkeypatch):
    calls = []

    class DummyProducer:
        def observe_post_tool_call(self, **kwargs):
            calls.append(kwargs)

    declaration = {
        "work_id": "work:1",
        "event_type": "verified_completion",
        "label": "完成",
        "summary": "工作已完成",
    }
    monkeypatch.setattr(plugin, "_work_producer", lambda: DummyProducer())
    plugin.on_post_tool_call(
        tool_name=REPORT_TOOL_NAME,
        args=declaration,
        result='{"ok":true}',
        session_id="s",
        turn_id="t",
        tool_call_id="c",
        status="ok",
    )
    assert calls[0]["args"] == declaration
