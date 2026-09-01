import json, socket, threading
from hermes_life_bridge.config import BridgeConfig
from hermes_life_bridge.transport import UnixSocketTransport

def test_socket_transport_parses_runtime_ack(tmp_path):
    path = str(tmp_path / "runtime.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path); server.listen(1)

    def worker():
        conn, _ = server.accept()
        conn.recv(4096)
        conn.sendall((json.dumps({
            "ok": True,
            "duplicate": False,
            "persisted": True,
            "state_sequence": 3,
            "state_hash": "abc",
            "decision": {"outcome":"defer"},
        }) + "\n").encode())
        conn.close(); server.close()

    th = threading.Thread(target=worker); th.start()
    cfg = BridgeConfig("did:example:life", path, str(tmp_path/"trace.jsonl"), 1, 1)
    receipt = UnixSocketTransport(cfg).send_percept({"x":1})
    th.join()
    assert receipt.ok is True
    assert receipt.state_sequence == 3
    assert receipt.persisted is True
    assert receipt.decision_outcome == "defer"
