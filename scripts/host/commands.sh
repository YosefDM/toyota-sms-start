#!/usr/bin/env bash
# Perform a car action by LONG-PRESSING the app's button. UI ONLY — never an API call (hard rule).
# Usage: commands.sh start|lock|unlock|lock_trunk|unlock_trunk|lights|horn|buzzer|hazards
#
# Thin wrapper over server/toyota_control.py so this manual helper uses the SAME, tested logic as the
# TextGrid bridge: it locates each control by RESOURCE-ID (resolution-independent) with the guard model
# (verify the Advanced Remote panel is up, refuse if not), instead of blind coordinates.
set -eu
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL="*"
SDK="${ANDROID_HOME:-$LOCALAPPDATA/Android/Sdk}"
export ADB_PATH="${ADB_PATH:-$SDK/platform-tools/adb.exe}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export SERVER="$HERE/../../server"

cmd="${1:?usage: commands.sh <action>}"

python - "$cmd" <<'PY'
import os, sys
sys.path.insert(0, os.environ["SERVER"])
import toyota_control as tc

cmd = sys.argv[1]
if cmd not in tc.COMMAND_IDS:
    sys.exit(f"unknown action: {cmd} (choose from: {', '.join(tc.COMMAND_IDS)})")
print(f"long-pressing '{cmd}' by resource-id (hold {tc.HOLD_MS}ms)…")
since = tc.execute(cmd)
res = tc.await_result(since)
print(res["text"] if res and res.get("text") else "(sent; no push confirmation within timeout)")
PY
