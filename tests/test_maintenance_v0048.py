from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import os
import stat

from hermes_life_bridge.config import BridgeConfig
from hermes_life_bridge.maintenance import run_maintenance
from hermes_life_bridge.operation_store import OperationStore
from hermes_life_bridge.reliability_contract import (
    BridgeOperation,
    DeliveryOutcome,
    OperationState,
    RetryClass,
)
from hermes_life_bridge.trace import BridgeTracer


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def test_trace_rotates_and_remains_private(tmp_path):
    path = tmp_path / "trace.jsonl"
    tracer = BridgeTracer(str(path), max_bytes=1024, backup_count=2)
    for index in range(80):
        tracer.emit(
            trace_id=f"trace-{index}",
            stage="HOOK_RECEIVED",
            marker="x" * 80,
        )
    assert path.exists()
    assert (tmp_path / "trace.jsonl.1").exists()
    assert not (tmp_path / "trace.jsonl.3").exists()
    total = sum(
        candidate.stat().st_size
        for candidate in tmp_path.glob("trace.jsonl*")
        if candidate.is_file() and not candidate.name.endswith(".lock")
    )
    assert total <= (1024 * 3) + 1024
    for candidate in tmp_path.glob("trace.jsonl*"):
        assert stat.S_IMODE(candidate.stat().st_mode) == 0o600


def test_trace_tail_crosses_rotation_boundary(tmp_path):
    path = tmp_path / "trace.jsonl"
    tracer = BridgeTracer(str(path), max_bytes=1024, backup_count=2)
    for index in range(30):
        tracer.emit(
            trace_id=f"trace-{index}",
            stage="HOOK_RECEIVED",
            index=index,
            marker="y" * 80,
        )
    tail = tracer.tail(5)
    assert [row["index"] for row in tail] == [25, 26, 27, 28, 29]


def test_trace_rotation_never_persists_forbidden_raw_fields(tmp_path):
    path = tmp_path / "trace.jsonl"
    tracer = BridgeTracer(str(path), max_bytes=1024, backup_count=2)
    for index in range(30):
        tracer.emit(
            trace_id=f"privacy-{index}",
            stage="HOOK_RECEIVED",
            message="PRIVATE MESSAGE BODY",
            target="feishu:PRIVATE_CHAT",
            safe_marker="ok",
        )
    blob = b"".join(
        candidate.read_bytes()
        for candidate in tmp_path.glob("trace.jsonl*")
        if candidate.is_file()
    )
    assert b"PRIVATE MESSAGE BODY" not in blob
    assert b"PRIVATE_CHAT" not in blob


def _operation(
    operation_id: str,
    kind: RetryClass,
    state: OperationState,
    updated_at: str,
) -> BridgeOperation:
    return BridgeOperation(
        operation_id=operation_id,
        kind=kind,
        idempotency_key=f"idem:{operation_id}",
        request_hash=("a" if kind is RetryClass.PERCEPT else "b" if kind is RetryClass.COGNITION else "c") * 64,
        state=state,
        attempt=1,
        created_at=updated_at,
        updated_at=updated_at,
        delivery_outcome=(
            DeliveryOutcome.DELIVERED
            if kind is RetryClass.CONTACT and state is OperationState.COMPLETED
            else None
        ),
    )


def test_purge_removes_only_old_percept_cognition_terminal_rows(tmp_path):
    store = OperationStore(str(tmp_path / "operations.sqlite3"))
    old = iso(datetime.now(timezone.utc) - timedelta(days=60))
    recent = iso(datetime.now(timezone.utc))

    # Insert directly through validated durable rows because these are terminal fixtures.
    for operation in (
        _operation("old-percept", RetryClass.PERCEPT, OperationState.COMPLETED, old),
        _operation("old-cognition", RetryClass.COGNITION, OperationState.COMPLETED, old),
        _operation("recent-percept", RetryClass.PERCEPT, OperationState.COMPLETED, recent),
        _operation("old-contact", RetryClass.CONTACT, OperationState.COMPLETED, old),
    ):
        store.conn.execute(
            """
            INSERT INTO bridge_operations(
                operation_id,kind,idempotency_key,request_hash,state,attempt,
                next_attempt_at,delivery_outcome,last_error_code,created_at,
                updated_at,schema_version
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            store._operation_values(operation),
        )
    store.conn.commit()

    purged = store.purge_terminal(
        before=iso(datetime.now(timezone.utc) - timedelta(days=30))
    )
    assert purged == 2
    assert store.get("old-percept") is None
    assert store.get("old-cognition") is None
    assert store.get("recent-percept") is not None
    assert store.get("old-contact") is not None
    store.close()


def test_maintenance_retains_contact_dedupe_and_checkpoints(tmp_path):
    operation_db = tmp_path / "operations.sqlite3"
    cognition_db = tmp_path / "cognition.sqlite3"
    contact_db = tmp_path / "contact.sqlite3"
    for path in (cognition_db, contact_db):
        import sqlite3
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE sentinel(value TEXT)")
        conn.commit()
        conn.close()
        path.chmod(0o600)

    config = BridgeConfig(
        "did:x",
        "/tmp/runtime.sock",
        str(tmp_path / "trace.jsonl"),
        cognition_db=str(cognition_db),
        contact_db=str(contact_db),
        operation_db=str(operation_db),
        operation_retention_seconds=30 * 86400,
    )
    OperationStore(str(operation_db)).close()
    result = run_maintenance(config)
    assert result["ok"] is True
    assert result["contact_dedupe_retained"] is True
    assert result["checkpointed"]["operation_db"] is True
    assert result["checkpointed"]["cognition_db"] is True
    assert result["checkpointed"]["contact_db"] is True


def test_maintenance_timer_contract():
    service = Path("systemd/hermes-life-maintenance.service.template").read_text()
    timer = Path("systemd/hermes-life-maintenance.timer.template").read_text()
    assert "Type=oneshot" in service
    assert "hermes_life_bridge.maintenance" in service
    assert "OnUnitActiveSec=24h" in timer
    assert "Persistent=true" in timer
