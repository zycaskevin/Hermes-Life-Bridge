#!/usr/bin/env bash
set -euo pipefail
LR_ROOT="${LIVE_RUNTIME_INSTALL_DIR:-$HOME/Projects/live-runtime}"
STATE_DB="${XDG_STATE_HOME:-$HOME/.local/state}/nancy-live-runtime/runtime.sqlite3"
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
SOCKET="$RUNTIME_DIR/hermes-life-contact.sock"
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/hermes-life-bridge.env"
ROUTE_FILE="${HLB_ROUTE_PATH:-${XDG_STATE_HOME:-$HOME/.local/state}/hermes-life-bridge/last_route.json}"
TARGET=""
if [[ -f "$ROUTE_FILE" ]]; then
  TARGET="$(python3 - "$ROUTE_FILE" <<'PY_ROUTE'
import json, pathlib, sys
data=json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
print(data.get("target") or "")
PY_ROUTE
)"
fi
[[ -n "$TARGET" ]] || TARGET="$(grep '^HLB_CONTACT_TARGET=' "$CONFIG" | tail -1 | cut -d= -f2- || true)"
[[ -n "$TARGET" ]] || TARGET="telegram"
grep -q '^HLB_CONTACT_DELIVERY_ENABLED=false$' "$CONFIG" || {
  echo "FAIL: dry-run requires delivery disabled"; exit 2;
}
cd "$LR_ROOT"
PYTHONPATH=src .venv/bin/python -m live_runtime.cli contact-selftest \
  --db "$STATE_DB" --socket "$SOCKET" --target "$TARGET" \
  --message "HLB-003 dry-run contract self-test $(date -Iseconds)" --timeout 30
