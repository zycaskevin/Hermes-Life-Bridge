from __future__ import annotations

import json
import subprocess

from .config import BridgeConfig


class HermesSendError(RuntimeError):
    pass


class HermesSendFailedSafe(HermesSendError):
    """The child/provider invocation is known not to have started."""


class HermesSendOutcomeUnknown(HermesSendError):
    """Hermes/provider may have accepted the message; never blind retry."""


class HermesSendClient:
    def __init__(self, config: BridgeConfig):
        self.config = config
        self.backend_calls = 0

    def send(self, *, target: str, message: str) -> str:
        self.backend_calls += 1
        cmd = [self.config.hermes_cli_path, "send", "--to", target, "--json", message]
        try:
            process = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                timeout=self.config.contact_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise HermesSendOutcomeUnknown("hermes_send_timeout_after_invoke") from exc
        except OSError as exc:
            # Popen/exec could not be established, so provider invocation did not start.
            raise HermesSendFailedSafe(f"hermes_send_spawn_failed:{type(exc).__name__}") from exc
        except Exception as exc:
            # Unknown subprocess failure is conservative: side effect may have happened.
            raise HermesSendOutcomeUnknown(
                f"hermes_send_unknown_exception:{type(exc).__name__}"
            ) from exc

        if process.returncode != 0:
            # Once Hermes executed, a non-zero exit does not prove the provider did not
            # accept a message. Do not infer safety from stderr text.
            raise HermesSendOutcomeUnknown(f"hermes_send_exit_{process.returncode}")

        raw = (process.stdout or "").strip()
        if not raw:
            return ""
        try:
            data = json.loads(raw)
            return str(data.get("message_id") or data.get("id") or "")
        except Exception:
            # Successful Hermes exit remains authoritative acceptance even if an older
            # Hermes version emitted non-JSON output and no provider id can be parsed.
            return ""
