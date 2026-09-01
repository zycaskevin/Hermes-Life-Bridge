#!/usr/bin/env bash
set -euo pipefail

LR_DIR="${LIVE_RUNTIME_INSTALL_DIR:-$HOME/Projects/live-runtime}"
STATE_DB="${XDG_STATE_HOME:-$HOME/.local/state}/nancy-live-runtime/runtime.sqlite3"
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
COG_SOCKET="$RUNTIME_DIR/hermes-life-cognition.sock"

HERMES_CONFIG="$(hermes config path)"
HERMES_HOME="$(dirname "$HERMES_CONFIG")"
PLUGIN_DIR="$HERMES_HOME/plugins/hermes-life-bridge"

printf '\n== HLB-002 doctor ==\n'
PYTHONPATH="$PLUGIN_DIR/src" python3 -m hermes_life_bridge.cli doctor

printf '\n== Life Runtime cognition selftest ==\n'
cd "$LR_DIR"
PYTHONPATH=src .venv/bin/python -m live_runtime.cli cognition-selftest \
  --db "$STATE_DB" \
  --socket "$COG_SOCKET" \
  --timeout 180

printf '\n== HLB cognition trace tail ==\n'
PYTHONPATH="$PLUGIN_DIR/src" python3 -m hermes_life_bridge.cli trace --tail 40

printf '\nHLB002_E2E_SCRIPT=PASS\n'
