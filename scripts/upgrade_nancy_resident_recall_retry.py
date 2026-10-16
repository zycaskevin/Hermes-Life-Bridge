#!/usr/bin/env python3
"""Scoped one-time upgrade: bounded read-only DLMF retry for Nancy's Feishu tool.

Requires exact preceding deployed source and unchanged original Nancy/Hermes
authority. Does not modify identity, DLMF, budgets, AF, models or source memory.
"""
from __future__ import annotations

import argparse
import fcntl
import io
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from scripts import activate_nancy_resident_recall as ops
    from scripts.upgrade_nancy_resident_recall_timeout import _env_value
except ModuleNotFoundError:
    import activate_nancy_resident_recall as ops
    from upgrade_nancy_resident_recall_timeout import _env_value

PREVIOUS_ACCEPTED_COMMIT = "a8151114b07619b0042dfde7a2e3a4e471a5984b"
CHANGE_PART = "src/hermes_life_bridge/agent_tools.py"
UNCHANGED_PARTS = (
    "src/hermes_life_bridge/config.py",
    "src/hermes_life_bridge/plugin.py",
)


def previous(part: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(ops.ROOT), "show", PREVIOUS_ACCEPTED_COMMIT + ":" + part],
        check=True, capture_output=True, timeout=8,
    ).stdout


def preflight() -> tuple[bytes, bytes]:
    if not ops.PLUGIN.is_dir() or not ops.ROOT.is_dir():
        raise RuntimeError("hlb_source_missing")
    head = ops.run(["git", "-C", str(ops.PLUGIN), "rev-parse", "HEAD"]).stdout.strip()
    if head != ops.BASE_HEAD:
        raise RuntimeError("installed_hlb_head_changed")
    for part in (CHANGE_PART, *UNCHANGED_PARTS):
        if ops.checked_file(ops.PLUGIN / part) != previous(part):
            raise RuntimeError("live_hlb_source_not_accepted:" + part)
    env = ops.checked_file(ops.ENV)
    settings = env.decode("utf-8").splitlines()
    if (_env_value(settings, "HLB_RESIDENT_RECALL_TOOL_ENABLED") != "true"
            or _env_value(settings, "HLB_RESIDENT_RECALL_TIMEOUT_SECONDS") != "30"
            or _env_value(settings, "HLB_AGENT_TOOLS_ENABLED") not in (None, "false")):
        raise RuntimeError("nancy_recall_scope_or_budget_changed")
    did = _env_value(settings, "LIFE_RUNTIME_LIFE_DID") or _env_value(settings, "LIVE_RUNTIME_LIFE_DID")
    if did != "did:arthurverse:nancy":
        raise RuntimeError("nancy_resident_did_changed")
    for unit in ("hermes-gateway.service", "hermes-life-cognition.service",
                 "hermes-life-contact.service", "hermes-life-percept-recovery.service"):
        if ops.run(["systemctl", "--user", "is-active", unit]).stdout.strip() != "active":
            raise RuntimeError("gateway_unit_not_active:" + unit)
    ops.verify_tool()
    ops.doctor()
    return ops.checked_file(ops.PLUGIN / CHANGE_PART), env


def _verify_retry_tool() -> None:
    probe = """
from hermes_life_bridge.config import BridgeConfig
from hermes_life_bridge.agent_tools import resident_recall_enabled, _retryable_verified_retrieval_error
from urllib.error import HTTPError
import io
import time
c = BridgeConfig.from_env()
assert c.life_did == 'did:arthurverse:nancy'
assert c.resident_recall_tool_enabled and not c.agent_tools_enabled
assert c.resident_recall_timeout_seconds == 30
assert resident_recall_enabled()
e = HTTPError('http://127.0.0.1/non-sensitive',400,'rejected',None,io.BytesIO(b'{"error":"digital_life_stack_request_failed"}'))
assert _retryable_verified_retrieval_error(e, deadline=time.monotonic() + 2) is True
e = HTTPError('http://127.0.0.1/non-sensitive',403,'denied',None,io.BytesIO(b'{"error":"digital_life_stack_request_failed"}'))
assert _retryable_verified_retrieval_error(e, deadline=time.monotonic() + 2) is False
print('NANCY_BOUNDED_READ_RETRY_TOOL_VERIFIED')
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ops.PLUGIN / "src")
    result = ops.run([str(ops.RUNTIME_PY), "-c", probe], timeout=18, env=environment)
    if "NANCY_BOUNDED_READ_RETRY_TOOL_VERIFIED" not in result.stdout:
        raise RuntimeError("bounded_read_retry_not_registered")


def main() -> None:
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true")
    action.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    ops.BACKUPS.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (ops.BACKUPS.is_symlink() or ops.BACKUPS.stat().st_uid != os.geteuid()
            or ops.BACKUPS.stat().st_mode & 0o077):
        raise RuntimeError("private_backup_root_untrusted")
    lock_path = ops.BACKUPS / ".apply.lock"
    with lock_path.open("a+b") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        old_code, old_env = preflight()
        new_code = (ops.ROOT / CHANGE_PART).read_bytes()
        if new_code == old_code:
            raise RuntimeError("bounded_retry_candidate_missing")
        if not args.apply:
            print(json.dumps({
                "status": "NANCY_BOUNDED_READ_RETRY_READY",
                "tool": "digital_life_recall", "maximumReadAttempts": 2,
                "timeBudgetSeconds": 30, "researchEnabled": False,
                "configurationChanged": False,
            }))
            return

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = ops.BACKUPS / ("bounded-retry-" + stamp)
        backup.mkdir(mode=0o700)
        ops.atomic_private(backup / "agent-tools.before.bak", old_code)
        ops.atomic_private(backup / "hlb-env.before.bak", old_env)
        ops.atomic_private(backup / "manifest.json", json.dumps({
            "acceptedCodeCommit": PREVIOUS_ACCEPTED_COMMIT,
            "originalCodeSha256": ops.sha(old_code),
            "envSha256": ops.sha(old_env),
        }, sort_keys=True).encode())
        touched = False
        try:
            if ops.checked_file(ops.PLUGIN / CHANGE_PART) != old_code:
                raise RuntimeError("concurrent_code_update")
            if ops.checked_file(ops.ENV) != old_env:
                raise RuntimeError("concurrent_config_update")
            for part in UNCHANGED_PARTS:
                if ops.checked_file(ops.PLUGIN / part) != previous(part):
                    raise RuntimeError("concurrent_hlb_dependency_update")
            touched = True
            ops.atomic_private(ops.PLUGIN / CHANGE_PART, new_code)
            ops.restart_gateway()
            _verify_retry_tool()
            ops.wait_for_doctor()
            print(json.dumps({
                "status": "NANCY_BOUNDED_READ_RETRY_UPGRADE_PASS",
                "lifeDid": "did:arthurverse:nancy",
                "tool": "digital_life_recall",
                "maximumReadAttempts": 2,
                "timeBudgetSeconds": 30,
                "configUnchanged": ops.checked_file(ops.ENV) == old_env,
                "researchEnabled": False,
                "rollbackBackup": str(backup),
                "doctor": "HEALTHY",
            }))
        except Exception:
            if touched:
                ops.atomic_private(ops.PLUGIN / CHANGE_PART, old_code)
                ops.restart_gateway()
                ops.wait_for_doctor()
                print("NANCY_BOUNDED_READ_RETRY_ROLLBACK=PASS", file=sys.stderr)
            raise


if __name__ == "__main__":
    main()
