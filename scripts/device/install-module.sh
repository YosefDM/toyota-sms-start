#!/usr/bin/env bash
# Install the boot-durability Magisk module onto the (rooted) emulator.
# Pushes: the module (module.prop/service.sh/install_ca.sh), your content-filter CA cert, and frida-server.
# Run once; then reboot the emulator and the module re-applies everything on every boot.
#
# Usage:  install-module.sh <path-to-ca-cert.0> <path-to-frida-server>
#   e.g.  install-module.sh ./6f2a8e1c.0 ./frida-server
# (CA cert optional — omit if the rig's network does not intercept TLS.)
set -eu
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL="*"
SDK="${ANDROID_HOME:-$LOCALAPPDATA/Android/Sdk}"; ADB="$SDK/platform-tools/adb.exe"
SER="${ADB_SERIAL:-emulator-5554}"
HERE="$(cd "$(dirname "$0")" && pwd)"
MOD="$HERE/magisk-module"
CA="${1:-}"; FRIDA="${2:-}"
DEST=/data/adb/modules/toyota_boot

echo "=== staging module to device ==="
"$ADB" -s "$SER" push "$MOD/module.prop" "$MOD/service.sh" "$MOD/install_ca.sh" /data/local/tmp/toyota_boot/
[ -n "$CA" ] && "$ADB" -s "$SER" push "$CA" /data/local/tmp/toyota_boot/
[ -n "$FRIDA" ] && "$ADB" -s "$SER" push "$FRIDA" /data/local/tmp/frida-server

echo "=== installing module (root) ==="
"$ADB" -s "$SER" shell "su -c '
  mkdir -p $DEST
  cp /data/local/tmp/toyota_boot/* $DEST/
  chmod 755 $DEST/service.sh $DEST/install_ca.sh
  chmod 644 $DEST/module.prop $DEST/*.0 2>/dev/null
  chmod 755 /data/local/tmp/frida-server 2>/dev/null
  rm -rf /data/local/tmp/toyota_boot
  ls -la $DEST
'"
echo "=== installed. Reboot the emulator to activate (service.sh runs late_start). ==="
