from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import json
import sqlite3
import time

import pytest

from hermes_life_bridge.bridge import HermesLifeBridge
from hermes_life_bridge.cognition_model import CognitiveReceipt, CognitiveTaskEnvelope, sha256_text
from hermes_life_bridge.cognition_service import CognitionService
from hermes_life_bridge.config import BridgeConfig
from hermes_life_bridge.hermes_api import HermesApiError
from hermes_life_bridge.model import RuntimeReceipt
from hermes_life_bridge.operation_store import OperationStore
from hermes_life_bridge.percept_delivery import PerceptReliabilityExecutor
from hermes_life_bridge.reliability_contract import (
    BridgeOperation,
    OperationState,
    RetryClass,
)
from hermes_life_bridge.routing import HermesRoute, RouteStore


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def percept_event(key: str = "hermes:gateway:telegram:fault-1") -> dict:
    return {
        "event_id": "fault-percept",
        "life_did": "did:x",
        "source_body_id": "hermes-gateway:telegram",
        "modality": "system",
        "observed_at": iso(datetime.now(timezone.utc)),
        "payload_ref": "hermes://gateway/telegram/message/fault-1",
        "salience_hint": 0.5,
        "idempotency_key": key,
        "schema_version": "v0.1",
    }


def percept_cfg(tmp_path) -> BridgeConfig:
    return BridgeConfig(
        "did:x",
        str(tmp_path / "runtime.sock"),
        str(tmp_path / "trace.jsonl"),
        operation_db=str(tmp_path / "operations.sqlite3"),
        route_path=str(tmp_path / "route.json"),
    )


class FlakyRuntime:
    def __init__(self, *, fail_before=0, lose_ack_after_persist=0):
        self.fail_before = fail_before
        self.lose_ack_after_persist = lose_ack_after_persist
        self.calls = 0
        self.persisted_keys: set[str] = set()
        self.state_advances = 0

    def send_percept(self, event: dict) -> RuntimeReceipt:
        self.calls += 1
        if self.fail_before > 0:
            self.fail_before -= 1
            raise ConnectionRefusedError("runtime restarting")
        key = event["idempotency_key"]
        duplicate = key in self.persisted_keys
        if not duplicate:
            self.persisted_keys.add(key)
            self.state_advances += 1
        if self.lose_ack_after_persist > 0:
            self.lose_ack_after_persist -= 1
            raise TimeoutError("ack lost after persistence")
        return RuntimeReceipt(
            ok=True,
            duplicate=duplicate,
            persisted=True,
            state_sequence=self.state_advances,
            state_hash=f"state-{self.state_advances}",
            decision_outcome="defer",
        )


def test_runtime_disappears_then_outbox_recovers_without_raw_message(tmp_path):
    runtime = FlakyRuntime(fail_before=1)
    config = percept_cfg(tmp_path)
    executor = PerceptReliabilityExecutor(config, transport=runtime)
    first = executor.submit(percept_event())
    assert first.ok is False
    assert first.error == "percept_retry_deferred"
    assert len(executor.store.list_percept_outbox_operation_ids()) == 1
    executor.store.close()

    time.sleep(0.35)
    restarted = PerceptReliabilityExecutor(config, transport=runtime)
    result = restarted.pump()
    assert result["completed"] == 1
    assert result["outbox_remaining"] == 0
    assert runtime.calls == 2
    assert runtime.state_advances == 1

    raw = Path(config.operation_db).read_bytes()
    assert b"PRIVATE MESSAGE BODY" not in raw


def test_runtime_ack_loss_replay_advances_runtime_only_once(tmp_path):
    runtime = FlakyRuntime(lose_ack_after_persist=1)
    config = percept_cfg(tmp_path)
    executor = PerceptReliabilityExecutor(config, transport=runtime)
    result = executor.submit(percept_event("hermes:gateway:telegram:ack-lost"))
    assert result.ok is False
    assert runtime.state_advances == 1

    time.sleep(0.35)
    recovered = PerceptReliabilityExecutor(config, transport=runtime)
    recovered.pump()
    assert runtime.calls == 2
    assert runtime.state_advances == 1
    operation = recovered.store.get_by_idempotency(
        RetryClass.PERCEPT,
        "hermes:gateway:telegram:ack-lost",
    )
    assert operation.state is OperationState.COMPLETED


def test_crash_between_reservation_and_send_is_recovered(tmp_path):
    config = percept_cfg(tmp_path)
    runtime = FlakyRuntime()
    store = OperationStore(config.operation_db)
    event = percept_event("hermes:gateway:telegram:reserve-crash")
    request_hash = hashlib.sha256(
        json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    operation = BridgeOperation(
        operation_id="op-reserve-crash",
        kind=RetryClass.PERCEPT,
        idempotency_key=event["idempotency_key"],
        request_hash=request_hash,
        state=OperationState.PREPARED,
        attempt=0,
        created_at=event["observed_at"],
        updated_at=event["observed_at"],
    )
    store.reserve_percept(operation, event)
    store.close()

    restarted = PerceptReliabilityExecutor(config, transport=runtime)
    result = restarted.pump()
    assert result["completed"] == 1
    assert runtime.calls == 1
    assert runtime.state_advances == 1


def test_crash_after_runtime_persist_before_ack_recovers_idempotently(tmp_path):
    config = percept_cfg(tmp_path)
    runtime = FlakyRuntime()
    executor = PerceptReliabilityExecutor(config, transport=runtime)
    event = percept_event("hermes:gateway:telegram:crash-after-persist")
    operation = executor._initial_operation(event)
    executor.store.reserve_percept(operation, event)
    executor.store.start_attempt(operation.operation_id, updated_at=iso(datetime.now(timezone.utc)))
    # Runtime accepted before the HLB process disappeared.
    runtime.send_percept(event)
    executor.store.close()

    restarted = PerceptReliabilityExecutor(config, transport=runtime)
    restarted.pump()  # in-flight -> failed-safe -> retry-wait
    time.sleep(0.35)
    restarted.pump()  # due retry -> Runtime duplicate ACK
    assert runtime.calls == 2
    assert runtime.state_advances == 1
    assert restarted.store.get(operation.operation_id).state is OperationState.COMPLETED


def test_duplicate_percept_after_restart_never_calls_runtime_again(tmp_path):
    config = percept_cfg(tmp_path)
    runtime = FlakyRuntime()
    event = percept_event("hermes:gateway:telegram:duplicate-restart")
    first = PerceptReliabilityExecutor(config, transport=runtime)
    assert first.submit(event).ok is True
    assert runtime.calls == 1
    first.store.close()

    restarted = PerceptReliabilityExecutor(config, transport=runtime)
    duplicate = restarted.submit(event)
    assert duplicate.ok is True
    assert duplicate.duplicate is True
    assert runtime.calls == 1


def cognition_task(task_id="fault-cognition", idem="fault-cognition-idem"):
    now = datetime.now(timezone.utc)
    return CognitiveTaskEnvelope(
        task_id=task_id,
        idempotency_key=idem,
        life_did="did:x",
        event_id="event",
        basis_state_sequence=1,
        basis_state_hash="a" * 64,
        purpose="failure-injection",
        instruction="PRIVATE COGNITION INSTRUCTION",
        projection_ref="projection://fault",
        projection_hash="b" * 64,
        risk_level="L0",
        created_at=iso(now),
        expires_at=iso(now + timedelta(minutes=5)),
    )


def cognition_cfg(tmp_path):
    return BridgeConfig(
        "did:x",
        "/tmp/runtime.sock",
        str(tmp_path / "trace.jsonl"),
        cognition_socket=str(tmp_path / "cognition.sock"),
        cognition_db=str(tmp_path / "cognition.sqlite3"),
        operation_db=str(tmp_path / "operations.sqlite3"),
    )


class FlakyApi:
    def __init__(self, failures: int):
        self.failures = failures
        self.calls = 0

    def cognize(self, *, instruction, task_id):
        self.calls += 1
        if self.failures > 0:
            self.failures -= 1
            raise HermesApiError("http_500:temporary")
        return "recovered cognition", f"hlb-cognition-{task_id}"


def test_hermes_api_transient_500_retries_and_recovers(tmp_path):
    api = FlakyApi(2)
    sleeps = []
    service = CognitionService(
        cognition_cfg(tmp_path),
        api_client=api,
        sleep_fn=sleeps.append,
    )
    receipt = service.process(cognition_task())
    assert receipt.status == "completed"
    assert api.calls == 3
    assert len(sleeps) == 2
    operation = service.operations.get_by_idempotency(
        RetryClass.COGNITION,
        "fault-cognition-idem",
    )
    assert operation.state is OperationState.COMPLETED


def test_hermes_api_persistent_failure_exhausts_after_three_attempts(tmp_path):
    api = FlakyApi(99)
    service = CognitionService(
        cognition_cfg(tmp_path),
        api_client=api,
        sleep_fn=lambda seconds: None,
    )
    with pytest.raises(RuntimeError, match="cognition_retry_exhausted"):
        service.process(cognition_task(idem="always-fail"))
    assert api.calls == 3
    operation = service.operations.get_by_idempotency(
        RetryClass.COGNITION,
        "always-fail",
    )
    assert operation.state is OperationState.EXHAUSTED


def test_cognition_crash_after_receipt_before_operation_commit_heals_without_api_call(tmp_path):
    config = cognition_cfg(tmp_path)
    task = cognition_task(idem="receipt-before-crash")
    api = FlakyApi(0)
    service = CognitionService(config, api_client=api, sleep_fn=lambda seconds: None)
    service.store.reserve_task(task)
    operation, _ = service.operations.reserve(service._initial_operation(task))
    service.operations.start_attempt(operation.operation_id, updated_at=iso(datetime.now(timezone.utc)))
    output = "already computed"
    service.store.save_receipt(
        CognitiveReceipt(
            receipt_id="receipt-before-crash",
            task_id=task.task_id,
            idempotency_key=task.idempotency_key,
            life_did=task.life_did,
            status="completed",
            basis_state_sequence=task.basis_state_sequence,
            basis_state_hash=task.basis_state_hash,
            projection_hash=task.projection_hash,
            output_text=output,
            output_hash=sha256_text(output),
            hermes_session_id="session-before-crash",
            request_hash=task.request_hash(),
            started_at=task.created_at,
            completed_at=iso(datetime.now(timezone.utc)),
        )
    )
    service.operations.close()
    service.store.close()

    restarted_api = FlakyApi(0)
    restarted = CognitionService(
        config,
        api_client=restarted_api,
        sleep_fn=lambda seconds: None,
    )
    result = restarted.recover_startup()
    assert result["completed"] == 1
    assert restarted_api.calls == 0
    operation = restarted.operations.get_by_idempotency(
        RetryClass.COGNITION,
        task.idempotency_key,
    )
    assert operation.state is OperationState.COMPLETED
    receipt = restarted.process(task)
    assert receipt.duplicate is True
    assert restarted_api.calls == 0


def test_corrupt_operation_db_fails_closed(tmp_path):
    path = tmp_path / "operations.sqlite3"
    path.write_bytes(b"not a sqlite database")
    with pytest.raises(sqlite3.DatabaseError):
        OperationStore(str(path))


def test_stale_route_failure_is_already_enforced(tmp_path):
    path = tmp_path / "route.json"
    store = RouteStore(str(path))
    store.save(HermesRoute("telegram", "old-chat"))
    store.invalidate()
    loaded = store.load()
    assert loaded["valid"] is False


def test_service_templates_have_unbounded_restart_recovery_contract():
    for name in (
        "hermes-life-cognition.service.template",
        "hermes-life-contact.service.template",
        "hermes-life-percept-recovery.service.template",
    ):
        text = Path("systemd", name).read_text()
        assert "Restart=on-failure" in text
        assert "RestartSec=1" in text
        assert "StartLimitIntervalSec=0" in text


def test_plugin_reload_is_idempotent_and_privacy_safe(monkeypatch, tmp_path):
    from hermes_life_bridge import plugin

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    class Ctx:
        def __init__(self):
            self.handlers = {}
        def register_hook(self, name, fn):
            self.handlers[name] = fn

    first = Ctx()
    second = Ctx()
    plugin.register(first)
    plugin.register(second)
    assert set(first.handlers) == {"pre_gateway_dispatch", "pre_llm_call"}
    assert set(second.handlers) == {"pre_gateway_dispatch", "pre_llm_call"}
    evidence = tmp_path / "state" / "hermes-life-bridge" / "compatibility-evidence.json"
    text = evidence.read_text(encoding="utf-8")
    assert '"plugin_registered": true' in text
    assert "PRIVATE_CHAT" not in text


def test_installer_enables_percept_recovery_service():
    installer = Path("scripts/install_on_hermes.sh").read_text()
    assert "hermes-life-percept-recovery.service" in installer
    assert "enable --now hermes-life-percept-recovery.service" in installer
    assert "is-active --quiet hermes-life-percept-recovery.service" in installer


def test_duplicate_percept_with_regenerated_observed_at_keeps_same_logical_request(tmp_path):
    config = percept_cfg(tmp_path)
    runtime = FlakyRuntime()
    executor = PerceptReliabilityExecutor(config, transport=runtime)
    first = percept_event("hermes:gateway:telegram:observed-at-replay")
    assert executor.submit(first).ok is True

    replay = dict(first)
    replay["observed_at"] = iso(datetime.now(timezone.utc) + timedelta(seconds=1))
    duplicate = executor.submit(replay)
    assert duplicate.ok is True
    assert duplicate.duplicate is True
    assert runtime.calls == 1
    assert runtime.state_advances == 1
