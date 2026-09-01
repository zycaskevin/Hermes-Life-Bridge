from hermes_life_bridge import plugin

class Ctx:
    def __init__(self): self.hooks = {}
    def register_hook(self, name, cb): self.hooks[name] = cb

def test_registers_dual_hooks():
    ctx = Ctx()
    plugin.register(ctx)
    assert set(ctx.hooks) == {"pre_gateway_dispatch", "pre_llm_call"}

def test_gateway_hook_always_allows(monkeypatch):
    class DummyBridge:
        def gateway_message(self, *a, **k): raise RuntimeError("bridge failure")
    monkeypatch.setattr(plugin, "_BRIDGE", DummyBridge())
    class E:
        source="telegram"; message_id="m1"; chat_id="c1"
    assert plugin.on_pre_gateway_dispatch(E()) == {"action":"allow"}

def test_pre_llm_call_skips_gateway_platform(monkeypatch):
    called = []
    class DummyBridge:
        def cli_turn(self, **kwargs): called.append(kwargs)
    monkeypatch.setattr(plugin, "_BRIDGE", DummyBridge())
    plugin.on_pre_llm_call(session_id="s", user_message="x", platform="telegram", turn_id="t")
    assert called == []
