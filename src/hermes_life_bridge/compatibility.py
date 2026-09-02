from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
from typing import Callable

from .config import BridgeConfig
from .hermes_api import HermesApiClient
from .reliability_contract import HermesCompatibilityReport
from .representation import canonical_platform, canonicalize_operational_value
from .routing import RouteStore, normalize_session_source


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class CompatibilityEvidenceStore:
    """Privacy-minimized observations proving Hermes plugin/runtime capabilities."""

    def __init__(self, path: str):
        self.path = Path(path)

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        safe = canonicalize_operational_value(data)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(safe, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        tmp.chmod(0o600)
        tmp.replace(self.path)
        self.path.chmod(0o600)

    def record_registration(self, *, plugin_api_version: str = "register_hook") -> None:
        data = self._load()
        data.update(
            {
                "plugin_registered": True,
                "gateway_hook_registered": True,
                "cli_hook_registered": True,
                "plugin_api_version": plugin_api_version,
                "registered_at": _now(),
            }
        )
        self._save(data)

    def record_gateway_event(self, source) -> None:
        data = self._load()
        route = normalize_session_source(source)
        platforms = {
            canonical_platform(value)
            for value in data.get("platforms", [])
            if canonical_platform(value)
        }
        if route.platform:
            platforms.add(route.platform)
        data.update(
            {
                "gateway_hook_observed": True,
                "gateway_hook_observed_at": _now(),
                "platforms": sorted(platforms),
            }
        )
        if route.platform and route.chat_id:
            data["session_source_supported"] = True
            data["session_source_observed_at"] = _now()
        self._save(data)

    def snapshot(self) -> dict:
        return canonicalize_operational_value(self._load())


class CompatibilityDiscovery:
    def __init__(
        self,
        config: BridgeConfig | None = None,
        *,
        command_runner: Callable[..., subprocess.CompletedProcess] | None = None,
        api_health_probe: Callable[[], object] | None = None,
    ):
        self.config = config or BridgeConfig.from_env()
        self.command_runner = command_runner or subprocess.run
        self.api_health_probe = api_health_probe or HermesApiClient(self.config).health
        self.evidence = CompatibilityEvidenceStore(
            self.config.compatibility_evidence_path
        )

    def _command(self, args: list[str]) -> tuple[bool, str]:
        try:
            result = self.command_runner(
                args,
                text=True,
                capture_output=True,
                timeout=5,
            )
        except Exception:
            return False, ""
        if getattr(result, "returncode", 1) != 0:
            return False, ""
        output = str(getattr(result, "stdout", "") or getattr(result, "stderr", "") or "")
        return True, output[:2048]

    @staticmethod
    def _version(output: str) -> str:
        match = re.search(r"\b(?:v)?(\d+\.\d+(?:\.\d+)?(?:[-+][A-Za-z0-9_.-]+)?)\b", output)
        return match.group(1) if match else "unknown"

    def discover(self) -> HermesCompatibilityReport:
        warnings: list[str] = []
        blocking: list[str] = []
        observed = self.evidence.snapshot()

        cli_ok, version_output = self._command([self.config.hermes_cli_path, "--version"])
        hermes_version = self._version(version_output) if cli_ok else "unknown"
        if not cli_ok:
            blocking.append("hermes_cli_unavailable")
        elif hermes_version == "unknown":
            warnings.append("hermes_version_unparseable")

        send_supported, _ = self._command([self.config.hermes_cli_path, "send", "--help"])
        if not send_supported:
            warnings.append("hermes_send_unavailable")

        api_server_supported = False
        try:
            self.api_health_probe()
            api_server_supported = True
        except Exception:
            warnings.append("hermes_api_unavailable")

        gateway_hook_supported = bool(observed.get("gateway_hook_registered"))
        if not gateway_hook_supported:
            blocking.append("gateway_hook_registration_unobserved")
        elif not observed.get("gateway_hook_observed"):
            warnings.append("gateway_hook_not_yet_observed")

        route = RouteStore(self.config.route_path).load()
        route_platform = canonical_platform((route or {}).get("platform"))
        route_proves_session_source = bool(
            route_platform and (route or {}).get("chat_id")
        )
        session_source_supported = bool(
            observed.get("session_source_supported") or route_proves_session_source
        )
        if not session_source_supported:
            warnings.append("session_source_not_yet_observed")

        platforms = {
            canonical_platform(value)
            for value in observed.get("platforms", [])
            if canonical_platform(value)
        }
        if route_platform:
            platforms.add(route_platform)
        configured_platform = canonical_platform(self.config.contact_target)
        if configured_platform:
            platforms.add(configured_platform)

        if self.config.contact_delivery_enabled and not send_supported:
            blocking.append("contact_delivery_enabled_but_send_unavailable")

        plugin_api_version = str(observed.get("plugin_api_version") or "unknown")
        supported = not blocking
        report = HermesCompatibilityReport(
            hermes_version=hermes_version,
            plugin_api_version=plugin_api_version,
            gateway_hook_supported=gateway_hook_supported,
            session_source_supported=session_source_supported,
            api_server_supported=api_server_supported,
            send_supported=send_supported,
            platforms=tuple(sorted(platforms)),
            warnings=tuple(sorted(set(warnings))),
            blocking_issues=tuple(sorted(set(blocking))),
            supported=supported,
            observed_at=_now(),
        )
        self.publish(report)
        return report

    def publish(self, report: HermesCompatibilityReport) -> None:
        path = Path(self.config.compatibility_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(report.to_dict(), sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        tmp.chmod(0o600)
        tmp.replace(path)
        path.chmod(0o600)

    def load_published(self) -> dict | None:
        path = Path(self.config.compatibility_path)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return data if isinstance(data, dict) else None
