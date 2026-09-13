from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
import re
from types import MappingProxyType
from typing import Any, Mapping, Sequence


class WorkContractError(ValueError):
    """A work projection input crossed the content-free contract boundary."""


class WorkOutcome(str, Enum):
    VERIFIED_COMPLETE = "verified_complete"
    VERIFIED_BLOCKER = "verified_blocker"
    MATERIAL_FAILURE = "material_failure"


WORK_FACT_FIELDS = frozenset(
    {
        "schema_version",
        "fact_id",
        "idempotency_key",
        "work_item_id",
        "session_id",
        "executor_id",
        "outcome_class",
        "terminal",
        "result_hash",
        "novelty_hash",
        "evidence_refs",
        "counters",
        "observed_at",
    }
)
ACTIVITY_FACT_FIELDS = frozenset(
    {
        "schema_version",
        "session_id",
        "observer_id",
        "observed_at",
    }
)
COUNTER_FIELDS = frozenset({"attempt_count", "recovery_count"})

_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_HASH_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_UTC_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z"
)
_RAW_COMPONENTS = frozenset(
    {
        "prompt",
        "response",
        "transcript",
        "message",
        "content",
        "command",
        "filepath",
        "pathname",
        "url",
        "chat",
        "chatid",
        "target",
        "toolargs",
        "toolresult",
        "tooloutput",
        "credential",
        "credentials",
        "secret",
        "authorization",
        "cookie",
    }
)
_ROUTE_COMPONENTS = frozenset(
    {"feishu", "telegram", "discord", "slack", "signal", "sms", "whatsapp"}
)
MAX_EVIDENCE_REFS = 32
MAX_COUNTER = 1_000_000


def parse_utc(value: str, *, field: str = "timestamp") -> datetime:
    if not isinstance(value, str) or not _UTC_RE.fullmatch(value):
        raise WorkContractError(f"{field}_must_be_rfc3339_utc")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise WorkContractError(f"{field}_must_be_rfc3339_utc") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise WorkContractError(f"{field}_must_be_utc")
    return parsed


def _require_exact_mapping(
    value: Any, expected: frozenset[str], *, name: str
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkContractError(f"{name}_must_be_mapping")
    keys = set(value.keys())
    if any(not isinstance(key, str) for key in keys) or keys != expected:
        raise WorkContractError(f"{name}_fields_must_be_exact")
    return value


def _safe_token(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _TOKEN_RE.fullmatch(value):
        raise WorkContractError(f"{field}_must_be_bounded_opaque_token")

    components = tuple(part for part in re.split(r"[_.-]+", value.casefold()) if part)
    collapsed = "".join(components)
    if any(part in _RAW_COMPONENTS for part in components) or collapsed in _RAW_COMPONENTS:
        raise WorkContractError(f"{field}_contains_raw_sensitive_value")
    if components and components[0] in _ROUTE_COMPONENTS:
        raise WorkContractError(f"{field}_contains_route_syntax")
    return value


def _safe_hash(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise WorkContractError(f"{field}_must_be_prefixed_sha256")
    return value


def _safe_evidence_refs(value: Any) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise WorkContractError("evidence_refs_must_be_sequence")
    if len(value) > MAX_EVIDENCE_REFS:
        raise WorkContractError("evidence_refs_too_many")
    refs = tuple(_safe_token(item, field="evidence_ref") for item in value)
    if len(set(refs)) != len(refs):
        raise WorkContractError("evidence_refs_must_be_unique")
    return refs


def _safe_counters(value: Any) -> dict[str, int]:
    counters = _require_exact_mapping(value, COUNTER_FIELDS, name="counters")
    result: dict[str, int] = {}
    for name in sorted(COUNTER_FIELDS):
        counter = counters[name]
        if isinstance(counter, bool) or not isinstance(counter, int):
            raise WorkContractError(f"{name}_must_be_integer")
        if counter < 0 or counter > MAX_COUNTER:
            raise WorkContractError(f"{name}_out_of_range")
        result[name] = counter
    return result


@dataclass(frozen=True)
class WorkFact:
    schema_version: str
    fact_id: str
    idempotency_key: str
    work_item_id: str
    session_id: str
    executor_id: str
    outcome_class: WorkOutcome
    terminal: bool
    result_hash: str
    novelty_hash: str
    evidence_refs: tuple[str, ...]
    counters: Mapping[str, int]
    observed_at: str

    def __post_init__(self) -> None:
        if self.schema_version != "work_fact.v1":
            raise WorkContractError("unsupported_work_fact_schema")
        for name in (
            "fact_id",
            "idempotency_key",
            "work_item_id",
            "session_id",
            "executor_id",
        ):
            _safe_token(getattr(self, name), field=name)
        try:
            outcome = WorkOutcome(self.outcome_class)
        except (TypeError, ValueError) as exc:
            raise WorkContractError("outcome_class_not_allowed") from exc
        if self.terminal is not True:
            raise WorkContractError("work_fact_must_be_terminal")
        _safe_hash(self.result_hash, field="result_hash")
        _safe_hash(self.novelty_hash, field="novelty_hash")
        refs = _safe_evidence_refs(self.evidence_refs)
        counters = _safe_counters(self.counters)
        parse_utc(self.observed_at, field="observed_at")

        object.__setattr__(self, "outcome_class", outcome)
        object.__setattr__(self, "evidence_refs", refs)
        object.__setattr__(self, "counters", MappingProxyType(counters))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "WorkFact":
        data = _require_exact_mapping(value, WORK_FACT_FIELDS, name="work_fact")
        return cls(
            schema_version=data["schema_version"],
            fact_id=data["fact_id"],
            idempotency_key=data["idempotency_key"],
            work_item_id=data["work_item_id"],
            session_id=data["session_id"],
            executor_id=data["executor_id"],
            outcome_class=data["outcome_class"],
            terminal=data["terminal"],
            result_hash=data["result_hash"],
            novelty_hash=data["novelty_hash"],
            evidence_refs=data["evidence_refs"],
            counters=data["counters"],
            observed_at=data["observed_at"],
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "fact_id": self.fact_id,
            "idempotency_key": self.idempotency_key,
            "work_item_id": self.work_item_id,
            "session_id": self.session_id,
            "executor_id": self.executor_id,
            "outcome_class": self.outcome_class.value,
            "terminal": self.terminal,
            "result_hash": self.result_hash,
            "novelty_hash": self.novelty_hash,
            "evidence_refs": list(self.evidence_refs),
            "counters": dict(self.counters),
            "observed_at": self.observed_at,
        }

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.to_mapping(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")

    def digest(self) -> str:
        return f"sha256:{sha256(self.canonical_bytes()).hexdigest()}"


@dataclass(frozen=True)
class ActivityFact:
    schema_version: str
    session_id: str
    observer_id: str
    observed_at: str

    def __post_init__(self) -> None:
        if self.schema_version != "activity_fact.v1":
            raise WorkContractError("unsupported_activity_fact_schema")
        _safe_token(self.session_id, field="session_id")
        _safe_token(self.observer_id, field="observer_id")
        parse_utc(self.observed_at, field="observed_at")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ActivityFact":
        data = _require_exact_mapping(value, ACTIVITY_FACT_FIELDS, name="activity_fact")
        return cls(
            schema_version=data["schema_version"],
            session_id=data["session_id"],
            observer_id=data["observer_id"],
            observed_at=data["observed_at"],
        )

    def to_mapping(self) -> dict[str, str]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "observer_id": self.observer_id,
            "observed_at": self.observed_at,
        }

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.to_mapping(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
