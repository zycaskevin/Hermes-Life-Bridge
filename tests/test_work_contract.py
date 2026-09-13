from __future__ import annotations

from copy import deepcopy

import pytest

from hermes_life_bridge.work_contract import (
    ACTIVITY_FACT_FIELDS,
    WORK_FACT_FIELDS,
    ActivityFact,
    WorkContractError,
    WorkFact,
    WorkOutcome,
)


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64


def valid_work_fact() -> dict[str, object]:
    return {
        "schema_version": "work_fact.v1",
        "fact_id": "fact-001",
        "idempotency_key": "idem-001",
        "work_item_id": "work-001",
        "session_id": "session-001",
        "executor_id": "executor-001",
        "outcome_class": "verified_complete",
        "terminal": True,
        "result_hash": HASH_A,
        "novelty_hash": HASH_B,
        "evidence_refs": ["evidence-001", "receipt-002"],
        "counters": {"attempt_count": 1, "recovery_count": 0},
        "observed_at": "2026-09-13T12:00:00.123456Z",
    }


def valid_activity_fact() -> dict[str, object]:
    return {
        "schema_version": "activity_fact.v1",
        "session_id": "session-001",
        "observer_id": "observer-001",
        "observed_at": "2026-09-13T12:00:01Z",
    }


def test_work_fact_contract_is_exact_closed_and_canonical() -> None:
    raw = valid_work_fact()
    fact = WorkFact.from_mapping(raw)

    assert set(raw) == WORK_FACT_FIELDS
    assert fact.outcome_class is WorkOutcome.VERIFIED_COMPLETE
    assert fact.to_mapping() == raw
    assert fact.canonical_bytes() == (
        b'{"counters":{"attempt_count":1,"recovery_count":0},'
        b'"evidence_refs":["evidence-001","receipt-002"],'
        b'"executor_id":"executor-001","fact_id":"fact-001",'
        b'"idempotency_key":"idem-001",'
        b'"novelty_hash":"sha256:' + b"b" * 64 + b'",'
        b'"observed_at":"2026-09-13T12:00:00.123456Z",'
        b'"outcome_class":"verified_complete",'
        b'"result_hash":"sha256:' + b"a" * 64 + b'",'
        b'"schema_version":"work_fact.v1","session_id":"session-001",'
        b'"terminal":true,"work_item_id":"work-001"}'
    )
    assert fact.digest().startswith("sha256:")
    assert len(fact.digest()) == 71

    for changed in (
        {**raw, "prompt": "PRIVATE RAW PROMPT"},
        {key: value for key, value in raw.items() if key != "terminal"},
    ):
        with pytest.raises(WorkContractError, match="work_fact_fields_must_be_exact"):
            WorkFact.from_mapping(changed)


@pytest.mark.parametrize(
    ("path", "hostile"),
    [
        (("fact_id",), "prompt"),
        (("idempotency_key",), "https://private.example/path"),
        (("work_item_id",), "telegram-chat-123"),
        (("executor_id",), "../../private/file"),
        (("evidence_refs",), ["tool_output"]),
        (("counters",), {"attempt_count": 1, "recovery_count": 0, "message": 1}),
    ],
)
def test_work_fact_rejects_hostile_raw_names_values_and_syntax(
    path: tuple[str, ...], hostile: object
) -> None:
    raw = deepcopy(valid_work_fact())
    raw[path[0]] = hostile
    with pytest.raises(WorkContractError):
        WorkFact.from_mapping(raw)


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("schema_version", "work_fact.v2"),
        ("outcome_class", "success"),
        ("terminal", False),
        ("result_hash", "a" * 64),
        ("novelty_hash", "sha256:" + "A" * 64),
        ("evidence_refs", ["evidence-001", "evidence-001"]),
        ("counters", {"attempt_count": True, "recovery_count": 0}),
        ("observed_at", "2026-09-13T12:00:00+00:00"),
    ],
)
def test_work_fact_rejects_non_contract_values(field: str, invalid: object) -> None:
    raw = valid_work_fact()
    raw[field] = invalid
    with pytest.raises(WorkContractError):
        WorkFact.from_mapping(raw)


def test_activity_fact_is_exact_metadata_only_contract() -> None:
    raw = valid_activity_fact()
    activity = ActivityFact.from_mapping(raw)
    assert set(activity.to_mapping()) == ACTIVITY_FACT_FIELDS
    assert activity.to_mapping() == raw

    with pytest.raises(WorkContractError, match="activity_fact_fields_must_be_exact"):
        ActivityFact.from_mapping({**raw, "message": "PRIVATE ACTIVITY"})

    hostile = dict(raw)
    hostile["observer_id"] = "chat_id"
    with pytest.raises(WorkContractError, match="contains_raw_sensitive_value"):
        ActivityFact.from_mapping(hostile)
