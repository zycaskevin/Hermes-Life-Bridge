from __future__ import annotations

from pathlib import Path

import pytest

from hermes_life_bridge.config import BridgeConfig
from hermes_life_bridge.interest_producer import (
    AmbientInterestCredentials,
    AmbientInterestClient,
    HermesInterestProducer,
    InterestProducerError,
)


def config(tmp_path: Path, **changes: object) -> BridgeConfig:
    values: dict[str, object] = {
        "life_did": "did:example:life",
        "runtime_socket": str(tmp_path / "runtime.sock"),
        "trace_path": str(tmp_path / "trace.jsonl"),
        "ambient_interest_enabled": True,
        "ambient_interest_endpoint": "http://127.0.0.1:8794",
        "ambient_interest_runtime_id": "nancy-ambient-canary",
        "ambient_interest_credentials_file": str(tmp_path / "ambient.token"),
        "ambient_interest_timeout_seconds": 0.5,
    }
    values.update(changes)
    return BridgeConfig(**values)


class FakeClient:
    def __init__(self):
        self.signals: list[dict] = []

    def post_signal(self, signal: dict) -> dict:
        self.signals.append(dict(signal))
        return {
            "signal_id": signal["signal_id"],
            "matched_watch_ids": ["watch:agent-runtime"],
            "reactivated_watch_ids": ["watch:agent-runtime"],
        }


def test_owner_discussion_emits_bounded_normalized_interest_signal_without_persistence(tmp_path: Path) -> None:
    client = FakeClient()
    producer = HermesInterestProducer(config(tmp_path), client=client)
    result = producer.observe_owner_discussion(
        "  我最近又想继续研究   Agent Runtime 和 persistent agent   ",
        event_ref="message-1",
        observed_at="2026-09-15T00:00:00Z",
    )
    assert result is not None
    assert result["reactivated_watch_ids"] == ["watch:agent-runtime"]
    assert len(client.signals) == 1
    signal = client.signals[0]
    assert signal["runtime_id"] == "nancy-ambient-canary"
    assert signal["life_did"] == "did:example:life"
    assert signal["source"] == "owner_discussion"
    assert signal["strength"] == 0.9
    assert signal["subjects"] == ["我最近又想继续研究 Agent Runtime 和 persistent agent"]
    assert signal["signal_id"].startswith("interest:hermes:")
    assert signal["provenance"] == {
        "origin": "REAL",
        "actor_kind": "human",
        "source_ref": "message-1",
    }


def test_empty_owner_message_emits_no_interest_signal(tmp_path: Path) -> None:
    client = FakeClient()
    producer = HermesInterestProducer(config(tmp_path), client=client)
    assert producer.observe_owner_discussion("   ", event_ref="message-2") is None
    assert client.signals == []


def test_interest_credentials_require_owner_only_file(tmp_path: Path) -> None:
    token = tmp_path / "ambient.token"
    token.write_text("owner-private-runtime-token-0123456789", encoding="utf-8")
    token.chmod(0o600)
    loaded = AmbientInterestCredentials.from_file(str(token))
    assert "runtime-token" not in repr(loaded)

    token.chmod(0o640)
    with pytest.raises(InterestProducerError, match="owner_only"):
        AmbientInterestCredentials.from_file(str(token))


def test_interest_credentials_reuse_owner_private_work_producer_runtime_bearer(tmp_path: Path) -> None:
    credentials = tmp_path / "producer.json"
    credentials.write_text(
        '{"schema_version":"work-producer-credentials.v0.1",'
        '"principal_id":"hermes:nancy:work",'
        '"runtime_bearer":"shared-runtime-bearer-0123456789",'
        '"work_bearer":"work-only-bearer-0123456789"}',
        encoding="utf-8",
    )
    credentials.chmod(0o600)
    loaded = AmbientInterestCredentials.from_file(str(credentials))
    assert loaded.bearer == "shared-runtime-bearer-0123456789"
    assert "shared-runtime-bearer" not in repr(loaded)


def test_interest_client_rejects_non_loopback_plain_http(tmp_path: Path) -> None:
    credentials = AmbientInterestCredentials("owner-private-runtime-token-0123456789")
    with pytest.raises(InterestProducerError, match="loopback"):
        AmbientInterestClient("http://example.com:8794", credentials)
