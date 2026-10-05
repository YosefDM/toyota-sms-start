#!/system/bin/sh
# Magisk late_start service — runs as root after boot. Re-applies everything the rig needs so it
# survives a reboot unattended. (POSIX sh; runs on the device, not the host.)
MODDIR=${0%/*}

# wait until the framework is fully up
while [ "$(getprop sys.boot_completed)" != "1" ]; do sleep 2; done
sleep 5

LOG=/data/local/tmp/toyota_boot.log
echo "=== toyota_boot service $(date) ===" > "$LOG"

# 1) SELinux permissive — required for frida-server to spawn-gate (ptrace zygote)
setenforce 0 2>>"$LOG" && echo "setenforce 0 ok" >>"$LOG"

# 2) captive-portal detection off — on a TLS-intercepting network the probe fails and the net is
#    marked PARTIAL_CONNECTIVITY, which makes apps think there's no internet. (Persisted in settings,
#    re-applied here to be safe.)
settings put global captive_portal_mode 0 2>>"$LOG"
settings put global captive_portal_detection_enabled 0 2>>"$LOG"
echo "captive-portal off" >>"$LOG"

# 3) trust the content-filter CA (only matters behind a TLS-intercepting filter; no-op if cert absent)
[ -f "$MODDIR/install_ca.sh" ] && sh "$MODDIR/install_ca.sh" >>"$LOG" 2>&1

# 4) keep the screen on while plugged in
svc power stayon true 2>>"$LOG" && echo "stayon true" >>"$LOG"

# 5) start frida-server (root). The host supervisor waits for this, then spawn-gates the app.
if [ -f /data/local/tmp/frida-server ]; then
  chmod 755 /data/local/tmp/frida-server
  killall frida-server 2>/dev/null
  nohup /data/local/tmp/frida-server -D >/dev/null 2>&1 &
  echo "frida-server started" >>"$LOG"
fi

echo "=== done $(date) ===" >>"$LOG"
