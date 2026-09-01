from __future__ import annotations
import json, subprocess
from .config import BridgeConfig

class HermesSendError(RuntimeError): pass

class HermesSendClient:
    def __init__(self, config: BridgeConfig):
        self.config=config
        self.backend_calls=0

    def send(self, *, target:str, message:str) -> str:
        self.backend_calls += 1
        cmd=[self.config.hermes_cli_path, "send", "--to", target, "--json", message]
        try:
            p=subprocess.run(cmd, text=True, capture_output=True, timeout=self.config.contact_timeout_seconds)
        except Exception as exc:
            raise HermesSendError(f"{type(exc).__name__}:{exc}") from exc
        if p.returncode != 0:
            detail=(p.stderr or p.stdout or "")[:500]
            raise HermesSendError(f"exit_{p.returncode}:{detail}")
        raw=(p.stdout or "").strip()
        if not raw:
            return ""
        try:
            data=json.loads(raw)
            return str(data.get("message_id") or data.get("id") or "")
        except Exception:
            return ""
