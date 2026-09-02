#!/usr/bin/env bash
set -Eeuo pipefail

CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
HLB_ENV="$CONFIG_HOME/hermes-life-bridge.env"
[[ -f "$HLB_ENV" ]] || { echo "FAIL: HLB config not found" >&2; exit 2; }
set -a
# shellcheck disable=SC1090
source "$HLB_ENV"
set +a

HERMES_CONFIG="$(hermes config path 2>/dev/null || true)"
if [[ -n "$HERMES_CONFIG" ]]; then HERMES_HOME="$(dirname "$HERMES_CONFIG")"; else HERMES_HOME="$HOME/.hermes"; fi
PLUGIN_DIR="$HERMES_HOME/plugins/hermes-life-bridge"
PY="$PLUGIN_DIR/.venv/bin/python"
[[ -x "$PY" ]] || { echo "FAIL: HLB virtualenv missing" >&2; exit 3; }

for unit in hermes-life-cognition.service hermes-life-contact.service hermes-life-percept-recovery.service; do
  systemctl --user is-active --quiet "$unit" || { echo "FAIL: $unit is not active" >&2; exit 4; }
done
systemctl --user is-active --quiet hermes-life-maintenance.timer || { echo "FAIL: maintenance timer is not active" >&2; exit 5; }

PYTHONPATH="$PLUGIN_DIR/src" "$PY" -m hermes_life_bridge.cli selftest >/dev/null
PYTHONPATH="$PLUGIN_DIR/src" "$PY" -m hermes_life_bridge.cli compatibility >/dev/null || true

DOCTOR_JSON="$(PYTHONPATH="$PLUGIN_DIR/src" "$PY" -m hermes_life_bridge.cli doctor)"
printf '%s' "$DOCTOR_JSON" | "$PY" -c '
import json, sys
report=json.load(sys.stdin)
if report.get("overall") == "BLOCKED":
    print(json.dumps(report, indent=2, ensure_ascii=False))
    raise SystemExit(2)
print("NANCY_ACCEPTANCE=PASS")
print("Overall:", report.get("overall"))
for name, item in report.get("components", {}).items():
    print("  {}: {}".format(name, item.get("status")))
'
