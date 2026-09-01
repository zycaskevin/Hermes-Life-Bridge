from hermes_life_bridge.config import BridgeConfig
from hermes_life_bridge.normalize import normalize_cli

def test_cli_idempotency_uses_session_and_turn():
    cfg = BridgeConfig("did:example:life", "/tmp/runtime.sock", "/tmp/trace.jsonl")
    p = normalize_cli(session_id="s1", turn_id="t1", user_message="hello", config=cfg)
    assert p.idempotency_key == "hermes:cli:s1:t1"
    assert p.surface == "cli"
