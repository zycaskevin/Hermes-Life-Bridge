from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
from typing import Callable, Mapping

from .operation_store import OperationStateConflict, OperationStore
from .reliability_contract import BridgeOperation, OperationState, RetryClass, RetryPolicy


class RetryEngineError(RuntimeError):
    pass


class RetryTimestampError(RetryEngineError):
    pass


class RetryDisposition(str, Enum):
    SCHEDULED = "scheduled"
    EXHAUSTED = "exhausted"
    COMPLETED = "completed"
    READY = "ready"
    STARTED = "started"


@dataclass(frozen=True)
class RetryResult:
    disposition: RetryDisposition
    operation: BridgeOperation
    delay_seconds: float | None = None


PERCEPT_RETRY_POLICY = RetryPolicy(
    retry_class=RetryClass.PERCEPT,
    max_attempts=5,
    initial_backoff_seconds=0.25,
    max_backoff_seconds=4.0,
    backoff_multiplier=2.0,
    jitter_ratio=0.20,
    retry_after_failed_safe=True,
    retry_after_delivery_unknown=False,
    requires_durable_state=True,
    reconcile_before_retry=False,
)

COGNITION_RETRY_POLICY = RetryPolicy(
    retry_class=RetryClass.COGNITION,
    max_attempts=3,
    initial_backoff_seconds=1.0,
    max_backoff_seconds=8.0,
    backoff_multiplier=2.0,
    jitter_ratio=0.20,
    retry_after_failed_safe=True,
    retry_after_delivery_unknown=False,
    requires_durable_state=True,
    reconcile_before_retry=True,
)

CONTACT_RETRY_POLICY = RetryPolicy(
    retry_class=RetryClass.CONTACT,
    max_attempts=2,
    initial_backoff_seconds=2.0,
    max_backoff_seconds=8.0,
    backoff_multiplier=2.0,
    jitter_ratio=0.20,
    retry_after_failed_safe=True,
    retry_after_delivery_unknown=False,
    requires_durable_state=True,
    reconcile_before_retry=False,
)

DEFAULT_RETRY_POLICIES: Mapping[RetryClass, RetryPolicy] = {
    RetryClass.PERCEPT: PERCEPT_RETRY_POLICY,
    RetryClass.COGNITION: COGNITION_RETRY_POLICY,
    RetryClass.CONTACT: CONTACT_RETRY_POLICY,
}


def _parse_timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise RetryTimestampError("timestamp_required")
    raw = value.strip()
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise RetryTimestampError("timestamp_must_be_iso8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RetryTimestampError("timestamp_must_be_timezone_aware")
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    value = value.astimezone(timezone.utc)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _deterministic_unit_interval(operation: BridgeOperation) -> float:
    seed = (
        f"{operation.kind.value}|{operation.operation_id}|"
        f"{operation.idempotency_key}|{operation.attempt}"
    ).encode("utf-8")
    raw = hashlib.sha256(seed).digest()[:8]
    return int.from_bytes(raw, "big") / float((1 << 64) - 1)


def retry_delay_seconds(operation: BridgeOperation, policy: RetryPolicy) -> float:
    """Return deterministic bounded backoff after the current failed attempt."""
    if operation.kind is not policy.retry_class:
        raise RetryEngineError("retry_policy_class_mismatch")
    if operation.attempt < 1:
        raise RetryEngineError("retry_delay_requires_attempt")

    nominal = float(policy.initial_backoff_seconds)
    remaining_growth = operation.attempt - 1
    while remaining_growth > 0 and nominal < policy.max_backoff_seconds:
        nominal = min(
            float(policy.max_backoff_seconds),
            nominal * float(policy.backoff_multiplier),
        )
        remaining_growth -= 1

    if policy.jitter_ratio == 0 or nominal == 0:
        return nominal

    # Stable per operation/attempt. This avoids restart-dependent schedules while
    # still de-synchronizing independent operations.
    unit = _deterministic_unit_interval(operation)
    factor = 1.0 + ((unit * 2.0) - 1.0) * float(policy.jitter_ratio)
    return max(0.0, min(float(policy.max_backoff_seconds), nominal * factor))


class RetryEngine:
    """Policy/scheduling layer over OperationStore; never executes operations."""

    def __init__(
        self,
        store: OperationStore,
        policies: Mapping[RetryClass, RetryPolicy] | None = None,
    ):
        self.store = store
        expected = set(RetryClass)
        if policies is None:
            selected = dict(DEFAULT_RETRY_POLICIES)
        else:
            selected = dict(policies)
        if set(selected) != expected:
            raise RetryEngineError("retry_policy_registry_must_cover_all_classes")
        for kind, policy in selected.items():
            if policy.retry_class is not kind:
                raise RetryEngineError("retry_policy_registry_class_mismatch")
            if not policy.requires_durable_state:
                raise RetryEngineError("retry_policy_must_require_durable_state")
            if kind is RetryClass.COGNITION and not policy.reconcile_before_retry:
                raise RetryEngineError("cognition_retry_requires_reconciliation")
            if kind is RetryClass.CONTACT and policy.retry_after_delivery_unknown:
                raise RetryEngineError("contact_unknown_delivery_cannot_retry")
            if kind is RetryClass.CONTACT and policy.reconcile_before_retry:
                raise RetryEngineError("contact_reconciliation_belongs_to_hlb0044")
        self.policies = selected

    def policy_for(self, kind: RetryClass) -> RetryPolicy:
        return self.policies[kind]

    def schedule_failed_safe(
        self,
        operation_id: str,
        *,
        now: str,
        cognition_receipt_accepted: Callable[[BridgeOperation], bool] | None = None,
    ) -> RetryResult:
        """Apply the class policy to one already durable FAILED_SAFE operation."""
        now_dt = _parse_timestamp(now)
        operation = self.store.get(operation_id)
        if operation is None:
            raise KeyError("operation_not_found")
        if operation.state is not OperationState.FAILED_SAFE:
            raise OperationStateConflict("retry_engine_requires_failed_safe")

        policy = self.policy_for(operation.kind)
        if policy.reconcile_before_retry:
            if operation.kind is not RetryClass.COGNITION:
                raise RetryEngineError("unsupported_reconciliation_policy")
            if cognition_receipt_accepted is None:
                raise RetryEngineError("cognition_reconciliation_required")
            if cognition_receipt_accepted(operation):
                completed = self.store.mark_completed(
                    operation.operation_id,
                    updated_at=_format_timestamp(now_dt),
                )
                return RetryResult(RetryDisposition.COMPLETED, completed)

        if not policy.retry_after_failed_safe or operation.attempt >= policy.max_attempts:
            exhausted = self.store.mark_exhausted(
                operation.operation_id,
                updated_at=_format_timestamp(now_dt),
            )
            return RetryResult(RetryDisposition.EXHAUSTED, exhausted)

        delay = retry_delay_seconds(operation, policy)
        next_attempt_at = _format_timestamp(now_dt + timedelta(seconds=delay))
        waiting = self.store.schedule_retry(
            operation.operation_id,
            next_attempt_at=next_attempt_at,
            updated_at=_format_timestamp(now_dt),
        )
        return RetryResult(RetryDisposition.SCHEDULED, waiting, delay)

    def schedule_pending_failed_safe(
        self,
        *,
        now: str,
        kind: RetryClass | None = None,
        limit: int = 100,
        cognition_receipt_accepted: Callable[[BridgeOperation], bool] | None = None,
    ) -> list[RetryResult]:
        """Resume durable FAILED_SAFE operations after a scheduler/process crash."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 1000:
            raise ValueError("retry_pending_limit_out_of_range")
        canonical_now = _format_timestamp(_parse_timestamp(now))
        candidates = self.store.list_operations(
            kind=kind,
            state=OperationState.FAILED_SAFE,
        )[:limit]
        if any(self.policy_for(item.kind).reconcile_before_retry for item in candidates):
            if cognition_receipt_accepted is None:
                raise RetryEngineError("cognition_reconciliation_required")

        results: list[RetryResult] = []
        for operation in candidates:
            try:
                results.append(
                    self.schedule_failed_safe(
                        operation.operation_id,
                        now=canonical_now,
                        cognition_receipt_accepted=cognition_receipt_accepted,
                    )
                )
            except OperationStateConflict:
                continue
        return results

    def due_operations(
        self,
        *,
        now: str,
        kind: RetryClass | None = None,
        limit: int = 100,
    ) -> list[BridgeOperation]:
        """Read due RETRY_WAIT operations without mutating or executing them."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 1000:
            raise ValueError("retry_due_limit_out_of_range")
        now_dt = _parse_timestamp(now)
        candidates = self.store.list_operations(kind=kind, state=OperationState.RETRY_WAIT)
        due: list[tuple[datetime, BridgeOperation]] = []
        for operation in candidates:
            if operation.next_attempt_at is None:
                raise RetryTimestampError("retry_wait_missing_next_attempt_at")
            due_at = _parse_timestamp(operation.next_attempt_at)
            if due_at <= now_dt:
                due.append((due_at, operation))
        due.sort(key=lambda item: (item[0], item[1].operation_id))
        return [operation for _, operation in due[:limit]]

    def release_due(
        self,
        *,
        now: str,
        kind: RetryClass | None = None,
        limit: int = 100,
        cognition_receipt_accepted: Callable[[BridgeOperation], bool] | None = None,
    ) -> list[RetryResult]:
        """Release due retries to PREPARED; never starts or executes an attempt."""
        canonical_now = _format_timestamp(_parse_timestamp(now))
        due = self.due_operations(now=now, kind=kind, limit=limit)
        if any(self.policy_for(item.kind).reconcile_before_retry for item in due):
            if cognition_receipt_accepted is None:
                raise RetryEngineError("cognition_reconciliation_required")

        released: list[RetryResult] = []
        for operation in due:
            policy = self.policy_for(operation.kind)
            if policy.reconcile_before_retry:
                if operation.kind is not RetryClass.COGNITION:
                    raise RetryEngineError("unsupported_reconciliation_policy")
                if cognition_receipt_accepted is None:
                    raise RetryEngineError("cognition_reconciliation_required")
                if cognition_receipt_accepted(operation):
                    try:
                        completed = self.store.mark_completed(
                            operation.operation_id,
                            updated_at=canonical_now,
                        )
                    except OperationStateConflict:
                        continue
                    released.append(
                        RetryResult(RetryDisposition.COMPLETED, completed)
                    )
                    continue

            try:
                ready = self.store.make_retry_ready(
                    operation.operation_id,
                    updated_at=canonical_now,
                )
            except OperationStateConflict:
                # Another scheduler may have claimed it between selection and update.
                continue
            released.append(RetryResult(RetryDisposition.READY, ready))
        return released

    def begin_attempt(
        self,
        operation_id: str,
        *,
        now: str,
        cognition_receipt_accepted: Callable[[BridgeOperation], bool] | None = None,
    ) -> RetryResult:
        """Durably begin an attempt; never invokes the operation executor itself."""
        canonical_now = _format_timestamp(_parse_timestamp(now))
        operation = self.store.get(operation_id)
        if operation is None:
            raise KeyError("operation_not_found")
        if operation.state is not OperationState.PREPARED:
            raise OperationStateConflict("begin_attempt_requires_prepared")

        policy = self.policy_for(operation.kind)
        if operation.attempt > 0 and policy.reconcile_before_retry:
            if operation.kind is not RetryClass.COGNITION:
                raise RetryEngineError("unsupported_reconciliation_policy")
            if cognition_receipt_accepted is None:
                raise RetryEngineError("cognition_reconciliation_required")
            if cognition_receipt_accepted(operation):
                completed = self.store.mark_completed(
                    operation.operation_id,
                    updated_at=canonical_now,
                )
                return RetryResult(RetryDisposition.COMPLETED, completed)

        started = self.store.start_attempt(
            operation.operation_id,
            updated_at=canonical_now,
        )
        return RetryResult(RetryDisposition.STARTED, started)

    def recover_interrupted_percept(self, *, recovered_at: str) -> list[RetryResult]:
        """Percept replay is safe because ingress uses deterministic idempotency."""
        canonical_time = _format_timestamp(_parse_timestamp(recovered_at))
        results: list[RetryResult] = []
        operations = self.store.list_operations(
            kind=RetryClass.PERCEPT,
            state=OperationState.IN_FLIGHT,
        )
        for operation in operations:
            try:
                self.store.mark_failed_safe(
                    operation.operation_id,
                    updated_at=canonical_time,
                    error_code="process_restart_idempotent_replay_safe",
                )
                results.append(
                    self.schedule_failed_safe(operation.operation_id, now=canonical_time)
                )
            except OperationStateConflict:
                continue
        return results

    def recover_interrupted_cognition(
        self,
        *,
        recovered_at: str,
        receipt_accepted: Callable[[BridgeOperation], bool],
    ) -> list[RetryResult]:
        """Reconcile Cognition receipt existence before allowing retry after restart."""
        if not callable(receipt_accepted):
            raise TypeError("receipt_accepted_must_be_callable")
        canonical_time = _format_timestamp(_parse_timestamp(recovered_at))
        results: list[RetryResult] = []
        operations = self.store.list_operations(
            kind=RetryClass.COGNITION,
            state=OperationState.IN_FLIGHT,
        )
        for operation in operations:
            # Probe first. Probe failure is fail-closed: leave durable IN_FLIGHT state
            # unchanged so a later owner can retry reconciliation safely.
            if receipt_accepted(operation):
                try:
                    completed = self.store.mark_completed(
                        operation.operation_id,
                        updated_at=canonical_time,
                    )
                except OperationStateConflict:
                    continue
                results.append(RetryResult(RetryDisposition.COMPLETED, completed))
                continue

            try:
                self.store.mark_failed_safe(
                    operation.operation_id,
                    updated_at=canonical_time,
                    error_code="process_restart_no_accepted_receipt",
                )
                results.append(
                    self.schedule_failed_safe(
                        operation.operation_id,
                        now=canonical_time,
                        cognition_receipt_accepted=receipt_accepted,
                    )
                )
            except OperationStateConflict:
                continue
        return results
