from __future__ import annotations

from pathlib import Path
import sqlite3
import stat
import threading

import pytest

from hermes_life_bridge.operation_store import (
    OperationConflictError,
    OperationStateConflict,
    OperationStore,
    OperationStoreError,
)
from hermes_life_bridge.reliability_contract import (
    BridgeOperation,
    DeliveryOutcome,
    OperationState,
    RetryClass,
)
from hermes_life_bridge.representation import contains_forbidden_representation_bytes


T0 = "2026-09-02T02:00:00Z"
T1 = "2026-09-02T02:00:01Z"
T2 = "2026-09-02T02:00:02Z"
T3 = "2026-09-02T02:00:03Z"
T4 = "2026-09-02T02:00:04Z"


def _operation(
    operation_id: str = "op-contact-1",
    *,
    kind: RetryClass = RetryClass.CONTACT,
    idempotency_key: str = "idem-contact-1",
    request_hash: str = "a" * 64,
) -> BridgeOperation:
    return BridgeOperation(
        operation_id=operation_id,
        kind=kind,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        state=OperationState.PREPARED,
        attempt=0,
        created_at=T0,
        updated_at=T0,
        delivery_outcome=(
            DeliveryOutcome.NOT_ATTEMPTED
            if kind is RetryClass.CONTACT
            else None
        ),
    )


def _store(tmp_path: Path) -> OperationStore:
    return OperationStore(str(tmp_path / "operations.db"))


def test_store_file_and_sqlite_sidecars_are_private(tmp_path):
    store = _store(tmp_path)
    store.reserve(_operation())

    paths = [
        tmp_path / "operations.db",
        tmp_path / "operations.db-wal",
        tmp_path / "operations.db-shm",
    ]
    for path in paths:
        if path.exists():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
    store.close()


def test_schema_contains_only_privacy_minimized_operation_fields(tmp_path):
    store = _store(tmp_path)
    columns = {
        row[1]
        for row in store.conn.execute("PRAGMA table_info(bridge_operations)").fetchall()
    }
    assert columns == {
        "operation_id",
        "kind",
        "idempotency_key",
        "request_hash",
        "state",
        "attempt",
        "next_attempt_at",
        "delivery_outcome",
        "last_error_code",
        "created_at",
        "updated_at",
        "schema_version",
    }
    assert not ({"target", "chat_id", "thread_id", "message", "prompt", "payload"} & columns)
    assert store.conn.execute("PRAGMA user_version").fetchone()[0] == 1
    store.close()


def test_reservation_survives_reopen(tmp_path):
    store = _store(tmp_path)
    operation, created = store.reserve(_operation())
    assert created is True
    assert operation.state is OperationState.PREPARED
    store.close()

    reopened = _store(tmp_path)
    loaded = reopened.get("op-contact-1")
    assert loaded == operation
    reopened.close()


def test_duplicate_idempotency_returns_original_durable_operation(tmp_path):
    store = _store(tmp_path)
    original, created = store.reserve(_operation())
    assert created is True

    duplicate, created = store.reserve(
        _operation(operation_id="op-new-id-same-logical-request")
    )
    assert created is False
    assert duplicate.operation_id == original.operation_id
    assert len(store.list_operations()) == 1
    store.close()


def test_idempotency_key_with_different_request_hash_is_conflict(tmp_path):
    store = _store(tmp_path)
    store.reserve(_operation())
    with pytest.raises(
        OperationConflictError,
        match="idempotency_key_reused_with_different_request",
    ):
        store.reserve(
            _operation(operation_id="op-2", request_hash="b" * 64)
        )
    assert len(store.list_operations()) == 1
    store.close()


def test_operation_id_reuse_for_different_logical_operation_is_conflict(tmp_path):
    store = _store(tmp_path)
    store.reserve(_operation())
    with pytest.raises(OperationConflictError, match="operation_id_reused"):
        store.reserve(
            _operation(
                idempotency_key="idem-other",
                request_hash="b" * 64,
            )
        )
    store.close()


def test_start_attempt_is_durable_and_increments_exactly_once(tmp_path):
    store = _store(tmp_path)
    store.reserve(_operation())
    started = store.start_attempt("op-contact-1", updated_at=T1)
    assert started.state is OperationState.IN_FLIGHT
    assert started.attempt == 1

    with pytest.raises(OperationStateConflict, match="start_attempt_requires_prepared"):
        store.start_attempt("op-contact-1", updated_at=T2)
    assert store.get("op-contact-1").attempt == 1
    store.close()


def test_two_store_instances_cannot_start_same_attempt_twice(tmp_path):
    db = str(tmp_path / "operations.db")
    first = OperationStore(db)
    second = OperationStore(db)
    first.reserve(_operation())

    barrier = threading.Barrier(2)
    results: list[str] = []

    def start(store: OperationStore, when: str) -> None:
        barrier.wait()
        try:
            store.start_attempt("op-contact-1", updated_at=when)
            results.append("started")
        except OperationStateConflict:
            results.append("conflict")

    a = threading.Thread(target=start, args=(first, T1))
    b = threading.Thread(target=start, args=(second, T2))
    a.start()
    b.start()
    a.join()
    b.join()

    assert sorted(results) == ["conflict", "started"]
    loaded = first.get("op-contact-1")
    assert loaded.state is OperationState.IN_FLIGHT
    assert loaded.attempt == 1
    first.close()
    second.close()


def test_failed_safe_retry_wait_and_attempt_survive_restart(tmp_path):
    store = _store(tmp_path)
    store.reserve(_operation())
    store.start_attempt("op-contact-1", updated_at=T1)
    store.mark_failed_safe(
        "op-contact-1",
        updated_at=T2,
        error_code="provider_rejected_no_send",
    )
    waiting = store.schedule_retry(
        "op-contact-1",
        next_attempt_at="2026-09-02T02:01:00Z",
        updated_at=T3,
    )
    assert waiting.delivery_outcome is DeliveryOutcome.FAILED_SAFE
    store.close()

    reopened = _store(tmp_path)
    loaded = reopened.get("op-contact-1")
    assert loaded.state is OperationState.RETRY_WAIT
    assert loaded.attempt == 1
    assert loaded.next_attempt_at == "2026-09-02T02:01:00Z"
    assert loaded.delivery_outcome is DeliveryOutcome.FAILED_SAFE
    assert loaded.last_error_code == "provider_rejected_no_send"
    reopened.close()


def test_retry_ready_does_not_execute_or_increment_attempt(tmp_path):
    store = _store(tmp_path)
    store.reserve(_operation())
    store.start_attempt("op-contact-1", updated_at=T1)
    store.mark_failed_safe("op-contact-1", updated_at=T2)
    store.schedule_retry(
        "op-contact-1",
        next_attempt_at="2026-09-02T02:01:00Z",
        updated_at=T3,
    )
    ready = store.make_retry_ready("op-contact-1", updated_at=T4)
    assert ready.state is OperationState.PREPARED
    assert ready.attempt == 1
    assert ready.next_attempt_at is None
    assert ready.delivery_outcome is DeliveryOutcome.NOT_ATTEMPTED
    store.close()


def test_delivery_unknown_is_durable_and_cannot_be_scheduled_for_retry(tmp_path):
    store = _store(tmp_path)
    store.reserve(_operation())
    store.start_attempt("op-contact-1", updated_at=T1)
    unknown = store.mark_delivery_unknown(
        "op-contact-1",
        updated_at=T2,
        error_code="provider_timeout_after_invoke",
    )
    assert unknown.delivery_outcome is DeliveryOutcome.DELIVERY_UNKNOWN
    store.close()

    reopened = _store(tmp_path)
    loaded = reopened.get("op-contact-1")
    assert loaded.state is OperationState.DELIVERY_UNKNOWN
    with pytest.raises(OperationStateConflict, match="schedule_retry_requires_failed_safe"):
        reopened.schedule_retry(
            "op-contact-1",
            next_attempt_at="2026-09-02T02:01:00Z",
            updated_at=T3,
        )
    assert reopened.get("op-contact-1").state is OperationState.DELIVERY_UNKNOWN
    reopened.close()


def test_reconciliation_can_prove_unknown_delivery_completed(tmp_path):
    store = _store(tmp_path)
    store.reserve(_operation())
    store.start_attempt("op-contact-1", updated_at=T1)
    store.mark_delivery_unknown("op-contact-1", updated_at=T2)
    completed = store.mark_completed("op-contact-1", updated_at=T3)
    assert completed.state is OperationState.COMPLETED
    assert completed.delivery_outcome is DeliveryOutcome.DELIVERED
    store.close()


def test_reconciliation_can_prove_unknown_delivery_failed_safe(tmp_path):
    store = _store(tmp_path)
    store.reserve(_operation())
    store.start_attempt("op-contact-1", updated_at=T1)
    store.mark_delivery_unknown("op-contact-1", updated_at=T2)
    safe = store.mark_failed_safe(
        "op-contact-1",
        updated_at=T3,
        error_code="provider_reconcile_not_found",
    )
    assert safe.state is OperationState.FAILED_SAFE
    assert safe.delivery_outcome is DeliveryOutcome.FAILED_SAFE
    store.close()


def test_contact_owner_restart_recovers_in_flight_as_delivery_unknown(tmp_path):
    store = _store(tmp_path)
    store.reserve(_operation())
    store.start_attempt("op-contact-1", updated_at=T1)
    store.close()

    restarted = _store(tmp_path)
    recovered = restarted.recover_interrupted_contact_operations(recovered_at=T2)
    assert [item.operation_id for item in recovered] == ["op-contact-1"]
    loaded = restarted.get("op-contact-1")
    assert loaded.state is OperationState.DELIVERY_UNKNOWN
    assert loaded.delivery_outcome is DeliveryOutcome.DELIVERY_UNKNOWN
    assert loaded.last_error_code == "process_restart_in_flight"
    restarted.close()


def test_contact_recovery_does_not_touch_percept_or_cognition_in_flight(tmp_path):
    store = _store(tmp_path)
    store.reserve(_operation())
    store.reserve(
        _operation(
            "op-cognition-1",
            kind=RetryClass.COGNITION,
            idempotency_key="idem-cognition-1",
            request_hash="b" * 64,
        )
    )
    store.reserve(
        _operation(
            "op-percept-1",
            kind=RetryClass.PERCEPT,
            idempotency_key="idem-percept-1",
            request_hash="c" * 64,
        )
    )
    store.start_attempt("op-contact-1", updated_at=T1)
    store.start_attempt("op-cognition-1", updated_at=T1)
    store.start_attempt("op-percept-1", updated_at=T1)

    recovered = store.recover_interrupted_contact_operations(recovered_at=T2)
    assert [item.operation_id for item in recovered] == ["op-contact-1"]
    assert store.get("op-cognition-1").state is OperationState.IN_FLIGHT
    assert store.get("op-percept-1").state is OperationState.IN_FLIGHT
    store.close()


def test_completed_and_exhausted_operations_are_terminal(tmp_path):
    store = _store(tmp_path)
    store.reserve(_operation())
    store.start_attempt("op-contact-1", updated_at=T1)
    store.mark_completed("op-contact-1", updated_at=T2)
    with pytest.raises(OperationStateConflict):
        store.start_attempt("op-contact-1", updated_at=T3)

    store.reserve(
        _operation(
            "op-contact-2",
            idempotency_key="idem-contact-2",
            request_hash="b" * 64,
        )
    )
    store.start_attempt("op-contact-2", updated_at=T1)
    store.mark_failed_safe("op-contact-2", updated_at=T2)
    exhausted = store.mark_exhausted("op-contact-2", updated_at=T3)
    assert exhausted.state is OperationState.EXHAUSTED
    with pytest.raises(OperationStateConflict):
        store.schedule_retry(
            "op-contact-2",
            next_attempt_at="2026-09-02T02:01:00Z",
            updated_at=T4,
        )
    store.close()


def test_list_operations_filters_by_kind_and_state(tmp_path):
    store = _store(tmp_path)
    store.reserve(_operation())
    store.reserve(
        _operation(
            "op-cognition-1",
            kind=RetryClass.COGNITION,
            idempotency_key="idem-cognition-1",
            request_hash="b" * 64,
        )
    )
    store.start_attempt("op-cognition-1", updated_at=T1)

    contact = store.list_operations(kind=RetryClass.CONTACT)
    in_flight = store.list_operations(state=OperationState.IN_FLIGHT)
    assert [item.operation_id for item in contact] == ["op-contact-1"]
    assert [item.operation_id for item in in_flight] == ["op-cognition-1"]
    store.close()


def test_store_bytes_do_not_contain_runtime_repr_markers(tmp_path):
    store = _store(tmp_path)
    store.reserve(_operation())
    store.start_attempt("op-contact-1", updated_at=T1)
    store.mark_delivery_unknown("op-contact-1", updated_at=T2)
    store.close()

    raw = b""
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{tmp_path / 'operations.db'}{suffix}")
        if path.exists():
            raw += path.read_bytes()
    assert contains_forbidden_representation_bytes(raw) is False
    assert b"SessionSource(" not in raw
    assert b"Platform." not in raw


def test_store_fails_closed_on_newer_schema_version(tmp_path):
    path = tmp_path / "operations.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version=2")
    conn.commit()
    conn.close()

    with pytest.raises(
        OperationStoreError,
        match="operation_store_schema_newer_than_supported",
    ):
        OperationStore(str(path))

    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    conn.close()


def test_store_rejects_runtime_repr_in_durable_identity(tmp_path):
    store = _store(tmp_path)
    unsafe = _operation(idempotency_key="SessionSource(platform=Platform.FEISHU)")
    with pytest.raises(ValueError, match="idempotency_key_must_be_canonical"):
        store.reserve(unsafe)
    store.close()

    raw = (tmp_path / "operations.db").read_bytes()
    assert b"SessionSource(" not in raw
    assert b"Platform.FEISHU" not in raw


def test_store_rejects_exact_route_as_operation_identity(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="idempotency_key_must_not_be_exact_route"):
        store.reserve(_operation(idempotency_key="feishu:oc_private_chat"))
    assert store.list_operations() == []
    store.close()
