#!/usr/bin/env python3
"""Narrow, reversible, original-Feishu Nancy Runtime status activation.

Adds only read-only capability-health observation. Never grants research,
chooses/executess a Runtime, changes native-tool authority or writes canonical
memory. Exact accepted code/config are checked before any live mutation.
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

PREVIOUS_COMMIT = "d1b7dd02e90ff802142c8cb2b9e5df9f1b69848a"
CHANGED = ("src/hermes_life_bridge/config.py", "src/hermes_life_bridge/plugin.py")
NEW = "src/hermes_life_bridge/resident_runtime_status.py"
UNCHANGED = "src/hermes_life_bridge/agent_tools.py"
SETTING = "HLB_RESIDENT_RUNTIME_STATUS_ENABLED"


def baseline_bytes(part: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(ops.ROOT), "show", PREVIOUS_COMMIT + ":" + part],
        check=True, capture_output=True, timeout=8,
    ).stdout


def _env_value(lines: list[str], key: str) -> str | None:
    entries = [
        line.split("=", 1)[1].strip().strip('"\'')
        for line in lines if line.startswith(key + "=")
    ]
    if len(entries) > 1:
        raise RuntimeError("duplicate_config_setting")
    return entries[0] if entries else None


def preflight() -> tuple[dict[str, bytes], bytes, bytes]:
    if not ops.PLUGIN.is_dir() or not ops.ROOT.is_dir():
        raise RuntimeError("repo_or_installed_plugin_absent")
    if ops.run(["git", "-C", str(ops.PLUGIN), "rev-parse", "HEAD"]).stdout.strip() != ops.BASE_HEAD:
        raise RuntimeError("installed_plugin_baseline_drift")
    original: dict[str, bytes] = {}
    for name in (*CHANGED, UNCHANGED):
        raw = ops.checked_file(ops.PLUGIN / name)
        if raw != baseline_bytes(name):
            raise RuntimeError("previous_approved_plugin_changed:" + name)
        original[name] = raw
    if (ops.PLUGIN / NEW).exists() or (ops.PLUGIN / NEW).is_symlink():
        raise RuntimeError("runtime_status_already_present")
    if not (ops.ROOT / NEW).is_file():
        raise RuntimeError("runtime_status_candidate_missing")

    old_env = ops.checked_file(ops.ENV)
    lines = old_env.decode("utf-8").splitlines()
    if (_env_value(lines, SETTING) not in (None, "false", "0")
            or _env_value(lines, "HLB_AGENT_TOOLS_ENABLED") not in (None, "false", "0")
            or _env_value(lines, "HLB_RESIDENT_RECALL_TOOL_ENABLED") != "true"):
        raise RuntimeError("unexpected_native_or_status_permission")
    did = _env_value(lines, "LIFE_RUNTIME_LIFE_DID") or _env_value(lines, "LIVE_RUNTIME_LIFE_DID")
    if did != "did:arthurverse:nancy":
        raise RuntimeError("nancy_existing_resident_identity_changed")
    for unit in ("hermes-gateway.service", "hermes-life-cognition.service",
                 "hermes-life-contact.service", "hermes-life-percept-recovery.service"):
        if ops.run(["systemctl", "--user", "is-active", unit]).stdout.strip() != "active":
            raise RuntimeError("existing_service_not_ready:" + unit)
    ops.doctor()

    # Preserve every preexisting setting byte-for-byte, append just one.
    if _env_value(lines, SETTING) is None:
        new_env = old_env.rstrip(b"\n") + b"\n" + SETTING.encode() + b"=true\n"
    else:
        new_env = b"\n".join(
            (SETTING + "=true").encode() if x.startswith((SETTING + "=").encode()) else x
            for x in old_env.split(b"\n")
        )
    return original, old_env, new_env


def verify_registered() -> None:
    code = """
import json
import sqlite3
from hermes_life_bridge import plugin, resident_runtime_status as runtime
from hermes_life_bridge.config import BridgeConfig
from hermes_life_bridge.skill_proposal import _assert_real_resident_session
c=BridgeConfig.from_env()
assert c.life_did == 'did:arthurverse:nancy' and c.resident_runtime_status_enabled
assert not c.agent_tools_enabled and c.resident_recall_tool_enabled
assert runtime.enabled()
class Ctx:
 def __init__(self):self.hooks={};self.tools={}
 def register_hook(self,name,cb):self.hooks[name]=cb
 def register_tool(self,name,**kw):self.tools[name]=kw
ctx=Ctx();plugin.register(ctx)
assert runtime.TOOL in ctx.tools and ctx.tools[runtime.TOOL]['check_fn']()
assert 'digital_life_research' not in ctx.tools and 'digital_life_status' not in ctx.tools
assert 'digital_life_recall' in ctx.tools
state,policy=runtime._verified_owner_binding(c)
owner=None
with sqlite3.connect('file:'+str(state)+'?mode=ro',uri=True,timeout=3) as db:
 for (sid,) in db.execute("select id from sessions where source='feishu' and chat_type='dm' and hidden=0 order by rowid desc limit 250"):
  try:_assert_real_resident_session(state,policy,sid);owner=sid;break
  except Exception:pass
assert owner
status=json.loads(runtime.handler({},session_id=owner))
assert status['ok'] is True and status['lifeDid']==c.life_did and status['continuityVerified'] is True
assert status['agentFactory']['productionRuntimeSwitchReady'] is False
assert status['runtimeInvocationAttempted'] is False
negative=json.loads(runtime.handler({},session_id='forged-operator-nonowner'))
assert negative['ok'] is False and negative['executed'] is False
print('NANCY_FEISHU_RUNTIME_STATUS_READONLY_PASS')
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ops.PLUGIN / "src")
    result = ops.run([str(ops.RUNTIME_PY), "-c", code], timeout=25, env=env)
    if "NANCY_FEISHU_RUNTIME_STATUS_READONLY_PASS" not in result.stdout:
        raise RuntimeError("runtime_status_real_owner_acceptance_failed")


def main() -> None:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    ops.BACKUPS.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (ops.BACKUPS.is_symlink() or ops.BACKUPS.stat().st_uid != os.geteuid()
            or ops.BACKUPS.stat().st_mode & 0o077):
        raise RuntimeError("backup_directory_untrusted")
    lock_path = ops.BACKUPS / ".apply.lock"
    with lock_path.open("a+b") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        old, old_env, new_env = preflight()
        candidate = {p: (ops.ROOT / p).read_bytes() for p in (*CHANGED, NEW)}
        if any(candidate[p] == old[p] for p in CHANGED):
            raise RuntimeError("new_status_code_not_changed")
        if not args.apply:
            print(json.dumps({
                "status": "NANCY_EXISTING_FEISHU_RUNTIME_STATUS_READY",
                "lifeDid": "did:arthurverse:nancy",
                "changes": [*CHANGED, NEW, SETTING],
                "nativeResearchEnabled": False,
                "stateWritten": False,
            }))
            return

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = ops.BACKUPS / ("runtime-status-" + stamp)
        backup.mkdir(mode=0o700)
        for i, name in enumerate(CHANGED):
            ops.atomic_private(backup / f"source-{i}.bak", old[name])
        ops.atomic_private(backup / "env.before.bak", old_env)
        ops.atomic_private(backup / "manifest.json", json.dumps({
            "base": PREVIOUS_COMMIT,
            "sourceHashes": {p: ops.sha(old[p]) for p in CHANGED},
            "envHash": ops.sha(old_env),
        }, sort_keys=True).encode())
        touched = False
        try:
            if any(ops.checked_file(ops.PLUGIN / p) != old[p] for p in (*CHANGED, UNCHANGED)):
                raise RuntimeError("concurrent_plugin_update_detected")
            if ops.checked_file(ops.ENV) != old_env:
                raise RuntimeError("concurrent_environment_update_detected")
            if (ops.PLUGIN / NEW).exists() or (ops.PLUGIN / NEW).is_symlink():
                raise RuntimeError("new_status_module_concurrent_writer")
            touched = True
            for name, raw in candidate.items():
                ops.atomic_private(ops.PLUGIN / name, raw)
            ops.atomic_private(ops.ENV, new_env)
            ops.restart_gateway()
            ops.wait_for_doctor()
            verify_registered()
            print(json.dumps({
                "status": "NANCY_EXISTING_FEISHU_RUNTIME_STATUS_MODULE_ACTIVE",
                "lifeDid": "did:arthurverse:nancy",
                "registeredTool": "digital_life_runtime_status",
                "nativeResearchEnabled": False,
                "runtimeInvocationAttempted": False,
                "pluginRegistrationProbePassed": True,
                "feishuProviderTurnVerified": False,
                "productionRuntimeSwitchReady": False,
                "doctor": "HEALTHY",
                "backup": str(backup),
            }))
        except Exception:
            if touched:
                for name in CHANGED:
                    ops.atomic_private(ops.PLUGIN / name, old[name])
                new_file = ops.PLUGIN / NEW
                if new_file.exists():
                    new_file.unlink()
                ops.atomic_private(ops.ENV, old_env)
                ops.restart_gateway()
                ops.wait_for_doctor()
                print("NANCY_EXISTING_FEISHU_RUNTIME_STATUS_ROLLBACK=PASS", file=sys.stderr)
            raise


if __name__ == "__main__":
    main()
