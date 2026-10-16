#!/usr/bin/env python3
"""Reversible timeout/diagnostics upgrade for the already-enabled Nancy Feishu recall.

Does not mint a grant, call a model, enable research/other Runtime, or modify DLMF.
Requires the exact prior live source and config; --check changes nothing.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

try:
    from scripts import activate_nancy_resident_recall as ops
except ModuleNotFoundError:
    import activate_nancy_resident_recall as ops

EXPECTED_PREVIOUS_COMMIT = "3a06b64367674e535f58da783d974a9c17ece8f7"
CHANGED_PARTS = (
    "src/hermes_life_bridge/config.py",
    "src/hermes_life_bridge/agent_tools.py",
)
UNCHANGED_PART = "src/hermes_life_bridge/plugin.py"
TIMEOUT_SETTING = "HLB_RESIDENT_RECALL_TIMEOUT_SECONDS"
NEW_TIMEOUT = 30


def expected_previous(part: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(ops.ROOT), "show", EXPECTED_PREVIOUS_COMMIT + ":" + part],
        check=True, capture_output=True, timeout=8,
    ).stdout


def _env_value(lines: list[str], key: str) -> str | None:
    values = [line.split("=", 1)[1].strip().strip("'\"")
              for line in lines if line.startswith(key + "=")]
    if len(values) > 1:
        raise RuntimeError("duplicate_hlb_configuration:" + key)
    return values[0] if values else None


def preflight() -> tuple[dict[str, bytes], bytes, bytes]:
    if not ops.PLUGIN.is_dir() or not ops.ROOT.is_dir():
        raise RuntimeError("plugin_or_source_missing")
    head = ops.run(["git", "-C", str(ops.PLUGIN), "rev-parse", "HEAD"]).stdout.strip()
    if head != ops.BASE_HEAD:
        raise RuntimeError("installed_hlb_git_head_drift")
    current: dict[str, bytes] = {}
    for part in (*CHANGED_PARTS, UNCHANGED_PART):
        live = ops.checked_file(ops.PLUGIN / part)
        if live != expected_previous(part):
            raise RuntimeError("installed_recall_baseline_mismatch:" + part)
        current[part] = live
    env_before = ops.checked_file(ops.ENV)
    lines = env_before.decode("utf-8").splitlines()
    if _env_value(lines, "HLB_RESIDENT_RECALL_TOOL_ENABLED") != "true":
        raise RuntimeError("resident_recall_not_previously_enabled")
    if _env_value(lines, "HLB_AGENT_TOOLS_ENABLED") not in (None, "false"):
        raise RuntimeError("native_agent_tool_authority_changed")
    did = _env_value(lines, "LIFE_RUNTIME_LIFE_DID") or _env_value(lines, "LIVE_RUNTIME_LIFE_DID")
    if did != "did:arthurverse:nancy":
        raise RuntimeError("existing_nancy_identity_mismatch")
    if _env_value(lines, TIMEOUT_SETTING) != "15":
        raise RuntimeError("previous_manual_recall_budget_not_15")
    for unit in ("hermes-gateway.service", "hermes-life-cognition.service",
                 "hermes-life-contact.service", "hermes-life-percept-recovery.service"):
        if ops.run(["systemctl", "--user", "is-active", unit]).stdout.strip() != "active":
            raise RuntimeError("preexisting_hlb_service_unhealthy:" + unit)
    ops.verify_tool()
    ops.doctor()
    replacement = (TIMEOUT_SETTING + "=" + str(NEW_TIMEOUT)).encode()
    updated = b"\n".join(
        replacement if line.startswith((TIMEOUT_SETTING + "=").encode()) else line
        for line in env_before.split(b"\n")
    )
    if updated == env_before or updated.count(replacement) != 1:
        raise RuntimeError("timeout_configuration_upgrade_invalid")
    return current, env_before, updated


def _verify_new_configuration() -> None:
    code = """
from hermes_life_bridge.config import BridgeConfig
from hermes_life_bridge.agent_tools import resident_recall_enabled
c=BridgeConfig.from_env()
assert c.life_did == "did:arthurverse:nancy"
assert c.resident_recall_tool_enabled and not c.agent_tools_enabled
assert c.resident_recall_timeout_seconds == 30.0
assert resident_recall_enabled()
print("NANCY_RECALL_BUDGET_UPGRADE_VERIFIED")
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ops.PLUGIN / "src")
    result = ops.run([str(ops.RUNTIME_PY), "-c", code], timeout=16, env=env)
    if "NANCY_RECALL_BUDGET_UPGRADE_VERIFIED" not in result.stdout:
        raise RuntimeError("resident_recall_budget_readback_invalid")


def main() -> None:
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true")
    action.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    ops.BACKUPS.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (ops.BACKUPS.is_symlink() or ops.BACKUPS.stat().st_uid != os.geteuid()
            or ops.BACKUPS.stat().st_mode & 0o077):
        raise RuntimeError("backup_root_untrusted")
    lock_path = ops.BACKUPS / ".apply.lock"
    with lock_path.open("a+b") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        old, previous_env, new_env = preflight()
        changed = {part: (ops.ROOT / part).read_bytes() for part in CHANGED_PARTS}
        if any(changed[p] == old[p] for p in CHANGED_PARTS):
            raise RuntimeError("reviewed_upgrade_delta_missing")
        if not args.apply:
            print(json.dumps({
                "status": "NANCY_RECALL_TIMEOUT_UPGRADE_READY",
                "manualTimeoutSeconds": NEW_TIMEOUT,
                "changedPaths": list(CHANGED_PARTS),
                "researchEnabled": False,
                "configurationChanged": False,
            }))
            return

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        folder = ops.BACKUPS / ("timeout-upgrade-" + stamp)
        folder.mkdir(mode=0o700)
        for idx, part in enumerate(CHANGED_PARTS):
            ops.atomic_private(folder / f"source-{idx}.bak", old[part])
        ops.atomic_private(folder / "hlb-env.bak", previous_env)
        ops.atomic_private(folder / "manifest.json", json.dumps({
            "installedBaselineCommit": EXPECTED_PREVIOUS_COMMIT,
            "originalSha256": {part: ops.sha(old[part]) for part in CHANGED_PARTS},
            "envSha256": ops.sha(previous_env),
        }, sort_keys=True).encode())
        touched = False
        try:
            for part in (*CHANGED_PARTS, UNCHANGED_PART):
                if ops.checked_file(ops.PLUGIN / part) != old[part]:
                    raise RuntimeError("concurrent_plugin_writer_detected")
            if ops.checked_file(ops.ENV) != previous_env:
                raise RuntimeError("concurrent_configuration_writer_detected")
            touched = True
            for part in CHANGED_PARTS:
                ops.atomic_private(ops.PLUGIN / part, changed[part])
            ops.atomic_private(ops.ENV, new_env)
            ops.restart_gateway()
            _verify_new_configuration()
            ops.wait_for_doctor()
            print(json.dumps({
                "status": "NANCY_RECALL_TIMEOUT_UPGRADE_PASS",
                "lifeDid": "did:arthurverse:nancy",
                "manualTimeoutSeconds": NEW_TIMEOUT,
                "tool": "digital_life_recall",
                "researchEnabled": False,
                "doctor": "HEALTHY",
                "rollbackBackup": str(folder),
            }))
        except Exception:
            if touched:
                for part in CHANGED_PARTS:
                    ops.atomic_private(ops.PLUGIN / part, old[part])
                ops.atomic_private(ops.ENV, previous_env)
                ops.restart_gateway()
                ops.wait_for_doctor()
                print("NANCY_RECALL_TIMEOUT_UPGRADE_ROLLBACK=PASS", file=sys.stderr)
            raise


if __name__ == "__main__":
    main()
