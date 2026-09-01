from dataclasses import dataclass
from hermes_life_bridge.config import BridgeConfig
from hermes_life_bridge.normalize import normalize_gateway

@dataclass
class Event:
    text: str = "private user text"
    source: str = "telegram"
    message_id: str = "msg-123"

def test_gateway_normalization_uses_message_id_and_no_raw_text():
    cfg = BridgeConfig("did:example:life", "/tmp/runtime.sock", "/tmp/trace.jsonl")
    p = normalize_gateway(Event(), cfg, session_ref="chat-1")
    assert p.idempotency_key == "hermes:gateway:telegram:msg-123"
    assert p.content_fingerprint.startswith("sha256:")
    assert "private user text" not in str(p.to_dict())
    assert p.surface == "gateway"


def test_session_source_object_normalization():
    from enum import Enum
    from hermes_life_bridge.config import BridgeConfig
    from hermes_life_bridge.normalize import normalize_gateway
    class P(Enum): FEISHU="feishu"
    class S:
        platform=P.FEISHU; chat_id="oc_abc"; thread_id=None; message_id="om_abc"
        def to_dict(self): return {"platform":"feishu","chat_id":self.chat_id,"message_id":self.message_id}
    class E:
        text="x"; source=S(); message_id=""
    cfg=BridgeConfig("did:x","/tmp/r","/tmp/t")
    p=normalize_gateway(E(),cfg,session_ref="s")
    assert p.platform=="feishu"
    assert p.idempotency_key=="hermes:gateway:feishu:om_abc"
