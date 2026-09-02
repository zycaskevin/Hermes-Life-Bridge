from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import re
from typing import Any

from .representation import canonicalize_operational_value


class ContractEnum(str, Enum):
    """String enum with stable wire/storage values."""


class OperationState(ContractEnum):
    PREPARED = "prepared"
    IN_FLIGHT = "in_flight"
    RETRY_WAIT = "retry_wait"
    COMPLETED = "completed"
    FAILED_SAFE = "failed_safe"
    DELIVERY_UNKNOWN = "delivery_unknown"
    EXHAUSTED = "exhausted"


class RetryClass(ContractEnum):
    PERCEPT = "percept"
    COGNITION = "cognition"
    CONTACT = "contact"


class DeliveryOutcome(ContractEnum):
    NOT_ATTEMPTED = "not_attempted"
    DELIVERED = "delivered"
    FAILED_SAFE = "failed_safe"
    DELIVERY_UNKNOWN = "delivery_unknown"


class RouteStatus(ContractEnum):
    UNKNOWN = "unknown"
    FRESH = "fresh"
    STALE = "stale"
    INVALID = "invalid"


ALLOWED_OPERATION_TRANSITIONS: dict[OperationState, frozenset[OperationState]] = {
    OperationState.PREPARED: frozenset({OperationState.IN_FLIGHT}),
    OperationState.IN_FLIGHT: frozenset(
        {
            OperationState.COMPLETED,
            OperationState.FAILED_SAFE,
            OperationState.DELIVERY_UNKNOWN,
        }
    ),
    OperationState.FAILED_SAFE: frozenset(
        {OperationState.RETRY_WAIT, OperationState.EXHAUSTED}
    ),
    OperationState.RETRY_WAIT: frozenset({OperationState.PREPARED}),
    # Reconciliation may prove either delivery or non-delivery. It may never
    # directly schedule a retry while the provider outcome remains unknown.
    OperationState.DELIVERY_UNKNOWN: frozenset(
        {OperationState.COMPLETED, OperationState.FAILED_SAFE}
    ),
    OperationState.COMPLETED: frozenset(),
    OperationState.EXHAUSTED: frozenset(),
}


def _require_bool(name: str, value: Any) -> None:
    if not isinstance(value, bool):
        raise ValueError(f"{name}_must_be_bool")


def _require_number(name: str, value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name}_must_be_number")


@dataclass(frozen=True)
class RetryPolicy:
    """Declarative bounded retry policy; execution belongs to HLB-004.3."""

    retry_class: RetryClass
    max_attempts: int
    initial_backoff_seconds: float
    max_backoff_seconds: float
    backoff_multiplier: float
    jitter_ratio: float
    retry_after_failed_safe: bool
    retry_after_delivery_unknown: bool
    requires_durable_state: bool
    reconcile_before_retry: bool
    schema_version: str = "v0.4"

    def __post_init__(self) -> None:
        if not isinstance(self.retry_class, RetryClass):
            raise ValueError("retry_class_must_be_retry_class")
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int):
            raise ValueError("max_attempts_must_be_int")
        if self.max_attempts < 1:
            raise ValueError("max_attempts_must_be_bounded_positive")

        for name in (
            "initial_backoff_seconds",
            "max_backoff_seconds",
            "backoff_multiplier",
            "jitter_ratio",
        ):
            _require_number(name, getattr(self, name))
        for name in (
            "retry_after_failed_safe",
            "retry_after_delivery_unknown",
            "requires_durable_state",
            "reconcile_before_retry",
        ):
            _require_bool(name, getattr(self, name))

        if self.initial_backoff_seconds < 0:
            raise ValueError("initial_backoff_seconds_must_be_non_negative")
        if self.max_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError("max_backoff_seconds_below_initial")
        if self.backoff_multiplier < 1:
            raise ValueError("backoff_multiplier_must_be_at_least_one")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio_out_of_range")
        if self.retry_class is RetryClass.CONTACT and self.retry_after_delivery_unknown:
            raise ValueError("contact_delivery_unknown_must_not_blind_retry")
        if self.schema_version != "v0.4":
            raise ValueError("unsupported_reliability_schema_version")

    def to_dict(self) -> dict[str, Any]:
        return canonicalize_operational_value(asdict(self))


@dataclass(frozen=True)
class BridgeOperation:
    """Privacy-minimized durable identity/state for one bridge operation."""

    operation_id: str
    kind: RetryClass
    idempotency_key: str
    request_hash: str
    state: OperationState
    attempt: int
    created_at: str
    updated_at: str
    next_attempt_at: str | None = None
    delivery_outcome: DeliveryOutcome | None = None
    last_error_code: str | None = None
    schema_version: str = "v0.4"

    def __post_init__(self) -> None:
        if not isinstance(self.kind, RetryClass):
            raise ValueError("kind_must_be_retry_class")
        if not isinstance(self.state, OperationState):
            raise ValueError("state_must_be_operation_state")
        if self.delivery_outcome is not None and not isinstance(
            self.delivery_outcome, DeliveryOutcome
        ):
            raise ValueError("delivery_outcome_must_be_delivery_outcome")

        for name in (
            "operation_id",
            "idempotency_key",
            "request_hash",
            "created_at",
            "updated_at",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name}_required")

        if not re.fullmatch(r"[0-9a-f]{64}", self.request_hash):
            raise ValueError("request_hash_must_be_sha256_hex")
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int):
            raise ValueError("attempt_must_be_int")
        if self.attempt < 0:
            raise ValueError("attempt_must_be_non_negative")
        if self.next_attempt_at is not None and (
            not isinstance(self.next_attempt_at, str) or not self.next_attempt_at.strip()
        ):
            raise ValueError("next_attempt_at_must_be_non_empty_string")
        if self.last_error_code is not None and not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,128}", self.last_error_code
        ):
            raise ValueError("last_error_code_must_be_safe_token")
        if self.schema_version != "v0.4":
            raise ValueError("unsupported_reliability_schema_version")

        if self.state is OperationState.RETRY_WAIT:
            if not self.next_attempt_at:
                raise ValueError("retry_wait_requires_next_attempt_at")
        elif self.next_attempt_at is not None:
            raise ValueError("next_attempt_at_only_valid_for_retry_wait")

        if (
            self.state is OperationState.DELIVERY_UNKNOWN
            and self.kind is not RetryClass.CONTACT
        ):
            raise ValueError("delivery_unknown_is_contact_only")

        if self.kind is not RetryClass.CONTACT:
            if self.delivery_outcome is not None:
                raise ValueError("delivery_outcome_is_contact_only")
            return

        if self.state is OperationState.COMPLETED:
            if self.delivery_outcome is not DeliveryOutcome.DELIVERED:
                raise ValueError("completed_contact_requires_delivered_outcome")
        elif self.state is OperationState.FAILED_SAFE:
            if self.delivery_outcome is not DeliveryOutcome.FAILED_SAFE:
                raise ValueError("failed_safe_contact_requires_failed_safe_outcome")
        elif self.state is OperationState.DELIVERY_UNKNOWN:
            if self.delivery_outcome is not DeliveryOutcome.DELIVERY_UNKNOWN:
                raise ValueError("delivery_unknown_state_requires_unknown_outcome")
        elif self.state is OperationState.EXHAUSTED:
            if self.delivery_outcome is not DeliveryOutcome.FAILED_SAFE:
                raise ValueError("exhausted_contact_requires_last_failed_safe_outcome")
        elif self.state is OperationState.RETRY_WAIT:
            if self.delivery_outcome is not DeliveryOutcome.FAILED_SAFE:
                raise ValueError("retry_wait_contact_requires_failed_safe_outcome")
        elif self.delivery_outcome not in (None, DeliveryOutcome.NOT_ATTEMPTED):
            raise ValueError("contact_outcome_not_valid_for_operation_state")

    def to_dict(self) -> dict[str, Any]:
        return canonicalize_operational_value(asdict(self))


LATE_COGNITION_COMPLETION_STATES = frozenset(
    {
        OperationState.PREPARED,
        OperationState.FAILED_SAFE,
        OperationState.RETRY_WAIT,
    }
)


def is_operation_transition_allowed(
    current: BridgeOperation,
    candidate: BridgeOperation,
) -> bool:
    """Class-aware transition guard for durable reliability state."""
    if current.operation_id != candidate.operation_id:
        return False
    if current.kind is not candidate.kind:
        return False
    if current.idempotency_key != candidate.idempotency_key:
        return False
    if current.request_hash != candidate.request_hash:
        return False
    if current.created_at != candidate.created_at:
        return False
    if current.schema_version != candidate.schema_version:
        return False

    if current.state is OperationState.PREPARED and candidate.state is OperationState.IN_FLIGHT:
        if candidate.attempt != current.attempt + 1:
            return False
    elif candidate.attempt != current.attempt:
        return False

    if candidate.state in ALLOWED_OPERATION_TRANSITIONS[current.state]:
        return True
    return (
        current.kind is RetryClass.COGNITION
        and current.attempt >= 1
        and current.state in LATE_COGNITION_COMPLETION_STATES
        and candidate.state is OperationState.COMPLETED
    )


@dataclass(frozen=True)
class HermesCompatibilityReport:
    """Startup discovery result for Hermes capabilities consumed by HLB."""

    hermes_version: str
    plugin_api_version: str
    gateway_hook_supported: bool
    session_source_supported: bool
    api_server_supported: bool
    send_supported: bool
    platforms: tuple[str, ...]
    warnings: tuple[str, ...]
    blocking_issues: tuple[str, ...]
    supported: bool
    observed_at: str
    schema_version: str = "v0.4"

    def __post_init__(self) -> None:
        for name in ("hermes_version", "plugin_api_version", "observed_at"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name}_required")
        for name in (
            "gateway_hook_supported",
            "session_source_supported",
            "api_server_supported",
            "send_supported",
            "supported",
        ):
            _require_bool(name, getattr(self, name))
        for name in ("platforms", "warnings", "blocking_issues"):
            value = getattr(self, name)
            if not isinstance(value, tuple) or not all(
                isinstance(item, str) and item.strip() for item in value
            ):
                raise ValueError(f"{name}_must_be_string_tuple")
        if self.schema_version != "v0.4":
            raise ValueError("unsupported_reliability_schema_version")
        if self.blocking_issues and self.supported:
            raise ValueError("supported_report_cannot_have_blocking_issues")
        if not self.supported and not self.blocking_issues:
            raise ValueError("unsupported_report_requires_blocking_issue")

    def to_dict(self) -> dict[str, Any]:
        return canonicalize_operational_value(asdict(self))
