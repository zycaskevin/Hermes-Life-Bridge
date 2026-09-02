#!/usr/bin/env bash
set -Eeuo pipefail
HOURS="${1:-24}"
case "$HOURS" in
  1|24|72) ;;
  *) echo "Usage: $0 {1|24|72}" >&2; exit 2 ;;
esac
HERMES_CONFIG="$(hermes config path 2>/dev/null || true)"
if [[ -n "$HERMES_CONFIG" ]]; then HERMES_HOME="$(dirname "$HERMES_CONFIG")"; else HERMES_HOME="$HOME/.hermes"; fi
PLUGIN_DIR="$HERMES_HOME/plugins/hermes-life-bridge"
STATE_HOME="${XDG_STATE_HOME:-$HOME/.local/state}"
OUTPUT="$STATE_HOME/hermes-life-bridge/soak-${HOURS}h.jsonl"
PYTHONPATH="$PLUGIN_DIR/src" "$PLUGIN_DIR/.venv/bin/python" -m hermes_life_bridge.soak \
  --monitor-hours "$HOURS" --interval 60 --output "$OUTPUT"
echo "SOAK_${HOURS}H=PASS"
echo "Report: $OUTPUT"
