#!/usr/bin/env bash
# Boot the Toyota emulator cleanly. Windows/Git-Bash; adapt SDK path + process-kill for Linux.
set -u
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL="*"
SDK="${ANDROID_HOME:-$LOCALAPPDATA/Android/Sdk}"
AVD="${AVD_NAME:-toyota_test}"
MODE="${1:-headful}"   # headful | headless

# 1) Kill stale instances + locks (the headless proc is qemu-system-x86_64-headless.exe!)
#    On Windows run this via PowerShell; on Linux: pkill -f qemu-system; rm -f locks.
if command -v powershell.exe >/dev/null 2>&1; then
  powershell.exe -NoProfile -Command "Get-Process | ? {\$_.Name -like 'qemu*' -or \$_.Name -like '*emulator*' -or \$_.Name -like 'crashpad*'} | Stop-Process -Force -ErrorAction SilentlyContinue" 2>/dev/null
fi
rm -f "$HOME/.android/avd/$AVD.avd/"*.lock 2>/dev/null

# 2) Boot — software GPU (NVIDIA hosts crash on -gpu auto/gfxstream); writable-system for root/CA work.
WIN="-no-window"; [ "$MODE" = "headful" ] && WIN=""
"$SDK/emulator/emulator.exe" -avd "$AVD" -no-snapshot-load -writable-system \
  -gpu swiftshader_indirect $WIN -no-boot-anim &

# 3) Wait for boot
ADB="$SDK/platform-tools/adb.exe"
"$ADB" wait-for-device
until [ "$("$ADB" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ]; do sleep 2; done
echo "booted."

# 4) Per-boot device setup (root). SELinux permissive is REQUIRED for frida spawn-gating.
"$ADB" shell "su -c 'setenforce 0'"
"$ADB" shell "su -c 'nohup /data/local/tmp/frida-server -D >/dev/null 2>&1 &'"
"$ADB" shell "svc power stayon true"
# (filtered-network only) re-inject CA + disable captive-portal — uncomment if behind a MITM filter:
# "$ADB" push ../device/install_ca.sh /data/local/tmp/ && "$ADB" shell "su -c 'sh /data/local/tmp/install_ca.sh'"
# "$ADB" shell "su -c 'settings put global captive_portal_mode 0; settings put global captive_portal_detection_enabled 0'"
echo "device setup done (setenforce 0, frida-server, stayon)."
