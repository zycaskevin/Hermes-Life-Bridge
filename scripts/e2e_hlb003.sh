#!/usr/bin/env bash
set -euo pipefail

[[ "${HLB_CONTACT_E2E_REAL_SEND:-}" == "YES" ]] || {
  echo "REFUSED: set HLB_CONTACT_E2E_REAL_SEND=YES for the one-shot real delivery test."
  exit 2
}

LR_ROOT="${LIVE_RUNTIME_INSTALL_DIR:-$HOME/Projects/live-runtime}"
STATE_DB="${XDG_STATE_HOME:-$HOME/.local/state}/nancy-live-runtime/runtime.sqlite3"
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/hermes-life-bridge.env"
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
SOCKET="$RUNTIME_DIR/hermes-life-contact.sock"
ROUTE_FILE="${HLB_ROUTE_PATH:-${XDG_STATE_HOME:-$HOME/.local/state}/hermes-life-bridge/last_route.json}"
TARGET="${HLB_CONTACT_E2E_TARGET:-}"

[[ -d "$LR_ROOT" ]] || { echo "FAIL: Life Runtime repo missing"; exit 3; }
[[ -f "$CONFIG" ]] || { echo "FAIL: HLB config missing"; exit 4; }
[[ -S "$SOCKET" ]] || { echo "FAIL: HLB contact socket missing"; exit 5; }

if [[ -z "$TARGET" ]]; then
  [[ -f "$ROUTE_FILE" ]] || {
    echo "FAIL: no normalized Hermes route available yet."
    echo "Send one normal Gateway message after Hermes reload, then rerun."
    exit 6
  }
  TARGET="$(python3 - "$ROUTE_FILE" <<'PY_ROUTE'
import json, pathlib, sys
data=json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
print(data.get("target") or "")
PY_ROUTE
)"
fi
[[ -n "$TARGET" ]] || { echo "FAIL: normalized route has no target"; exit 7; }
case "$TARGET" in
  *"SessionSource("*|*"sessionsource("*|*"Platform."*)
    echo "FAIL: refusing non-canonical SessionSource/Enum repr target"
    exit 8
    ;;
esac

backup="${CONFIG}.before-hlb003-e2e.$(date +%Y%m%d-%H%M%S)"
cp "$CONFIG" "$backup"

restore() {
  cp "$backup" "$CONFIG"
  systemctl --user restart hermes-life-contact.service >/dev/null 2>&1 || true
}
trap restore EXIT

python3 - "$CONFIG" "$TARGET" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); target=sys.argv[2]
lines=p.read_text(encoding="utf-8").splitlines()
wanted={"HLB_CONTACT_DELIVERY_ENABLED":"true","HLB_CONTACT_TARGET":target}
out=[]; seen=set()
for line in lines:
    if "=" in line and not line.lstrip().startswith("#"):
        k=line.split("=",1)[0].strip()
        if k in wanted:
            out.append(f"{k}={wanted[k]}"); seen.add(k); continue
    out.append(line)
for k,v in wanted.items():
    if k not in seen: out.append(f"{k}={v}")
p.write_text("\n".join(out)+"\n",encoding="utf-8")
PY

systemctl --user restart hermes-life-contact.service
for i in $(seq 1 50); do [[ -S "$SOCKET" ]] && break; sleep 0.2; done
[[ -S "$SOCKET" ]] || { echo "FAIL: contact service did not return"; exit 7; }

STAMP="$(date -Iseconds)"
MESSAGE="HLB-003 E2E PASS candidate — Nancy proactive contact delivery path is connected. ${STAMP}"

cd "$LR_ROOT"
PYTHONPATH=src .venv/bin/python -m live_runtime.cli contact-selftest \
  --db "$STATE_DB" \
  --socket "$SOCKET" \
  --target "$TARGET" \
  --message "$MESSAGE" \
  --timeout 30

echo
echo "ONE_SHOT_SEND_COMPLETE=YES"
echo "TARGET_PLATFORM=${TARGET%%:*}"
echo "TARGET_HAS_CHAT_ID=$([[ "$TARGET" == *:* ]] && echo YES || echo NO)"
echo "Config will now be restored to delivery-disabled by trap."
