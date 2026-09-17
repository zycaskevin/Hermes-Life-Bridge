from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from .config import BridgeConfig


_TRANSPORT_START_COMMAND = re.compile(r"^/start(?:@[A-Za-z0-9_]+)?(?:\s+\S.*)?$")


class InterestProducerError(RuntimeError):
    pass


@dataclass(frozen=True, repr=False)
class AmbientInterestCredentials:
    bearer: str

    @classmethod
    def from_file(cls, path: str) -> "AmbientInterestCredentials":
        target = Path(path)
        stat = target.stat()
        if not target.is_file():
            raise InterestProducerError("ambient_interest_credentials_not_file")
        if stat.st_mode & 0o077:
            raise InterestProducerError("ambient_interest_credentials_not_owner_only")
        if hasattr(os, "getuid") and stat.st_uid != os.getuid():
            raise InterestProducerError("ambient_interest_credentials_wrong_owner")
        value = target.read_text(encoding="utf-8").strip()
        try:
            parsed = json.loads(value)
        except Exception:
            parsed = None
        if isinstance(parsed, dict) and isinstance(parsed.get("runtime_bearer"), str):
            value = parsed["runtime_bearer"].strip()
        if len(value) < 16 or len(value) > 4096:
            raise InterestProducerError("ambient_interest_credentials_invalid")
        return cls(bearer=value)


class AmbientInterestClient:
    def __init__(
        self,
        endpoint: str,
        credentials: AmbientInterestCredentials,
        *,
        timeout_seconds: float = 0.75,
    ) -> None:
        self.endpoint = _validate_endpoint(endpoint)
        self.credentials = credentials
        self.timeout_seconds = timeout_seconds

    def post_signal(self, signal: dict) -> dict:
        body = json.dumps(signal, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) > 16_384:
            raise InterestProducerError("ambient_interest_signal_too_large")
        request = urlrequest.Request(
            f"{self.endpoint}/v1/ambient/interest-signals",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.credentials.bearer}",
            },
        )
        try:
            with urlrequest.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = response.read(65_537)
                if len(payload) > 65_536:
                    raise InterestProducerError("ambient_interest_response_too_large")
                status = int(getattr(response, "status", 0))
        except urlerror.HTTPError as exc:
            raise InterestProducerError(f"ambient_interest_http_{exc.code}") from exc
        except Exception as exc:
            raise InterestProducerError("ambient_interest_transport_failed") from exc
        if status < 200 or status >= 300:
            raise InterestProducerError(f"ambient_interest_http_{status}")
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except Exception as exc:
            raise InterestProducerError("ambient_interest_response_invalid") from exc
        if not isinstance(parsed, dict) or parsed.get("ok") is not True:
            raise InterestProducerError("ambient_interest_response_rejected")
        result = parsed.get("result")
        return result if isinstance(result, dict) else {}


class HermesInterestProducer:
    """
    Best-effort recent-interest refresh adapter.

    Raw owner text is bounded and sent only over the configured local HTTPS/
    loopback boundary. It is never written to HLB trace/operation stores. Life
    Runtime uses it only for transient subject matching against existing Watches.
    """

    def __init__(self, config: BridgeConfig, *, client: AmbientInterestClient | None = None) -> None:
        self.config = config
        if client is not None:
            self.client = client
        else:
            credentials = AmbientInterestCredentials.from_file(
                config.ambient_interest_credentials_file
            )
            self.client = AmbientInterestClient(
                config.ambient_interest_endpoint,
                credentials,
                timeout_seconds=config.ambient_interest_timeout_seconds,
            )

    def observe_owner_discussion(
        self,
        text: str,
        *,
        event_ref: str = "",
        observed_at: str | None = None,
    ) -> dict | None:
        bounded = _bounded_text(text)
        if not bounded:
            return None
        at = observed_at or _now()
        signal_id = _signal_id(event_ref, bounded, at)
        return self.client.post_signal(
            {
                "signal_id": signal_id,
                "runtime_id": self.config.ambient_interest_runtime_id,
                "life_did": self.config.life_did,
                "observed_at": at,
                "source": "owner_discussion",
                "strength": 0.9,
                "subjects": [bounded],
                "provenance": {
                    "origin": "REAL",
                    "actor_kind": "human",
                    "source_ref": (event_ref or signal_id)[:1024],
                },
            }
        )


def create_interest_producer(config: BridgeConfig) -> HermesInterestProducer:
    return HermesInterestProducer(config)


def _bounded_text(value: str) -> str:
    # Enough context for subject matching without turning the interest boundary
    # into a second raw-conversation store or unlimited payload path.
    normalized = " ".join(str(value or "").split())
    # Telegram /start is transport activation, not evidence of owner interest.
    # Suppress it before Life Runtime can create a Candidate Watch. DLMF has an
    # independent transient-source gate for the same platform control command.
    if _TRANSPORT_START_COMMAND.fullmatch(normalized):
        return ""
    return normalized[:1024]


def _signal_id(event_ref: str, text: str, observed_at: str) -> str:
    digest = hashlib.sha256(
        f"{event_ref}\0{text}\0{observed_at}".encode("utf-8")
    ).hexdigest()[:32]
    return f"interest:hermes:{digest}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_endpoint(value: str) -> str:
    parsed = urlparse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.port is None:
        raise InterestProducerError("ambient_interest_endpoint_invalid")
    if parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise InterestProducerError("ambient_interest_endpoint_must_be_origin_only")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise InterestProducerError("ambient_interest_http_endpoint_must_be_loopback")
    return value.rstrip("/")
