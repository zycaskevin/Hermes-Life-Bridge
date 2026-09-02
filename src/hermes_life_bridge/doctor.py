from __future__ import annotations

from pathlib import Path
import json
import socket
import sqlite3
import stat
from typing import Any

from .compatibility import CompatibilityDiscovery
from .config import BridgeConfig
from .hermes_api import HermesApiClient
from .representation import (
    canonical_platform,
    contains_forbidden_representation_bytes,
)
from .routing import RouteStore, route_status
from .trace import BridgeTracer


def _probe_unix(path: str, timeout: float) -> dict[str, Any]:
    result: dict[str, Any] = {"path": path, "exists": False, "connect": False}
    if not path:
        return result
    result["exists"] = Path(path).exists()
    if result["exists"]:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                sock.connect(path)
                result["connect"] = True
        except Exception as exc:
            result["error"] = type(exc).__name__
    return result


def _mode(path: str) -> str | None:
    if not path:
        return None
    candidate = Path(path)
    if not candidate.exists():
        return None
    try:
        return oct(stat.S_IMODE(candidate.stat().st_mode))
    except Exception:
        return None


def _sqlite_counts(path: str) -> dict[str, int]:
    counts = {
        "prepared": 0,
        "in_flight": 0,
        "retry_wait": 0,
        "completed": 0,
        "failed_safe": 0,
        "delivery_unknown": 0,
        "exhausted": 0,
    }
    if not path or not Path(path).exists():
        return counts
    try:
        uri = f"file:{Path(path).resolve()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        rows = conn.execute(
            "SELECT state, COUNT(*) FROM bridge_operations GROUP BY state"
        ).fetchall()
        conn.close()
        for state, count in rows:
            if state in counts:
                counts[state] = int(count)
    except Exception:
        counts["corrupt_or_unreadable"] = 1
    return counts


def _forbidden_repr_scan(paths: list[str]) -> dict[str, Any]:
    scanned = 0
    violation = False
    for raw_path in paths:
        if not raw_path:
            continue
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{raw_path}{suffix}")
            if not candidate.exists() or not candidate.is_file():
                continue
            scanned += 1
            try:
                if contains_forbidden_representation_bytes(candidate.read_bytes()):
                    violation = True
            except Exception:
                violation = True
    return {"files_scanned": scanned, "violation": violation}


def _component(status: str, message: str, **details: Any) -> dict[str, Any]:
    return {"status": status, "message": message, **details}


def _overall(components: dict[str, dict[str, Any]]) -> str:
    statuses = {item.get("status") for item in components.values()}
    if "blocked" in statuses:
        return "BLOCKED"
    if "degraded" in statuses:
        return "DEGRADED"
    return "HEALTHY"


def run_doctor(
    config: BridgeConfig | None = None,
    *,
    compatibility_discovery: CompatibilityDiscovery | None = None,
) -> dict[str, Any]:
    config = config or BridgeConfig.from_env()
    tracer = BridgeTracer(config.trace_path, max_bytes=config.trace_max_bytes, backup_count=config.trace_backup_count)

    runtime_socket = _probe_unix(config.runtime_socket, config.connect_timeout_seconds)
    cognition_socket = _probe_unix(config.cognition_socket, config.connect_timeout_seconds)
    contact_socket = _probe_unix(config.contact_socket, config.connect_timeout_seconds)

    api = {
        "base_url": config.hermes_api_base_url,
        "healthy": False,
        "key_loaded": bool(config.hermes_api_key),
    }
    try:
        HermesApiClient(config).health()
        api["healthy"] = True
    except Exception as exc:
        api["error"] = type(exc).__name__

    discovery = compatibility_discovery or CompatibilityDiscovery(config)
    try:
        compatibility = discovery.discover()
        compatibility_data = compatibility.to_dict()
    except Exception as exc:
        compatibility = None
        compatibility_data = {
            "supported": False,
            "hermes_version": "unknown",
            "warnings": [],
            "blocking_issues": ["compatibility_discovery_failed"],
            "error": type(exc).__name__,
        }

    private_route = RouteStore(config.route_path).load()
    route_state = route_status(
        private_route,
        max_age_seconds=config.route_max_age_seconds,
    )
    route_platform = canonical_platform((private_route or {}).get("platform"))
    configured_platform = canonical_platform(config.contact_target)
    route_info = {
        "status": route_state.value,
        "platform": route_platform or configured_platform or "unknown",
        "learned_route_present": bool(private_route),
        "configured_fallback_present": bool(config.contact_target),
        "mode": _mode(config.route_path),
        "max_age_seconds": config.route_max_age_seconds,
    }

    operation_counts = _sqlite_counts(config.operation_db)
    trace_paths = [config.trace_path] + [
        f"{config.trace_path}.{index}"
        for index in range(1, config.trace_backup_count + 1)
    ]
    privacy_scan = _forbidden_repr_scan(
        [config.operation_db, config.contact_db, *trace_paths]
    )
    private_modes = {
        "route_store": _mode(config.route_path),
        "operation_db": _mode(config.operation_db),
        "contact_db": _mode(config.contact_db),
        "compatibility": _mode(config.compatibility_path),
        "compatibility_evidence": _mode(config.compatibility_evidence_path),
        "trace": _mode(config.trace_path),
        "trace_lock": _mode(f"{config.trace_path}.lock"),
        **{
            f"trace_backup_{index}": _mode(f"{config.trace_path}.{index}")
            for index in range(1, config.trace_backup_count + 1)
        },
    }
    bad_modes = {
        name: mode
        for name, mode in private_modes.items()
        if mode is not None and mode != "0o600"
    }

    trace_info: dict[str, Any] = {
        "path": config.trace_path,
        "exists": Path(config.trace_path).exists() if config.trace_path else False,
        "writable_parent": False,
        "last_stage": None,
        "last_status": None,
        "last_hook": None,
    }
    if config.trace_path:
        parent = Path(config.trace_path).parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
            trace_info["writable_parent"] = parent.is_dir()
        except Exception:
            pass
    tail = tracer.tail(1)
    if tail:
        last = tail[-1]
        trace_info.update(
            last_stage=last.get("stage"),
            last_status=last.get("status"),
            last_hook=last.get("hook"),
        )

    compatibility_blocked = bool(compatibility_data.get("blocking_issues"))
    compatibility_warn = bool(compatibility_data.get("warnings"))
    gateway_supported = bool(compatibility_data.get("gateway_hook_supported"))

    if not runtime_socket["connect"] or not gateway_supported:
        ingress = _component(
            "blocked",
            "Hermes messages cannot be trusted to reach Life Runtime yet.",
            runtime_socket=runtime_socket["connect"],
            gateway_hook=gateway_supported,
        )
    elif "gateway_hook_not_yet_observed" in compatibility_data.get("warnings", []):
        ingress = _component(
            "degraded",
            "Ingress is installed, but no real Gateway message has proven the hook yet.",
            runtime_socket=True,
            gateway_hook=True,
        )
    else:
        ingress = _component(
            "healthy",
            "Hermes Gateway can feed Life Runtime.",
            runtime_socket=True,
            gateway_hook=True,
        )

    if not cognition_socket["connect"] or not api["healthy"]:
        cognition = _component(
            "blocked",
            "Cognition bridge is unavailable until its local service and Hermes API are healthy.",
            service_socket=cognition_socket["connect"],
            hermes_api=api["healthy"],
        )
    else:
        cognition = _component(
            "healthy",
            "Life Runtime can request bounded cognition from Hermes.",
            service_socket=True,
            hermes_api=True,
        )

    contact_blockers: list[str] = []
    if not contact_socket["connect"]:
        contact_blockers.append("contact_service_unavailable")
    if config.contact_delivery_enabled and not compatibility_data.get("send_supported", False):
        contact_blockers.append("hermes_send_unavailable")
    if operation_counts.get("delivery_unknown", 0) > 0:
        contact_blockers.append("unresolved_delivery_unknown")
    if config.contact_delivery_enabled:
        if route_state.value in {"stale", "invalid"}:
            contact_blockers.append(f"route_{route_state.value}")
        if route_state.value == "unknown" and not config.contact_target:
            contact_blockers.append("route_unknown")

    if contact_blockers:
        contact = _component(
            "blocked",
            "Proactive contact is paused until the listed safety issue is resolved.",
            delivery_enabled=config.contact_delivery_enabled,
            route_status=route_state.value,
            unresolved_delivery_unknown=operation_counts.get("delivery_unknown", 0),
            blockers=contact_blockers,
        )
    elif config.contact_delivery_enabled:
        contact = _component(
            "healthy",
            "Governed proactive contact is ready.",
            delivery_enabled=True,
            route_status=route_state.value,
            unresolved_delivery_unknown=0,
        )
    else:
        contact = _component(
            "healthy",
            "Contact service is healthy; external delivery is intentionally OFF.",
            delivery_enabled=False,
            route_status=route_state.value,
            unresolved_delivery_unknown=0,
        )

    if privacy_scan["violation"] or bad_modes:
        privacy = _component(
            "blocked",
            "Operational privacy boundary needs repair before normal operation.",
            representation_boundary="fail" if privacy_scan["violation"] else "pass",
            bad_modes=bad_modes,
            files_scanned=privacy_scan["files_scanned"],
        )
    else:
        privacy = _component(
            "healthy",
            "Operational storage is representation-clean and private files are mode 0600.",
            representation_boundary="pass",
            bad_modes={},
            files_scanned=privacy_scan["files_scanned"],
        )

    if compatibility_blocked:
        compatibility_component = _component(
            "blocked",
            "Installed Hermes is missing a required HLB capability.",
            version=compatibility_data.get("hermes_version", "unknown"),
            blockers=compatibility_data.get("blocking_issues", []),
            warnings=compatibility_data.get("warnings", []),
        )
    elif compatibility_warn:
        compatibility_component = _component(
            "degraded",
            "Core compatibility is usable, with non-blocking capability warnings.",
            version=compatibility_data.get("hermes_version", "unknown"),
            blockers=[],
            warnings=compatibility_data.get("warnings", []),
        )
    else:
        compatibility_component = _component(
            "healthy",
            "Hermes capabilities required by HLB are available.",
            version=compatibility_data.get("hermes_version", "unknown"),
            blockers=[],
            warnings=[],
        )

    components = {
        "ingress": ingress,
        "cognition": cognition,
        "contact": contact,
        "privacy": privacy,
        "compatibility": compatibility_component,
    }
    overall = _overall(components)

    # Keep the old low-level keys for scripts that already consume Doctor output,
    # but never echo an exact contact target.
    result = {
        "overall": overall,
        "components": components,
        "runtime_socket": runtime_socket,
        "cognition_socket": cognition_socket,
        "contact_socket": contact_socket,
        "hermes_api": api,
        "contact": {
            "delivery_enabled": config.contact_delivery_enabled,
            "target_platform": configured_platform or route_platform or "unknown",
            "unresolved_delivery_unknown": operation_counts.get("delivery_unknown", 0),
        },
        "route": route_info,
        "operations": operation_counts,
        "privacy": {
            "representation_boundary": "FAIL" if privacy_scan["violation"] else "PASS",
            "file_modes": private_modes,
            "bad_modes": bad_modes,
        },
        "compatibility": compatibility_data,
        "trace": trace_info,
    }
    return result
