#!/usr/bin/env bash
# Host supervisor for the always-on rig. Boots the emulator, unlocks it, and keeps the Toyota app
# spawn-gated under a PERSISTENT frida session (re-spawning if it drops). The on-device boot setup
# (SELinux/CA/captive-portal/frida-server) is handled by the Magisk module (see scripts/device/).
#
# Pairs with the SMS bridge (server/) which performs the actual UI taps on the running app.
set -u
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL="*"
SDK="${ANDROID_HOME:-$LOCALAPPDATA/Android/Sdk}"
ADB="$SDK/platform-tools/adb.exe"
SER="${ADB_SERIAL:-emulator-5554}"
AVD="${AVD_NAME:-toyota_test}"
PIN="${EMU_PIN:-0000}"                      # the device lockscreen PIN (override via env; don't hardcode secrets)
HERE="$(cd "$(dirname "$0")" && pwd)"
FRIDA_DIR="$HERE/../frida"
FRIDA_BIN="${FRIDA_BIN:-frida}"             # version-matched to frida-server
PKG="com.toyota.oneapp"
win(){ cygpath -m "$1" 2>/dev/null || echo "$1"; }  # frida.exe needs Windows-form -l paths

log(){ echo "[$(date +%H:%M:%S)] $*"; }

kill_stale(){
  if command -v powershell.exe >/dev/null 2>&1; then
    powershell.exe -NoProfile -Command "Get-Process | ? {\$_.Name -like 'qemu*' -or \$_.Name -like '*emulator*' -or \$_.Name -like 'crashpad*'} | Stop-Process -Force -ErrorAction SilentlyContinue" 2>/dev/null
  else pkill -f qemu-system 2>/dev/null; fi
  rm -f "$HOME/.android/avd/$AVD.avd/"*.lock 2>/dev/null
}

boot_emulator(){
  log "booting emulator ($AVD, software GPU)…"
  "$SDK/emulator/emulator.exe" -avd "$AVD" -no-snapshot-load -writable-system \
    -gpu swiftshader_indirect -no-boot-anim >/dev/null 2>&1 &
  "$ADB" -s "$SER" wait-for-device
  until [ "$("$ADB" -s "$SER" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ]; do sleep 2; done
  log "boot_completed."
  sleep 8   # give the Magisk service.sh time to run (setenforce/CA/frida-server)
}

unlock(){
  log "unlocking (PIN)…"
  "$ADB" -s "$SER" shell input keyevent 224 >/dev/null 2>&1; sleep 1   # wake
  "$ADB" -s "$SER" shell input swipe 540 1700 540 400 >/dev/null 2>&1; sleep 1  # reveal PIN pad
  "$ADB" -s "$SER" shell input text "$PIN" >/dev/null 2>&1
  "$ADB" -s "$SER" shell input keyevent 66 >/dev/null 2>&1; sleep 2  # enter
  # NOTE: if `input text` doesn't register on the secure keyguard, fall back to tapping digits
  # (the PIN-pad key coordinates — re-dump with uiautomator). Documented in docs/PRODUCTION.md.
}

wait_frida_server(){
  log "waiting for frida-server (started by the Magisk module)…"
  for _ in $(seq 1 30); do
    "$ADB" -s "$SER" shell "su -c 'pidof frida-server'" 2>/dev/null | tr -d '\r' | grep -q '[0-9]' && { log "frida-server up."; return 0; }
    sleep 2
  done
  log "WARN: frida-server not detected; attempting to start it."
  "$ADB" -s "$SER" shell "su -c 'nohup /data/local/tmp/frida-server -D >/dev/null 2>&1 &'" 2>/dev/null
}

spawn_app_persistent(){
  # Keep frida attached for the whole uptime; hooks unload if it detaches. `tail -f /dev/null`
  # holds stdin open so the frida REPL doesn't EOF-exit. Re-spawn if it ever drops.
  local args=(-U -f "$PKG" -l "$(win "$FRIDA_DIR/toyota_bypass.js")")
  [ -f "$FRIDA_DIR/combined_unpin.js" ] && args=(-U -f "$PKG" -l "$(win "$FRIDA_DIR/combined_unpin.js")" -l "$(win "$FRIDA_DIR/toyota_bypass.js")")
  while true; do
    log "spawn-gating $PKG under frida…"
    tail -f /dev/null | "$FRIDA_BIN" "${args[@]}"
    log "frida session ended (code $?); re-spawning in 5s."
    sleep 5
  done
}

trap 'log "supervisor stopping"; exit 0' INT TERM
kill_stale
boot_emulator
unlock
wait_frida_server
spawn_app_persistent
