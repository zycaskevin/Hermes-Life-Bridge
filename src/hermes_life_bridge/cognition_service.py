from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Callable

from .cognition_model import CognitiveReceipt, CognitiveTaskEnvelope, sha256_text
from .cognition_store import CognitionStore
from .config import BridgeConfig
from .correlation import stable_id
from .hermes_api import HermesApiClient
from .operation_store import OperationStateConflict, OperationStore
from .reliability_contract import BridgeOperation, OperationState, RetryClass
from .retry_engine import RetryDisposition, RetryEngine
from .trace import BridgeTracer


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _error_fingerprint(exc: Exception) -> str:
    return hashlib.sha256(str(exc).encode("utf-8")).hexdigest()[:16]


class CognitionService:
    def __init__(
        self,
        config: BridgeConfig | None = None,
        api_client=None,
        *,
        operation_store: OperationStore | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        self.config = config or BridgeConfig.from_env()
        self.api = api_client or HermesApiClient(self.config)
        self.trace = BridgeTracer(self.config.trace_path, max_bytes=self.config.trace_max_bytes, backup_count=self.config.trace_backup_count)
        self.store = CognitionStore(self.config.cognition_db)
        operation_path = self.config.operation_db or str(
            Path(self.config.cognition_db or self.config.trace_path).with_name(
                "operations.sqlite3"
            )
        )
        self.operations = operation_store or OperationStore(operation_path)
        self.retry = RetryEngine(self.operations)
        self.sleep_fn = sleep_fn
        self._startup_recovered = False

    def _validate(self, task: CognitiveTaskEnvelope) -> None:
        if task.life_did != self.config.life_did:
            raise ValueError("life_did_mismatch")
        if task.session_policy != "task_isolated":
            raise ValueError("unsupported_session_policy")
        if _parse_time(task.expires_at) <= datetime.now(timezone.utc):
            raise ValueError("task_expired")
        if task.risk_level not in {"L0", "L1", "L2", "L3"}:
            raise ValueError("invalid_risk_level")
        if task.risk_level in {"L2", "L3"}:
            raise ValueError("risk_requires_human_or_governed_path")

    def _initial_operation(self, task: CognitiveTaskEnvelope) -> BridgeOperation:
        request_hash = task.request_hash()
        return BridgeOperation(
            operation_id=stable_id(
                "cognition-operation",
                task.idempotency_key,
                request_hash,
            ),
            kind=RetryClass.COGNITION,
            idempotency_key=task.idempotency_key,
            request_hash=request_hash,
            state=OperationState.PREPARED,
            attempt=0,
            created_at=task.created_at,
            updated_at=_now(),
        )

    def _receipt_accepted(self, operation: BridgeOperation) -> bool:
        return self.store.get_receipt(operation.idempotency_key) is not None

    def _prepare_operation(self, operation: BridgeOperation) -> BridgeOperation:
        if operation.state is OperationState.COMPLETED:
            cached = self.store.get_receipt(operation.idempotency_key)
            if cached is None:
                raise RuntimeError("cognition_completed_receipt_missing")
            return operation
        if operation.state is OperationState.IN_FLIGHT:
            raise RuntimeError("cognition_operation_in_flight")
        if operation.state is OperationState.EXHAUSTED:
            raise RuntimeError("cognition_retry_exhausted")
        if operation.state is OperationState.FAILED_SAFE:
            result = self.retry.schedule_failed_safe(
                operation.operation_id,
                now=_now(),
                cognition_receipt_accepted=self._receipt_accepted,
            )
            if result.disposition is RetryDisposition.COMPLETED:
                return result.operation
            if result.disposition is RetryDisposition.EXHAUSTED:
                raise RuntimeError("cognition_retry_exhausted")
            operation = result.operation
        if operation.state is OperationState.RETRY_WAIT:
            due_at = _parse_time(operation.next_attempt_at or _now())
            delay = max(0.0, (due_at - datetime.now(timezone.utc)).total_seconds())
            if delay:
                self.sleep_fn(delay)
            results = self.retry.release_due(
                now=_now(),
                kind=RetryClass.COGNITION,
                cognition_receipt_accepted=self._receipt_accepted,
            )
            refreshed = self.operations.get(operation.operation_id)
            if refreshed is None:
                raise RuntimeError("cognition_operation_missing")
            operation = refreshed
            if operation.state is OperationState.COMPLETED:
                return operation
            if operation.state is not OperationState.PREPARED:
                # A competing worker may have claimed it.
                raise RuntimeError("cognition_retry_not_ready")
        return operation

    def process(self, task: CognitiveTaskEnvelope) -> CognitiveReceipt:
        trace_id = stable_id("cognition-trace", task.task_id)
        self.trace.emit(
            trace_id=trace_id,
            stage="COGNITION_TASK_RECEIVED",
            task_id=task.task_id,
            basis_state_sequence=task.basis_state_sequence,
            projection_hash=task.projection_hash,
        )
        self._validate(task)
        cached = self.store.get_receipt(task.idempotency_key)
        if cached:
            self.trace.emit(
                trace_id=trace_id,
                stage="COGNITION_DEDUPE_HIT",
                task_id=task.task_id,
                receipt_id=cached.receipt_id,
            )
            return cached

        self.store.reserve_task(task)
        operation, _created = self.operations.reserve(self._initial_operation(task))
        last_error: Exception | None = None

        while True:
            cached = self.store.get_receipt(task.idempotency_key)
            if cached:
                return cached
            operation = self.operations.get(operation.operation_id) or operation
            operation = self._prepare_operation(operation)
            if operation.state is OperationState.COMPLETED:
                cached = self.store.get_receipt(task.idempotency_key)
                if cached:
                    return cached
                raise RuntimeError("cognition_completed_receipt_missing")
            if operation.state is not OperationState.PREPARED:
                raise RuntimeError("cognition_operation_not_prepared")

            begin = self.retry.begin_attempt(
                operation.operation_id,
                now=_now(),
                cognition_receipt_accepted=self._receipt_accepted,
            )
            if begin.disposition is RetryDisposition.COMPLETED:
                cached = self.store.get_receipt(task.idempotency_key)
                if cached:
                    return cached
                raise RuntimeError("cognition_completed_receipt_missing")

            started_at = _now()
            self.trace.emit(
                trace_id=trace_id,
                stage="HERMES_API_CONNECT",
                task_id=task.task_id,
                endpoint=self.config.hermes_api_base_url,
                attempt=begin.operation.attempt,
            )
            try:
                output, session_id = self.api.cognize(
                    instruction=task.instruction,
                    task_id=task.task_id,
                )
            except Exception as exc:
                last_error = exc
                failed = self.operations.mark_failed_safe(
                    operation.operation_id,
                    updated_at=_now(),
                    error_code="hermes_api_failed_safe",
                )
                self.trace.emit(
                    trace_id=trace_id,
                    stage="COGNITION_FAILED",
                    status="fail",
                    task_id=task.task_id,
                    error=type(exc).__name__,
                    error_fingerprint=_error_fingerprint(exc),
                    attempt=failed.attempt,
                )
                result = self.retry.schedule_failed_safe(
                    operation.operation_id,
                    now=_now(),
                    cognition_receipt_accepted=self._receipt_accepted,
                )
                if result.disposition is RetryDisposition.EXHAUSTED:
                    raise RuntimeError("cognition_retry_exhausted") from last_error
                if result.disposition is RetryDisposition.COMPLETED:
                    cached = self.store.get_receipt(task.idempotency_key)
                    if cached:
                        return cached
                    raise RuntimeError("cognition_completed_receipt_missing")
                delay = float(result.delay_seconds or 0.0)
                if delay:
                    self.sleep_fn(delay)
                operation = self.operations.make_retry_ready(
                    operation.operation_id,
                    updated_at=_now(),
                )
                continue

            completed_at = _now()
            receipt = CognitiveReceipt(
                receipt_id=stable_id(
                    "cognitive-receipt",
                    task.task_id,
                    task.request_hash(),
                    sha256_text(output),
                ),
                task_id=task.task_id,
                idempotency_key=task.idempotency_key,
                life_did=task.life_did,
                status="completed",
                basis_state_sequence=task.basis_state_sequence,
                basis_state_hash=task.basis_state_hash,
                projection_hash=task.projection_hash,
                output_text=output,
                output_hash=sha256_text(output),
                hermes_session_id=session_id,
                request_hash=task.request_hash(),
                started_at=started_at,
                completed_at=completed_at,
            )
            # Receipt first; if the process crashes before the operation transition,
            # startup reconciliation proves completion and avoids duplicate compute.
            self.store.save_receipt(receipt)
            try:
                self.operations.mark_completed(
                    operation.operation_id,
                    updated_at=completed_at,
                )
            except OperationStateConflict:
                pass
            self.trace.emit(
                trace_id=trace_id,
                stage="HERMES_API_RESPONSE",
                task_id=task.task_id,
                receipt_id=receipt.receipt_id,
                output_hash=receipt.output_hash,
                hermes_session_id=session_id,
                attempt=begin.operation.attempt,
            )
            return receipt

    def recover_startup(self) -> dict[str, int]:
        if self._startup_recovered:
            return {"completed": 0, "scheduled": 0, "exhausted": 0}
        results = self.retry.recover_interrupted_cognition(
            recovered_at=_now(),
            receipt_accepted=self._receipt_accepted,
        )
        self._startup_recovered = True
        return {
            "completed": sum(
                item.disposition is RetryDisposition.COMPLETED for item in results
            ),
            "scheduled": sum(
                item.disposition is RetryDisposition.SCHEDULED for item in results
            ),
            "exhausted": sum(
                item.disposition is RetryDisposition.EXHAUSTED for item in results
            ),
        }

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=5)
            if not raw:
                return
            data = json.loads(raw.decode("utf-8"))
            task = CognitiveTaskEnvelope(**data)
            receipt = await asyncio.to_thread(self.process, task)
            trace_id = stable_id("cognition-trace", task.task_id)
            self.trace.emit(
                trace_id=trace_id,
                stage="COGNITIVE_RECEIPT_SENT",
                task_id=task.task_id,
                receipt_id=receipt.receipt_id,
                duplicate=receipt.duplicate,
            )
            writer.write(
                (
                    json.dumps(
                        {"ok": True, "receipt": receipt.to_dict()},
                        ensure_ascii=False,
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()
        except Exception as exc:
            try:
                writer.write(
                    (
                        json.dumps(
                            {"ok": False, "error": str(exc)[:500]},
                            ensure_ascii=False,
                        )
                        + "\n"
                    ).encode()
                )
                await writer.drain()
            except Exception:
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def serve(self) -> None:
        self.recover_startup()
        sock = Path(self.config.cognition_socket)
        sock.parent.mkdir(parents=True, exist_ok=True)
        if sock.exists():
            sock.unlink()
        server = await asyncio.start_unix_server(self.handle_client, path=str(sock))
        os.chmod(sock, 0o660)
        async with server:
            await server.serve_forever()
