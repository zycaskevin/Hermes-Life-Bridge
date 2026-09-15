from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json

import pytest

from hermes_life_bridge import codex_decision, plugin
from hermes_life_bridge.codex_decision import CodexDecisionError, CodexDecisionRouter
from hermes_life_bridge.config import BridgeConfig
from hermes_life_bridge.contact_model import DeliveryReceipt
from hermes_life_bridge.contact_store import ContactStore


def _cfg(tmp_path, *, enabled=True) -> BridgeConfig:
    return BridgeConfig(
        "did:x",
        "/tmp/runtime.sock",
        str(tmp_path / "trace.jsonl"),
        contact_db=str(tmp_path / "contact.sqlite3"),
        codex_decision_enabled=enabled,
        codex_decision_socket=str(tmp_path / "decision.sock"),
        codex_decision_timeout_seconds=1.0,
    )


def _delivered_work_contact(store: ContactStore, target: str, work_event_id: str) -> None:
    key = "contact:codex-owner-decision"
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    store.reserve(
        idempotency_key=key,
        request_hash="hash",
        metadata={
            "intent_id": "intent-codex",
            "target": target,
            "message_hash": hashlib.sha256(b"message").hexdigest(),
            "work_event_id": work_event_id,
        },
        created_at=now,
    )
    store.save_receipt(
        DeliveryReceipt(
            receipt_id="receipt-codex",
            intent_id="intent-codex",
            idempotency_key=key,
            life_did="did:x",
            target=target,
            status="delivered",
            message_hash=hashlib.sha256(b"message").hexdigest(),
            provider_message_id="provider-1",
            delivered_at=now,
        )
    )


def test_router_resolves_only_latest_delivered_work_event_for_exact_route(tmp_path, monkeypatch):
    store = ContactStore(str(tmp_path / "contact.sqlite3"))
    _delivered_work_contact(store, "telegram:chat-a", "codexevt:owner-approval")
    captured = {}

    def fake_send(path, *, work_event_id, decision, timeout_seconds):
        captured.update(
            path=path,
            work_event_id=work_event_id,
            decision=decision,
            timeout_seconds=timeout_seconds,
        )
        return {
            "schema_version": "clb-owner-decision-response.v0.1",
            "status": "accepted",
            "decision": decision,
        }

    monkeypatch.setattr(codex_decision, "_send_decision", fake_send)
    router = CodexDecisionRouter(_cfg(tmp_path), contact_store=store)
    result = router.resolve(target="telegram:chat-a", decision="accept")
    assert result == {"ok": True, "status": "accepted", "decision": "accept"}
    assert captured["work_event_id"] == "codexevt:owner-approval"
    assert captured["decision"] == "accept"

    with pytest.raises(CodexDecisionError, match="no_delivered_work_contact_for_route"):
        router.resolve(target="telegram:chat-b", decision="accept")


def test_router_translates_stale_event_to_no_live_approval(tmp_path, monkeypatch):
    store = ContactStore(str(tmp_path / "contact.sqlite3"))
    _delivered_work_contact(store, "telegram:chat-a", "codexevt:stale")
    monkeypatch.setattr(
        codex_decision,
        "_send_decision",
        lambda *a, **k: {
            "schema_version": "clb-owner-decision-response.v0.1",
            "status": "rejected",
            "error": "work_event_not_found",
        },
    )
    router = CodexDecisionRouter(_cfg(tmp_path), contact_store=store)
    with pytest.raises(
        CodexDecisionError,
        match="no_live_codex_approval_for_latest_work_contact",
    ):
        router.resolve(target="telegram:chat-a", decision="decline")


def test_plugin_handler_uses_current_session_route_not_model_supplied_event_id(monkeypatch, tmp_path):
    calls = []

    class Evidence:
        def record_gateway_event(self, *args, **kwargs):
            return None

    class Bridge:
        def gateway_message(self, *args, **kwargs):
            return None

    class Router:
        def resolve(self, *, target, decision):
            calls.append((target, decision))
            return {"ok": True, "status": "accepted", "decision": decision}

    class Event:
        source = {"platform": "telegram", "chat_id": "chat-private"}
        message_id = "m-1"
        chat_id = "chat-private"
        text = "yes"

    with plugin._SESSION_ROUTE_LOCK:
        plugin._SESSION_ROUTE_TARGETS.clear()
    monkeypatch.setattr(plugin, "_evidence_store", lambda: Evidence())
    monkeypatch.setattr(plugin, "_BRIDGE", Bridge())
    monkeypatch.setattr(plugin, "_work_producer", lambda: None)
    monkeypatch.setattr(plugin, "_interest_producer", lambda: None)
    monkeypatch.setattr(plugin, "_codex_decision_router", lambda: Router())

    assert plugin.on_pre_gateway_dispatch(Event(), session_id="session-current") == {
        "action": "allow"
    }
    result = json.loads(
        plugin.resolve_codex_approval_handler(
            {"decision": "accept"}, session_id="session-current"
        )
    )
    assert result["status"] == "accepted"
    assert calls == [("telegram:chat-private", "accept")]

    extra = json.loads(
        plugin.resolve_codex_approval_handler(
            {"decision": "accept", "work_event_id": "model-must-not-supply-this"},
            session_id="session-current",
        )
    )
    assert extra == {"error": "invalid_codex_decision"}


def test_plugin_handler_fails_closed_without_current_session_route(monkeypatch):
    with plugin._SESSION_ROUTE_LOCK:
        plugin._SESSION_ROUTE_TARGETS.clear()
    result = json.loads(
        plugin.resolve_codex_approval_handler({"decision": "accept"}, session_id="unknown")
    )
    assert result == {"error": "no_current_delivery_route"}
