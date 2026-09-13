from __future__ import annotations

from pathlib import Path

import pytest

from hermes_life_bridge.work_contract import ActivityFact, WorkFact
from hermes_life_bridge.work_store import WorkStore


def work_fact(
    *,
    fact_id: str = "fact-001",
    idempotency_key: str = "idem-001",
    observed_at: str = "2026-09-13T12:00:00Z",
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
            "result_hash": "sha256:" + "a" * 64,
            "novelty_hash": "sha256:" + "b" * 64,
            "evidence_refs": ["evidence-001"],
            "counters": {"attempt_count": 1, "recovery_count": 0},
            "observed_at": observed_at,
        }
    )


def activity(observed_at: str) -> ActivityFact:
    return ActivityFact.from_mapping(
        {
            "schema_version": "activity_fact.v1",
            "session_id": "session-001",
            "observer_id": "observer-001",
            "observed_at": observed_at,
        }
    )


def test_new_activity_epoch_atomically_invalidates_pending_fact(
    tmp_path: Path,
) -> None:
    with WorkStore(str(tmp_path / "work.db")) as store:
        accepted = store.accept(work_fact(), idle_delay_seconds=30)
        assert accepted.accepted_epoch == 0

        noted = store.note_activity(activity("2026-09-13T12:00:10Z"))
        assert noted.epoch == 1
        assert noted.invalidated_count == 1
        assert store.get_outbox("fact-001")["state"] == "invalidated"
        assert store.get_ledger("fact-001")["disposition"] == "invalidated"
        assert (
            store.evaluate_due(now="2026-09-13T12:01:00Z")
            == ()
        )


def test_missing_activity_observer_suppresses_due_fact_fail_closed(
    tmp_path: Path,
) -> None:
    with WorkStore(str(tmp_path / "work.db")) as store:
        store.accept(work_fact(), idle_delay_seconds=30)

        evaluations = store.evaluate_due(now="2026-09-13T12:00:30Z")
        assert len(evaluations) == 1
        assert evaluations[0].state == "suppressed"
        assert evaluations[0].reason == "activity_observer_unavailable"
        assert store.get_outbox("fact-001")["state"] == "suppressed"
        assert store.evaluate_due(now="2026-09-13T12:01:00Z") == ()
        assert (
            store.conn.execute(
                "SELECT count(*) FROM shadow_outbox WHERE state='shadow_candidate'"
            ).fetchone()[0]
            == 0
        )
        with pytest.raises(TypeError):
            store.evaluate_due(
                now="2026-09-13T12:01:00Z",
                activity_observer_available=True,
            )


def test_matching_epoch_creates_exactly_one_shadow_candidate(
    tmp_path: Path,
) -> None:
    with WorkStore(str(tmp_path / "work.db")) as store:
        noted = store.note_activity(activity("2026-09-13T11:59:59Z"))
        accepted = store.accept(work_fact(), idle_delay_seconds=30)
        assert accepted.accepted_epoch == noted.epoch == 1

        evaluations = store.evaluate_due(now="2026-09-13T12:00:30Z")
        assert len(evaluations) == 1
        assert evaluations[0].state == "shadow_candidate"
        assert evaluations[0].reason == "idle_epoch_matched"
        assert evaluations[0].accepted_epoch == evaluations[0].current_epoch == 1

        assert store.evaluate_due(now="2026-09-13T12:01:00Z") == ()
        assert (
            store.conn.execute(
                "SELECT count(*) FROM shadow_outbox WHERE state='shadow_candidate'"
            ).fetchone()[0]
            == 1
        )
