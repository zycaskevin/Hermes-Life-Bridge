"""Nancy Feishu read-only bounded retry upgrade: isolation and rollback."""
from __future__ import annotations

import json
import sys

import pytest

from scripts import activate_nancy_resident_recall as ops
from scripts import upgrade_nancy_resident_recall_retry as retry
from test_nancy_resident_recall_timeout_upgrade import _scenario


def _current(tmp_path, monkeypatch):
    plugin, env, backups, originals, old_env = _scenario(tmp_path, monkeypatch)
    env.write_bytes(old_env.replace(
        b"HLB_RESIDENT_RECALL_TIMEOUT_SECONDS=15",
        b"HLB_RESIDENT_RECALL_TIMEOUT_SECONDS=30",
    ))
    before = env.read_bytes()
    monkeypatch.setattr(retry, "previous", lambda part: originals[part])
    return plugin, env, backups, originals, before


def test_retry_upgrade_preflight_does_not_restart_or_mutate(tmp_path, monkeypatch, capsys):
    plugin, env, backups, originals, before = _current(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["upgrade_nancy_resident_recall_retry.py", "--check"])
    monkeypatch.setattr(ops, "restart_gateway", lambda: pytest.fail("preflight must not restart"))
    retry.main()
    value = json.loads(capsys.readouterr().out)
    assert value["status"] == "NANCY_BOUNDED_READ_RETRY_READY"
    assert value["maximumReadAttempts"] == 2 and value["researchEnabled"] is False
    assert env.read_bytes() == before
    assert all((plugin / part).read_bytes() == original for part, original in originals.items())
    assert not list(backups.glob("bounded-retry-*/manifest.json"))


def test_retry_upgrade_applies_only_agent_tools_and_preserves_nancy_authority(tmp_path, monkeypatch, capsys):
    plugin, env, backups, originals, before = _current(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["upgrade_nancy_resident_recall_retry.py", "--apply"])
    events = []
    monkeypatch.setattr(ops, "restart_gateway", lambda: events.append("restart"))
    monkeypatch.setattr(ops, "wait_for_doctor", lambda: events.append("doctor"))
    monkeypatch.setattr(retry, "_verify_retry_tool", lambda: events.append("verified"))
    retry.main()
    value = json.loads(capsys.readouterr().out)
    assert value["status"] == "NANCY_BOUNDED_READ_RETRY_UPGRADE_PASS"
    assert value["configUnchanged"] is True and value["researchEnabled"] is False
    assert events == ["restart", "verified", "doctor"]
    assert (plugin / retry.CHANGE_PART).read_bytes() == (ops.ROOT / retry.CHANGE_PART).read_bytes()
    assert all((plugin / part).read_bytes() == originals[part] for part in retry.UNCHANGED_PARTS)
    assert env.read_bytes() == before
    assert len(list(backups.glob("bounded-retry-*/agent-tools.before.bak"))) == 1


def test_retry_upgrade_failed_canary_restores_previous_plugin_and_config(tmp_path, monkeypatch):
    plugin, env, backups, originals, before = _current(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["upgrade_nancy_resident_recall_retry.py", "--apply"])
    events = []
    monkeypatch.setattr(ops, "restart_gateway", lambda: events.append("restart"))
    monkeypatch.setattr(ops, "wait_for_doctor", lambda: events.append("doctor"))
    monkeypatch.setattr(retry, "_verify_retry_tool",
                        lambda: (_ for _ in ()).throw(RuntimeError("receipt_guard_failed")))
    with pytest.raises(RuntimeError, match="receipt_guard_failed"):
        retry.main()
    assert events == ["restart", "restart", "doctor"]
    assert all((plugin / part).read_bytes() == original for part, original in originals.items())
    assert env.read_bytes() == before
    assert len(list(backups.glob("bounded-retry-*/manifest.json"))) == 1


def test_retry_upgrade_denies_modified_runtime_and_owner_policy(tmp_path, monkeypatch):
    plugin, env, backups, originals, before = _current(tmp_path, monkeypatch)
    for invalid in (
        before.replace(b"did:arthurverse:nancy", b"did:arthurverse:luna"),
        before.replace(b"HLB_AGENT_TOOLS_ENABLED=false", b"HLB_AGENT_TOOLS_ENABLED=true"),
        before.replace(b"HLB_RESIDENT_RECALL_TOOL_ENABLED=true", b"HLB_RESIDENT_RECALL_TOOL_ENABLED=false"),
        before.replace(b"HLB_RESIDENT_RECALL_TIMEOUT_SECONDS=30", b"HLB_RESIDENT_RECALL_TIMEOUT_SECONDS=35"),
    ):
        env.write_bytes(invalid)
        with pytest.raises(RuntimeError):
            retry.preflight()
    env.write_bytes(before)
    assert retry.preflight()[1] == before
