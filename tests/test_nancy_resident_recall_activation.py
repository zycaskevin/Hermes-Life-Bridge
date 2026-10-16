"""Scoped Nancy Feishu manual-recall rollout: preflight, rollback and isolation."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from scripts import activate_nancy_resident_recall as activation


def _deployment(tmp_path, monkeypatch):
    live = tmp_path / "plugin"
    config = tmp_path / "hlb.env"
    backups = tmp_path / "backups"
    originals = {}
    for rel in activation.PARTS:
        target = live / rel
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        original = ("old-version:" + rel).encode()
        target.write_bytes(original)
        target.chmod(0o600)
        originals[rel] = original
    old_env = b"LIVE_RUNTIME_LIFE_DID=did:arthurverse:nancy\nHLB_AGENT_TOOLS_ENABLED=false\n"
    config.write_bytes(old_env)
    config.chmod(0o600)
    monkeypatch.setattr(activation, "PLUGIN", live)
    monkeypatch.setattr(activation, "ENV", config)
    monkeypatch.setattr(activation, "BACKUPS", backups)
    monkeypatch.setattr(
        activation, "baseline",
        lambda: (originals, old_env, b"HLB_RESIDENT_RECALL_TOOL_ENABLED=true\n"),
    )
    monkeypatch.setattr(activation, "doctor", lambda: "HEALTHY")
    return live, config, backups, originals, old_env


def test_preflight_does_not_change_live_plugin_or_configuration(tmp_path, monkeypatch, capsys):
    live, env, backups, originals, old_env = _deployment(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["activate_nancy_resident_recall.py", "--check"])
    monkeypatch.setattr(
        activation, "restart_gateway",
        lambda: pytest.fail("no restart during preflight"),
    )
    activation.main()
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "READY_TO_APPLY"
    assert report["nativeResearchActivated"] is False
    assert env.read_bytes() == old_env
    assert all((live / part).read_bytes() == data for part, data in originals.items())
    assert not any(backups.glob("*/hlb-env.bak"))


def test_failed_registration_rolls_back_all_files_and_restarts(tmp_path, monkeypatch):
    live, env, backups, originals, old_env = _deployment(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["activate_nancy_resident_recall.py", "--apply"])
    restarts = []
    monkeypatch.setattr(activation, "restart_gateway", lambda: restarts.append("restart"))

    def reject_registration():
        raise RuntimeError("tool_registry_not_verified")

    monkeypatch.setattr(activation, "verify_tool", reject_registration)
    with pytest.raises(RuntimeError, match="tool_registry_not_verified"):
        activation.main()
    assert restarts == ["restart", "restart"]
    assert env.read_bytes() == old_env
    assert all((live / part).read_bytes() == data for part, data in originals.items())
    assert all((live / part).stat().st_mode & 0o777 == 0o600 for part in originals)
    assert len(list(backups.glob("*/hlb-env.bak"))) == 1


def test_apply_writes_only_the_three_reviewed_modules_and_flag(tmp_path, monkeypatch, capsys):
    live, env, backups, originals, old_env = _deployment(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["activate_nancy_resident_recall.py", "--apply"])
    restarts = []
    monkeypatch.setattr(activation, "restart_gateway", lambda: restarts.append("restart"))
    monkeypatch.setattr(activation, "verify_tool", lambda: None)
    activation.main()
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "NANCY_RESIDENT_FEISHU_RECALL_ACTIVATED"
    assert result["nativeResearchActivated"] is False
    assert restarts == ["restart"]
    assert b"HLB_AGENT_TOOLS_ENABLED=false" in env.read_bytes()
    assert env.read_bytes().count(b"HLB_RESIDENT_RECALL_TOOL_ENABLED=true") == 1
    assert all((live / part).read_bytes() == (activation.ROOT / part).read_bytes()
               for part in originals)
    assert len(list(backups.glob("*/manifest.json"))) == 1


def test_gateway_doctor_startup_race_is_bounded(monkeypatch):
    outcomes = iter([False, False, True])
    sleeps = []

    def initially_unavailable():
        if not next(outcomes):
            raise RuntimeError('doctor_not_healthy')

    monkeypatch.setattr(activation, 'doctor', initially_unavailable)
    monkeypatch.setattr(activation.time, 'sleep', lambda seconds: sleeps.append(seconds))
    activation.wait_for_doctor(attempts=3)
    assert sleeps == [1, 1]
    monkeypatch.setattr(activation, 'doctor', lambda: (_ for _ in ()).throw(RuntimeError('still_blocked')))
    with pytest.raises(RuntimeError, match='still_blocked'):
        activation.wait_for_doctor(attempts=2)
    assert sleeps == [1, 1, 1]


def test_concurrent_config_mutation_denies_activation(tmp_path, monkeypatch):
    live, env, backups, originals, old_env = _deployment(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["activate_nancy_resident_recall.py", "--apply"])
    env.write_bytes(old_env + b"OTHER_WRITER=true\n")
    monkeypatch.setattr(activation, "restart_gateway", lambda: pytest.fail("no restart"))
    with pytest.raises(RuntimeError, match="concurrent_config_change"):
        activation.main()
    assert env.read_bytes().endswith(b"OTHER_WRITER=true\n")
    assert all((live / part).read_bytes() == data for part, data in originals.items())
