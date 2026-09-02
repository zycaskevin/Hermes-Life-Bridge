from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import json
import subprocess

import pytest

from hermes_life_bridge.config import BridgeConfig
from hermes_life_bridge.contact_delivery import (
    HermesSendClient,
    HermesSendFailedSafe,
    HermesSendOutcomeUnknown,
)
from hermes_life_bridge.contact_model import (
    ContactDecisionEnvelope,
    ContactIntentEnvelope,
    DeliveryReceipt,
)
from hermes_life_bridge.contact_reconciliation import (
    ContactEvidence,
    ContactEvidenceOutcome,
    ContactReconciler,
)
from hermes_life_bridge.contact_service import (
    _request_hash,
    ContactDeliveryUnknown,
    ContactRetryDeferred,
    ContactService,
)
from hermes_life_bridge.contact_store import ContactStore
from hermes_life_bridge.operation_store import OperationStore
from hermes_life_bridge.reliability_contract import (
    BridgeOperation,
    DeliveryOutcome,
    OperationState,
    RetryClass,
)
from hermes_life_bridge.correlation import stable_id


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def make_intent(
    *,
    intent_id: str = "i-v0044",
    idempotency_key: str = "contact:v0044",
    target: str = "feishu:oc_PRIVATE_CHAT",
    message: str = "PRIVATE BODY V0044",
) -> ContactIntentEnvelope:
    now = datetime.now(timezone.utc)
    return ContactIntentEnvelope(
        intent_id=intent_id,
        idempotency_key=idempotency_key,
        life_did="did:x",
        basis_state_sequence=1,
        basis_state_hash="h",
        source_event_id="e",
        cognitive_receipt_id="c",
        target=target,
        message_text=message,
        message_hash=hashlib.sha256(message.encode()).hexdigest(),
        utility=.8,
        urgency=.8,
        evidence_refs=["cognitive:c"],
        created_at=iso(now),
        expires_at=iso(now + timedelta(minutes=5)),
    )


def make_decision(intent_id: str = "i-v0044") -> ContactDecisionEnvelope:
    return ContactDecisionEnvelope(
        "d-v0044",
        intent_id,
        "contact",
        .8,
        0,
        ["ok"],
        iso(datetime.now(timezone.utc)),
    )


def cfg(tmp_path, *, enabled: bool = True) -> BridgeConfig:
    return BridgeConfig(
        "did:x",
        "/tmp/runtime.sock",
        str(tmp_path / "trace.jsonl"),
        contact_socket=str(tmp_path / "contact.sock"),
        contact_db=str(tmp_path / "contact.sqlite3"),
        contact_delivery_enabled=enabled,
        contact_target="feishu:oc_PRIVATE_CHAT",
        operation_db=str(tmp_path / "operations.sqlite3"),
    )


class SuccessSender:
    def __init__(self, provider_id: str = "provider-v0044"):
        self.calls = 0
        self.provider_id = provider_id

    def send(self, **kwargs):
        self.calls += 1
        return self.provider_id


class FailedSafeSender:
    def __init__(self):
        self.calls = 0

    def send(self, **kwargs):
        self.calls += 1
        raise HermesSendFailedSafe("spawn_failed")


class UnknownSender:
    def __init__(self):
        self.calls = 0

    def send(self, **kwargs):
        self.calls += 1
        raise HermesSendOutcomeUnknown("timeout_after_invoke")


def _single_contact_operation(service: ContactService) -> BridgeOperation:
    operations = service.operations.list_operations(kind=RetryClass.CONTACT)
    assert len(operations) == 1
    return operations[0]


def test_hermes_spawn_failure_is_failed_safe(monkeypatch, tmp_path):
    def fail(*args, **kwargs):
        raise FileNotFoundError("hermes missing")

    monkeypatch.setattr("subprocess.run", fail)
    client = HermesSendClient(cfg(tmp_path))
    with pytest.raises(HermesSendFailedSafe, match="hermes_send_spawn_failed"):
        client.send(target="feishu:chat", message="hello")


def test_hermes_timeout_is_delivery_unknown(monkeypatch, tmp_path):
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="hermes", timeout=30)

    monkeypatch.setattr("subprocess.run", fail)
    client = HermesSendClient(cfg(tmp_path))
    with pytest.raises(HermesSendOutcomeUnknown, match="timeout_after_invoke"):
        client.send(target="feishu:chat", message="hello")


def test_hermes_nonzero_after_execution_is_conservatively_unknown(monkeypatch, tmp_path):
    class Process:
        returncode = 2
        stdout = ""
        stderr = "invalid target maybe after provider interaction"

    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: Process())
    client = HermesSendClient(cfg(tmp_path))
    with pytest.raises(HermesSendOutcomeUnknown, match="hermes_send_exit_2"):
        client.send(target="feishu:chat", message="hello")


def test_successful_contact_commits_receipt_and_operation_once(tmp_path):
    sender = SuccessSender()
    service = ContactService(cfg(tmp_path), sender)
    intent = make_intent()
    decision = make_decision()

    first = service.process(intent, decision)
    second = service.process(intent, decision)

    assert first.status == "delivered"
    assert second.duplicate is True
    assert sender.calls == 1
    operation = _single_contact_operation(service)
    assert operation.state is OperationState.COMPLETED
    assert operation.attempt == 1
    assert operation.delivery_outcome is DeliveryOutcome.DELIVERED


def test_failed_safe_schedules_retry_without_immediate_resend(tmp_path):
    sender = FailedSafeSender()
    service = ContactService(cfg(tmp_path), sender)

    with pytest.raises(ContactRetryDeferred) as captured:
        service.process(make_intent(), make_decision())

    assert captured.value.next_attempt_at
    assert sender.calls == 1
    operation = _single_contact_operation(service)
    assert operation.state is OperationState.RETRY_WAIT
    assert operation.attempt == 1
    assert operation.delivery_outcome is DeliveryOutcome.FAILED_SAFE


def test_unknown_outcome_without_evidence_stays_locked_and_never_resends(tmp_path):
    sender = UnknownSender()
    service = ContactService(cfg(tmp_path), sender)
    intent = make_intent()
    decision = make_decision()

    with pytest.raises(ContactDeliveryUnknown):
        service.process(intent, decision)
    assert sender.calls == 1
    operation = _single_contact_operation(service)
    assert operation.state is OperationState.DELIVERY_UNKNOWN

    with pytest.raises(ContactDeliveryUnknown):
        service.process(intent, decision)
    assert sender.calls == 1
    operation = _single_contact_operation(service)
    assert operation.state is OperationState.DELIVERY_UNKNOWN


def test_unknown_outcome_delivered_probe_returns_receipt_without_resend(tmp_path):
    sender = UnknownSender()

    def probe(operation: BridgeOperation) -> ContactEvidence:
        return ContactEvidence(
            ContactEvidenceOutcome.DELIVERED,
            "fake_provider_query",
            "provider-confirmed-1",
        )

    service = ContactService(cfg(tmp_path), sender, evidence_probe=probe)
    receipt = service.process(make_intent(), make_decision())

    assert receipt.status == "delivered"
    assert receipt.provider_message_id == "provider-confirmed-1"
    assert sender.calls == 1
    operation = _single_contact_operation(service)
    assert operation.state is OperationState.COMPLETED

    duplicate = service.process(make_intent(), make_decision())
    assert duplicate.duplicate is True
    assert sender.calls == 1


def test_unknown_outcome_not_delivered_probe_enters_bounded_retry_only(tmp_path):
    sender = UnknownSender()

    def probe(operation: BridgeOperation) -> ContactEvidence:
        return ContactEvidence(
            ContactEvidenceOutcome.NOT_DELIVERED,
            "fake_provider_query",
        )

    service = ContactService(cfg(tmp_path), sender, evidence_probe=probe)
    with pytest.raises(ContactRetryDeferred):
        service.process(make_intent(), make_decision())

    assert sender.calls == 1
    operation = _single_contact_operation(service)
    assert operation.state is OperationState.RETRY_WAIT
    assert operation.delivery_outcome is DeliveryOutcome.FAILED_SAFE


def test_retry_requires_resubmitted_payload_and_does_not_store_message(tmp_path):
    first_sender = FailedSafeSender()
    service = ContactService(cfg(tmp_path), first_sender)
    intent = make_intent(message="DO NOT PERSIST RETRY PAYLOAD")
    decision = make_decision()

    with pytest.raises(ContactRetryDeferred):
        service.process(intent, decision)
    operation = _single_contact_operation(service)
    assert operation.state is OperationState.RETRY_WAIT

    # Simulate the due scheduler releasing eligibility. HLB still has no stored
    # message payload and therefore cannot send anything by itself.
    service.operations.make_retry_ready(operation.operation_id, updated_at=iso(datetime.now(timezone.utc)))
    success = SuccessSender("provider-retry-2")
    service.sender = success
    receipt = service.process(intent, decision)
    assert receipt.status == "delivered"
    assert first_sender.calls == 1
    assert success.calls == 1
    final = _single_contact_operation(service)
    assert final.state is OperationState.COMPLETED
    assert final.attempt == 2

    for path in tmp_path.glob("*.sqlite3*"):
        if path.is_file():
            assert b"DO NOT PERSIST RETRY PAYLOAD" not in path.read_bytes()


def test_startup_crash_after_receipt_persist_resolves_without_resend(tmp_path):
    config = cfg(tmp_path)
    contact_store = ContactStore(config.contact_db)
    operation_store = OperationStore(config.operation_db)
    intent = make_intent()
    decision = make_decision()

    # Simulate: durable IN_FLIGHT -> provider success -> ContactStore receipt commit
    # -> process crashes before OperationStore COMPLETED commit.
    request_hash = _request_hash(intent, decision, intent.target)
    operation_id = stable_id("contact-operation", intent.idempotency_key, request_hash)
    operation_store.reserve(
        BridgeOperation(
            operation_id=operation_id,
            kind=RetryClass.CONTACT,
            idempotency_key=intent.idempotency_key,
            request_hash=request_hash,
            state=OperationState.PREPARED,
            attempt=0,
            created_at=intent.created_at,
            updated_at=intent.created_at,
            delivery_outcome=DeliveryOutcome.NOT_ATTEMPTED,
        )
    )
    operation_store.start_attempt(operation_id, updated_at=intent.created_at)
    contact_store.reserve(
        idempotency_key=intent.idempotency_key,
        request_hash=request_hash,
        metadata={"intent_id": intent.intent_id, "target": intent.target, "message_hash": intent.message_hash},
        created_at=intent.created_at,
    )
    contact_store.save_receipt(
        DeliveryReceipt(
            receipt_id="receipt-before-crash",
            intent_id=intent.intent_id,
            idempotency_key=intent.idempotency_key,
            life_did=intent.life_did,
            target=intent.target,
            status="delivered",
            message_hash=intent.message_hash,
            provider_message_id="provider-before-crash",
            delivered_at=intent.created_at,
        )
    )
    operation_store.close()
    contact_store.conn.close()

    sender = SuccessSender("MUST-NOT-BE-CALLED")
    restarted = ContactService(config, sender)
    result = restarted.recover_startup()
    assert result["recovered_in_flight"] == 1
    assert result["resolved"] == 1
    assert result["unknown"] == 0
    assert sender.calls == 0
    assert restarted.operations.get(operation_id).state is OperationState.COMPLETED


def test_startup_unknown_without_authoritative_probe_never_becomes_retryable(tmp_path):
    config = cfg(tmp_path)
    intent = make_intent()
    decision = make_decision()
    service = ContactService(config, UnknownSender())
    with pytest.raises(ContactDeliveryUnknown):
        service.process(intent, decision)
    operation = _single_contact_operation(service)
    assert operation.state is OperationState.DELIVERY_UNKNOWN

    restarted = ContactService(config, SuccessSender("MUST-NOT-SEND"))
    result = restarted.recover_startup()
    # Already DELIVERY_UNKNOWN is intentionally not reclassified by startup's
    # interrupted-IN_FLIGHT recovery. It remains locked until explicit evidence.
    assert result["recovered_in_flight"] == 0
    assert restarted.operations.get(operation.operation_id).state is OperationState.DELIVERY_UNKNOWN

    with pytest.raises(ContactDeliveryUnknown):
        restarted.process(intent, decision)
    assert restarted.sender.calls == 0


def test_reconciliation_record_is_conflict_safe_and_payload_free(tmp_path):
    store = ContactStore(str(tmp_path / "contact.sqlite3"))
    store.save_reconciliation(
        idempotency_key="contact:reconcile",
        outcome="delivered",
        source="provider_query",
        provider_message_id="provider-xyz",
        observed_at="2026-09-02T00:00:00Z",
    )
    stored = store.get_reconciliation("contact:reconcile")
    assert stored["outcome"] == "delivered"
    assert stored["provider_message_id"] == "provider-xyz"

    with pytest.raises(ValueError, match="contact_reconciliation_conflict"):
        store.save_reconciliation(
            idempotency_key="contact:reconcile",
            outcome="not_delivered",
            source="provider_query",
        )
    store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    store.conn.close()
    raw = (tmp_path / "contact.sqlite3").read_bytes()
    assert b"PRIVATE_CHAT" not in raw
    assert b"PRIVATE BODY" not in raw
