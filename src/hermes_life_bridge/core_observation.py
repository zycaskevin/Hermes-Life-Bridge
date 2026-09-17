"""Read-only Hermes Life Bridge -> Digital Life Core health projection.

This observes HLB's native life binding and local Unix service reachability. It never
sends a message, invokes cognition, authorizes contact, or grants component attachment.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import sys
from typing import Any, Callable

from .config import BridgeConfig


class CoreObservationError(ValueError):
    pass


def _canonical(value: Any) -> Any:
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    if isinstance(value, dict):
        return {key: _canonical(value[key]) for key in sorted(value) if value[key] is not None}
    return value


def manifest_digest(value: Any) -> str:
    raw = json.dumps(_canonical(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _required(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 1024:
        raise CoreObservationError(code)
    return value


def _manifest(value: Any, *, expected_life_did: str, expected_digest: str) -> dict[str, Any]:
    if not isinstance(value, dict) or manifest_digest(value) != expected_digest:
        raise CoreObservationError("MANIFEST_PIN_MISMATCH")
    if (
        value.get("schema") != "digital-life.component.v1"
        or value.get("componentType") != "channel-adapter"
        or value.get("contract") != "dlcore/channel-reference/v1"
        or value.get("contractVersion") != "1"
        or value.get("binding") != {"scope": "digital-life", "lifeDid": expected_life_did}
        or not isinstance(value.get("componentId"), str)
        or not value["componentId"]
    ):
        raise CoreObservationError("MANIFEST_TARGET_MISMATCH")
    return value


def probe_unix_socket(path: str, timeout_seconds: float = 0.25) -> bool:
    candidate = Path(_required(path, "NATIVE_SOCKET_INVALID"))
    if not candidate.is_absolute():
        raise CoreObservationError("NATIVE_SOCKET_INVALID")
    try:
        mode = candidate.lstat().st_mode
        if not stat.S_ISSOCK(mode):
            return False
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(max(0.05, min(timeout_seconds, 1.0)))
            client.connect(str(candidate))
        return True
    except (FileNotFoundError, ConnectionRefusedError, TimeoutError, socket.timeout, OSError):
        return False


def project_core_observation(
    config: BridgeConfig,
    *,
    component_manifest: Any,
    expected_life_did: str,
    identity_binding: Any,
    socket_probe: Callable[[str, float], bool] = probe_unix_socket,
    observed_at: str | None = None,
) -> dict[str, Any]:
    expected_life_did = _required(expected_life_did, "OBSERVATION_TARGET_INVALID")
    digest = manifest_digest(component_manifest)
    manifest = _manifest(component_manifest, expected_life_did=expected_life_did, expected_digest=digest)
    if config.life_did != expected_life_did:
        raise CoreObservationError("NATIVE_IDENTITY_MISMATCH")
    if (
        not isinstance(identity_binding, dict)
        or identity_binding.get("schema") != "lifetime-hub.subject-observation.v1"
        or identity_binding.get("authority") != "lifetime-hub"
        or identity_binding.get("identityVerified") is not True
        or identity_binding.get("activationAuthorized") is not False
        or identity_binding.get("lifeDid") != expected_life_did
    ):
        raise CoreObservationError("NATIVE_IDENTITY_NOT_VERIFIED")

    runtime_ok = socket_probe(config.runtime_socket, config.connect_timeout_seconds)
    contact_ok = socket_probe(config.contact_socket, min(config.contact_timeout_seconds, 1.0))
    # Core's channel profile includes receive + deliver, so external delivery OFF
    # is intentionally not advertised as ready even when the safety posture is valid.
    ready = runtime_ok and contact_ok and config.contact_delivery_enabled
    timestamp = observed_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
    except (ValueError, TypeError):
        raise CoreObservationError("OBSERVATION_TIMESTAMP_INVALID") from None

    base = {
        "schema": "digital-life.health.v1",
        "componentId": manifest["componentId"],
        "contract": "dlcore/channel-reference/v1",
        "contractVersion": "1",
        "binding": {"scope": "digital-life", "lifeDid": expected_life_did},
        "manifestDigest": digest,
        "observedAt": timestamp,
    }
    return {
        "health": {**base, "status": "healthy" if ready else "degraded", "ready": ready},
        "readiness": {**base, "status": "healthy" if ready else "degraded", "ready": ready},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--life-did", required=True)
    parser.add_argument("--lifetime-db", required=True)
    parser.add_argument("--companion-id", required=True)
    parser.add_argument("--lifetime-pythonpath", required=True)
    parser.add_argument("--config-file", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        sys.path.insert(0, str(Path(args.lifetime_pythonpath).expanduser().resolve()))
        from lifetime_hub.component_observation import verify_agent_subject
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        old_config = os.environ.get("HLB_CONFIG_FILE")
        os.environ["HLB_CONFIG_FILE"] = str(Path(args.config_file).expanduser().resolve())
        try:
            config = BridgeConfig.from_env()
        finally:
            if old_config is None:
                os.environ.pop("HLB_CONFIG_FILE", None)
            else:
                os.environ["HLB_CONFIG_FILE"] = old_config
        identity = verify_agent_subject(
            db_path=args.lifetime_db,
            companion_id=args.companion_id,
            expected_life_did=args.life_did,
        )
        result = project_core_observation(
            config,
            component_manifest=manifest,
            expected_life_did=args.life_did,
            identity_binding=identity,
        )
        Path(args.output).write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.chmod(args.output, 0o600)
        print(json.dumps({"result": "PASS", "output": str(Path(args.output).resolve())}, sort_keys=True))
        return 0
    except Exception:
        print(json.dumps({"result": "NON_CONFORMANT", "reasonCodes": ["HLB_NATIVE_OBSERVATION_FAILED"]}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
