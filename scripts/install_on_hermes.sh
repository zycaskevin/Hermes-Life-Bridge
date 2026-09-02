#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
STATE_HOME="${XDG_STATE_HOME:-$HOME/.local/state}"
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
HLB_ENV="$CONFIG_HOME/hermes-life-bridge.env"
UNIT_DIR="$CONFIG_HOME/systemd/user"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_ROOT="$STATE_HOME/hermes-life-bridge/install-backup-$STAMP"
mkdir -p "$CONFIG_HOME" "$STATE_HOME/hermes-life-bridge" "$UNIT_DIR" "$BACKUP_ROOT"
chmod 700 "$BACKUP_ROOT" 2>/dev/null || true

command -v hermes >/dev/null || { echo "FAIL: hermes not found"; exit 2; }
command -v python3 >/dev/null || { echo "FAIL: python3 not found"; exit 2; }
command -v tar >/dev/null || { echo "FAIL: tar not found"; exit 2; }

HERMES_CONFIG="$(hermes config path 2>/dev/null || true)"
if [[ -n "$HERMES_CONFIG" ]]; then
  HERMES_HOME="$(dirname "$HERMES_CONFIG")"
else
  HERMES_HOME="$HOME/.hermes"
fi
PLUGIN_DIR="$HERMES_HOME/plugins/hermes-life-bridge"
PLUGIN_BACKUP="$HERMES_HOME/plugin-backups/hermes-life-bridge.$STAMP"
HERMES_ENV="$(hermes config env-path 2>/dev/null || true)"
[[ -z "$HERMES_ENV" ]] && HERMES_ENV="$HERMES_HOME/.env"

SERVICES=(
  hermes-life-cognition.service
  hermes-life-contact.service
  hermes-life-percept-recovery.service
)
TIMER="hermes-life-maintenance.timer"
declare -A WAS_ACTIVE=()
declare -A WAS_ENABLED=()
for unit in "${SERVICES[@]}" "$TIMER"; do
  if systemctl --user is-active --quiet "$unit" 2>/dev/null; then WAS_ACTIVE["$unit"]=1; else WAS_ACTIVE["$unit"]=0; fi
  if systemctl --user is-enabled --quiet "$unit" 2>/dev/null; then WAS_ENABLED["$unit"]=1; else WAS_ENABLED["$unit"]=0; fi
done

OLD_PLUGIN_DIR="$HERMES_HOME/plugins/nancy-live-runtime"
OLD_PLUGIN_BACKUP="$HERMES_HOME/plugin-backups/nancy-live-runtime.$STAMP"
OLD_PLUGIN_MOVED=0
OLD_PLUGIN_WAS_LISTED=0
OLD_PLUGIN_WAS_ENABLED=0
if hermes plugins list 2>/dev/null | grep -q 'nancy-live-runtime'; then
  OLD_PLUGIN_WAS_LISTED=1
  if hermes plugins list 2>/dev/null | grep 'nancy-live-runtime' | grep -qi 'enabled'; then OLD_PLUGIN_WAS_ENABLED=1; fi
fi
HLB_PLUGIN_WAS_ENABLED=0
if hermes plugins list 2>/dev/null | grep 'hermes-life-bridge' | grep -qi 'enabled'; then HLB_PLUGIN_WAS_ENABLED=1; fi

PLUGIN_MOVED=0
ENV_BACKED_UP=0
INSTALL_SUCCESS=0

for file in \
  hermes-life-cognition.service \
  hermes-life-contact.service \
  hermes-life-percept-recovery.service \
  hermes-life-maintenance.service \
  hermes-life-maintenance.timer; do
  if [[ -f "$UNIT_DIR/$file" ]]; then
    cp -a "$UNIT_DIR/$file" "$BACKUP_ROOT/$file"
  fi
done
if [[ -f "$HLB_ENV" ]]; then
  cp -a "$HLB_ENV" "$BACKUP_ROOT/hermes-life-bridge.env"
  ENV_BACKED_UP=1
fi

rollback_install() {
  local rc="$1"
  trap - EXIT
  if [[ "$INSTALL_SUCCESS" -eq 1 || "$rc" -eq 0 ]]; then
    rm -rf "$BACKUP_ROOT" 2>/dev/null || true
    exit "$rc"
  fi

  echo "INSTALL FAILED — restoring previous Hermes Life Bridge state..." >&2
  for unit in "${SERVICES[@]}"; do
    systemctl --user stop "$unit" >/dev/null 2>&1 || true
  done
  systemctl --user stop "$TIMER" >/dev/null 2>&1 || true

  rm -rf "$PLUGIN_DIR"
  if [[ "$PLUGIN_MOVED" -eq 1 && -d "$PLUGIN_BACKUP" ]]; then
    mv "$PLUGIN_BACKUP" "$PLUGIN_DIR" || true
  fi
  if [[ "$OLD_PLUGIN_MOVED" -eq 1 && -d "$OLD_PLUGIN_BACKUP" ]]; then
    mv "$OLD_PLUGIN_BACKUP" "$OLD_PLUGIN_DIR" || true
  fi
  if [[ "$ENV_BACKED_UP" -eq 1 && -f "$BACKUP_ROOT/hermes-life-bridge.env" ]]; then
    cp -a "$BACKUP_ROOT/hermes-life-bridge.env" "$HLB_ENV" || true
  else
    rm -f "$HLB_ENV"
  fi

  for file in \
    hermes-life-cognition.service \
    hermes-life-contact.service \
    hermes-life-percept-recovery.service \
    hermes-life-maintenance.service \
    hermes-life-maintenance.timer; do
    if [[ -f "$BACKUP_ROOT/$file" ]]; then
      cp -a "$BACKUP_ROOT/$file" "$UNIT_DIR/$file" || true
    else
      rm -f "$UNIT_DIR/$file"
    fi
  done
  systemctl --user daemon-reload >/dev/null 2>&1 || true

  if [[ "$PLUGIN_MOVED" -eq 1 && "$HLB_PLUGIN_WAS_ENABLED" -eq 1 ]]; then
    hermes plugins enable hermes-life-bridge >/dev/null 2>&1 || true
  fi
  if [[ "$OLD_PLUGIN_WAS_LISTED" -eq 1 && "$OLD_PLUGIN_WAS_ENABLED" -eq 1 ]]; then
    hermes plugins enable nancy-live-runtime >/dev/null 2>&1 || true
  fi
  for unit in "${SERVICES[@]}" "$TIMER"; do
    if [[ "${WAS_ENABLED[$unit]:-0}" -eq 1 ]]; then
      systemctl --user enable "$unit" >/dev/null 2>&1 || true
    fi
    if [[ "${WAS_ACTIVE[$unit]:-0}" -eq 1 ]]; then
      systemctl --user start "$unit" >/dev/null 2>&1 || true
    fi
  done
  echo "ROLLBACK=PASS" >&2
  exit "$rc"
}
trap 'rollback_install $?' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Discover the existing Life Runtime deployment without changing it.
LIFE_ENV="$CONFIG_HOME/nancy-live-runtime.env"
LIFE_DID="did:example:life"
if [[ -f "$LIFE_ENV" ]]; then
  found="$(grep -E '^(LIVE_RUNTIME_LIFE_DID|LIFE_RUNTIME_LIFE_DID)=' "$LIFE_ENV" | tail -1 | cut -d= -f2- || true)"
  [[ -n "$found" ]] && LIFE_DID="$found"
fi
SOCKET="$RUNTIME_DIR/nancy-live-runtime.sock"
if [[ ! -S "$SOCKET" ]]; then
  echo "FAIL: Life Runtime socket not found at $SOCKET" >&2
  exit 3
fi

# Preserve an explicit static target across upgrades, but always reset external
# delivery to OFF. Learned exact routes remain separately in the private RouteStore.
CONTACT_TARGET_DISCOVERED="${HLB_CONTACT_TARGET:-}"
if [[ -z "$CONTACT_TARGET_DISCOVERED" && -f "$HLB_ENV" ]]; then
  CONTACT_TARGET_DISCOVERED="$(grep -E '^HLB_CONTACT_TARGET=' "$HLB_ENV" | tail -1 | cut -d= -f2- || true)"
fi
cat > "$HLB_ENV.new" <<EOF
LIVE_RUNTIME_LIFE_DID=$LIFE_DID
LIVE_RUNTIME_SOCKET=$SOCKET
HLB_TRACE_PATH=$STATE_HOME/hermes-life-bridge/trace.jsonl
HLB_TRACE_MAX_BYTES=10485760
HLB_TRACE_BACKUP_COUNT=3
HLB_OPERATION_RETENTION_SECONDS=2592000
HLB_COGNITION_SOCKET=$RUNTIME_DIR/hermes-life-cognition.sock
HLB_COGNITION_DB=$STATE_HOME/hermes-life-bridge/cognition.sqlite3
HLB_HERMES_API_BASE_URL=http://127.0.0.1:8642
HLB_HERMES_ENV=$HERMES_ENV
HLB_CONTACT_SOCKET=$RUNTIME_DIR/hermes-life-contact.sock
HLB_CONTACT_DB=$STATE_HOME/hermes-life-bridge/contact.sqlite3
HLB_OPERATION_DB=$STATE_HOME/hermes-life-bridge/operations.sqlite3
HLB_COMPATIBILITY_PATH=$STATE_HOME/hermes-life-bridge/compatibility.json
HLB_COMPATIBILITY_EVIDENCE_PATH=$STATE_HOME/hermes-life-bridge/compatibility-evidence.json
HLB_CONTACT_DELIVERY_ENABLED=false
HLB_CONTACT_TARGET=$CONTACT_TARGET_DISCOVERED
HLB_ROUTE_PATH=$STATE_HOME/hermes-life-bridge/last_route.json
HLB_ROUTE_MAX_AGE_SECONDS=604800
EOF
chmod 600 "$HLB_ENV.new"
mv "$HLB_ENV.new" "$HLB_ENV"
chmod 600 "$HLB_ENV"

# Stop HLB workers before replacing the plugin tree.
for unit in "${SERVICES[@]}"; do
  systemctl --user stop "$unit" >/dev/null 2>&1 || true
done
systemctl --user stop "$TIMER" >/dev/null 2>&1 || true

if [[ -d "$PLUGIN_DIR" ]]; then
  mkdir -p "$(dirname "$PLUGIN_BACKUP")"
  mv "$PLUGIN_DIR" "$PLUGIN_BACKUP"
  PLUGIN_MOVED=1
fi
mkdir -p "$PLUGIN_DIR"
tar -C "$ROOT" \
  --exclude=.git \
  --exclude=.venv \
  --exclude=.pytest_cache \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  -cf - . | tar -C "$PLUGIN_DIR" -xf -
python3 -m venv "$PLUGIN_DIR/.venv"
"$PLUGIN_DIR/.venv/bin/python" -m pip install --disable-pip-version-check -e "$PLUGIN_DIR"

# Remove the legacy competing plugin only after the new HLB tree is installable.
if [[ "$OLD_PLUGIN_WAS_LISTED" -eq 1 ]]; then
  hermes plugins disable nancy-live-runtime >/dev/null 2>&1 || true
fi
if [[ -d "$OLD_PLUGIN_DIR" ]]; then
  mkdir -p "$(dirname "$OLD_PLUGIN_BACKUP")"
  mv "$OLD_PLUGIN_DIR" "$OLD_PLUGIN_BACKUP"
  OLD_PLUGIN_MOVED=1
fi
hermes plugins enable hermes-life-bridge

# Install all HLB-004 services in one systemd reload.
sed -e "s|@PLUGIN_DIR@|$PLUGIN_DIR|g" -e "s|@HERMES_ENV@|$HERMES_ENV|g" \
  "$PLUGIN_DIR/systemd/hermes-life-cognition.service.template" \
  > "$UNIT_DIR/hermes-life-cognition.service"
sed "s|@PLUGIN_DIR@|$PLUGIN_DIR|g" \
  "$PLUGIN_DIR/systemd/hermes-life-contact.service.template" \
  > "$UNIT_DIR/hermes-life-contact.service"
sed "s|@PLUGIN_DIR@|$PLUGIN_DIR|g" \
  "$PLUGIN_DIR/systemd/hermes-life-percept-recovery.service.template" \
  > "$UNIT_DIR/hermes-life-percept-recovery.service"
sed "s|@PLUGIN_DIR@|$PLUGIN_DIR|g" \
  "$PLUGIN_DIR/systemd/hermes-life-maintenance.service.template" \
  > "$UNIT_DIR/hermes-life-maintenance.service"
cp "$PLUGIN_DIR/systemd/hermes-life-maintenance.timer.template" \
  "$UNIT_DIR/hermes-life-maintenance.timer"

systemctl --user daemon-reload
systemctl --user enable --now hermes-life-cognition.service
systemctl --user enable --now hermes-life-contact.service
systemctl --user enable --now hermes-life-percept-recovery.service
systemctl --user enable --now hermes-life-maintenance.timer

for i in $(seq 1 50); do
  [[ -S "$RUNTIME_DIR/hermes-life-cognition.sock" && -S "$RUNTIME_DIR/hermes-life-contact.sock" ]] && break
  sleep 0.2
done
[[ -S "$RUNTIME_DIR/hermes-life-cognition.sock" ]] || { echo "FAIL: cognition socket unavailable" >&2; exit 6; }
[[ -S "$RUNTIME_DIR/hermes-life-contact.sock" ]] || { echo "FAIL: contact socket unavailable" >&2; exit 7; }
systemctl --user is-active --quiet hermes-life-percept-recovery.service || { echo "FAIL: Percept recovery service inactive" >&2; exit 8; }
systemctl --user is-active --quiet hermes-life-maintenance.timer || { echo "FAIL: maintenance timer inactive" >&2; exit 9; }

# Offline/self-contained release gates. These cannot external-send because delivery
# was forcibly reset to false above.
PYTHONPATH="$PLUGIN_DIR/src" "$PLUGIN_DIR/.venv/bin/python" -m hermes_life_bridge.cli selftest
PYTHONPATH="$PLUGIN_DIR/src" "$PLUGIN_DIR/.venv/bin/python" -m hermes_life_bridge.soak --iterations 1000

# Hermes plugins are loaded by the Gateway process. Current Hermes releases
# require a gateway restart for plugin changes to take effect. Restart when the
# managed gateway is available; acceptance below will detect any remaining issue.
GATEWAY_RESTARTED=0
if hermes gateway restart >/dev/null 2>&1; then
  GATEWAY_RESTARTED=1
fi

INSTALL_SUCCESS=1
rm -rf "$BACKUP_ROOT" 2>/dev/null || true
trap - EXIT

echo
echo "HLB_INSTALL=PASS"
echo "Version: $($PLUGIN_DIR/.venv/bin/python -c 'import hermes_life_bridge; print(hermes_life_bridge.__version__)')"
echo "External proactive delivery: OFF (safe default)"
echo "Plugin: $PLUGIN_DIR"
echo
if [[ "$GATEWAY_RESTARTED" -eq 1 ]]; then
  echo "Hermes Gateway restart: PASS"
  echo "Now send Nancy one normal message, then run:"
else
  echo "Hermes Gateway restart: NEEDS MANUAL RETRY"
  echo "Run: hermes gateway restart"
  echo "Then send Nancy one normal message and run:"
fi
echo "  $PLUGIN_DIR/scripts/accept_on_nancy.sh"
