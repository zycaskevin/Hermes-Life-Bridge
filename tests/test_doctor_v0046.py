from __future__ import annotations

from pathlib import Path
import json
import os

import pytest

from hermes_life_bridge.config import BridgeConfig
from hermes_life_bridge.doctor import run_doctor
from hermes_life_bridge.operation_store import OperationStore
from hermes_life_bridge.reliability_contract import (
    BridgeOperation,
    DeliveryOutcome,
    HermesCompatibilityReport,
    OperationState,
    RetryClass,
)
from hermes_life_bridge.routing import HermesRoute, RouteStore


class Discovery:
    def __init__(self, report):
        self.report = report

    def discover(self):
        return self.report


def compatibility(*, warnings=(), blockers=(), api=True, send=True, gateway=True):
    return HermesCompatibilityReport(
        hermes_version="0.20.0",
        plugin_api_version="register_hook",
        gateway_hook_supported=gateway,
        session_source_supported=True,
        api_server_supported=api,
        send_supported=send,
        platforms=("feishu",),
        warnings=tuple(warnings),
        blocking_issues=tuple(blockers),
        supported=not blockers,
        observed_at="2026-09-02T04:00:00Z",
    )


def cfg(tmp_path, *, delivery=False):
    return BridgeConfig(
        "did:x",
        str(tmp_path / "runtime.sock"),
        str(tmp_path / "trace.jsonl"),
        cognition_socket=str(tmp_path / "cognition.sock"),
        contact_socket=str(tmp_path / "contact.sock"),
        cognition_db=str(tmp_path / "cognition.sqlite3"),
        contact_db=str(tmp_path / "contact.sqlite3"),
        operation_db=str(tmp_path / "operations.sqlite3"),
        contact_delivery_enabled=delivery,
        contact_target="feishu:oc_PRIVATE",
        route_path=str(tmp_path / "route.json"),
        compatibility_path=str(tmp_path / "compatibility.json"),
        compatibility_evidence_path=str(tmp_path / "compatibility-evidence.json"),
        route_max_age_seconds=60,
    )


def healthy_probes(monkeypatch):
    monkeypatch.setattr(
        "hermes_life_bridge.doctor._probe_unix",
        lambda path, timeout: {"path": path, "exists": True, "connect": True},
    )
    monkeypatch.setattr(
        "hermes_life_bridge.doctor.HermesApiClient.health",
        lambda self: {"ok": True},
    )


def test_doctor_healthy_components_are_human_readable(monkeypatch, tmp_path):
    healthy_probes(monkeypatch)
    config = cfg(tmp_path, delivery=True)
    RouteStore(config.route_path).save(HermesRoute("feishu", "oc_PRIVATE"))
    report = run_doctor(config, compatibility_discovery=Discovery(compatibility()))
    assert report["overall"] == "HEALTHY"
    assert report["components"]["ingress"]["status"] == "healthy"
    assert report["components"]["cognition"]["status"] == "healthy"
    assert report["components"]["contact"]["status"] == "healthy"
    assert report["components"]["privacy"]["status"] == "healthy"
    assert report["components"]["compatibility"]["status"] == "healthy"
    assert "Life Runtime" in report["components"]["ingress"]["message"]


def test_doctor_never_echoes_exact_private_target(monkeypatch, tmp_path):
    healthy_probes(monkeypatch)
    config = cfg(tmp_path, delivery=True)
    RouteStore(config.route_path).save(HermesRoute("feishu", "oc_PRIVATE_DOCTOR"))
    report = run_doctor(config, compatibility_discovery=Discovery(compatibility()))
    serialized = json.dumps(report, sort_keys=True)
    assert "oc_PRIVATE_DOCTOR" not in serialized
    assert "feishu:oc_PRIVATE" not in serialized
    assert report["route"]["platform"] == "feishu"
    assert report["contact"]["target_platform"] == "feishu"


def test_runtime_socket_failure_blocks_ingress(monkeypatch, tmp_path):
    def probe(path, timeout):
        return {"path": path, "exists": True, "connect": "runtime" not in path}

    monkeypatch.setattr("hermes_life_bridge.doctor._probe_unix", probe)
    monkeypatch.setattr(
        "hermes_life_bridge.doctor.HermesApiClient.health", lambda self: {"ok": True}
    )
    report = run_doctor(
        cfg(tmp_path),
        compatibility_discovery=Discovery(compatibility()),
    )
    assert report["components"]["ingress"]["status"] == "blocked"
    assert report["overall"] == "BLOCKED"


def test_gateway_not_yet_observed_is_degraded_when_otherwise_healthy(monkeypatch, tmp_path):
    healthy_probes(monkeypatch)
    report = run_doctor(
        cfg(tmp_path),
        compatibility_discovery=Discovery(
            compatibility(warnings=("gateway_hook_not_yet_observed",))
        ),
    )
    assert report["components"]["ingress"]["status"] == "degraded"
    assert report["components"]["compatibility"]["status"] == "degraded"
    assert report["overall"] == "DEGRADED"


def test_cognition_api_failure_blocks_only_cognition_path(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "hermes_life_bridge.doctor._probe_unix",
        lambda path, timeout: {"path": path, "exists": True, "connect": True},
    )

    def fail(self):
        raise RuntimeError("api down")

    monkeypatch.setattr("hermes_life_bridge.doctor.HermesApiClient.health", fail)
    report = run_doctor(
        cfg(tmp_path),
        compatibility_discovery=Discovery(compatibility(api=False)),
    )
    assert report["components"]["ingress"]["status"] == "healthy"
    assert report["components"]["cognition"]["status"] == "blocked"
    assert report["overall"] == "BLOCKED"


def test_delivery_unknown_blocks_contact(monkeypatch, tmp_path):
    healthy_probes(monkeypatch)
    config = cfg(tmp_path, delivery=True)
    RouteStore(config.route_path).save(HermesRoute("feishu", "oc_PRIVATE"))
    store = OperationStore(config.operation_db)
    operation = BridgeOperation(
        operation_id="op-doctor-unknown",
        kind=RetryClass.CONTACT,
        idempotency_key="contact:doctor-unknown",
        request_hash="a" * 64,
        state=OperationState.PREPARED,
        attempt=0,
        created_at="2026-09-02T04:00:00Z",
        updated_at="2026-09-02T04:00:00Z",
        delivery_outcome=DeliveryOutcome.NOT_ATTEMPTED,
    )
    store.reserve(operation)
    store.start_attempt(operation.operation_id, updated_at="2026-09-02T04:00:01Z")
    store.mark_delivery_unknown(
        operation.operation_id,
        updated_at="2026-09-02T04:00:02Z",
        error_code="provider_timeout_after_invoke",
    )
    store.close()

    report = run_doctor(config, compatibility_discovery=Discovery(compatibility()))
    assert report["operations"]["delivery_unknown"] == 1
    assert report["components"]["contact"]["status"] == "blocked"
    assert "unresolved_delivery_unknown" in report["components"]["contact"]["blockers"]


def test_stale_route_blocks_enabled_contact(monkeypatch, tmp_path):
    healthy_probes(monkeypatch)
    config = cfg(tmp_path, delivery=True)
    path = Path(config.route_path)
    path.write_text(
        json.dumps(
            {
                "platform": "feishu",
                "chat_id": "oc_OLD",
                "target": "feishu:oc_OLD",
                "updated_at": "2020-01-01T00:00:00Z",
                "valid": True,
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    report = run_doctor(config, compatibility_discovery=Discovery(compatibility()))
    assert report["route"]["status"] == "stale"
    assert "route_stale" in report["components"]["contact"]["blockers"]


def test_privacy_bad_mode_blocks_doctor(monkeypatch, tmp_path):
    healthy_probes(monkeypatch)
    config = cfg(tmp_path)
    Path(config.contact_db).write_bytes(b"safe")
    os.chmod(config.contact_db, 0o644)
    report = run_doctor(config, compatibility_discovery=Discovery(compatibility()))
    assert report["components"]["privacy"]["status"] == "blocked"
    assert report["privacy"]["bad_modes"]["contact_db"] == "0o644"


def test_privacy_forbidden_repr_blocks_doctor(monkeypatch, tmp_path):
    healthy_probes(monkeypatch)
    config = cfg(tmp_path)
    Path(config.trace_path).write_bytes(b"Platform.FEISHU")
    report = run_doctor(config, compatibility_discovery=Discovery(compatibility()))
    assert report["privacy"]["representation_boundary"] == "FAIL"
    assert report["components"]["privacy"]["status"] == "blocked"


def test_privacy_scans_rotated_trace_files(monkeypatch, tmp_path):
    healthy_probes(monkeypatch)
    config = cfg(tmp_path)
    rotated = Path(f"{config.trace_path}.1")
    rotated.write_bytes(b"SessionSource(platform=Platform.FEISHU)")
    rotated.chmod(0o600)
    report = run_doctor(config, compatibility_discovery=Discovery(compatibility()))
    assert report["privacy"]["representation_boundary"] == "FAIL"
    assert report["components"]["privacy"]["status"] == "blocked"
