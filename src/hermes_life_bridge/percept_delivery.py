from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from .config import BridgeConfig
from .correlation import stable_id
from .model import RuntimeReceipt
from .operation_store import OperationStateConflict, OperationStore
from .reliability_contract import BridgeOperation, OperationState, RetryClass
from .retry_engine import RetryDisposition, RetryEngine
from .transport import UnixSocketTransport


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _request_hash(event: dict[str, Any]) -> str:
    raw = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class PerceptReliabilityExecutor:
    """Durable, content-free Percept delivery to Life Runtime.

    The durable outbox stores only the canonical Runtime Percept envelope; raw
    Hermes message content is never placed in the outbox.
    """

    def __init__(
        self,
        config: BridgeConfig,
        *,
        operation_store: OperationStore | None = None,
        transport: UnixSocketTransport | None = None,
    ):
        self.config = config
        operation_path = config.operation_db or str(
            Path(config.trace_path).with_name("operations.sqlite3")
        )
        self.store = operation_store or OperationStore(operation_path)
        self.retry = RetryEngine(self.store)
        self.transport = transport or UnixSocketTransport(config)

    def _initial_operation(self, event: dict[str, Any]) -> BridgeOperation:
        request_hash = _request_hash(event)
        idempotency_key = str(event["idempotency_key"])
        return BridgeOperation(
            operation_id=stable_id("percept-operation", idempotency_key, request_hash),
            kind=RetryClass.PERCEPT,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            state=OperationState.PREPARED,
            attempt=0,
            created_at=str(event.get("observed_at") or _now()),
            updated_at=_now(),
        )

    def submit(self, event: dict[str, Any]) -> RuntimeReceipt:
        operation, _created = self.store.reserve_percept(
            self._initial_operation(event),
            event,
        )
        return self.attempt(operation.operation_id)

    def _schedule_failed(self, operation_id: str, *, error: str) -> RuntimeReceipt:
        operation = self.store.get(operation_id)
        if operation is None:
            return RuntimeReceipt(ok=False, error="percept_operation_missing")
        if operation.state is OperationState.IN_FLIGHT:
            operation = self.store.mark_failed_safe(
                operation_id,
                updated_at=_now(),
                error_code=error,
            )
        if operation.state is OperationState.FAILED_SAFE:
            result = self.retry.schedule_failed_safe(operation_id, now=_now())
            if result.disposition is RetryDisposition.EXHAUSTED:
                return RuntimeReceipt(ok=False, error="percept_retry_exhausted")
            return RuntimeReceipt(ok=False, error="percept_retry_deferred")
        if operation.state is OperationState.EXHAUSTED:
            return RuntimeReceipt(ok=False, error="percept_retry_exhausted")
        return RuntimeReceipt(ok=False, error="percept_delivery_failed")

    def attempt(self, operation_id: str) -> RuntimeReceipt:
        operation = self.store.get(operation_id)
        if operation is None:
            return RuntimeReceipt(ok=False, error="percept_operation_missing")
        if operation.kind is not RetryClass.PERCEPT:
            return RuntimeReceipt(ok=False, error="percept_operation_kind_mismatch")
        if operation.state is OperationState.COMPLETED:
            return RuntimeReceipt(ok=True, duplicate=True, persisted=True)
        if operation.state is OperationState.EXHAUSTED:
            return RuntimeReceipt(ok=False, error="percept_retry_exhausted")
        if operation.state is OperationState.IN_FLIGHT:
            return RuntimeReceipt(ok=False, error="percept_in_flight")
        if operation.state is OperationState.FAILED_SAFE:
            return self._schedule_failed(operation_id, error=operation.last_error_code or "percept_failed_safe")
        if operation.state is OperationState.RETRY_WAIT:
            due = {
                item.operation_id
                for item in self.retry.due_operations(now=_now(), kind=RetryClass.PERCEPT)
            }
            if operation_id not in due:
                return RuntimeReceipt(ok=False, error="percept_retry_deferred")
            try:
                operation = self.store.make_retry_ready(operation_id, updated_at=_now())
            except OperationStateConflict:
                return RuntimeReceipt(ok=False, error="percept_retry_claimed")

        if operation.state is not OperationState.PREPARED:
            return RuntimeReceipt(ok=False, error="percept_not_ready")

        try:
            self.retry.begin_attempt(operation_id, now=_now())
        except OperationStateConflict:
            return RuntimeReceipt(ok=False, error="percept_retry_claimed")

        event = self.store.get_percept_event(operation_id)
        if event is None:
            return self._schedule_failed(operation_id, error="percept_outbox_payload_missing")
        try:
            receipt = self.transport.send_percept(event)
        except Exception:
            return self._schedule_failed(operation_id, error="runtime_transport_exception")

        if not receipt.ok:
            return self._schedule_failed(
                operation_id,
                error="runtime_ack_not_ok",
            )

        self.store.mark_completed(operation_id, updated_at=_now())
        self.store.clear_percept_event(operation_id)
        return receipt

    def pump(self, *, limit: int = 100) -> dict[str, int]:
        """Recover/release/deliver pending Percepts without manual intervention."""
        self.retry.recover_interrupted_percept(recovered_at=_now())
        self.retry.schedule_pending_failed_safe(
            now=_now(),
            kind=RetryClass.PERCEPT,
            limit=limit,
        )
        self.retry.release_due(now=_now(), kind=RetryClass.PERCEPT, limit=limit)

        attempted = 0
        completed = 0
        deferred = 0
        exhausted = 0
        for operation_id in self.store.list_percept_outbox_operation_ids()[:limit]:
            operation = self.store.get(operation_id)
            if operation is None:
                continue
            if operation.state is not OperationState.PREPARED:
                continue
            attempted += 1
            receipt = self.attempt(operation_id)
            if receipt.ok:
                completed += 1
            elif receipt.error == "percept_retry_exhausted":
                exhausted += 1
            else:
                deferred += 1
        return {
            "attempted": attempted,
            "completed": completed,
            "deferred": deferred,
            "exhausted": exhausted,
            "outbox_remaining": len(self.store.list_percept_outbox_operation_ids()),
        }
