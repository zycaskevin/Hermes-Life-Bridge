#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
HLB_ENV="$CONFIG_HOME/hermes-life-bridge.env"
STATE_HOME="${XDG_STATE_HOME:-$HOME/.local/state}"
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

command -v hermes >/dev/null || { echo "FAIL: hermes not found"; exit 2; }
command -v python3 >/dev/null || { echo "FAIL: python3 not found"; exit 2; }

HERMES_CONFIG="$(hermes config path 2>/dev/null || true)"
if [[ -n "$HERMES_CONFIG" ]]; then
  HERMES_HOME="$(dirname "$HERMES_CONFIG")"
else
  HERMES_HOME="$HOME/.hermes"
fi
PLUGIN_DIR="$HERMES_HOME/plugins/hermes-life-bridge"
HERMES_ENV="$(hermes config env-path 2>/dev/null || true)"
[[ -z "$HERMES_ENV" ]] && HERMES_ENV="$HERMES_HOME/.env"

# Discover the existing Life Runtime deployment without changing it.
LIFE_ENV="$CONFIG_HOME/nancy-live-runtime.env"
LIFE_DID="did:example:life"
if [[ -f "$LIFE_ENV" ]]; then
  found="$(grep -E '^(LIVE_RUNTIME_LIFE_DID|LIFE_RUNTIME_LIFE_DID)=' "$LIFE_ENV" | tail -1 | cut -d= -f2- || true)"
  [[ -n "$found" ]] && LIFE_DID="$found"
fi

SOCKET="$RUNTIME_DIR/nancy-live-runtime.sock"
if [[ ! -S "$SOCKET" ]]; then
  echo "FAIL: Life Runtime socket not found at $SOCKET"
  exit 3
fi

mkdir -p "$CONFIG_HOME" "$STATE_HOME/hermes-life-bridge"
CONTACT_TARGET_DISCOVERED="${HLB_CONTACT_TARGET:-}"
cat > "$HLB_ENV" <<EOF
LIVE_RUNTIME_LIFE_DID=$LIFE_DID
LIVE_RUNTIME_SOCKET=$SOCKET
HLB_TRACE_PATH=$STATE_HOME/hermes-life-bridge/trace.jsonl
HLB_COGNITION_SOCKET=$RUNTIME_DIR/hermes-life-cognition.sock
HLB_COGNITION_DB=$STATE_HOME/hermes-life-bridge/cognition.sqlite3
HLB_HERMES_API_BASE_URL=http://127.0.0.1:8642
HLB_HERMES_ENV=$HERMES_ENV
HLB_CONTACT_SOCKET=$RUNTIME_DIR/hermes-life-contact.sock
HLB_CONTACT_DB=$STATE_HOME/hermes-life-bridge/contact.sqlite3
HLB_CONTACT_DELIVERY_ENABLED=false
HLB_CONTACT_TARGET=$CONTACT_TARGET_DISCOVERED
HLB_ROUTE_PATH=$STATE_HOME/hermes-life-bridge/last_route.json
EOF
chmod 600 "$HLB_ENV"

# Preserve old plugin but remove it from active discovery to avoid competing ingress.
OLD_PLUGIN_DIR="$HERMES_HOME/plugins/nancy-live-runtime"
OLD_PLUGIN_BACKUP="$HERMES_HOME/plugin-backups/nancy-live-runtime.$(date +%Y%m%d-%H%M%S)"
if hermes plugins list 2>/dev/null | grep -q 'nancy-live-runtime'; then
  hermes plugins disable nancy-live-runtime
fi
if [[ -d "$OLD_PLUGIN_DIR" ]]; then
  mkdir -p "$(dirname "$OLD_PLUGIN_BACKUP")"
  mv "$OLD_PLUGIN_DIR" "$OLD_PLUGIN_BACKUP"
  echo "Old plugin backed up: $OLD_PLUGIN_BACKUP"
fi
if [[ -d "$OLD_PLUGIN_DIR" ]]; then
  echo "FAIL: old nancy-live-runtime plugin still exists in active plugin directory"
  exit 4
fi
if hermes plugins list 2>/dev/null | grep 'nancy-live-runtime' | grep -Eq '│[[:space:]]*enabled[[:space:]]*│'; then
  echo "FAIL: old nancy-live-runtime plugin still reports enabled"
  exit 5
fi

rm -rf "$PLUGIN_DIR"
mkdir -p "$PLUGIN_DIR"
cp -a "$ROOT"/. "$PLUGIN_DIR"/
python3 -m venv "$PLUGIN_DIR/.venv"
"$PLUGIN_DIR/.venv/bin/python" -m pip install --disable-pip-version-check -e "$PLUGIN_DIR"

hermes plugins enable hermes-life-bridge

# HLB-002 cognition service. Read API_SERVER_KEY directly from Hermes env at service runtime;
# do not copy or print the key into HLB config.
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"
UNIT="$UNIT_DIR/hermes-life-cognition.service"
sed -e "s|@PLUGIN_DIR@|$PLUGIN_DIR|g" -e "s|@HERMES_ENV@|$HERMES_ENV|g" \
  "$PLUGIN_DIR/systemd/hermes-life-cognition.service.template" > "$UNIT"
systemctl --user daemon-reload
systemctl --user enable --now hermes-life-cognition.service
for i in $(seq 1 50); do
  [[ -S "$RUNTIME_DIR/hermes-life-cognition.sock" ]] && break
  sleep 0.2
done
[[ -S "$RUNTIME_DIR/hermes-life-cognition.sock" ]] || {
  systemctl --user status hermes-life-cognition.service --no-pager || true
  echo "FAIL: HLB cognition socket not available"
  exit 6
}

echo
echo "Installed Hermes Life Bridge:"
echo "  $PLUGIN_DIR"
echo "Config:"
echo "  $HLB_ENV"
echo
echo "Running bridge self-test against current Life Runtime..."
PYTHONPATH="$PLUGIN_DIR/src" python3 -m hermes_life_bridge.cli selftest
echo

echo "Checking HLB-002 cognition service..."
PYTHONPATH="$PLUGIN_DIR/src" python3 -m hermes_life_bridge.cognition_cli health || {
  echo "WARN: Hermes API server health check failed. HLB-001 ingress remains available, but HLB-002 real cognition E2E needs the local Hermes API server (:8642)."
}

echo "Run after Hermes reload:"
echo "  PYTHONPATH=\"$PLUGIN_DIR/src\" python3 -m hermes_life_bridge.cli doctor"
echo "  PYTHONPATH=\"$PLUGIN_DIR/src\" python3 -m hermes_life_bridge.cli trace --tail 20"


# Install HLB-003 contact delivery service (default delivery disabled).
CONTACT_TEMPLATE="$ROOT/systemd/hermes-life-contact.service.template"
CONTACT_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
CONTACT_UNIT="$CONTACT_UNIT_DIR/hermes-life-contact.service"
mkdir -p "$CONTACT_UNIT_DIR"
sed "s|@PLUGIN_DIR@|$PLUGIN_DIR|g" "$CONTACT_TEMPLATE" > "$CONTACT_UNIT"
systemctl --user daemon-reload
systemctl --user enable --now hermes-life-contact.service
for i in $(seq 1 50); do
  [[ -S "$RUNTIME_DIR/hermes-life-contact.sock" ]] && break
  sleep 0.2
done
[[ -S "$RUNTIME_DIR/hermes-life-contact.sock" ]] || {
  systemctl --user status hermes-life-contact.service --no-pager || true
  echo "FAIL: HLB contact socket not available"
  exit 7
}
systemctl --user status hermes-life-contact.service --no-pager || true
