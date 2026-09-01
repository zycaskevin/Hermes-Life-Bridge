import json, socket, threading
from dataclasses import dataclass
from hermes_life_bridge.bridge import HermesLifeBridge
from hermes_life_bridge.config import BridgeConfig
from hermes_life_bridge.trace import BridgeTracer

@dataclass
class Event:
    text: str = "private"
    source: str = "telegram"
    message_id: str = "safe-stop-message"

def test_safestop_ack_is_not_reported_as_state_advanced(tmp_path):
    sock = str(tmp_path / "runtime.sock")
    trace = str(tmp_path / "trace.jsonl")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock); server.listen(1)
    def worker():
        conn, _ = server.accept(); conn.recv(8192)
        conn.sendall((json.dumps({
            "ok": True, "duplicate": False, "persisted": False,
            "state_sequence": 8, "state_hash": "persisted-hash",
            "decision": {"outcome": "safe_stop"},
        }) + "\n").encode())
        conn.close(); server.close()
    th=threading.Thread(target=worker); th.start()
    bridge=HermesLifeBridge(BridgeConfig("did:example:life", sock, trace, 1, 1))
    receipt=bridge.gateway_message(Event(), session_ref="chat")
    th.join()
    assert receipt.persisted is False
    rows=BridgeTracer(trace).tail(20)
    stages=[r["stage"] for r in rows]
    assert "STATE_ADVANCED" not in stages
    assert "STATE_NOT_ADVANCED" in stages
    last=[r for r in rows if r["stage"]=="STATE_NOT_ADVANCED"][-1]
    assert last["status"] == "fail"
