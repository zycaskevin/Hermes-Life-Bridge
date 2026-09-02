import pytest

from hermes_life_bridge.reliability_contract import (
    ALLOWED_OPERATION_TRANSITIONS,
    BridgeOperation,
    DeliveryOutcome,
    HermesCompatibilityReport,
    OperationState,
    RetryClass,
    RetryPolicy,
    RouteStatus,
)


def _contact_policy(**overrides):
    values = dict(
        retry_class=RetryClass.CONTACT,
        max_attempts=3,
        initial_backoff_seconds=1.0,
        max_backoff_seconds=10.0,
        backoff_multiplier=2.0,
        jitter_ratio=0.2,
        retry_after_failed_safe=True,
        retry_after_delivery_unknown=False,
        requires_durable_state=True,
        reconcile_before_retry=True,
    )
    values.update(overrides)
    return RetryPolicy(**values)


def _operation(**overrides):
    values = dict(
        operation_id="op-1",
        kind=RetryClass.CONTACT,
        idempotency_key="idem-1",
        request_hash="a" * 64,
        state=OperationState.PREPARED,
        attempt=0,
        created_at="2026-09-02T00:00:00Z",
        updated_at="2026-09-02T00:00:00Z",
        delivery_outcome=DeliveryOutcome.NOT_ATTEMPTED,
    )
    values.update(overrides)
    return BridgeOperation(**values)


def test_contract_enums_use_stable_canonical_values():
    assert RetryClass.PERCEPT.value == "percept"
    assert OperationState.DELIVERY_UNKNOWN.value == "delivery_unknown"
    assert DeliveryOutcome.FAILED_SAFE.value == "failed_safe"
    assert RouteStatus.FRESH.value == "fresh"


def test_transition_contract_forbids_unknown_to_retry_wait():
    assert OperationState.RETRY_WAIT not in ALLOWED_OPERATION_TRANSITIONS[
        OperationState.DELIVERY_UNKNOWN
    ]
    assert ALLOWED_OPERATION_TRANSITIONS[OperationState.DELIVERY_UNKNOWN] == {
        OperationState.COMPLETED,
        OperationState.FAILED_SAFE,
    }


def test_retry_policy_is_bounded():
    with pytest.raises(ValueError, match="max_attempts_must_be_bounded_positive"):
        _contact_policy(max_attempts=0)


def test_retry_policy_rejects_untyped_retry_class():
    with pytest.raises(ValueError, match="retry_class_must_be_retry_class"):
        _contact_policy(retry_class="contact")


def test_contact_policy_forbids_blind_retry_after_unknown_delivery():
    with pytest.raises(ValueError, match="contact_delivery_unknown_must_not_blind_retry"):
        _contact_policy(retry_after_delivery_unknown=True)


def test_retry_wait_requires_next_attempt_at():
    with pytest.raises(ValueError, match="retry_wait_requires_next_attempt_at"):
        _operation(
            state=OperationState.RETRY_WAIT,
            delivery_outcome=DeliveryOutcome.FAILED_SAFE,
        )


def test_contact_retry_wait_preserves_failed_safe_evidence():
    operation = _operation(
        state=OperationState.RETRY_WAIT,
        attempt=1,
        delivery_outcome=DeliveryOutcome.FAILED_SAFE,
        next_attempt_at="2026-09-02T00:00:10Z",
    )
    assert operation.delivery_outcome is DeliveryOutcome.FAILED_SAFE


def test_delivery_unknown_is_contact_only():
    with pytest.raises(ValueError, match="delivery_unknown_is_contact_only"):
        BridgeOperation(
            operation_id="op-p",
            kind=RetryClass.PERCEPT,
            idempotency_key="idem-p",
            request_hash="b" * 64,
            state=OperationState.DELIVERY_UNKNOWN,
            attempt=1,
            created_at="2026-09-02T00:00:00Z",
            updated_at="2026-09-02T00:00:01Z",
        )


def test_contact_unknown_outcome_cannot_carry_retry_schedule():
    with pytest.raises(ValueError, match="next_attempt_at_only_valid_for_retry_wait"):
        _operation(
            state=OperationState.DELIVERY_UNKNOWN,
            attempt=1,
            delivery_outcome=DeliveryOutcome.DELIVERY_UNKNOWN,
            next_attempt_at="2026-09-02T00:00:10Z",
        )


def test_contact_state_requires_matching_delivery_outcome():
    with pytest.raises(ValueError, match="completed_contact_requires_delivered_outcome"):
        _operation(
            state=OperationState.COMPLETED,
            attempt=1,
            delivery_outcome=DeliveryOutcome.FAILED_SAFE,
        )


def test_operation_rejects_raw_error_detail_in_code_field():
    with pytest.raises(ValueError, match="last_error_code_must_be_safe_token"):
        _operation(last_error_code="timeout talking to provider for chat 123")


def test_operation_serialization_is_primitive_and_repr_safe():
    data = _operation().to_dict()
    assert data["kind"] == "contact"
    assert data["state"] == "prepared"
    assert data["delivery_outcome"] == "not_attempted"
    raw = repr(data)
    assert "RetryClass." not in raw
    assert "OperationState." not in raw
    assert "DeliveryOutcome." not in raw


def test_compatibility_report_rejects_supported_with_blockers():
    with pytest.raises(ValueError, match="supported_report_cannot_have_blocking_issues"):
        HermesCompatibilityReport(
            hermes_version="0.20.0",
            plugin_api_version="unknown",
            gateway_hook_supported=True,
            session_source_supported=True,
            api_server_supported=True,
            send_supported=False,
            platforms=("feishu",),
            warnings=(),
            blocking_issues=("send_missing",),
            supported=True,
            observed_at="2026-09-02T00:00:00Z",
        )


def test_compatibility_report_serialization_is_canonical():
    report = HermesCompatibilityReport(
        hermes_version="0.20.0",
        plugin_api_version="unknown",
        gateway_hook_supported=True,
        session_source_supported=True,
        api_server_supported=True,
        send_supported=True,
        platforms=("feishu", "telegram"),
        warnings=("plugin_api_version_not_reported",),
        blocking_issues=(),
        supported=True,
        observed_at="2026-09-02T00:00:00Z",
    )
    data = report.to_dict()
    assert data["platforms"] == ["feishu", "telegram"]
    assert data["supported"] is True
