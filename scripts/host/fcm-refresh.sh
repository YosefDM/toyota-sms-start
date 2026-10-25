#!/usr/bin/env bash
# Nightly FCM re-registration: force-stop the Toyota app so it re-registers its push token.
#
# WHY THIS EXISTS. Toyota's pushes are how we learn what the CAR actually did -- `await_result()`
# reads them out of the notification shade and puts the car's own words in the reply. Twice now the
# emulator's FCM registration has gone stale while everything else kept working: commands still
# reached the car, but every reply degraded to "accepted by the Toyota app, but the car's own
# confirmation didn't arrive". It is silent (nothing errors) and it does not self-heal -- the first
# outage ran 2026-10-11, the second from 2026-10-15 to 2026-10-23 and cost 13 unconfirmed commands.
#
# The fix both times was simply to restart the app: Firebase re-registers the token on launch. We do
# it on a schedule rather than waiting to notice, because noticing takes a human reading the journal.
#
# This script does NOT restart the app itself -- it only kills it. `rig-supervisor.sh` is sitting in
# a loop waiting for the frida session to drop, and re-spawns the app under the anti-tamper bypass
# within ~8s. Starting it any other way would launch it WITHOUT the bypass, which is worse than a
# stale token. That is also why this runs as Yosef: the adb server belongs to that user, and root
# would start a second one that cannot see the emulator.
#
# This is the blunt version of the watchdog the docs keep asking for (detect "the app logged success
# but no push arrived", then restart). A nightly restart costs nothing and covers the common case;
# the event-driven watchdog is still worth building.
set -uo pipefail

PKG="${TOYOTA_PKG:-com.toyota.oneapp}"
ADB="${ADB_PATH:-/home/Yosef/android-sdk/platform-tools/adb}"
SERIAL="${ADB_SERIAL:-emulator-5554}"
BUSY_WINDOW="${BUSY_WINDOW:-3 min ago}"
WAIT_SECS="${WAIT_SECS:-180}"

log() { echo "fcm-refresh: $*"; }
adb_() { "$ADB" -s "$SERIAL" "$@"; }

# Don't kill the app out from under a command that is mid-flight. 04:00 makes this very unlikely,
# but a dropped command is user-visible and the check is nearly free. The webhook logs a line for
# every inbound SMS/call and every dispatch, so recent traffic on that unit means "busy".
if journalctl -u toyota-sms --since "$BUSY_WINDOW" --no-pager 2>/dev/null \
     | grep -qE 'inbound (SMS|call)|dispatching'; then
  log "a command ran within the last '$BUSY_WINDOW' -- skipping tonight's restart"
  exit 0
fi

if ! adb_ get-state >/dev/null 2>&1; then
  log "ERROR: $SERIAL is not available to adb -- skipping"
  exit 1
fi

before="$(adb_ shell ps -A 2>/dev/null | grep -w "$PKG" | awk '{print $2}' | head -1)"
log "force-stopping $PKG (was pid ${before:-none})"
adb_ shell am force-stop "$PKG"

# Wait for the supervisor to bring it back, and report the NEW pid so the journal shows this
# actually cycled rather than silently doing nothing.
deadline=$(( SECONDS + WAIT_SECS ))
while (( SECONDS < deadline )); do
  sleep 5
  after="$(adb_ shell ps -A 2>/dev/null | grep -w "$PKG" | awk '{print $2}' | head -1)"
  if [[ -n "$after" && "$after" != "$before" ]]; then
    log "respawned as pid $after after $(( SECONDS - (deadline - WAIT_SECS) ))s"
    exit 0
  fi
done

# Use `ps -A | grep` to decide this, never `pgrep -f`: pgrep reports the Toyota app as absent on
# this build even while it is plainly running, which has already sent one investigation the wrong
# way. An empty answer from the wrong probe is not evidence.
log "ERROR: $PKG did not come back within ${WAIT_SECS}s -- check rig-supervisor"
exit 1
