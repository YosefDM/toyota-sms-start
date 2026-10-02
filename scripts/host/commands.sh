#!/usr/bin/env bash
# Perform a car action by LONG-PRESSING the app's button. UI ONLY — never an API call (hard rule).
# Usage: commands.sh start|lock|unlock|lock_trunk|unlock_trunk|lights|horn|buzzer|hazards
#
# Assumes the app is on the "Advanced Remote" panel. Coordinates are for pixel_7 (1080w) / software GPU;
# RE-DUMP with `uiautomator dump` if the layout/resolution/build changes — do NOT trust these across builds.
set -u
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL="*"
SDK="${ANDROID_HOME:-$LOCALAPPDATA/Android/Sdk}"
ADB="$SDK/platform-tools/adb.exe"
HOLD_MS="${HOLD_MS:-3500}"

declare -A X=( [start]=208 [lock]=540 [unlock]=871 [lock_trunk]=208 [unlock_trunk]=540 [lights]=871 [horn]=208 [buzzer]=540 [hazards]=871 )
declare -A Y=( [start]=650 [lock]=650 [unlock]=650 [lock_trunk]=932 [unlock_trunk]=932 [lights]=932 [horn]=1214 [buzzer]=1214 [hazards]=1214 )

cmd="${1:?usage: commands.sh <action>}"
[ -z "${X[$cmd]:-}" ] && { echo "unknown action: $cmd"; exit 1; }

# TODO (hardening): verify we're actually on the Advanced Remote panel via uiautomator before pressing;
# and handle the "Sending..." stuck-UI state (treat the CAR as source of truth, not the dialog).
echo "long-pressing $cmd at (${X[$cmd]},${Y[$cmd]}) for ${HOLD_MS}ms"
"$ADB" shell input swipe "${X[$cmd]}" "${Y[$cmd]}" "${X[$cmd]}" "${Y[$cmd]}" "$HOLD_MS"
