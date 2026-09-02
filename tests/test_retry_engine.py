from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import threading

import pytest

from hermes_life_bridge.operation_store import OperationStateConflict, OperationStore
from hermes_life_bridge.reliability_contract import (
    BridgeOperation,
    DeliveryOutcome,
    OperationState,
    RetryClass,
)
from hermes_life_bridge.retry_engine import (
    COGNITION_RETRY_POLICY,
    CONTACT_RETRY_POLICY,
    DEFAULT_RETRY_POLICIES,
    PERCEPT_RETRY_POLICY,
    RetryDisposition,
    RetryEngine,
    RetryEngineError,
    RetryTimestampError,
    retry_delay_seconds,
)


T0 = "2026-09-02T03:00:00Z"
T1 = "2026-09-02T03:00:01Z"
T2 = "2026-09-02T03:00:02Z"
FUTURE = "2026-09-02T04:00:00Z"


def _store(tmp_path: Path, name: str = "operations.db") -> OperationStore:
    return OperationStore(str(tmp_path / name))


def _operation(
    operation_id: str,
    kind: RetryClass,
    *,
    idempotency_key: str | None = None,
    request_hash: str | None = None,
) -> BridgeOperation:
    letter = {
        RetryClass.PERCEPT: "a",
        RetryClass.COGNITION: "b",
        RetryClass.CONTACT: "c",
    }[kind]
    return BridgeOperation(
        operation_id=operation_id,
        kind=kind,
        idempotency_key=idempotency_key or f"idem-{operation_id}",
        request_hash=request_hash or letter * 64,
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


def _failed_safe(
    store: OperationStore,
    operation_id: str,
    kind: RetryClass,
) -> BridgeOperation:
    store.reserve(_operation(operation_id, kind))
    store.start_attempt(operation_id, updated_at=T1)
    return store.mark_failed_safe(
        operation_id,
        updated_at=T2,
        error_code="transport_failed_safe",
    )


def test_default_policies_are_explicit_per_action_class():
    assert PERCEPT_RETRY_POLICY.max_attempts == 5
    assert COGNITION_RETRY_POLICY.max_attempts == 3
    assert CONTACT_RETRY_POLICY.max_attempts == 2
    assert PERCEPT_RETRY_POLICY.reconcile_before_retry is False
    assert COGNITION_RETRY_POLICY.reconcile_before_retry is True
    assert CONTACT_RETRY_POLICY.reconcile_before_retry is False
    assert CONTACT_RETRY_POLICY.retry_after_delivery_unknown is False
    assert set(DEFAULT_RETRY_POLICIES) == set(RetryClass)


def test_policy_registry_must_cover_all_classes(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(
        RetryEngineError,
        match="retry_policy_registry_must_cover_all_classes",
    ):
        RetryEngine(store, policies={})
    store.close()


def test_policy_registry_rejects_non_durable_policy(tmp_path):
    store = _store(tmp_path)
    policies = dict(DEFAULT_RETRY_POLICIES)
    policies[RetryClass.PERCEPT] = replace(
        PERCEPT_RETRY_POLICY,
        requires_durable_state=False,
    )
    with pytest.raises(RetryEngineError, match="retry_policy_must_require_durable_state"):
        RetryEngine(store, policies=policies)
    store.close()


def test_backoff_jitter_is_deterministic_and_bounded():
    operation = BridgeOperation(
        operation_id="op-percept-delay",
        kind=RetryClass.PERCEPT,
        idempotency_key="idem-delay",
        request_hash="a" * 64,
        state=OperationState.FAILED_SAFE,
        attempt=1,
        created_at=T0,
        updated_at=T1,
    )
    first = retry_delay_seconds(operation, PERCEPT_RETRY_POLICY)
    second = retry_delay_seconds(operation, PERCEPT_RETRY_POLICY)
    assert first == second
    assert 0.20 <= first <= 0.30

    late_attempt = replace(operation, attempt=20)
    late_delay = retry_delay_seconds(late_attempt, PERCEPT_RETRY_POLICY)
    assert 0 <= late_delay <= PERCEPT_RETRY_POLICY.max_backoff_seconds


def test_backoff_rejects_policy_class_mismatch():
    operation = BridgeOperation(
        operation_id="op-percept-delay",
        kind=RetryClass.PERCEPT,
        idempotency_key="idem-delay",
        request_hash="a" * 64,
        state=OperationState.FAILED_SAFE,
        attempt=1,
        created_at=T0,
        updated_at=T1,
    )
    with pytest.raises(RetryEngineError, match="retry_policy_class_mismatch"):
        retry_delay_seconds(operation, CONTACT_RETRY_POLICY)


def test_percept_failed_safe_is_scheduled_but_not_started(tmp_path):
    store = _store(tmp_path)
    failed = _failed_safe(store, "op-percept-1", RetryClass.PERCEPT)
    engine = RetryEngine(store)
    result = engine.schedule_failed_safe(failed.operation_id, now=T2)

    assert result.disposition is RetryDisposition.SCHEDULED
    assert result.delay_seconds is not None
    assert result.operation.state is OperationState.RETRY_WAIT
    assert result.operation.attempt == 1
    assert result.operation.next_attempt_at is not None
    assert store.get(failed.operation_id).state is OperationState.RETRY_WAIT
    store.close()


def test_contact_allows_only_one_retry_after_first_failed_safe(tmp_path):
    store = _store(tmp_path)
    engine = RetryEngine(store)
    failed = _failed_safe(store, "op-contact-1", RetryClass.CONTACT)
    first = engine.schedule_failed_safe(failed.operation_id, now=T2)
    assert first.disposition is RetryDisposition.SCHEDULED

    released = engine.release_due(now=FUTURE, kind=RetryClass.CONTACT)
    assert [item.disposition for item in released] == [RetryDisposition.READY]
    ready = store.get(failed.operation_id)
    assert ready.state is OperationState.PREPARED
    assert ready.attempt == 1

    store.start_attempt(failed.operation_id, updated_at="2026-09-02T04:00:01Z")
    store.mark_failed_safe(
        failed.operation_id,
        updated_at="2026-09-02T04:00:02Z",
        error_code="provider_rejected_no_send",
    )
    final = engine.schedule_failed_safe(
        failed.operation_id,
        now="2026-09-02T04:00:03Z",
    )
    assert final.disposition is RetryDisposition.EXHAUSTED
    assert final.operation.state is OperationState.EXHAUSTED
    assert final.operation.attempt == 2
    store.close()


def test_delivery_unknown_never_enters_retry_scheduler(tmp_path):
    store = _store(tmp_path)
    store.reserve(_operation("op-contact-unknown", RetryClass.CONTACT))
    store.start_attempt("op-contact-unknown", updated_at=T1)
    store.mark_delivery_unknown(
        "op-contact-unknown",
        updated_at=T2,
        error_code="provider_timeout_after_invoke",
    )
    engine = RetryEngine(store)
    with pytest.raises(OperationStateConflict, match="retry_engine_requires_failed_safe"):
        engine.schedule_failed_safe("op-contact-unknown", now=FUTURE)
    assert store.get("op-contact-unknown").state is OperationState.DELIVERY_UNKNOWN
    store.close()


def test_cognition_requires_receipt_reconciliation_before_schedule(tmp_path):
    store = _store(tmp_path)
    failed = _failed_safe(store, "op-cognition-1", RetryClass.COGNITION)
    engine = RetryEngine(store)

    with pytest.raises(RetryEngineError, match="cognition_reconciliation_required"):
        engine.schedule_failed_safe(failed.operation_id, now=T2)
    assert store.get(failed.operation_id).state is OperationState.FAILED_SAFE
    store.close()


def test_late_cognition_receipt_completes_instead_of_scheduling(tmp_path):
    store = _store(tmp_path)
    failed = _failed_safe(store, "op-cognition-late", RetryClass.COGNITION)
    engine = RetryEngine(store)
    result = engine.schedule_failed_safe(
        failed.operation_id,
        now=T2,
        cognition_receipt_accepted=lambda operation: True,
    )
    assert result.disposition is RetryDisposition.COMPLETED
    assert result.operation.state is OperationState.COMPLETED
    assert store.get(failed.operation_id).state is OperationState.COMPLETED
    store.close()


def test_cognition_without_receipt_is_scheduled(tmp_path):
    store = _store(tmp_path)
    failed = _failed_safe(store, "op-cognition-2", RetryClass.COGNITION)
    engine = RetryEngine(store)
    result = engine.schedule_failed_safe(
        failed.operation_id,
        now=T2,
        cognition_receipt_accepted=lambda operation: False,
    )
    assert result.disposition is RetryDisposition.SCHEDULED
    assert result.operation.state is OperationState.RETRY_WAIT
    store.close()


def test_due_read_is_non_mutating_and_release_does_not_start_attempt(tmp_path):
    store = _store(tmp_path)
    failed = _failed_safe(store, "op-percept-due", RetryClass.PERCEPT)
    engine = RetryEngine(store)
    scheduled = engine.schedule_failed_safe(failed.operation_id, now=T2)
    due_at = scheduled.operation.next_attempt_at
    assert due_at is not None

    assert engine.due_operations(now=T2) == []
    due = engine.due_operations(now=FUTURE)
    assert [item.operation_id for item in due] == [failed.operation_id]
    assert store.get(failed.operation_id).state is OperationState.RETRY_WAIT

    released = engine.release_due(now=FUTURE)
    assert [item.disposition for item in released] == [RetryDisposition.READY]
    ready = store.get(failed.operation_id)
    assert ready.state is OperationState.PREPARED
    assert ready.attempt == 1
    store.close()


def test_due_limit_is_bounded(tmp_path):
    store = _store(tmp_path)
    engine = RetryEngine(store)
    for index in range(3):
        op_id = f"op-percept-limit-{index}"
        _failed_safe(store, op_id, RetryClass.PERCEPT)
        engine.schedule_failed_safe(op_id, now=T2)

    assert len(engine.due_operations(now=FUTURE, limit=2)) == 2
    with pytest.raises(ValueError, match="retry_due_limit_out_of_range"):
        engine.due_operations(now=FUTURE, limit=0)
    with pytest.raises(ValueError, match="retry_due_limit_out_of_range"):
        engine.due_operations(now=FUTURE, limit=1001)
    store.close()


def test_retry_time_requires_timezone_aware_iso8601(tmp_path):
    store = _store(tmp_path)
    engine = RetryEngine(store)
    with pytest.raises(RetryTimestampError, match="timestamp_must_be_timezone_aware"):
        engine.due_operations(now="2026-09-02T03:00:00")
    with pytest.raises(RetryTimestampError, match="timestamp_must_be_iso8601"):
        engine.due_operations(now="not-a-time")
    store.close()


def test_due_cognition_requires_reconciliation_before_any_batch_mutation(tmp_path):
    store = _store(tmp_path)
    engine = RetryEngine(store)

    percept = _failed_safe(store, "op-percept-batch", RetryClass.PERCEPT)
    engine.schedule_failed_safe(percept.operation_id, now=T2)
    cognition = _failed_safe(store, "op-cognition-batch", RetryClass.COGNITION)
    engine.schedule_failed_safe(
        cognition.operation_id,
        now=T2,
        cognition_receipt_accepted=lambda operation: False,
    )

    with pytest.raises(RetryEngineError, match="cognition_reconciliation_required"):
        engine.release_due(now=FUTURE)
    assert store.get(percept.operation_id).state is OperationState.RETRY_WAIT
    assert store.get(cognition.operation_id).state is OperationState.RETRY_WAIT
    store.close()


def test_late_cognition_receipt_while_waiting_cancels_retry(tmp_path):
    store = _store(tmp_path)
    engine = RetryEngine(store)
    cognition = _failed_safe(store, "op-cognition-wait", RetryClass.COGNITION)
    engine.schedule_failed_safe(
        cognition.operation_id,
        now=T2,
        cognition_receipt_accepted=lambda operation: False,
    )

    results = engine.release_due(
        now=FUTURE,
        kind=RetryClass.COGNITION,
        cognition_receipt_accepted=lambda operation: True,
    )
    assert [item.disposition for item in results] == [RetryDisposition.COMPLETED]
    assert store.get(cognition.operation_id).state is OperationState.COMPLETED
    store.close()


def test_cognition_due_without_receipt_becomes_ready_not_in_flight(tmp_path):
    store = _store(tmp_path)
    engine = RetryEngine(store)
    cognition = _failed_safe(store, "op-cognition-ready", RetryClass.COGNITION)
    engine.schedule_failed_safe(
        cognition.operation_id,
        now=T2,
        cognition_receipt_accepted=lambda operation: False,
    )

    results = engine.release_due(
        now=FUTURE,
        kind=RetryClass.COGNITION,
        cognition_receipt_accepted=lambda operation: False,
    )
    assert [item.disposition for item in results] == [RetryDisposition.READY]
    ready = store.get(cognition.operation_id)
    assert ready.state is OperationState.PREPARED
    assert ready.attempt == 1
    store.close()


def test_two_schedulers_release_one_due_operation_at_most_once(tmp_path):
    path = str(tmp_path / "operations.db")
    first_store = OperationStore(path)
    second_store = OperationStore(path)
    first_engine = RetryEngine(first_store)
    second_engine = RetryEngine(second_store)

    failed = _failed_safe(first_store, "op-percept-race", RetryClass.PERCEPT)
    first_engine.schedule_failed_safe(failed.operation_id, now=T2)

    barrier = threading.Barrier(2)
    results: list[RetryDisposition] = []

    def release(engine: RetryEngine) -> None:
        barrier.wait()
        batch = engine.release_due(now=FUTURE, kind=RetryClass.PERCEPT)
        results.extend(item.disposition for item in batch)

    a = threading.Thread(target=release, args=(first_engine,))
    b = threading.Thread(target=release, args=(second_engine,))
    a.start()
    b.start()
    a.join()
    b.join()

    assert results == [RetryDisposition.READY]
    loaded = first_store.get(failed.operation_id)
    assert loaded.state is OperationState.PREPARED
    assert loaded.attempt == 1
    first_store.close()
    second_store.close()


def test_interrupted_percept_is_safely_rescheduled(tmp_path):
    store = _store(tmp_path)
    store.reserve(_operation("op-percept-recover", RetryClass.PERCEPT))
    store.start_attempt("op-percept-recover", updated_at=T1)
    engine = RetryEngine(store)

    results = engine.recover_interrupted_percept(recovered_at=T2)
    assert [item.disposition for item in results] == [RetryDisposition.SCHEDULED]
    loaded = store.get("op-percept-recover")
    assert loaded.state is OperationState.RETRY_WAIT
    assert loaded.attempt == 1
    assert loaded.last_error_code == "process_restart_idempotent_replay_safe"
    store.close()


def test_interrupted_cognition_with_accepted_receipt_completes(tmp_path):
    store = _store(tmp_path)
    store.reserve(_operation("op-cognition-recover-yes", RetryClass.COGNITION))
    store.start_attempt("op-cognition-recover-yes", updated_at=T1)
    engine = RetryEngine(store)

    results = engine.recover_interrupted_cognition(
        recovered_at=T2,
        receipt_accepted=lambda operation: True,
    )
    assert [item.disposition for item in results] == [RetryDisposition.COMPLETED]
    assert store.get("op-cognition-recover-yes").state is OperationState.COMPLETED
    store.close()


def test_interrupted_cognition_without_receipt_is_rescheduled(tmp_path):
    store = _store(tmp_path)
    store.reserve(_operation("op-cognition-recover-no", RetryClass.COGNITION))
    store.start_attempt("op-cognition-recover-no", updated_at=T1)
    engine = RetryEngine(store)

    results = engine.recover_interrupted_cognition(
        recovered_at=T2,
        receipt_accepted=lambda operation: False,
    )
    assert [item.disposition for item in results] == [RetryDisposition.SCHEDULED]
    loaded = store.get("op-cognition-recover-no")
    assert loaded.state is OperationState.RETRY_WAIT
    assert loaded.last_error_code == "process_restart_no_accepted_receipt"
    store.close()


def test_cognition_reconciliation_probe_failure_is_fail_closed(tmp_path):
    store = _store(tmp_path)
    store.reserve(_operation("op-cognition-probe-fail", RetryClass.COGNITION))
    store.start_attempt("op-cognition-probe-fail", updated_at=T1)
    engine = RetryEngine(store)

    def broken_probe(operation: BridgeOperation) -> bool:
        raise RuntimeError("receipt store unavailable")

    with pytest.raises(RuntimeError, match="receipt store unavailable"):
        engine.recover_interrupted_cognition(
            recovered_at=T2,
            receipt_accepted=broken_probe,
        )
    assert store.get("op-cognition-probe-fail").state is OperationState.IN_FLIGHT
    store.close()


def test_non_contact_recovery_never_mutates_contact_in_flight(tmp_path):
    store = _store(tmp_path)
    store.reserve(_operation("op-contact-owned-elsewhere", RetryClass.CONTACT))
    store.start_attempt("op-contact-owned-elsewhere", updated_at=T1)
    engine = RetryEngine(store)

    assert engine.recover_interrupted_percept(recovered_at=T2) == []
    assert engine.recover_interrupted_cognition(
        recovered_at=T2,
        receipt_accepted=lambda operation: False,
    ) == []
    assert store.get("op-contact-owned-elsewhere").state is OperationState.IN_FLIGHT
    store.close()


def test_initial_cognition_attempt_can_begin_without_retry_reconciliation(tmp_path):
    store = _store(tmp_path)
    store.reserve(_operation("op-cognition-initial", RetryClass.COGNITION))
    engine = RetryEngine(store)

    result = engine.begin_attempt("op-cognition-initial", now=T1)
    assert result.disposition is RetryDisposition.STARTED
    assert result.operation.state is OperationState.IN_FLIGHT
    assert result.operation.attempt == 1
    store.close()


def test_cognition_retry_begin_requires_last_moment_reconciliation(tmp_path):
    store = _store(tmp_path)
    engine = RetryEngine(store)
    cognition = _failed_safe(store, "op-cognition-last-mile", RetryClass.COGNITION)
    engine.schedule_failed_safe(
        cognition.operation_id,
        now=T2,
        cognition_receipt_accepted=lambda operation: False,
    )
    engine.release_due(
        now=FUTURE,
        kind=RetryClass.COGNITION,
        cognition_receipt_accepted=lambda operation: False,
    )

    with pytest.raises(RetryEngineError, match="cognition_reconciliation_required"):
        engine.begin_attempt(
            cognition.operation_id,
            now="2026-09-02T04:00:01Z",
        )
    assert store.get(cognition.operation_id).state is OperationState.PREPARED
    assert store.get(cognition.operation_id).attempt == 1
    store.close()


def test_late_cognition_receipt_at_attempt_boundary_prevents_retry_start(tmp_path):
    store = _store(tmp_path)
    engine = RetryEngine(store)
    cognition = _failed_safe(store, "op-cognition-boundary", RetryClass.COGNITION)
    engine.schedule_failed_safe(
        cognition.operation_id,
        now=T2,
        cognition_receipt_accepted=lambda operation: False,
    )
    engine.release_due(
        now=FUTURE,
        kind=RetryClass.COGNITION,
        cognition_receipt_accepted=lambda operation: False,
    )

    result = engine.begin_attempt(
        cognition.operation_id,
        now="2026-09-02T04:00:01Z",
        cognition_receipt_accepted=lambda operation: True,
    )
    assert result.disposition is RetryDisposition.COMPLETED
    assert result.operation.state is OperationState.COMPLETED
    assert result.operation.attempt == 1
    store.close()


def test_cognition_retry_begin_starts_only_after_negative_reconciliation(tmp_path):
    store = _store(tmp_path)
    engine = RetryEngine(store)
    cognition = _failed_safe(store, "op-cognition-boundary-no", RetryClass.COGNITION)
    engine.schedule_failed_safe(
        cognition.operation_id,
        now=T2,
        cognition_receipt_accepted=lambda operation: False,
    )
    engine.release_due(
        now=FUTURE,
        kind=RetryClass.COGNITION,
        cognition_receipt_accepted=lambda operation: False,
    )

    result = engine.begin_attempt(
        cognition.operation_id,
        now="2026-09-02T04:00:01Z",
        cognition_receipt_accepted=lambda operation: False,
    )
    assert result.disposition is RetryDisposition.STARTED
    assert result.operation.state is OperationState.IN_FLIGHT
    assert result.operation.attempt == 2
    store.close()


def test_contact_failed_safe_cannot_be_reclassified_completed_without_send_evidence(tmp_path):
    store = _store(tmp_path)
    contact = _failed_safe(store, "op-contact-no-late-complete", RetryClass.CONTACT)
    with pytest.raises(
        OperationStateConflict,
        match="late_completion_from_retry_state_is_cognition_only",
    ):
        store.mark_completed(contact.operation_id, updated_at=FUTURE)
    assert store.get(contact.operation_id).state is OperationState.FAILED_SAFE
    store.close()


def test_pending_failed_safe_sweep_resumes_after_scheduler_crash(tmp_path):
    store = _store(tmp_path)
    percept = _failed_safe(store, "op-percept-stranded", RetryClass.PERCEPT)
    contact = _failed_safe(store, "op-contact-stranded", RetryClass.CONTACT)
    store.close()

    reopened = _store(tmp_path)
    engine = RetryEngine(reopened)
    results = engine.schedule_pending_failed_safe(now=FUTURE)
    assert {item.operation.operation_id for item in results} == {
        percept.operation_id,
        contact.operation_id,
    }
    assert all(item.disposition is RetryDisposition.SCHEDULED for item in results)
    assert reopened.get(percept.operation_id).state is OperationState.RETRY_WAIT
    assert reopened.get(contact.operation_id).state is OperationState.RETRY_WAIT
    reopened.close()


def test_pending_failed_safe_cognition_requires_reconciliation_before_batch_mutation(tmp_path):
    store = _store(tmp_path)
    percept = _failed_safe(store, "op-percept-stranded-batch", RetryClass.PERCEPT)
    cognition = _failed_safe(store, "op-cognition-stranded-batch", RetryClass.COGNITION)
    engine = RetryEngine(store)

    with pytest.raises(RetryEngineError, match="cognition_reconciliation_required"):
        engine.schedule_pending_failed_safe(now=FUTURE)
    assert store.get(percept.operation_id).state is OperationState.FAILED_SAFE
    assert store.get(cognition.operation_id).state is OperationState.FAILED_SAFE
    store.close()


def test_pending_cognition_late_receipt_completes_instead_of_retry(tmp_path):
    store = _store(tmp_path)
    cognition = _failed_safe(store, "op-cognition-stranded-late", RetryClass.COGNITION)
    engine = RetryEngine(store)

    results = engine.schedule_pending_failed_safe(
        now=FUTURE,
        kind=RetryClass.COGNITION,
        cognition_receipt_accepted=lambda operation: True,
    )
    assert [item.disposition for item in results] == [RetryDisposition.COMPLETED]
    assert store.get(cognition.operation_id).state is OperationState.COMPLETED
    store.close()


def test_pending_failed_safe_limit_is_bounded(tmp_path):
    store = _store(tmp_path)
    engine = RetryEngine(store)
    with pytest.raises(ValueError, match="retry_pending_limit_out_of_range"):
        engine.schedule_pending_failed_safe(now=FUTURE, limit=0)
    with pytest.raises(ValueError, match="retry_pending_limit_out_of_range"):
        engine.schedule_pending_failed_safe(now=FUTURE, limit=1001)
    store.close()


def test_custom_cognition_policy_cannot_disable_reconciliation(tmp_path):
    store = _store(tmp_path)
    policies = dict(DEFAULT_RETRY_POLICIES)
    policies[RetryClass.COGNITION] = replace(
        COGNITION_RETRY_POLICY,
        reconcile_before_retry=False,
    )
    with pytest.raises(
        RetryEngineError,
        match="cognition_retry_requires_reconciliation",
    ):
        RetryEngine(store, policies=policies)
    store.close()


def test_pending_sweep_validates_time_even_when_empty(tmp_path):
    store = _store(tmp_path)
    engine = RetryEngine(store)
    with pytest.raises(RetryTimestampError, match="timestamp_must_be_timezone_aware"):
        engine.schedule_pending_failed_safe(now="2026-09-02T03:00:00")
    store.close()
