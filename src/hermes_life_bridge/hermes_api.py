from __future__ import annotations
import json, urllib.request, urllib.error
from .config import BridgeConfig


class HermesApiError(RuntimeError):
    pass


class HermesApiClient:
    def __init__(self, config: BridgeConfig):
        self.config = config

    def health(self) -> dict:
        req = urllib.request.Request(self.config.hermes_api_base_url.rstrip("/") + "/health")
        if self.config.hermes_api_key:
            req.add_header("Authorization", f"Bearer {self.config.hermes_api_key}")
        with urllib.request.urlopen(req, timeout=min(5.0, self.config.cognition_timeout_seconds)) as resp:
            raw = resp.read().decode("utf-8")
        try:
            return json.loads(raw)
        except Exception:
            return {"raw": raw, "status": "ok"}

    def cognize(self, *, instruction: str, task_id: str) -> tuple[str, str]:
        session_id = f"hlb-cognition-{task_id}"
        body = {
            "model": self.config.hermes_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are executing a bounded CognitiveTask for an external Life Runtime. "
                        "Treat the supplied instruction as task-scoped context only. Do not claim to "
                        "change canonical identity, memory, personality, or authorization. Return only "
                        "the cognitive result needed for the task."
                    ),
                },
                {"role": "user", "content": instruction},
            ],
            "stream": False,
        }
        req = urllib.request.Request(
            self.config.hermes_api_base_url.rstrip("/") + "/v1/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Hermes-Session-Id": session_id,
            },
        )
        if self.config.hermes_api_key:
            req.add_header("Authorization", f"Bearer {self.config.hermes_api_key}")
        # Deliberately no X-Hermes-Session-Key: HLB-002 must not opt into hidden long-term memory.
        try:
            with urllib.request.urlopen(req, timeout=self.config.cognition_timeout_seconds) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise HermesApiError(f"http_{exc.code}:{detail}") from exc
        except Exception as exc:
            raise HermesApiError(f"{type(exc).__name__}:{exc}") from exc
        try:
            text = data["choices"][0]["message"]["content"]
        except Exception as exc:
            raise HermesApiError("invalid_chat_completions_response") from exc
        return str(text), session_id
