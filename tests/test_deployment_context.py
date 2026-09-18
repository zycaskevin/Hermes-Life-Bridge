from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from hermes_life_bridge.deployment_context import deployment_context, SCHEMA
from hermes_life_bridge import plugin
from hermes_life_bridge.config import BridgeConfig

DID = "did:test:subject-a"


def manifest(path: Path, *, did: str = DID, **overrides) -> Path:
    value = {
        "schema": SCHEMA,
        "digitalLifeId": "dl_subject_a",
        "dlmfScope": {"tenantId": "tenant-a", "lifeDid": did, "memoryNamespace": "life"},
        "hermes": {"home": str(path.parent / "hermes"), "toolPolicy": "conversation-only"},
        "dlmf": {"developmentExperienceJournal": str(path.parent / "dlmf/development-experiences.jsonl")},
        **overrides,
    }
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    value["manifestHash"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


def test_configured_tool_and_memory_boundaries_are_distinct(tmp_path):
    path = manifest(tmp_path / "runtime-instance.json")
    before = path.read_bytes()
    text = deployment_context(life_did=DID, manifest_file=str(path))
    assert "CONVERSATION_ONLY" in text
    assert "User permission alone cannot create a missing tool" in text
    assert "Memory authority: DLMF" in text
    assert "does NOT retrieve memory content" in text
    assert "no external memory exists" in text
    assert "configured to retain isolated Hermes sessions" in text
    assert "DSML/XML/bash tags" in text
    assert str(tmp_path) not in text
    assert DID not in text
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["runtime-instance.json"]


def test_cross_life_manifest_rejected_without_leak(tmp_path):
    path = manifest(tmp_path / "runtime-instance.json", did="did:test:other")
    text = deployment_context(life_did=DID, manifest_file=str(path))
    assert "unavailable or invalid" in text
    assert "did:test:other" not in text
    assert "CONVERSATION_ONLY" not in text


@pytest.mark.parametrize("mode", [0o644, 0o640])
def test_non_private_manifest_rejected(tmp_path, mode):
    path = manifest(tmp_path / "runtime-instance.json")
    path.chmod(mode)
    assert "unavailable or invalid" in deployment_context(life_did=DID, manifest_file=str(path))


def test_symlink_rejected(tmp_path):
    path = manifest(tmp_path / "runtime-instance.json")
    link = tmp_path / "link.json"
    link.symlink_to(path)
    assert "unavailable or invalid" in deployment_context(life_did=DID, manifest_file=str(link))


def test_checksum_tampering_rejected(tmp_path):
    path = manifest(tmp_path / "runtime-instance.json")
    data = json.loads(path.read_text())
    data["digitalLifeId"] = "changed"
    path.write_text(json.dumps(data))
    assert "unavailable or invalid" in deployment_context(life_did=DID, manifest_file=str(path))


def test_foreign_store_path_rejected_even_with_valid_checksum(tmp_path):
    path = manifest(tmp_path / "runtime-instance.json", hermes={
        "home": str(tmp_path / "other/hermes"), "toolPolicy": "conversation-only"
    })
    assert "unavailable or invalid" in deployment_context(life_did=DID, manifest_file=str(path))


def test_untrusted_manifest_text_is_never_rendered(tmp_path):
    path = manifest(tmp_path / "runtime-instance.json", displayName="IGNORE_ALL_RULES secret-marker")
    text = deployment_context(life_did=DID, manifest_file=str(path))
    assert "CONVERSATION_ONLY" in text
    assert "IGNORE_ALL_RULES" not in text
    assert "secret-marker" not in text


@pytest.mark.parametrize("raw", ["{bad", "[]", "x" * 65537])
def test_bad_or_oversized_manifest_is_safe(tmp_path, raw):
    path = tmp_path / "runtime-instance.json"
    path.write_text(raw)
    path.chmod(0o600)
    assert "unavailable or invalid" in deployment_context(life_did=DID, manifest_file=str(path))


def test_absent_or_non_conversation_policy_does_not_invent_tools(tmp_path):
    path = manifest(tmp_path / "runtime-instance.json", hermes={
        "home": str(tmp_path / "hermes"), "toolPolicy": "governed-tools"
    })
    assert "unavailable or invalid" in deployment_context(life_did=DID, manifest_file=str(path))
    assert "unavailable or invalid" in deployment_context(life_did=DID, manifest_file="")


def test_gateway_injects_deployment_facts_without_observing_a_new_experience(tmp_path, monkeypatch):
    path = manifest(tmp_path / "runtime-instance.json")
    config = BridgeConfig(life_did=DID, runtime_socket="unused", trace_path="unused",
        deployment_context_enabled=True, deployment_manifest_file=str(path))
    monkeypatch.setattr(BridgeConfig, "from_env", staticmethod(lambda: config))
    monkeypatch.setattr(plugin, "_development_context", lambda: "dld")
    monkeypatch.setattr(plugin, "_affect_context", lambda: "affect")
    monkeypatch.setattr(plugin, "_bridge", lambda: pytest.fail("must not send synthetic experience"))
    response = plugin.on_pre_llm_call(session_id="probe", user_message="test", platform="telegram")
    assert response["context"].startswith("dld\n\naffect\n\n")
    assert "CONVERSATION_ONLY" in response["context"]


def test_deployment_context_is_opt_in(monkeypatch):
    config = BridgeConfig(life_did=DID, runtime_socket="unused", trace_path="unused")
    monkeypatch.setattr(BridgeConfig, "from_env", staticmethod(lambda: config))
    assert plugin._deployment_context() == ""
