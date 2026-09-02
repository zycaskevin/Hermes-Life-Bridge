from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
import json
import re
import sqlite3
import threading

from .representation import canonicalize_operational_value, contains_forbidden_representation_bytes
from .reliability_contract import (
    BridgeOperation,
    DeliveryOutcome,
    OperationState,
    RetryClass,
    is_operation_transition_allowed,
)


class OperationStoreError(RuntimeError):
    pass


class OperationConflictError(OperationStoreError):
    pass


class OperationStateConflict(OperationStoreError):
    pass


class OperationCorruptionError(OperationStoreError):
    pass


class OperationStore:
    """Crash-safe operational persistence for HLB reliability state.

    This store intentionally contains no message/prompt/route payload columns. It is
    operational state, not canonical Life Runtime state or memory.
    """

    SCHEMA_VERSION = 2

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
        self.conn.row_factory = sqlite3.Row
        self._secure_files()

        with self._lock:
            self.conn.execute("PRAGMA busy_timeout=5000")
            self.conn.execute("PRAGMA journal_mode=WAL")
            self._secure_files()
            self.conn.execute("PRAGMA synchronous=FULL")
            self.conn.execute("PRAGMA secure_delete=ON")
            self.conn.execute("PRAGMA foreign_keys=ON")
            current_schema = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
            if current_schema > self.SCHEMA_VERSION:
                self._secure_files()
                self.conn.close()
                raise OperationStoreError("operation_store_schema_newer_than_supported")
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS bridge_operations(
                    operation_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK(kind IN ('percept','cognition','contact')),
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL CHECK(length(request_hash) = 64),
                    state TEXT NOT NULL CHECK(state IN (
                        'prepared','in_flight','retry_wait','completed',
                        'failed_safe','delivery_unknown','exhausted'
                    )),
                    attempt INTEGER NOT NULL CHECK(attempt >= 0),
                    next_attempt_at TEXT,
                    delivery_outcome TEXT CHECK(
                        delivery_outcome IS NULL OR delivery_outcome IN (
                            'not_attempted','delivered','failed_safe','delivery_unknown'
                        )
                    ),
                    last_error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    schema_version TEXT NOT NULL CHECK(schema_version = 'v0.4'),
                    UNIQUE(kind, idempotency_key),
                    CHECK(
                        (state = 'retry_wait' AND next_attempt_at IS NOT NULL)
                        OR
                        (state != 'retry_wait' AND next_attempt_at IS NULL)
                    )
                );
                CREATE INDEX IF NOT EXISTS idx_bridge_operations_kind_state
                    ON bridge_operations(kind, state);
                CREATE INDEX IF NOT EXISTS idx_bridge_operations_retry_wait
                    ON bridge_operations(state, next_attempt_at);
                CREATE TABLE IF NOT EXISTS percept_outbox(
                    operation_id TEXT PRIMARY KEY,
                    event_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(operation_id) REFERENCES bridge_operations(operation_id) ON DELETE CASCADE
                );
                """
            )
            if current_schema < self.SCHEMA_VERSION:
                self.conn.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")
            self.conn.commit()
        self._secure_files()

    def close(self) -> None:
        with self._lock:
            self._secure_files()
            self.conn.close()
        self._secure_files()

    def _secure_files(self) -> None:
        for candidate in (
            self.path,
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
        ):
            if not candidate.exists():
                continue
            try:
                candidate.chmod(0o600)
            except Exception:
                pass

    @staticmethod
    def _row_to_operation(row: sqlite3.Row) -> BridgeOperation:
        try:
            outcome_raw = row["delivery_outcome"]
            return BridgeOperation(
                operation_id=row["operation_id"],
                kind=RetryClass(row["kind"]),
                idempotency_key=row["idempotency_key"],
                request_hash=row["request_hash"],
                state=OperationState(row["state"]),
                attempt=int(row["attempt"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                next_attempt_at=row["next_attempt_at"],
                delivery_outcome=(
                    DeliveryOutcome(outcome_raw) if outcome_raw is not None else None
                ),
                last_error_code=row["last_error_code"],
                schema_version=row["schema_version"],
            )
        except Exception as exc:
            raise OperationCorruptionError("invalid_bridge_operation_row") from exc

    @staticmethod
    def _validate_initial(operation: BridgeOperation) -> None:
        canonical = operation.to_dict()
        if canonical["operation_id"] != operation.operation_id:
            raise ValueError("operation_id_must_be_canonical")
        if canonical["idempotency_key"] != operation.idempotency_key:
            raise ValueError("idempotency_key_must_be_canonical")
        route_prefix = r"^(?:feishu|telegram|discord|slack|signal|sms):"
        if re.match(route_prefix, operation.operation_id, re.IGNORECASE):
            raise ValueError("operation_id_must_not_be_exact_route")
        if re.match(route_prefix, operation.idempotency_key, re.IGNORECASE):
            raise ValueError("idempotency_key_must_not_be_exact_route")
        if operation.state is not OperationState.PREPARED:
            raise ValueError("initial_operation_must_be_prepared")
        if operation.attempt != 0:
            raise ValueError("initial_operation_attempt_must_be_zero")
        if operation.next_attempt_at is not None:
            raise ValueError("initial_operation_cannot_schedule_retry")
        if operation.last_error_code is not None:
            raise ValueError("initial_operation_cannot_have_error")
        if operation.kind is RetryClass.CONTACT and operation.delivery_outcome not in (
            None,
            DeliveryOutcome.NOT_ATTEMPTED,
        ):
            raise ValueError("initial_contact_outcome_must_be_not_attempted")

    @staticmethod
    def _operation_values(operation: BridgeOperation) -> tuple:
        data = operation.to_dict()
        return (
            data["operation_id"],
            data["kind"],
            data["idempotency_key"],
            data["request_hash"],
            data["state"],
            data["attempt"],
            data["next_attempt_at"],
            data["delivery_outcome"],
            data["last_error_code"],
            data["created_at"],
            data["updated_at"],
            data["schema_version"],
        )

    @staticmethod
    def _canonical_percept_event(event: dict) -> dict:
        allowed = {
            "event_id",
            "life_did",
            "source_body_id",
            "modality",
            "observed_at",
            "payload_ref",
            "salience_hint",
            "idempotency_key",
            "schema_version",
        }
        if not isinstance(event, dict) or set(event) != allowed:
            raise ValueError("percept_outbox_event_shape_invalid")
        safe = canonicalize_operational_value(event)
        raw = json.dumps(safe, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if contains_forbidden_representation_bytes(raw):
            raise ValueError("percept_outbox_representation_forbidden")
        for forbidden in ("message", "text", "content", "target", "chat_id", "thread_id"):
            if forbidden in safe:
                raise ValueError("percept_outbox_private_payload_forbidden")
        return safe

    def reserve_percept(
        self,
        operation: BridgeOperation,
        event: dict,
    ) -> tuple[BridgeOperation, bool]:
        """Atomically reserve a Percept operation and its content-free Runtime event."""
        self._validate_initial(operation)
        if operation.kind is not RetryClass.PERCEPT:
            raise ValueError("reserve_percept_requires_percept_operation")
        safe_event = self._canonical_percept_event(event)
        if safe_event["idempotency_key"] != operation.idempotency_key:
            raise ValueError("percept_outbox_idempotency_mismatch")
        payload = json.dumps(safe_event, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute(
                    "SELECT * FROM bridge_operations WHERE kind=? AND idempotency_key=?",
                    (operation.kind.value, operation.idempotency_key),
                ).fetchone()
                if row is not None:
                    existing = self._row_to_operation(row)
                    if existing.request_hash != operation.request_hash:
                        raise OperationConflictError("idempotency_key_reused_with_different_request")
                    outbox = self.conn.execute(
                        "SELECT event_json FROM percept_outbox WHERE operation_id=?",
                        (existing.operation_id,),
                    ).fetchone()
                    if existing.state is not OperationState.COMPLETED and outbox is None:
                        raise OperationCorruptionError("percept_outbox_payload_missing")
                    self.conn.commit()
                    return existing, False

                collision = self.conn.execute(
                    "SELECT 1 FROM bridge_operations WHERE operation_id=?",
                    (operation.operation_id,),
                ).fetchone()
                if collision is not None:
                    raise OperationConflictError("operation_id_reused")
                self.conn.execute(
                    """
                    INSERT INTO bridge_operations(
                        operation_id,kind,idempotency_key,request_hash,state,attempt,
                        next_attempt_at,delivery_outcome,last_error_code,created_at,
                        updated_at,schema_version
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    self._operation_values(operation),
                )
                self.conn.execute(
                    "INSERT INTO percept_outbox(operation_id,event_json,created_at) VALUES(?,?,?)",
                    (operation.operation_id, payload, operation.created_at),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        self._secure_files()
        return operation, True

    def get_percept_event(self, operation_id: str) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT event_json FROM percept_outbox WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row["event_json"])
            return self._canonical_percept_event(data)
        except Exception as exc:
            raise OperationCorruptionError("invalid_percept_outbox_payload") from exc

    def clear_percept_event(self, operation_id: str) -> None:
        with self._lock:
            self.conn.execute(
                "DELETE FROM percept_outbox WHERE operation_id=?",
                (operation_id,),
            )
            self.conn.commit()

    def list_percept_outbox_operation_ids(self) -> list[str]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT operation_id FROM percept_outbox ORDER BY created_at, operation_id"
            ).fetchall()
        return [str(row["operation_id"]) for row in rows]

    def reserve(self, operation: BridgeOperation) -> tuple[BridgeOperation, bool]:
        """Reserve an initial operation.

        Returns ``(operation, created)``. Reusing the same kind/idempotency key
        with the same request hash returns the already durable operation. A
        different request hash is an idempotency conflict.
        """
        self._validate_initial(operation)
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute(
                    "SELECT * FROM bridge_operations WHERE kind=? AND idempotency_key=?",
                    (operation.kind.value, operation.idempotency_key),
                ).fetchone()
                if row is not None:
                    existing = self._row_to_operation(row)
                    if existing.request_hash != operation.request_hash:
                        raise OperationConflictError(
                            "idempotency_key_reused_with_different_request"
                        )
                    self.conn.commit()
                    return existing, False

                collision = self.conn.execute(
                    "SELECT 1 FROM bridge_operations WHERE operation_id=?",
                    (operation.operation_id,),
                ).fetchone()
                if collision is not None:
                    raise OperationConflictError("operation_id_reused")

                self.conn.execute(
                    """
                    INSERT INTO bridge_operations(
                        operation_id,kind,idempotency_key,request_hash,state,attempt,
                        next_attempt_at,delivery_outcome,last_error_code,created_at,
                        updated_at,schema_version
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    self._operation_values(operation),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        self._secure_files()
        return operation, True

    def get(self, operation_id: str) -> BridgeOperation | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM bridge_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        return self._row_to_operation(row) if row is not None else None

    def get_by_idempotency(
        self, kind: RetryClass, idempotency_key: str
    ) -> BridgeOperation | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM bridge_operations WHERE kind=? AND idempotency_key=?",
                (kind.value, idempotency_key),
            ).fetchone()
        return self._row_to_operation(row) if row is not None else None

    def list_operations(
        self,
        *,
        kind: RetryClass | None = None,
        state: OperationState | None = None,
    ) -> list[BridgeOperation]:
        clauses: list[str] = []
        params: list[str] = []
        if kind is not None:
            clauses.append("kind=?")
            params.append(kind.value)
        if state is not None:
            clauses.append("state=?")
            params.append(state.value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self.conn.execute(
                f"SELECT * FROM bridge_operations{where} ORDER BY created_at, operation_id",
                params,
            ).fetchall()
        return [self._row_to_operation(row) for row in rows]

    def _persist_transition(
        self,
        current: BridgeOperation,
        candidate: BridgeOperation,
    ) -> BridgeOperation:
        if not is_operation_transition_allowed(current, candidate):
            raise OperationStateConflict(
                f"transition_not_allowed:{current.state.value}:{candidate.state.value}"
            )

        result = self.conn.execute(
            """
            UPDATE bridge_operations SET
                state=?,attempt=?,next_attempt_at=?,delivery_outcome=?,
                last_error_code=?,updated_at=?,schema_version=?
            WHERE operation_id=? AND state=? AND attempt=?
            """,
            (
                candidate.state.value,
                candidate.attempt,
                candidate.next_attempt_at,
                candidate.delivery_outcome.value
                if candidate.delivery_outcome is not None
                else None,
                candidate.last_error_code,
                candidate.updated_at,
                candidate.schema_version,
                current.operation_id,
                current.state.value,
                current.attempt,
            ),
        )
        if result.rowcount != 1:
            raise OperationStateConflict("operation_changed_concurrently")
        return candidate

    def _load_for_update(self, operation_id: str) -> BridgeOperation:
        row = self.conn.execute(
            "SELECT * FROM bridge_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise KeyError("operation_not_found")
        return self._row_to_operation(row)

    def _transition_transaction(self, operation_id: str, builder) -> BridgeOperation:
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                current = self._load_for_update(operation_id)
                candidate = builder(current)
                result = self._persist_transition(current, candidate)
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        self._secure_files()
        return result

    def start_attempt(self, operation_id: str, *, updated_at: str) -> BridgeOperation:
        def build(current: BridgeOperation) -> BridgeOperation:
            if current.state is not OperationState.PREPARED:
                raise OperationStateConflict("start_attempt_requires_prepared")
            return BridgeOperation(
                operation_id=current.operation_id,
                kind=current.kind,
                idempotency_key=current.idempotency_key,
                request_hash=current.request_hash,
                state=OperationState.IN_FLIGHT,
                attempt=current.attempt + 1,
                created_at=current.created_at,
                updated_at=updated_at,
                delivery_outcome=(
                    DeliveryOutcome.NOT_ATTEMPTED
                    if current.kind is RetryClass.CONTACT
                    else None
                ),
                schema_version=current.schema_version,
            )

        return self._transition_transaction(operation_id, build)

    def mark_completed(self, operation_id: str, *, updated_at: str) -> BridgeOperation:
        def build(current: BridgeOperation) -> BridgeOperation:
            if current.state in (
                OperationState.PREPARED,
                OperationState.FAILED_SAFE,
                OperationState.RETRY_WAIT,
            ):
                if current.kind is not RetryClass.COGNITION or current.attempt < 1:
                    raise OperationStateConflict(
                        "late_completion_from_retry_state_is_cognition_only"
                    )
            elif current.state not in (
                OperationState.IN_FLIGHT,
                OperationState.DELIVERY_UNKNOWN,
            ):
                raise OperationStateConflict(
                    "mark_completed_requires_supported_reconciliation_state"
                )
            return BridgeOperation(
                operation_id=current.operation_id,
                kind=current.kind,
                idempotency_key=current.idempotency_key,
                request_hash=current.request_hash,
                state=OperationState.COMPLETED,
                attempt=current.attempt,
                created_at=current.created_at,
                updated_at=updated_at,
                delivery_outcome=(
                    DeliveryOutcome.DELIVERED
                    if current.kind is RetryClass.CONTACT
                    else None
                ),
                schema_version=current.schema_version,
            )

        return self._transition_transaction(operation_id, build)

    def mark_failed_safe(
        self,
        operation_id: str,
        *,
        updated_at: str,
        error_code: str | None = None,
    ) -> BridgeOperation:
        def build(current: BridgeOperation) -> BridgeOperation:
            if current.state not in (
                OperationState.IN_FLIGHT,
                OperationState.DELIVERY_UNKNOWN,
            ):
                raise OperationStateConflict(
                    "mark_failed_safe_requires_in_flight_or_delivery_unknown"
                )
            return BridgeOperation(
                operation_id=current.operation_id,
                kind=current.kind,
                idempotency_key=current.idempotency_key,
                request_hash=current.request_hash,
                state=OperationState.FAILED_SAFE,
                attempt=current.attempt,
                created_at=current.created_at,
                updated_at=updated_at,
                delivery_outcome=(
                    DeliveryOutcome.FAILED_SAFE
                    if current.kind is RetryClass.CONTACT
                    else None
                ),
                last_error_code=error_code,
                schema_version=current.schema_version,
            )

        return self._transition_transaction(operation_id, build)

    def mark_delivery_unknown(
        self,
        operation_id: str,
        *,
        updated_at: str,
        error_code: str | None = None,
    ) -> BridgeOperation:
        def build(current: BridgeOperation) -> BridgeOperation:
            if current.kind is not RetryClass.CONTACT:
                raise OperationStateConflict("delivery_unknown_is_contact_only")
            if current.state is not OperationState.IN_FLIGHT:
                raise OperationStateConflict("delivery_unknown_requires_in_flight")
            return BridgeOperation(
                operation_id=current.operation_id,
                kind=current.kind,
                idempotency_key=current.idempotency_key,
                request_hash=current.request_hash,
                state=OperationState.DELIVERY_UNKNOWN,
                attempt=current.attempt,
                created_at=current.created_at,
                updated_at=updated_at,
                delivery_outcome=DeliveryOutcome.DELIVERY_UNKNOWN,
                last_error_code=error_code,
                schema_version=current.schema_version,
            )

        return self._transition_transaction(operation_id, build)

    def schedule_retry(
        self,
        operation_id: str,
        *,
        next_attempt_at: str,
        updated_at: str,
    ) -> BridgeOperation:
        """Persist retry scheduling only after a proven FAILED_SAFE outcome.

        This does not select policy/backoff or execute a retry; those belong to
        HLB-004.3.
        """
        def build(current: BridgeOperation) -> BridgeOperation:
            if current.state is not OperationState.FAILED_SAFE:
                raise OperationStateConflict("schedule_retry_requires_failed_safe")
            return BridgeOperation(
                operation_id=current.operation_id,
                kind=current.kind,
                idempotency_key=current.idempotency_key,
                request_hash=current.request_hash,
                state=OperationState.RETRY_WAIT,
                attempt=current.attempt,
                created_at=current.created_at,
                updated_at=updated_at,
                next_attempt_at=next_attempt_at,
                delivery_outcome=current.delivery_outcome,
                last_error_code=current.last_error_code,
                schema_version=current.schema_version,
            )

        return self._transition_transaction(operation_id, build)

    def make_retry_ready(self, operation_id: str, *, updated_at: str) -> BridgeOperation:
        """Move RETRY_WAIT to PREPARED after the scheduler decides it is due."""
        def build(current: BridgeOperation) -> BridgeOperation:
            if current.state is not OperationState.RETRY_WAIT:
                raise OperationStateConflict("make_retry_ready_requires_retry_wait")
            return BridgeOperation(
                operation_id=current.operation_id,
                kind=current.kind,
                idempotency_key=current.idempotency_key,
                request_hash=current.request_hash,
                state=OperationState.PREPARED,
                attempt=current.attempt,
                created_at=current.created_at,
                updated_at=updated_at,
                delivery_outcome=(
                    DeliveryOutcome.NOT_ATTEMPTED
                    if current.kind is RetryClass.CONTACT
                    else None
                ),
                schema_version=current.schema_version,
            )

        return self._transition_transaction(operation_id, build)

    def mark_exhausted(self, operation_id: str, *, updated_at: str) -> BridgeOperation:
        def build(current: BridgeOperation) -> BridgeOperation:
            if current.state is not OperationState.FAILED_SAFE:
                raise OperationStateConflict("mark_exhausted_requires_failed_safe")
            return BridgeOperation(
                operation_id=current.operation_id,
                kind=current.kind,
                idempotency_key=current.idempotency_key,
                request_hash=current.request_hash,
                state=OperationState.EXHAUSTED,
                attempt=current.attempt,
                created_at=current.created_at,
                updated_at=updated_at,
                delivery_outcome=current.delivery_outcome,
                last_error_code=current.last_error_code,
                schema_version=current.schema_version,
            )

        return self._transition_transaction(operation_id, build)

    @staticmethod
    def _parse_utc(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception as exc:
            raise ValueError("operation_timestamp_invalid") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("operation_timestamp_must_be_timezone_aware")
        return parsed.astimezone(timezone.utc)

    def purge_terminal(
        self,
        *,
        before: str,
        limit: int = 1000,
    ) -> int:
        """Purge old Percept/Cognition terminal reliability rows only.

        Contact operations are intentionally retained because their durable state is
        part of the external-send duplicate-prevention boundary.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 10000:
            raise ValueError("operation_purge_limit_out_of_range")
        cutoff = self._parse_utc(before)
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT operation_id,updated_at FROM bridge_operations
                WHERE kind IN ('percept','cognition')
                  AND state IN ('completed','exhausted')
                ORDER BY updated_at, operation_id
                """
            ).fetchall()
            selected: list[str] = []
            for row in rows:
                try:
                    updated = self._parse_utc(str(row["updated_at"]))
                except ValueError:
                    continue
                if updated < cutoff:
                    selected.append(str(row["operation_id"]))
                if len(selected) >= limit:
                    break
            if not selected:
                return 0
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.executemany(
                    "DELETE FROM bridge_operations WHERE operation_id=?",
                    [(operation_id,) for operation_id in selected],
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        self._secure_files()
        return len(selected)

    def compact(self) -> None:
        with self._lock:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.conn.execute("VACUUM")
            self.conn.commit()
        self._secure_files()

    def checkpoint(self) -> None:
        with self._lock:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.conn.commit()
        self._secure_files()

    def recover_interrupted_contact_operations(
        self, *, recovered_at: str
    ) -> list[BridgeOperation]:
        """Conservatively classify interrupted Contact sends after owner restart.

        The caller must invoke this only when it owns Contact startup recovery.
        Other action classes are intentionally untouched for HLB-004.3 to govern.
        """
        recovered: list[BridgeOperation] = []
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self.conn.execute(
                    """
                    SELECT * FROM bridge_operations
                    WHERE kind='contact' AND state='in_flight'
                    ORDER BY created_at, operation_id
                    """
                ).fetchall()
                for row in rows:
                    current = self._row_to_operation(row)
                    candidate = BridgeOperation(
                        operation_id=current.operation_id,
                        kind=current.kind,
                        idempotency_key=current.idempotency_key,
                        request_hash=current.request_hash,
                        state=OperationState.DELIVERY_UNKNOWN,
                        attempt=current.attempt,
                        created_at=current.created_at,
                        updated_at=recovered_at,
                        delivery_outcome=DeliveryOutcome.DELIVERY_UNKNOWN,
                        last_error_code="process_restart_in_flight",
                        schema_version=current.schema_version,
                    )
                    recovered.append(self._persist_transition(current, candidate))
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        self._secure_files()
        return recovered
