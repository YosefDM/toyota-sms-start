#!/usr/bin/env bash
# Linux rig supervisor for the GCP box (NOT the Windows dev machine — see supervisor.sh for that).
# Runs as the systemd service `toyota-rig` (enabled, so the whole rig comes up on VM reboot untended):
# boots the headless emulator, unlocks the keyguard, waits for frida-server, and keeps the Toyota app
# spawn-gated under the anti-tamper bypass (re-spawning if frida drops).
#
# frida-server itself is started by the Magisk boot MODULE `toyotafrida` (service.sh), which runs in
# magiskd's full-capability context — this is essential: plain `su` on this Magisk/Android build grants
# root with CapEff=0 (zero caps), so a su-started frida-server CANNOT load the SELinux policy it needs to
# spawn-inject. The module path gives it full caps. (That capless-su state, from a Magisk reset, was the
# root cause of the 2026-10-06 outage.)
#
# Install: copy to /home/Yosef/rig-supervisor.sh (chmod 755) + the toyota-rig.service unit, then
#   systemctl enable toyota-rig. Pairs with server/ (the TextGrid webhook that taps the running app).
set -u
export ANDROID_SDK_ROOT=/home/Yosef/android-sdk ANDROID_HOME=/home/Yosef/android-sdk HOME=/home/Yosef
ADB=/home/Yosef/android-sdk/platform-tools/adb
AVD="${AVD_NAME:-toyota_test}"
PIN="${EMU_PIN:-0000}"
BYPASS="${BYPASS_JS:-/home/Yosef/toyota_bypass.js}"
FRIDA="${FRIDA_BIN:-/home/Yosef/frida-venv/bin/frida}"
PKG=com.toyota.oneapp
log(){ echo "[$(date +%H:%M:%S)] $*"; }

# headless X display for the emulator (no GPU on the VM -> software GL)
pkill -f 'Xvfb :99' 2>/dev/null; sleep 1
setsid Xvfb :99 -screen 0 1280x720x24 >/tmp/xvfb99.log 2>&1 < /dev/null &
sleep 2

# fresh emulator (kill any stale instance + lockfiles first)
pkill -9 -f qemu-system 2>/dev/null; pkill -9 -f 'emulator/emulator' 2>/dev/null; pkill -9 -f crashpad 2>/dev/null
rm -f "/home/Yosef/.android/avd/$AVD.avd/"*.lock 2>/dev/null
sleep 2
log "booting emulator ($AVD, headless, software GPU)"
DISPLAY=:99 setsid "$ANDROID_HOME/emulator/emulator" -avd "$AVD" \
  -no-snapshot -gpu swiftshader_indirect -accel on -cores 4 -memory 6144 -no-audio -no-boot-anim \
  >/home/Yosef/emulator.log 2>&1 < /dev/null &

"$ADB" wait-for-device
until [ "$("$ADB" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ]; do sleep 2; done
log "boot_completed"
sleep 10   # let the Magisk module service.sh run (setenforce 0 + frida-server with full caps)

# unlock the keyguard (PIN) — required before the app (credential-encrypted storage) is spawnable
"$ADB" shell input keyevent 224 >/dev/null 2>&1; sleep 1
"$ADB" shell input keyevent 82 >/dev/null 2>&1; sleep 1
"$ADB" shell input swipe 360 1050 360 250 250 >/dev/null 2>&1; sleep 1
"$ADB" shell input text "$PIN" >/dev/null 2>&1; "$ADB" shell input keyevent 66 >/dev/null 2>&1; sleep 2
log "keyguard unlocked"

# wait for frida-server (started by the Magisk module, full caps)
for _ in $(seq 1 20); do
  "$ADB" shell su -c 'pidof frida-server' 2>/dev/null | tr -d '\r' | grep -q '[0-9]' && { log "frida-server up"; break; }
  sleep 2
done

# spawn-gate the app under the bypass, pinned (tail -f holds stdin so the frida REPL doesn't EOF and
# unload the hooks), and re-spawn if the session ever drops.
while true; do
  log "spawn-gating $PKG"
  "$ADB" shell am force-stop "$PKG" 2>/dev/null
  tail -f /dev/null | "$FRIDA" -U -f "$PKG" -l "$BYPASS"
  log "frida session ended; re-spawn in 8s"
  sleep 8
done
