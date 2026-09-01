import json, socket, threading
from dataclasses import dataclass
from hermes_life_bridge.bridge import HermesLifeBridge
from hermes_life_bridge.config import BridgeConfig
from hermes_life_bridge.trace import BridgeTracer

@dataclass
class Event:
    text: str = "do not persist me"
    source: str = "telegram"
    message_id: str = "msg-e2e"

def test_gateway_bridge_reaches_runtime_and_traces_stages(tmp_path):
    sock = str(tmp_path / "runtime.sock")
    trace = str(tmp_path / "trace.jsonl")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock); server.listen(1)
    received = {}

    def worker():
        conn, _ = server.accept()
        raw = conn.recv(8192)
        received.update(json.loads(raw.decode().strip()))
        conn.sendall((json.dumps({
            "ok": True,
            "duplicate": False,
            "persisted": True,
            "state_sequence": 3,
            "state_hash": "hash3",
            "decision": {"outcome":"defer"},
        }) + "\n").encode())
        conn.close(); server.close()

    th = threading.Thread(target=worker); th.start()
    bridge = HermesLifeBridge(BridgeConfig("did:example:life", sock, trace, 1, 1))
    receipt = bridge.gateway_message(Event(), session_ref="chat-1")
    th.join()

    assert receipt.ok is True
    assert received["idempotency_key"] == "hermes:gateway:telegram:msg-e2e"
    assert "do not persist me" not in json.dumps(received)

    rows = BridgeTracer(trace).tail(20)
    stages = [r["stage"] for r in rows]
    for stage in ("HOOK_RECEIVED","NORMALIZED","DEDUPE_CHECK","SOCKET_CONNECT","EVENT_SENT","RUNTIME_ACK","STATE_ADVANCED"):
        assert stage in stages
    assert "do not persist me" not in open(trace).read()
