from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3
import threading
from typing import Any

from .work_contract import ActivityFact, WorkContractError, WorkFact, parse_utc


OUTBOX_STATES = frozenset(
    {"pending_idle", "invalidated", "suppressed", "shadow_candidate"}
)


class WorkStoreError(RuntimeError):
    pass


class WorkConflictError(WorkStoreError):
    pass


class WorkStoreCorruptionError(WorkStoreError):
    pass


@dataclass(frozen=True)
class WorkAcceptance:
    status: str
    fact_id: str
    idempotency_key: str
    fact_digest: str
    accepted_epoch: int
    outbox_state: str

    @property
    def duplicate(self) -> bool:
        return self.status == "duplicate"


@dataclass(frozen=True)
class ActivityAcceptance:
    session_id: str
    epoch: int
    invalidated_count: int
    observed_at: str


@dataclass(frozen=True)
class WorkEvaluation:
    fact_id: str
    session_id: str
    accepted_epoch: int
    current_epoch: int
    state: str
    reason: str


def _timestamp_us(value: str, *, field: str) -> int:
    parsed = parse_utc(value, field=field)
    return int(parsed.timestamp() * 1_000_000)


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


class WorkStore:
    """Owner-only, content-free SQLite shadow ledger for WP-2.

    This is deliberately not a delivery queue. Its outbox state machine has no
    delivered or retry state and this module has no Runtime/contact dependency.
    """

    SCHEMA_VERSION = 1

    def __init__(self, path: str):
        self.path = Path(path)
        if self.path.exists() and self.path.is_symlink():
            raise WorkStoreError("work_store_path_must_not_be_symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.path.parent.chmod(0o700)
        except OSError as exc:
            raise WorkStoreError("work_store_directory_not_owner_only") from exc

        self._lock = threading.RLock()
        self._closed = False
        self.conn = sqlite3.connect(
            str(self.path),
            check_same_thread=False,
            timeout=5.0,
            isolation_level=None,
        )
        self.conn.row_factory = sqlite3.Row
        self._secure_files()
        try:
            with self._lock:
                self.conn.execute("PRAGMA busy_timeout=5000")
                self.conn.execute("PRAGMA journal_mode=WAL")
                self._secure_files()
                self.conn.execute("PRAGMA synchronous=FULL")
                self.conn.execute("PRAGMA secure_delete=ON")
                self.conn.execute("PRAGMA foreign_keys=ON")
                current_schema = int(
                    self.conn.execute("PRAGMA user_version").fetchone()[0]
                )
                if current_schema > self.SCHEMA_VERSION:
                    raise WorkStoreError("work_store_schema_newer_than_supported")
                self._create_schema()
                if current_schema < self.SCHEMA_VERSION:
                    self.conn.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")
        except Exception:
            self.conn.close()
            self._closed = True
            self._secure_files()
            raise
        self._secure_files()

    def _create_schema(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS work_ledger(
                fact_id TEXT PRIMARY KEY,
                idempotency_key TEXT UNIQUE NOT NULL,
                fact_digest TEXT NOT NULL,
                work_item_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                executor_id TEXT NOT NULL,
                outcome_class TEXT NOT NULL CHECK(outcome_class IN (
                    'verified_complete','verified_blocker','material_failure'
                )),
                result_hash TEXT NOT NULL,
                novelty_hash TEXT NOT NULL,
                evidence_refs_json TEXT NOT NULL,
                attempt_count INTEGER NOT NULL CHECK(attempt_count >= 0),
                recovery_count INTEGER NOT NULL CHECK(recovery_count >= 0),
                observed_at TEXT NOT NULL,
                accepted_epoch INTEGER NOT NULL CHECK(accepted_epoch >= 0),
                disposition TEXT NOT NULL CHECK(disposition IN (
                    'pending_idle','invalidated','suppressed','shadow_candidate'
                ))
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS session_epochs(
                session_id TEXT PRIMARY KEY,
                epoch INTEGER NOT NULL CHECK(epoch >= 0),
                observer_available INTEGER NOT NULL CHECK(observer_available IN (0,1)),
                last_activity_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS shadow_outbox(
                fact_id TEXT PRIMARY KEY,
                idempotency_key TEXT UNIQUE NOT NULL,
                session_id TEXT NOT NULL,
                accepted_epoch INTEGER NOT NULL CHECK(accepted_epoch >= 0),
                due_at TEXT NOT NULL,
                due_at_us INTEGER NOT NULL,
                state TEXT NOT NULL CHECK(state IN (
                    'pending_idle','invalidated','suppressed','shadow_candidate'
                )),
                reason TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(fact_id) REFERENCES work_ledger(fact_id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS work_metrics_daily(
                metric_date TEXT NOT NULL,
                metric_name TEXT NOT NULL,
                count INTEGER NOT NULL CHECK(count >= 0),
                PRIMARY KEY(metric_date, metric_name)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_shadow_outbox_due
            ON shadow_outbox(state, due_at_us, fact_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_shadow_outbox_session_epoch
            ON shadow_outbox(session_id, accepted_epoch, state)
            """,
        )
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in statements:
                self.conn.execute(statement)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _secure_files(self) -> None:
        for candidate in (
            self.path,
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
        ):
            if candidate.exists():
                try:
                    candidate.chmod(0o600)
                except OSError as exc:
                    raise WorkStoreError("work_store_file_not_owner_only") from exc

    def close(self) -> None:
        if self._closed:
            return
        with self._lock:
            self._secure_files()
            self.conn.close()
            self._closed = True
        self._secure_files()

    def __enter__(self) -> "WorkStore":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def _begin(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")

    def _metric(self, name: str, when: str, increment: int = 1) -> None:
        if increment <= 0:
            return
        metric_date = parse_utc(when, field="metric_timestamp").date().isoformat()
        self.conn.execute(
            """
            INSERT INTO work_metrics_daily(metric_date,metric_name,count)
            VALUES(?,?,?)
            ON CONFLICT(metric_date,metric_name)
            DO UPDATE SET count=count+excluded.count
            """,
            (metric_date, name, increment),
        )

    def accept(
        self,
        fact: WorkFact,
        *,
        idle_delay_seconds: float,
    ) -> WorkAcceptance:
        if type(fact) is not WorkFact:
            raise TypeError("accept_requires_validated_work_fact")
        # Do not trust a caller-provided frozen object: reconstruct through the
        # closed mapping parser before any digest, SQL or WAL write.
        try:
            fact = WorkFact.from_mapping(fact.to_mapping())
        except (AttributeError, TypeError, ValueError) as exc:
            raise WorkContractError("invalid_typed_work_fact") from exc
        if isinstance(idle_delay_seconds, bool) or not isinstance(
            idle_delay_seconds, (int, float)
        ):
            raise ValueError("idle_delay_seconds_must_be_number")
        if idle_delay_seconds < 0 or idle_delay_seconds > 604800:
            raise ValueError("idle_delay_seconds_out_of_range")

        fact_digest = fact.digest()
        due = parse_utc(fact.observed_at, field="observed_at") + timedelta(
            seconds=float(idle_delay_seconds)
        )
        due_at = _format_utc(due)
        due_at_us = int(due.timestamp() * 1_000_000)

        with self._lock:
            self._begin()
            try:
                existing = self.conn.execute(
                    """
                    SELECT fact_id,idempotency_key,fact_digest,accepted_epoch,disposition
                    FROM work_ledger WHERE idempotency_key=?
                    """,
                    (fact.idempotency_key,),
                ).fetchone()
                if existing is not None:
                    if existing["fact_digest"] != fact_digest:
                        raise WorkConflictError(
                            "idempotency_key_reused_with_different_fact"
                        )
                    self._metric("duplicate", fact.observed_at)
                    self.conn.commit()
                    return WorkAcceptance(
                        status="duplicate",
                        fact_id=str(existing["fact_id"]),
                        idempotency_key=str(existing["idempotency_key"]),
                        fact_digest=str(existing["fact_digest"]),
                        accepted_epoch=int(existing["accepted_epoch"]),
                        outbox_state=str(existing["disposition"]),
                    )

                collision = self.conn.execute(
                    "SELECT 1 FROM work_ledger WHERE fact_id=?", (fact.fact_id,)
                ).fetchone()
                if collision is not None:
                    raise WorkConflictError("fact_id_reused")

                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO session_epochs(
                        session_id,epoch,observer_available,last_activity_at
                    ) VALUES(?,0,0,NULL)
                    """,
                    (fact.session_id,),
                )
                epoch_row = self.conn.execute(
                    "SELECT epoch FROM session_epochs WHERE session_id=?",
                    (fact.session_id,),
                ).fetchone()
                if epoch_row is None:
                    raise WorkStoreCorruptionError("session_epoch_missing")
                accepted_epoch = int(epoch_row["epoch"])

                self.conn.execute(
                    """
                    INSERT INTO work_ledger(
                        fact_id,idempotency_key,fact_digest,work_item_id,session_id,
                        executor_id,outcome_class,result_hash,novelty_hash,
                        evidence_refs_json,attempt_count,recovery_count,observed_at,
                        accepted_epoch,disposition
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        fact.fact_id,
                        fact.idempotency_key,
                        fact_digest,
                        fact.work_item_id,
                        fact.session_id,
                        fact.executor_id,
                        fact.outcome_class.value,
                        fact.result_hash,
                        fact.novelty_hash,
                        json.dumps(list(fact.evidence_refs), separators=(",", ":")),
                        fact.counters["attempt_count"],
                        fact.counters["recovery_count"],
                        fact.observed_at,
                        accepted_epoch,
                        "pending_idle",
                    ),
                )
                self.conn.execute(
                    """
                    INSERT INTO shadow_outbox(
                        fact_id,idempotency_key,session_id,accepted_epoch,due_at,
                        due_at_us,state,reason,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        fact.fact_id,
                        fact.idempotency_key,
                        fact.session_id,
                        accepted_epoch,
                        due_at,
                        due_at_us,
                        "pending_idle",
                        "awaiting_idle_deadline",
                        fact.observed_at,
                    ),
                )
                self._metric("accepted", fact.observed_at)
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        self._secure_files()
        return WorkAcceptance(
            status="accepted",
            fact_id=fact.fact_id,
            idempotency_key=fact.idempotency_key,
            fact_digest=fact_digest,
            accepted_epoch=accepted_epoch,
            outbox_state="pending_idle",
        )

    def note_activity(self, activity: ActivityFact) -> ActivityAcceptance:
        if type(activity) is not ActivityFact:
            raise TypeError("note_activity_requires_validated_activity_fact")
        try:
            activity = ActivityFact.from_mapping(activity.to_mapping())
        except (AttributeError, TypeError, ValueError) as exc:
            raise WorkContractError("invalid_typed_activity_fact") from exc
        with self._lock:
            self._begin()
            try:
                self.conn.execute(
                    """
                    INSERT INTO session_epochs(
                        session_id,epoch,observer_available,last_activity_at
                    ) VALUES(?,1,1,?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        epoch=session_epochs.epoch+1,
                        observer_available=1,
                        last_activity_at=excluded.last_activity_at
                    """,
                    (activity.session_id, activity.observed_at),
                )
                epoch_row = self.conn.execute(
                    "SELECT epoch FROM session_epochs WHERE session_id=?",
                    (activity.session_id,),
                ).fetchone()
                if epoch_row is None:
                    raise WorkStoreCorruptionError("session_epoch_missing")
                epoch = int(epoch_row["epoch"])

                pending = self.conn.execute(
                    """
                    SELECT fact_id FROM shadow_outbox
                    WHERE session_id=? AND accepted_epoch<? AND state='pending_idle'
                    """,
                    (activity.session_id, epoch),
                ).fetchall()
                fact_ids = tuple(str(row["fact_id"]) for row in pending)
                if fact_ids:
                    self.conn.execute(
                        """
                        UPDATE shadow_outbox
                        SET state='invalidated', reason='newer_session_activity',
                            updated_at=?
                        WHERE session_id=? AND accepted_epoch<? AND state='pending_idle'
                        """,
                        (activity.observed_at, activity.session_id, epoch),
                    )
                    placeholders = ",".join("?" for _ in fact_ids)
                    self.conn.execute(
                        f"UPDATE work_ledger SET disposition='invalidated' "
                        f"WHERE fact_id IN ({placeholders})",
                        fact_ids,
                    )
                self._metric("activity", activity.observed_at)
                self._metric("invalidated", activity.observed_at, len(fact_ids))
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        self._secure_files()
        return ActivityAcceptance(
            session_id=activity.session_id,
            epoch=epoch,
            invalidated_count=len(fact_ids),
            observed_at=activity.observed_at,
        )

    def evaluate_due(
        self,
        *,
        now: str,
        limit: int = 100,
    ) -> tuple[WorkEvaluation, ...]:
        now_us = _timestamp_us(now, field="now")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit_out_of_range")

        results: list[WorkEvaluation] = []
        with self._lock:
            self._begin()
            try:
                rows = self.conn.execute(
                    """
                    SELECT fact_id,session_id,accepted_epoch
                    FROM shadow_outbox
                    WHERE state='pending_idle' AND due_at_us<=?
                    ORDER BY due_at_us,fact_id LIMIT ?
                    """,
                    (now_us, limit),
                ).fetchall()
                for row in rows:
                    fact_id = str(row["fact_id"])
                    session_id = str(row["session_id"])
                    accepted_epoch = int(row["accepted_epoch"])
                    epoch_row = self.conn.execute(
                        """
                        SELECT epoch,observer_available FROM session_epochs
                        WHERE session_id=?
                        """,
                        (session_id,),
                    ).fetchone()
                    current_epoch = int(epoch_row["epoch"]) if epoch_row else -1
                    stored_observer = bool(epoch_row["observer_available"]) if epoch_row else False
                    observer_ready = stored_observer

                    if not observer_ready:
                        state = "suppressed"
                        reason = "activity_observer_unavailable"
                    elif current_epoch != accepted_epoch:
                        state = "invalidated"
                        reason = "idle_epoch_mismatch"
                    else:
                        state = "shadow_candidate"
                        reason = "idle_epoch_matched"

                    changed = self.conn.execute(
                        """
                        UPDATE shadow_outbox SET state=?,reason=?,updated_at=?
                        WHERE fact_id=? AND state='pending_idle'
                        """,
                        (state, reason, now, fact_id),
                    ).rowcount
                    if changed != 1:
                        raise WorkStoreCorruptionError("outbox_transition_lost")
                    self.conn.execute(
                        "UPDATE work_ledger SET disposition=? WHERE fact_id=?",
                        (state, fact_id),
                    )
                    self._metric(state, now)
                    results.append(
                        WorkEvaluation(
                            fact_id=fact_id,
                            session_id=session_id,
                            accepted_epoch=accepted_epoch,
                            current_epoch=current_epoch,
                            state=state,
                            reason=reason,
                        )
                    )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        self._secure_files()
        return tuple(results)

    def get_ledger(self, fact_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM work_ledger WHERE fact_id=?", (fact_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def get_outbox(self, fact_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM shadow_outbox WHERE fact_id=?", (fact_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def list_outbox(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM shadow_outbox ORDER BY due_at_us,fact_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_session_epoch(self, session_id: str) -> int | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT epoch FROM session_epochs WHERE session_id=?", (session_id,)
            ).fetchone()
        return int(row["epoch"]) if row is not None else None
