from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import stat

import pytest

from hermes_life_bridge.compatibility import (
    CompatibilityDiscovery,
    CompatibilityEvidenceStore,
)
from hermes_life_bridge.config import BridgeConfig
from hermes_life_bridge.routing import HermesRoute, RouteStore
from hermes_life_bridge import plugin


@dataclass
class Result:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


def cfg(tmp_path, *, delivery_enabled: bool = False) -> BridgeConfig:
    return BridgeConfig(
        "did:x",
        "/tmp/runtime.sock",
        str(tmp_path / "trace.jsonl"),
        hermes_cli_path="hermes",
        contact_delivery_enabled=delivery_enabled,
        contact_target="feishu:oc_PRIVATE",
        route_path=str(tmp_path / "route.json"),
        compatibility_path=str(tmp_path / "compatibility.json"),
        compatibility_evidence_path=str(tmp_path / "compatibility-evidence.json"),
    )


def good_runner(args, **kwargs):
    if args[-1] == "--version":
        return Result(stdout="Hermes Agent v0.20.0\n")
    if args[-2:] == ["send", "--help"]:
        return Result(stdout="usage: hermes send ...\n")
    return Result(returncode=1)


def test_evidence_store_records_capabilities_without_private_route(tmp_path):
    path = tmp_path / "compatibility-evidence.json"
    store = CompatibilityEvidenceStore(str(path))
    store.record_registration()

    class Source:
        platform = "feishu"
        chat_id = "oc_PRIVATE_CHAT"
        thread_id = ""
        message_id = "m1"

    store.record_gateway_event(Source())
    data = store.snapshot()
    assert data["gateway_hook_registered"] is True
    assert data["gateway_hook_observed"] is True
    assert data["session_source_supported"] is True
    assert data["platforms"] == ["feishu"]

    raw = path.read_text(encoding="utf-8")
    assert "oc_PRIVATE_CHAT" not in raw
    assert "m1" not in raw
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_discovery_reports_supported_observed_runtime(tmp_path):
    config = cfg(tmp_path)
    evidence = CompatibilityEvidenceStore(config.compatibility_evidence_path)
    evidence.record_registration()

    class Source:
        platform = "feishu"
        chat_id = "oc_PRIVATE_CHAT"

    evidence.record_gateway_event(Source())
    discovery = CompatibilityDiscovery(
        config,
        command_runner=good_runner,
        api_health_probe=lambda: {"ok": True},
    )
    report = discovery.discover()
    assert report.supported is True
    assert report.hermes_version == "0.20.0"
    assert report.plugin_api_version == "register_hook"
    assert report.gateway_hook_supported is True
    assert report.session_source_supported is True
    assert report.api_server_supported is True
    assert report.send_supported is True
    assert report.platforms == ("feishu",)
    assert report.blocking_issues == ()

    published = Path(config.compatibility_path)
    assert published.exists()
    assert stat.S_IMODE(published.stat().st_mode) == 0o600
    text = published.read_text(encoding="utf-8")
    assert "oc_PRIVATE_CHAT" not in text


def test_missing_plugin_registration_is_explicit_blocker(tmp_path):
    report = CompatibilityDiscovery(
        cfg(tmp_path),
        command_runner=good_runner,
        api_health_probe=lambda: True,
    ).discover()
    assert report.supported is False
    assert "gateway_hook_registration_unobserved" in report.blocking_issues


def test_cli_missing_is_explicit_blocker(tmp_path):
    config = cfg(tmp_path)
    CompatibilityEvidenceStore(config.compatibility_evidence_path).record_registration()

    def missing(args, **kwargs):
        raise FileNotFoundError("hermes")

    report = CompatibilityDiscovery(
        config,
        command_runner=missing,
        api_health_probe=lambda: True,
    ).discover()
    assert report.supported is False
    assert "hermes_cli_unavailable" in report.blocking_issues
    assert report.hermes_version == "unknown"


def test_api_unavailable_degrades_capability_but_not_core_plugin_support(tmp_path):
    config = cfg(tmp_path)
    CompatibilityEvidenceStore(config.compatibility_evidence_path).record_registration()

    def api_down():
        raise RuntimeError("down")

    report = CompatibilityDiscovery(
        config,
        command_runner=good_runner,
        api_health_probe=api_down,
    ).discover()
    assert report.supported is True
    assert report.api_server_supported is False
    assert "hermes_api_unavailable" in report.warnings


def test_contact_delivery_enabled_without_send_is_blocking(tmp_path):
    config = cfg(tmp_path, delivery_enabled=True)
    CompatibilityEvidenceStore(config.compatibility_evidence_path).record_registration()

    def runner(args, **kwargs):
        if args[-1] == "--version":
            return Result(stdout="Hermes 0.20.0")
        return Result(returncode=2)

    report = CompatibilityDiscovery(
        config,
        command_runner=runner,
        api_health_probe=lambda: True,
    ).discover()
    assert report.supported is False
    assert "contact_delivery_enabled_but_send_unavailable" in report.blocking_issues


def test_existing_private_route_proves_session_source_without_leaking_route(tmp_path):
    config = cfg(tmp_path)
    CompatibilityEvidenceStore(config.compatibility_evidence_path).record_registration()
    RouteStore(config.route_path).save(HermesRoute("feishu", "oc_PRIVATE_ROUTE"))

    report = CompatibilityDiscovery(
        config,
        command_runner=good_runner,
        api_health_probe=lambda: True,
    ).discover()
    assert report.session_source_supported is True
    assert "feishu" in report.platforms
    published = Path(config.compatibility_path).read_text(encoding="utf-8")
    assert "oc_PRIVATE_ROUTE" not in published


def test_unparseable_version_is_warning_not_guess(tmp_path):
    config = cfg(tmp_path)
    CompatibilityEvidenceStore(config.compatibility_evidence_path).record_registration()

    def runner(args, **kwargs):
        if args[-1] == "--version":
            return Result(stdout="Hermes development build")
        return Result(stdout="send help")

    report = CompatibilityDiscovery(
        config,
        command_runner=runner,
        api_health_probe=lambda: True,
    ).discover()
    assert report.hermes_version == "unknown"
    assert "hermes_version_unparseable" in report.warnings


def test_plugin_register_writes_registration_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    class Ctx:
        def __init__(self):
            self.hooks = {}
        def register_hook(self, name, callback):
            self.hooks[name] = callback

    ctx = Ctx()
    plugin.register(ctx)
    evidence_path = tmp_path / "state" / "hermes-life-bridge" / "compatibility-evidence.json"
    data = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert data["plugin_registered"] is True
    assert data["gateway_hook_registered"] is True
    assert set(ctx.hooks) == {"pre_gateway_dispatch", "pre_llm_call"}


def test_gateway_hook_observation_does_not_break_normal_allow(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    class DummyBridge:
        def gateway_message(self, *args, **kwargs):
            raise RuntimeError("runtime down")

    class Source:
        platform = "telegram"
        chat_id = "PRIVATE_CHAT"

    class Event:
        source = Source()
        message_id = "m1"
        chat_id = "PRIVATE_CHAT"

    monkeypatch.setattr(plugin, "_BRIDGE", DummyBridge())
    assert plugin.on_pre_gateway_dispatch(Event()) == {"action": "allow"}
    evidence_path = tmp_path / "state" / "hermes-life-bridge" / "compatibility-evidence.json"
    data = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert data["gateway_hook_observed"] is True
    assert data["session_source_supported"] is True
    assert "PRIVATE_CHAT" not in evidence_path.read_text(encoding="utf-8")
