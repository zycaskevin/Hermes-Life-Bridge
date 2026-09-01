from __future__ import annotations
import json
import socket
from .config import BridgeConfig
from .model import RuntimeReceipt

class UnixSocketTransport:
    def __init__(self, config: BridgeConfig):
        self.config = config

    def send_percept(self, event: dict) -> RuntimeReceipt:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(self.config.connect_timeout_seconds)
            s.connect(self.config.runtime_socket)
            s.settimeout(self.config.ack_timeout_seconds)
            s.sendall((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
            data = b""
            while not data.endswith(b"\n"):
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        if not data:
            return RuntimeReceipt(ok=False, error="empty_runtime_ack")
        try:
            payload = json.loads(data.decode("utf-8"))
        except Exception as exc:
            return RuntimeReceipt(ok=False, error=f"invalid_runtime_ack:{type(exc).__name__}")
        return RuntimeReceipt.from_dict(payload)
