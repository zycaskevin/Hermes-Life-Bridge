from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import stat

import pytest

from hermes_life_bridge.config import BridgeConfig
from hermes_life_bridge.contact_model import ContactDecisionEnvelope, ContactIntentEnvelope
from hermes_life_bridge.contact_service import ContactRouteUnavailable, ContactService
from hermes_life_bridge.reliability_contract import RouteStatus
from hermes_life_bridge.routing import HermesRoute, RouteStore, route_status


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def intent(target: str = "auto") -> ContactIntentEnvelope:
    now = datetime.now(timezone.utc)
    message = "route lifecycle"
    return ContactIntentEnvelope(
        "route-intent",
        "contact:route-lifecycle",
        "did:x",
        1,
        "h",
        "e",
        "c",
        target,
        message,
        hashlib.sha256(message.encode()).hexdigest(),
        .8,
        .8,
        ["ev"],
        iso(now),
        iso(now + timedelta(minutes=5)),
    )


def decision() -> ContactDecisionEnvelope:
    return ContactDecisionEnvelope(
        "route-decision",
        "route-intent",
        "contact",
        .8,
        0,
        ["ok"],
        iso(datetime.now(timezone.utc)),
    )


class Sender:
    def __init__(self):
        self.calls = 0
        self.targets = []

    def send(self, *, target: str, message: str):
        self.calls += 1
        self.targets.append(target)
        return "provider-route"


def cfg(tmp_path, *, static_target: str = "feishu") -> BridgeConfig:
    return BridgeConfig(
        "did:x",
        "/tmp/runtime.sock",
        str(tmp_path / "trace.jsonl"),
        contact_db=str(tmp_path / "contact.sqlite3"),
        operation_db=str(tmp_path / "operations.sqlite3"),
        contact_delivery_enabled=True,
        contact_target=static_target,
        route_path=str(tmp_path / "route.json"),
        route_max_age_seconds=60,
    )


def test_route_status_unknown_without_route():
    assert route_status(None, max_age_seconds=60) is RouteStatus.UNKNOWN


def test_route_status_fresh_and_private_mode(tmp_path):
    store = RouteStore(str(tmp_path / "route.json"))
    store.save(HermesRoute("feishu", "oc_PRIVATE"))
    data = store.load()
    assert route_status(data, max_age_seconds=60) is RouteStatus.FRESH
    assert stat.S_IMODE((tmp_path / "route.json").stat().st_mode) == 0o600


def test_route_status_stale_after_ttl(tmp_path):
    now = datetime.now(timezone.utc)
    data = {
        "platform": "feishu",
        "chat_id": "oc_PRIVATE",
        "target": "feishu:oc_PRIVATE",
        "updated_at": iso(now - timedelta(seconds=61)),
        "valid": True,
    }
    assert route_status(data, max_age_seconds=60, now=now) is RouteStatus.STALE


def test_route_status_invalid_after_explicit_invalidation(tmp_path):
    store = RouteStore(str(tmp_path / "route.json"))
    store.save(HermesRoute("telegram", "chat-private"))
    store.invalidate()
    assert route_status(store.load(), max_age_seconds=60) is RouteStatus.INVALID


def test_new_gateway_route_replaces_invalid_route(tmp_path):
    store = RouteStore(str(tmp_path / "route.json"))
    store.save(HermesRoute("telegram", "old-chat"))
    store.invalidate()
    store.save(HermesRoute("telegram", "new-chat"))
    loaded = store.load()
    assert route_status(loaded, max_age_seconds=60) is RouteStatus.FRESH
    assert loaded["chat_id"] == "new-chat"
    assert loaded["valid"] is True
    assert "invalidated_at" not in loaded


def test_contact_uses_fresh_learned_route_for_auto_target(tmp_path):
    config = cfg(tmp_path)
    RouteStore(config.route_path).save(HermesRoute("feishu", "oc_PRIVATE"))
    sender = Sender()
    receipt = ContactService(config, sender).process(intent("auto"), decision())
    assert receipt.status == "delivered"
    assert receipt.target == "feishu:oc_PRIVATE"
    assert sender.targets == ["feishu:oc_PRIVATE"]


def test_stale_learned_route_blocks_contact_even_with_static_fallback(tmp_path):
    config = cfg(tmp_path, static_target="feishu:manual-fallback")
    path = tmp_path / "route.json"
    stale = {
        "platform": "feishu",
        "chat_id": "oc_OLD",
        "thread_id": "",
        "message_id": "",
        "target": "feishu:oc_OLD",
        "updated_at": iso(datetime.now(timezone.utc) - timedelta(days=1)),
        "valid": True,
    }
    path.write_text(json.dumps(stale), encoding="utf-8")
    path.chmod(0o600)
    sender = Sender()
    with pytest.raises(ContactRouteUnavailable, match="contact_route_stale"):
        ContactService(config, sender).process(intent("auto"), decision())
    assert sender.calls == 0


def test_invalid_route_blocks_contact(tmp_path):
    config = cfg(tmp_path)
    store = RouteStore(config.route_path)
    store.save(HermesRoute("feishu", "oc_PRIVATE"))
    store.invalidate()
    sender = Sender()
    with pytest.raises(ContactRouteUnavailable, match="contact_route_invalid"):
        ContactService(config, sender).process(intent("auto"), decision())
    assert sender.calls == 0


def test_no_learned_route_allows_explicit_configured_target(tmp_path):
    config = cfg(tmp_path, static_target="feishu:configured")
    sender = Sender()
    receipt = ContactService(config, sender).process(
        intent("feishu:configured"),
        decision(),
    )
    assert receipt.status == "delivered"
    assert sender.targets == ["feishu:configured"]


def test_platform_mismatch_between_learned_route_and_allowlist_blocks(tmp_path):
    config = cfg(tmp_path, static_target="telegram")
    RouteStore(config.route_path).save(HermesRoute("feishu", "oc_PRIVATE"))
    sender = Sender()
    with pytest.raises(ValueError, match="target_not_allowlisted"):
        ContactService(config, sender).process(intent("auto"), decision())
    assert sender.calls == 0
