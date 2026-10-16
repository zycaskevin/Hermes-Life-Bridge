"""Nancy Feishu runtime-status rollout must preserve existing plugins and authority."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from scripts import activate_nancy_resident_recall as ops
from scripts import activate_nancy_resident_runtime_status as rollout


def scenario(tmp_path, monkeypatch):
    plugin = tmp_path / "live-hlb"
    backup = tmp_path / "backup"
    env = tmp_path / "hlb.env"
    baseline = {}
    for name in (*rollout.CHANGED, rollout.UNCHANGED):
        path = plugin / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = ("accepted:" + name).encode()
        path.write_bytes(data)
        path.chmod(0o600)
        baseline[name] = data
    original_env = (
        b"LIVE_RUNTIME_LIFE_DID=did:arthurverse:nancy\n"
        b"HLB_AGENT_TOOLS_ENABLED=false\n"
        b"HLB_RESIDENT_RECALL_TOOL_ENABLED=true\n"
        b"HLB_RESIDENT_RECALL_TIMEOUT_SECONDS=30\n"
        b"HLB_WORK_PRODUCER_ENABLED=true\n"
    )
    env.write_bytes(original_env)
    env.chmod(0o600)
    monkeypatch.setattr(ops, "PLUGIN", plugin)
    monkeypatch.setattr(ops, "BACKUPS", backup)
    monkeypatch.setattr(ops, "ENV", env)
    monkeypatch.setattr(rollout, "baseline_bytes", lambda name: baseline[name])
    def run(argv, **kwargs):
        if argv[:3] == ["git", "-C", str(plugin)]:
            return subprocess.CompletedProcess(argv, 0, ops.BASE_HEAD + "\n", "")
        if argv[:3] == ["systemctl", "--user", "is-active"]:
            return subprocess.CompletedProcess(argv, 0, "active\n", "")
        raise AssertionError("unexpected command")
    monkeypatch.setattr(ops, "run", run)
    monkeypatch.setattr(ops, "doctor", lambda: "HEALTHY")
    return plugin, backup, env, baseline, original_env


def test_runtime_status_preflight_is_unchanged_and_no_restart(tmp_path, monkeypatch, capsys):
    plugin, backup, env, old, raw = scenario(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["activate_nancy_resident_runtime_status.py", "--check"])
    monkeypatch.setattr(ops, "restart_gateway", lambda: pytest.fail("preflight restarted Gateway"))
    rollout.main()
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "NANCY_EXISTING_FEISHU_RUNTIME_STATUS_READY"
    assert data["nativeResearchEnabled"] is False and data["stateWritten"] is False
    assert env.read_bytes() == raw
    assert all((plugin / part).read_bytes() == body for part, body in old.items())
    assert not (plugin / rollout.NEW).exists()
    assert not list(backup.glob("runtime-status-*/env.before.bak"))


def test_runtime_status_activation_adds_one_scoped_tool_only(tmp_path, monkeypatch, capsys):
    plugin, backup, env, old, original = scenario(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["activate_nancy_resident_runtime_status.py", "--apply"])
    observed = []
    monkeypatch.setattr(ops, "restart_gateway", lambda: observed.append("restart"))
    monkeypatch.setattr(ops, "wait_for_doctor", lambda: observed.append("doctor"))
    monkeypatch.setattr(rollout, "verify_registered", lambda: observed.append("tool-verified"))
    rollout.main()
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "NANCY_EXISTING_FEISHU_RUNTIME_STATUS_MODULE_ACTIVE"
    assert result["pluginRegistrationProbePassed"] is True
    assert result["feishuProviderTurnVerified"] is False
    assert result["productionRuntimeSwitchReady"] is False
    assert result["registeredTool"] == "digital_life_runtime_status"
    assert result["nativeResearchEnabled"] is False and result["runtimeInvocationAttempted"] is False
    assert observed == ["restart", "doctor", "tool-verified"]
    assert (plugin / rollout.NEW).read_bytes() == (ops.ROOT / rollout.NEW).read_bytes()
    for part in rollout.CHANGED:
        assert (plugin / part).read_bytes() == (ops.ROOT / part).read_bytes()
    assert (plugin / rollout.UNCHANGED).read_bytes() == old[rollout.UNCHANGED]
    updated = env.read_bytes()
    assert updated.startswith(original)
    assert updated.count(b"HLB_RESIDENT_RUNTIME_STATUS_ENABLED=true") == 1
    assert b"HLB_AGENT_TOOLS_ENABLED=false" in updated
    assert len(list(backup.glob("runtime-status-*/env.before.bak"))) == 1


def test_failed_live_acceptance_restores_all_previous_sources(tmp_path, monkeypatch):
    plugin, backup, env, old, original = scenario(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["activate_nancy_resident_runtime_status.py", "--apply"])
    observed = []
    monkeypatch.setattr(ops, "restart_gateway", lambda: observed.append("restart"))
    monkeypatch.setattr(ops, "wait_for_doctor", lambda: observed.append("doctor"))
    monkeypatch.setattr(rollout, "verify_registered",
                        lambda: (_ for _ in ()).throw(RuntimeError("real_session_verification_failed")))
    with pytest.raises(RuntimeError, match="real_session_verification_failed"):
        rollout.main()
    assert observed == ["restart", "doctor", "restart", "doctor"]
    assert env.read_bytes() == original
    assert not (plugin / rollout.NEW).exists()
    assert all((plugin / part).read_bytes() == content for part, content in old.items())
    assert len(list(backup.glob("runtime-status-*/env.before.bak"))) == 1


def test_config_drift_or_other_life_denies_deployment(tmp_path, monkeypatch):
    plugin, _, env, old, original = scenario(tmp_path, monkeypatch)
    for invalid in (
        original.replace(b"did:arthurverse:nancy", b"did:arthurverse:luna"),
        original.replace(b"HLB_AGENT_TOOLS_ENABLED=false", b"HLB_AGENT_TOOLS_ENABLED=true"),
        original.replace(b"HLB_RESIDENT_RECALL_TOOL_ENABLED=true", b"HLB_RESIDENT_RECALL_TOOL_ENABLED=false"),
        original + b"HLB_RESIDENT_RUNTIME_STATUS_ENABLED=true\n",
    ):
        env.write_bytes(invalid)
        with pytest.raises(RuntimeError):
            rollout.preflight()
    env.write_bytes(original)
    (plugin / rollout.UNCHANGED).write_bytes(b"unreviewed-other-writer")
    with pytest.raises(RuntimeError, match="previous_approved_plugin_changed"):
        rollout.preflight()
    assert not (plugin / rollout.NEW).exists()
