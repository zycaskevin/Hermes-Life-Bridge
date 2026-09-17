from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from hermes_life_bridge.config import BridgeConfig
from hermes_life_bridge.core_observation import (
    CoreObservationError,
    manifest_digest,
    project_core_observation,
)

DLI = f"digital-life-identity:root:{'a' * 64}"
AT = "2026-09-17T10:00:00Z"


def manifest() -> dict:
    return {
        "schema": "digital-life.component.v1",
        "componentId": "hlb-core-003b",
        "componentType": "channel-adapter",
        "contract": "dlcore/channel-reference/v1",
        "contractVersion": "1",
        "binding": {"scope": "digital-life", "lifeDid": DLI},
        "operations": ["receive", "deliver", "health"],
        "capabilities": ["telegram"],
        "authorityClaims": [],
        "endpoint": "unix:///tmp/hlb-core-003b.sock",
        "health": {
            "protocol": "digital-life.health.v1",
            "endpoint": "unix:///tmp/hlb-core-003b-health.sock",
            "readinessEndpoint": "unix:///tmp/hlb-core-003b-ready.sock",
        },
    }


def identity() -> dict:
    return {
        "schema": "lifetime-hub.subject-observation.v1",
        "authority": "lifetime-hub",
        "lifeDid": DLI,
        "companionId": "companion-a",
        "definitionHash": "b" * 64,
        "identityVerified": True,
        "activationAuthorized": False,
    }


def config(tmp_path: Path, *, delivery: bool = True) -> BridgeConfig:
    return BridgeConfig(
        life_did=DLI,
        runtime_socket=str(tmp_path / "runtime.sock"),
        trace_path=str(tmp_path / "trace.jsonl"),
        cognition_socket=str(tmp_path / "cognition.sock"),
        cognition_db=str(tmp_path / "cognition.sqlite3"),
        contact_socket=str(tmp_path / "contact.sock"),
        contact_db=str(tmp_path / "contact.sqlite3"),
        contact_delivery_enabled=delivery,
        hermes_api_key="PRIVATE-KEY-THAT-MUST-NEVER-APPEAR",
    )


def test_hlb_core_observation_is_identity_bound_and_content_minimized(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    calls: list[str] = []
    def probe(path: str, timeout: float) -> bool:
        calls.append(path)
        assert 0 < timeout <= 1
        return True
    result = project_core_observation(
        cfg,
        component_manifest=manifest(),
        expected_life_did=DLI,
        identity_binding=identity(),
        socket_probe=probe,
        observed_at=AT,
    )
    assert result["health"]["status"] == "healthy"
    assert result["health"]["ready"] is True
    assert result["health"]["binding"]["lifeDid"] == DLI
    assert result["health"]["manifestDigest"] == manifest_digest(manifest())
    assert calls == [cfg.runtime_socket, cfg.contact_socket]
    rendered = json.dumps(result)
    assert "PRIVATE-KEY" not in rendered
    assert cfg.runtime_socket not in rendered
    assert cfg.contact_socket not in rendered


def test_hlb_external_delivery_off_is_safe_but_not_core_ready(tmp_path: Path) -> None:
    cfg = config(tmp_path, delivery=False)
    result = project_core_observation(
        cfg,
        component_manifest=manifest(),
        expected_life_did=DLI,
        identity_binding=identity(),
        socket_probe=lambda *_: True,
        observed_at=AT,
    )
    assert result["health"]["status"] == "degraded"
    assert result["health"]["ready"] is False


def test_hlb_core_observation_rejects_wrong_native_life(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    object.__setattr__(cfg, "life_did", f"digital-life-identity:root:{'c' * 64}")
    with pytest.raises(CoreObservationError, match="NATIVE_IDENTITY_MISMATCH"):
        project_core_observation(
            cfg, component_manifest=manifest(), expected_life_did=DLI,
            identity_binding=identity(), socket_probe=lambda *_: True, observed_at=AT,
        )


def test_hlb_core_observation_rejects_unverified_subject(tmp_path: Path) -> None:
    binding = {**identity(), "identityVerified": False}
    with pytest.raises(CoreObservationError, match="NATIVE_IDENTITY_NOT_VERIFIED"):
        project_core_observation(
            config(tmp_path), component_manifest=manifest(), expected_life_did=DLI,
            identity_binding=binding, socket_probe=lambda *_: True, observed_at=AT,
        )


def test_hlb_missing_native_socket_is_degraded(tmp_path: Path) -> None:
    result = project_core_observation(
        config(tmp_path), component_manifest=manifest(), expected_life_did=DLI,
        identity_binding=identity(), socket_probe=lambda *_: False, observed_at=AT,
    )
    assert result["health"]["ready"] is False
    assert result["health"]["status"] == "degraded"


def test_manifest_digest_changes_when_binding_changes() -> None:
    first = manifest()
    second = manifest()
    second["binding"] = {"scope": "digital-life", "lifeDid": f"digital-life-identity:root:{'d' * 64}"}
    assert manifest_digest(first) != manifest_digest(second)
