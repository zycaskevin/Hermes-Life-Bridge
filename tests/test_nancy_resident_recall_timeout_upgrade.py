"""Existing Nancy tool timeout upgrade: exact source, no-grant, rollback."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from scripts import activate_nancy_resident_recall as ops
from scripts import upgrade_nancy_resident_recall_timeout as upgrade


def _scenario(tmp_path, monkeypatch):
    plugin = tmp_path / "plugin"
    env = tmp_path / "hlb.env"
    backups = tmp_path / "backups"
    files: dict[str, bytes] = {}
    for part in (*upgrade.CHANGED_PARTS, upgrade.UNCHANGED_PART):
        target = plugin / part
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        previous = ("accepted-live-version:" + part).encode()
        target.write_bytes(previous)
        target.chmod(0o600)
        files[part] = previous
    env_bytes = (
        b"LIVE_RUNTIME_LIFE_DID=did:arthurverse:nancy\n"
        b"HLB_AGENT_TOOLS_ENABLED=false\n"
        b"HLB_RESIDENT_RECALL_TOOL_ENABLED=true\n"
        b"HLB_RESIDENT_RECALL_TIMEOUT_SECONDS=15\n"
    )
    env.write_bytes(env_bytes)
    env.chmod(0o600)
    monkeypatch.setattr(ops, "PLUGIN", plugin)
    monkeypatch.setattr(ops, "ENV", env)
    monkeypatch.setattr(ops, "BACKUPS", backups)
    monkeypatch.setattr(upgrade, "expected_previous", lambda part: files[part])

    def fake_run(command, **kwargs):
        if command[:3] == ["git", "-C", str(plugin)]:
            return subprocess.CompletedProcess(command, 0, ops.BASE_HEAD, "")
        if command[:3] == ["systemctl", "--user", "is-active"]:
            return subprocess.CompletedProcess(command, 0, "active\n", "")
        raise AssertionError("unexpected external command")
    monkeypatch.setattr(ops, "run", fake_run)
    monkeypatch.setattr(ops, "doctor", lambda: "HEALTHY")
    monkeypatch.setattr(ops, "verify_tool", lambda: None)
    return plugin, env, backups, files, env_bytes


def test_manual_budget_upgrade_preflight_never_modifies_gateway(tmp_path, monkeypatch, capsys):
    plugin, env, backups, files, old_env = _scenario(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["upgrade_nancy_resident_recall_timeout.py", "--check"])
    monkeypatch.setattr(ops, "restart_gateway", lambda: pytest.fail("no restart allowed"))
    upgrade.main()
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "NANCY_RECALL_TIMEOUT_UPGRADE_READY"
    assert result["manualTimeoutSeconds"] == 30
    assert result["researchEnabled"] is False
    assert env.read_bytes() == old_env
    assert all((plugin / path).read_bytes() == old for path, old in files.items())
    assert not list(backups.glob("timeout-upgrade-*/manifest.json"))


def test_upgrade_preserves_other_capabilities_and_changes_only_two_files(tmp_path, monkeypatch, capsys):
    plugin, env, backups, files, old_env = _scenario(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["upgrade_nancy_resident_recall_timeout.py", "--apply"])
    calls = []
    monkeypatch.setattr(ops, "restart_gateway", lambda: calls.append("restart"))
    monkeypatch.setattr(ops, "wait_for_doctor", lambda: calls.append("doctor"))
    monkeypatch.setattr(upgrade, "_verify_new_configuration", lambda: calls.append("verified"))
    upgrade.main()
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "NANCY_RECALL_TIMEOUT_UPGRADE_PASS"
    assert result["researchEnabled"] is False
    assert calls == ["restart", "verified", "doctor"]
    assert (plugin / upgrade.UNCHANGED_PART).read_bytes() == files[upgrade.UNCHANGED_PART]
    for part in upgrade.CHANGED_PARTS:
        assert (plugin / part).read_bytes() == (ops.ROOT / part).read_bytes()
    updated = env.read_bytes()
    assert updated.count(b"HLB_RESIDENT_RECALL_TIMEOUT_SECONDS=30") == 1
    assert b"HLB_AGENT_TOOLS_ENABLED=false" in updated
    assert b"HLB_RESIDENT_RECALL_TOOL_ENABLED=true" in updated
    assert len(list(backups.glob("timeout-upgrade-*/hlb-env.bak"))) == 1


def test_upgrade_failure_restores_source_and_live_config(tmp_path, monkeypatch):
    plugin, env, backups, files, old_env = _scenario(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["upgrade_nancy_resident_recall_timeout.py", "--apply"])
    calls = []
    monkeypatch.setattr(ops, "restart_gateway", lambda: calls.append("restart"))
    monkeypatch.setattr(ops, "wait_for_doctor", lambda: calls.append("doctor"))
    monkeypatch.setattr(upgrade, "_verify_new_configuration", lambda: (_ for _ in ()).throw(RuntimeError("verification_failed")))
    with pytest.raises(RuntimeError, match="verification_failed"):
        upgrade.main()
    assert calls == ["restart", "restart", "doctor"]
    assert env.read_bytes() == old_env
    assert all((plugin / part).read_bytes() == old for part, old in files.items())
    assert len(list(backups.glob("timeout-upgrade-*/hlb-env.bak"))) == 1


def test_preflight_rejects_cross_life_and_any_new_native_grants(tmp_path, monkeypatch):
    _, env, _, _, old_env = _scenario(tmp_path, monkeypatch)
    for invalid in (
        old_env.replace(b"did:arthurverse:nancy", b"did:arthurverse:luna"),
        old_env.replace(b"HLB_AGENT_TOOLS_ENABLED=false", b"HLB_AGENT_TOOLS_ENABLED=true"),
        old_env.replace(b"HLB_RESIDENT_RECALL_TIMEOUT_SECONDS=15", b"HLB_RESIDENT_RECALL_TIMEOUT_SECONDS=30"),
    ):
        env.write_bytes(invalid)
        with pytest.raises(RuntimeError):
            upgrade.preflight()
    env.write_bytes(old_env)
    assert upgrade.preflight()[1] == old_env
