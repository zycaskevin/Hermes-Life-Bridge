from __future__ import annotations

from pathlib import Path
import stat

import pytest

from hermes_life_bridge.work_contract import ActivityFact, WorkContractError, WorkFact
from hermes_life_bridge.work_store import WorkConflictError, WorkStore


def work_fact(
    *,
    fact_id: str = "fact-001",
    idempotency_key: str = "idem-001",
    result_hash: str = "sha256:" + "a" * 64,
) -> WorkFact:
    return WorkFact.from_mapping(
        {
            "schema_version": "work_fact.v1",
            "fact_id": fact_id,
            "idempotency_key": idempotency_key,
            "work_item_id": "work-001",
            "session_id": "session-001",
            "executor_id": "executor-001",
            "outcome_class": "verified_complete",
            "terminal": True,
            "result_hash": result_hash,
            "novelty_hash": "sha256:" + "b" * 64,
            "evidence_refs": ["evidence-001"],
            "counters": {"attempt_count": 1, "recovery_count": 0},
            "observed_at": "2026-09-13T12:00:00Z",
        }
    )


def test_store_uses_owner_only_directory_db_wal_and_shm(tmp_path: Path) -> None:
    state_dir = tmp_path / "permissive-state"
    state_dir.mkdir(mode=0o777)
    state_dir.chmod(0o777)
    db_path = state_dir / "work_projection.sqlite3"

    with WorkStore(str(db_path)) as store:
        store.accept(work_fact(), idle_delay_seconds=30)

        assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
        for path in (
            db_path,
            Path(f"{db_path}-wal"),
            Path(f"{db_path}-shm"),
        ):
            assert path.exists(), path
            assert stat.S_IMODE(path.stat().st_mode) == 0o600

        assert store.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert store.conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert store.conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
        assert store.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_duplicate_returns_original_and_creates_no_second_rows(tmp_path: Path) -> None:
    with WorkStore(str(tmp_path / "work.db")) as store:
        first = store.accept(work_fact(), idle_delay_seconds=30)
        replay = store.accept(work_fact(), idle_delay_seconds=30)

        assert first.status == "accepted"
        assert replay.status == "duplicate"
        assert replay.fact_id == first.fact_id
        assert replay.fact_digest == first.fact_digest
        assert replay.duplicate is True
        assert store.conn.execute("SELECT count(*) FROM work_ledger").fetchone()[0] == 1
        assert store.conn.execute("SELECT count(*) FROM shadow_outbox").fetchone()[0] == 1


def test_idempotency_digest_conflict_rolls_back_without_second_outbox(
    tmp_path: Path,
) -> None:
    with WorkStore(str(tmp_path / "work.db")) as store:
        store.accept(work_fact(), idle_delay_seconds=30)

        with pytest.raises(
            WorkConflictError,
            match="idempotency_key_reused_with_different_fact",
        ):
            store.accept(
                work_fact(result_hash="sha256:" + "c" * 64),
                idle_delay_seconds=30,
            )

        assert store.conn.execute("SELECT count(*) FROM work_ledger").fetchone()[0] == 1
        assert store.conn.execute("SELECT count(*) FROM shadow_outbox").fetchone()[0] == 1


def test_fact_id_reuse_is_conflict_and_does_not_partially_persist(
    tmp_path: Path,
) -> None:
    with WorkStore(str(tmp_path / "work.db")) as store:
        store.accept(work_fact(), idle_delay_seconds=30)

        with pytest.raises(WorkConflictError, match="fact_id_reused"):
            store.accept(
                work_fact(idempotency_key="idem-002"),
                idle_delay_seconds=30,
            )

        assert store.conn.execute("SELECT count(*) FROM work_ledger").fetchone()[0] == 1
        assert store.conn.execute("SELECT count(*) FROM shadow_outbox").fetchone()[0] == 1


def test_forged_typed_facts_are_revalidated_before_any_persistence(tmp_path: Path) -> None:
    forged = object.__new__(WorkFact)
    for name, value in {
        "schema_version": "work_fact.v1", "fact_id": "fact-raw", "idempotency_key": "idem-raw",
        "work_item_id": "work-raw", "session_id": "session-raw", "executor_id": "executor-raw",
        "outcome_class": "verified_complete", "terminal": True,
        "result_hash": "PRIVATE RAW PROMPT", "novelty_hash": "sha256:" + "b" * 64,
        "evidence_refs": ("evidence-raw",), "counters": {"attempt_count": 0, "recovery_count": 0},
        "observed_at": "2026-09-13T12:00:00Z",
    }.items():
        object.__setattr__(forged, name, value)

    with WorkStore(str(tmp_path / "work.db")) as store:
        with pytest.raises(WorkContractError):
            store.accept(forged, idle_delay_seconds=30)
        assert store.conn.execute("SELECT count(*) FROM work_ledger").fetchone()[0] == 0


def test_forged_activity_is_revalidated_before_epoch_mutation(tmp_path: Path) -> None:
    forged = object.__new__(ActivityFact)
    for name, value in {
        "schema_version": "activity_fact.v1", "session_id": "PRIVATE RAW PATH",
        "observer_id": "observer-raw", "observed_at": "2026-09-13T12:00:00Z",
    }.items():
        object.__setattr__(forged, name, value)

    with WorkStore(str(tmp_path / "work.db")) as store:
        with pytest.raises(WorkContractError):
            store.note_activity(forged)
        assert store.get_session_epoch("session-raw") is None
