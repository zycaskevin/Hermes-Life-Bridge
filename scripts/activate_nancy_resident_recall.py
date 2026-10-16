#!/usr/bin/env python3
"""Scoped, reversible Nancy existing-Feishu resident DLMF recall activation.

Intentionally does NOT activate research, other runtimes, canonical writes, or
Lily/Luna native agent-tool grants. No owner identity or credentials are copied
into the Git checkout. Run with --check first; --apply requires the exact
reviewed resident plugin baseline.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
HOME = Path.home()
PLUGIN = HOME / ".hermes/plugins/hermes-life-bridge"
ENV = HOME / ".config/hermes-life-bridge.env"
BACKUPS = HOME / ".local/state/hermes-life-bridge/resident-recall-backups"
BASE_HEAD = "9e49f8e1641a72ba94e9a3c963ca6869b6074025"
PARTS = (
    "src/hermes_life_bridge/config.py",
    "src/hermes_life_bridge/agent_tools.py",
    "src/hermes_life_bridge/plugin.py",
)
SETTING = "HLB_RESIDENT_RECALL_TOOL_ENABLED"
RUNTIME_PY = HOME / ".local/share/hermes-life-bridge/runtime-venv/bin/python"


def run(cmd: list[str], *, timeout: int = 15, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout, env=env)


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def checked_file(path: Path) -> bytes:
    if path.is_symlink():
        raise RuntimeError("symlink_denied:" + path.name)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1 or info.st_mode & 0o077:
        raise RuntimeError("owner_private_regular_file_required:" + path.name)
    return path.read_bytes()


def atomic_private(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".nancy-recall-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        dirfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def baseline() -> tuple[dict[str, bytes], bytes, bytes]:
    head = run(["git", "-C", str(PLUGIN), "rev-parse", "HEAD"]).stdout.strip()
    if head != BASE_HEAD:
        raise RuntimeError("installed_plugin_head_drift")
    original: dict[str, bytes] = {}
    for name in PARTS:
        live = checked_file(PLUGIN / name)
        expected = subprocess.run(
            ["git", "-C", str(ROOT), "show", BASE_HEAD + ":" + name],
            check=True, capture_output=True, timeout=8,
        ).stdout
        if live != expected:
            raise RuntimeError("installed_plugin_source_drift:" + name)
        original[name] = live
    env_bytes = checked_file(ENV)
    lines = env_bytes.decode("utf-8").splitlines()
    matches = [line.split("=", 1)[1].strip().strip("'\"") for line in lines
               if line.startswith(SETTING + "=")]
    if len(matches) > 1 or (matches and matches[0].lower() not in {"false", "0", "off"}):
        raise RuntimeError("existing_recall_setting_not_disabled")
    for unit in ("hermes-gateway.service", "hermes-life-cognition.service",
                 "hermes-life-contact.service", "hermes-life-percept-recovery.service"):
        if run(["systemctl", "--user", "is-active", unit]).stdout.strip() != "active":
            raise RuntimeError("existing_service_not_active:" + unit)
    if not RUNTIME_PY.is_file():
        raise RuntimeError("runtime_venv_python_missing")
    return original, env_bytes, (SETTING + "=true\n").encode()


def doctor() -> str:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(PLUGIN / "src")
    p = run([str(RUNTIME_PY), "-m", "hermes_life_bridge.cli", "doctor"], timeout=12, env=environment)
    data = json.loads(p.stdout)
    if data.get("overall") != "HEALTHY":
        raise RuntimeError("doctor_not_healthy")
    return data["overall"]


def wait_for_doctor(attempts: int = 60) -> None:
    """Gateway may be active before its authenticated local Hermes API is ready."""
    for attempt in range(attempts):
        try:
            doctor()
            return
        except (RuntimeError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
            if attempt == attempts - 1:
                raise
            time.sleep(1)


def verify_tool() -> None:
    code = """
from hermes_life_bridge.config import BridgeConfig
from hermes_life_bridge.agent_tools import resident_recall_enabled
from hermes_life_bridge import plugin
c = BridgeConfig.from_env()
assert c.life_did == 'did:arthurverse:nancy'
assert c.resident_recall_tool_enabled and not c.agent_tools_enabled
assert resident_recall_enabled()
class Ctx:
    def __init__(self): self.hooks={};self.tools={}
    def register_hook(self,name,handler):self.hooks[name]=handler
    def register_tool(self,name,**kw):self.tools[name]=kw
ctx=Ctx()
plugin.register(ctx)
assert 'digital_life_recall' in ctx.tools
assert not {'digital_life_research','digital_life_status'} & ctx.tools.keys()
assert ctx.tools['digital_life_recall']['check_fn']()
print('NANCY_RESIDENT_FEISHU_RECALL_REGISTERED')
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(PLUGIN / "src")
    p = run([str(RUNTIME_PY), "-c", code], timeout=15, env=environment)
    if "NANCY_RESIDENT_FEISHU_RECALL_REGISTERED" not in p.stdout:
        raise RuntimeError("tool_registry_not_verified")


def restart_gateway() -> None:
    run(["systemctl", "--user", "restart", "hermes-gateway.service"], timeout=220)
    if run(["systemctl", "--user", "is-active", "hermes-gateway.service"]).stdout.strip() != "active":
        raise RuntimeError("hermes_gateway_not_active")


def main() -> None:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="verify current sources and leave production unchanged (default)")
    mode.add_argument("--apply", action="store_true", help="apply scoped update with automatic rollback on failed acceptance")
    args = parser.parse_args()
    if not ROOT.is_dir() or not PLUGIN.is_dir():
        raise RuntimeError("source_or_live_plugin_missing")
    BACKUPS.mkdir(mode=0o700, parents=True, exist_ok=True)
    if BACKUPS.is_symlink() or BACKUPS.stat().st_uid != os.geteuid() or BACKUPS.stat().st_mode & 0o077:
        raise RuntimeError("untrusted_backup_directory")
    lock_path = BACKUPS / ".apply.lock"
    with lock_path.open("a+b") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        originals, previous_env, setting = baseline()
        doctor()
        modified: dict[str, bytes] = {}
        for part in PARTS:
            replacement = (ROOT / part).read_bytes()
            if replacement == originals[part]:
                raise RuntimeError("candidate_missing_delta:" + part)
            modified[part] = replacement
        original_text = previous_env.decode("utf-8")
        if any(x.startswith(SETTING + "=") for x in original_text.splitlines()):
            updated_env = "\n".join(setting.decode().strip() if x.startswith(SETTING + "=") else x
                                    for x in original_text.splitlines()) + "\n"
        else:
            updated_env = original_text.rstrip("\n") + "\n" + setting.decode()
        if not args.apply:
            print(json.dumps({"status": "READY_TO_APPLY", "lifeDid": "did:arthurverse:nancy",
                              "paths": list(PARTS), "ownerFeishuOnly": True,
                              "nativeResearchActivated": False, "configurationChanged": False}))
            return

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        folder = BACKUPS / stamp
        folder.mkdir(mode=0o700)
        for index, (_, raw) in enumerate(originals.items()):
            atomic_private(folder / ("source-" + str(index) + ".bak"), raw)
        atomic_private(folder / "hlb-env.bak", previous_env)
        atomic_private(folder / "manifest.json",
                       json.dumps({"originalSha256": {k: sha(v) for k,v in originals.items()},
                                   "envSha256": sha(previous_env), "baselineHead": BASE_HEAD},
                                  sort_keys=True).encode())
        swapped = False
        try:
            for part in PARTS:
                if checked_file(PLUGIN / part) != originals[part]:
                    raise RuntimeError("concurrent_plugin_change")
            if checked_file(ENV) != previous_env:
                raise RuntimeError("concurrent_config_change")
            swapped = True
            for part, body in modified.items():
                atomic_private(PLUGIN / part, body)
            atomic_private(ENV, updated_env.encode())
            restart_gateway()
            verify_tool()
            wait_for_doctor()
            print(json.dumps({"status":"NANCY_RESIDENT_FEISHU_RECALL_ACTIVATED",
                              "lifeDid":"did:arthurverse:nancy",
                              "registeredTool":"digital_life_recall",
                              "nativeResearchActivated":False,
                              "backup":str(folder), "doctor":"HEALTHY"}))
        except Exception:
            if swapped:
                for part, body in originals.items():
                    atomic_private(PLUGIN / part, body)
                atomic_private(ENV, previous_env)
                restart_gateway()
                wait_for_doctor()
                print("NANCY_RESIDENT_FEISHU_RECALL_ROLLBACK=PASS", file=sys.stderr)
            raise


if __name__ == "__main__":
    main()
