#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLUGIN_DIR="$HOME/.hermes/plugins/hermes-life-bridge"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
STATE_HOME="${XDG_STATE_HOME:-$HOME/.local/state}"
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
HLB_ENV="$CONFIG_HOME/hermes-life-bridge.env"
BACKUP_PARENT="$STATE_HOME/hermes-life-bridge/code-backups"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="$BACKUP_PARENT/$STAMP"
SERVICES=(
  hermes-life-cognition.service
  hermes-life-contact.service
  hermes-life-percept-recovery.service
)
TIMER=hermes-life-maintenance.timer
GATEWAY=hermes-gateway.service
SWAPPED=0
DONE=0

fail() {
  echo "HLB code update: $*" >&2
  exit 1
}

selected_config() {
  python3 - "$HLB_ENV" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1])
keys={
    'HLB_CONTACT_DELIVERY_ENABLED',
    'HLB_WORK_PRODUCER_ENABLED',
    'HLB_AMBIENT_INTEREST_ENABLED',
    'HLB_CODEX_DECISION_ENABLED',
}
values={k:'unset' for k in keys}
if p.exists():
    for line in p.read_text(encoding='utf-8').splitlines():
        s=line.strip()
        if not s or s.startswith('#') or '=' not in s:
            continue
        k,v=s.split('=',1)
        if k in keys:
            values[k]=v.strip()
for k in sorted(keys):
    print(f'{k}={values[k]}')
PY
}

refresh_metadata() {
  if command -v uv >/dev/null 2>&1; then
    uv pip install --offline --python "$PLUGIN_DIR/.venv/bin/python" --no-deps -e "$PLUGIN_DIR" >/dev/null
  fi
}

restart_live() {
  systemctl --user daemon-reload
  systemctl --user restart "${SERVICES[@]}"
  systemctl --user restart "$TIMER"
  systemctl --user restart "$GATEWAY"
}

wait_live() {
  local i
  for i in $(seq 1 100); do
    if systemctl --user is-active --quiet "${SERVICES[@]}" "$GATEWAY" \
      && [ -S "$RUNTIME_DIR/hermes-life-cognition.sock" ] \
      && [ -S "$RUNTIME_DIR/hermes-life-contact.sock" ]; then
      return 0
    fi
    sleep 0.2
  done
  return 1
}

rollback() {
  local rc=$?
  if [ "$DONE" -eq 1 ]; then
    return
  fi
  if [ "$SWAPPED" -eq 1 ] && [ -d "$BACKUP_DIR" ]; then
    set +e
    for unit in "${SERVICES[@]}"; do
      systemctl --user stop "$unit" >/dev/null 2>&1
    done
    rsync -a --delete \
      --exclude='.venv/' \
      "$BACKUP_DIR/" "$PLUGIN_DIR/" >/dev/null 2>&1
    refresh_metadata >/dev/null 2>&1 || true
    restart_live >/dev/null 2>&1
    wait_live >/dev/null 2>&1
    echo "HLB_CODE_ROLLBACK=PASS" >&2
    set -e
  fi
  exit "$rc"
}
trap rollback EXIT

[ -d "$PLUGIN_DIR/.venv" ] || fail "live_plugin_venv_missing"
[ -f "$HLB_ENV" ] || fail "hlb_env_missing"
[ -S "$RUNTIME_DIR/nancy-live-runtime.sock" ] || fail "life_runtime_socket_missing"
command -v rsync >/dev/null || fail "rsync_missing"

ENV_HASH_BEFORE="$(sha256sum "$HLB_ENV" | awk '{print $1}')"
CONFIG_BEFORE="$(selected_config)"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_PARENT" "$BACKUP_DIR"
rsync -a \
  --exclude='.venv/' \
  --exclude='.git/' \
  --exclude='.pytest_cache/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  "$PLUGIN_DIR/" "$BACKUP_DIR/"

for unit in "${SERVICES[@]}"; do
  systemctl --user stop "$unit"
done
systemctl --user stop "$TIMER" >/dev/null 2>&1 || true

rsync -a --delete \
  --exclude='.venv/' \
  --exclude='.git/' \
  --exclude='.pytest_cache/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  "$ROOT/" "$PLUGIN_DIR/"
SWAPPED=1
refresh_metadata || fail "metadata_refresh_failed"

restart_live
wait_live || fail "service_restart_failed"

ENV_HASH_AFTER="$(sha256sum "$HLB_ENV" | awk '{print $1}')"
[ "$ENV_HASH_BEFORE" = "$ENV_HASH_AFTER" ] || fail "hlb_env_changed"
CONFIG_AFTER="$(selected_config)"
[ "$CONFIG_BEFORE" = "$CONFIG_AFTER" ] || fail "selected_config_changed"

LIVE_VERSION="$(PYTHONPATH="$PLUGIN_DIR/src" "$PLUGIN_DIR/.venv/bin/python" -c 'import hermes_life_bridge; print(hermes_life_bridge.__version__)')"
REPO_VERSION="$(PYTHONPATH="$ROOT/src" python3 -c 'import hermes_life_bridge; print(hermes_life_bridge.__version__)')"
[ "$LIVE_VERSION" = "$REPO_VERSION" ] || fail "live_version_mismatch"
METADATA_VERSION="$(PYTHONPATH="$PLUGIN_DIR/src" "$PLUGIN_DIR/.venv/bin/python" -c 'import importlib.metadata as m; print(m.version("hermes-life-bridge"))')"
if command -v uv >/dev/null 2>&1; then
  [ "$METADATA_VERSION" = "$REPO_VERSION" ] || fail "metadata_version_mismatch"
fi

DOCTOR_HEALTHY=0
for _ in $(seq 1 60); do
  doctor_json="$(PYTHONPATH="$PLUGIN_DIR/src" "$PLUGIN_DIR/.venv/bin/python" -m hermes_life_bridge.cli doctor)"
  if python3 - "$doctor_json" <<'PY'
import json, sys
d=json.loads(sys.argv[1])
raise SystemExit(0 if d.get('overall') == 'HEALTHY' else 1)
PY
  then
    DOCTOR_HEALTHY=1
    break
  fi
  sleep 0.5
done
[ "$DOCTOR_HEALTHY" -eq 1 ] || fail "doctor_not_healthy"

DONE=1
trap - EXIT

echo "HLB_CODE_UPDATE=PASS"
echo "version=$LIVE_VERSION"
echo "metadata_version=$METADATA_VERSION"
echo "env_hash_unchanged=true"
while IFS= read -r line; do echo "$line"; done <<<"$CONFIG_AFTER"
echo "backup=$BACKUP_DIR"
