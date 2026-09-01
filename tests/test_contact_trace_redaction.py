from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3

from hermes_life_bridge.config import BridgeConfig
from hermes_life_bridge.contact_model import ContactDecisionEnvelope, ContactIntentEnvelope
from hermes_life_bridge.contact_service import ContactService
from hermes_life_bridge.contact_store import ContactStore


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def make_intent(target="feishu:oc_PRIVATE_CHAT", message="PRIVATE MESSAGE BODY"):
    now = datetime.now(timezone.utc)
    return ContactIntentEnvelope(
        intent_id="i-redact",
        idempotency_key="contact:i-redact",
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


def make_decision():
    return ContactDecisionEnvelope(
        "d-redact", "i-redact", "contact", .8, 0, ["ok"], iso(datetime.now(timezone.utc))
    )


class SuccessSender:
    def __init__(self):
        self.calls = 0
    def send(self, **kwargs):
        self.calls += 1
        return "provider-1"


class FailingSender:
    def send(self, **kwargs):
        raise RuntimeError(f"failed for {kwargs['target']} with body {kwargs['message']}")


def cfg(tmp_path, enabled=True):
    return BridgeConfig(
        "did:x",
        "/tmp/runtime.sock",
        str(tmp_path / "trace.jsonl"),
        contact_socket=str(tmp_path / "contact.sock"),
        contact_db=str(tmp_path / "contact.sqlite3"),
        contact_delivery_enabled=enabled,
        contact_target="feishu:oc_PRIVATE_CHAT",
    )


def test_success_trace_redacts_route_and_message(tmp_path):
    service = ContactService(cfg(tmp_path), SuccessSender())
    receipt = service.process(make_intent(), make_decision())
    assert receipt.status == "delivered"
    trace = (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
    assert "oc_PRIVATE_CHAT" not in trace
    assert "feishu:oc_PRIVATE_CHAT" not in trace
    assert "PRIVATE MESSAGE BODY" not in trace
    rows = [json.loads(x) for x in trace.splitlines()]
    for row in rows:
        if row["stage"] in {
            "CONTACT_REQUEST_RECEIVED",
            "HERMES_SEND_START",
            "HERMES_SEND_SUCCESS",
            "DELIVERY_RECEIPT_SENT",
        }:
            assert row["target_platform"] == "feishu"
            assert row["target_redacted"] is True


def test_failure_trace_does_not_echo_target_or_message(tmp_path):
    service = ContactService(cfg(tmp_path), FailingSender())
    try:
        service.process(make_intent(), make_decision())
    except RuntimeError:
        pass
    trace = (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
    assert "oc_PRIVATE_CHAT" not in trace
    assert "PRIVATE MESSAGE BODY" not in trace
    failed = [json.loads(x) for x in trace.splitlines() if "CONTACT_FAILED" in x]
    assert len(failed) == 1
    assert "detail" not in failed[0]
    assert failed[0]["target_platform"] == "feishu"
    assert failed[0]["target_redacted"] is True
    assert len(failed[0]["error_fingerprint"]) == 16


def test_new_operational_db_never_stores_exact_target(tmp_path):
    service = ContactService(cfg(tmp_path), SuccessSender())
    service.process(make_intent(), make_decision())
    service.store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    service.store.conn.close()
    for path in tmp_path.glob("contact.sqlite3*"):
        if path.is_file():
            raw = path.read_bytes()
            assert b"oc_PRIVATE_CHAT" not in raw
            assert b"feishu:oc_PRIVATE_CHAT" not in raw


def test_previous_hlb0031_rows_are_physically_redacted(tmp_path):
    path = tmp_path / "contact.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript("""
      CREATE TABLE requests(
        idempotency_key TEXT PRIMARY KEY,
        request_hash TEXT NOT NULL,
        metadata_json TEXT NOT NULL,
        created_at TEXT NOT NULL
      );
      CREATE TABLE receipts(
        idempotency_key TEXT PRIMARY KEY,
        receipt_json TEXT NOT NULL,
        delivered_at TEXT NOT NULL
      );
    """)
    target = "feishu:oc_OLD_PRIVATE"
    conn.execute(
        "INSERT INTO requests VALUES(?,?,?,?)",
        (
            "contact:old", "rh",
            json.dumps({"intent_id":"old","target":target,"message_hash":"mh"}),
            "2026-09-01T00:00:00Z",
        ),
    )
    conn.execute(
        "INSERT INTO receipts VALUES(?,?,?)",
        (
            "contact:old",
            json.dumps({
                "receipt_id":"r-old", "intent_id":"old",
                "idempotency_key":"contact:old", "life_did":"did:x",
                "target":target, "status":"delivered", "message_hash":"mh",
                "provider_message_id":"p", "delivered_at":"2026-09-01T00:00:01Z",
                "duplicate":False, "error":"", "schema_version":"v0.3",
            }),
            "2026-09-01T00:00:01Z",
        ),
    )
    conn.commit()
    conn.close()

    store = ContactStore(str(path))
    store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    store.conn.close()

    for candidate in tmp_path.glob("contact.sqlite3*"):
        if candidate.is_file():
            raw = candidate.read_bytes()
            assert b"oc_OLD_PRIVATE" not in raw
            assert b"feishu:oc_OLD_PRIVATE" not in raw

    check = sqlite3.connect(path)
    metadata = json.loads(check.execute("SELECT metadata_json FROM requests").fetchone()[0])
    receipt = json.loads(check.execute("SELECT receipt_json FROM receipts").fetchone()[0])
    assert metadata["target_platform"] == "feishu"
    assert metadata["target_redacted"] is True
    assert receipt["target"] == "feishu"
    assert len(metadata["target_hash"]) == 64
    assert len(receipt["target_hash"]) == 64
    check.close()


def test_duplicate_reconstructs_exact_target_without_second_send(tmp_path):
    sender = SuccessSender()
    service = ContactService(cfg(tmp_path), sender)
    intent = make_intent()
    first = service.process(intent, make_decision())
    second = service.process(intent, make_decision())
    assert first.target == intent.target
    assert second.target == intent.target
    assert second.duplicate is True
    assert sender.calls == 1
