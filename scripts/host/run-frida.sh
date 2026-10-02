#!/usr/bin/env bash
# Spawn-gate the Toyota app with the TLS-unpinning + root-bypass hooks and KEEP ATTACHED.
# Host `frida` must match the device frida-server version (we used 17.18.0).
set -u
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL="*"
HERE="$(cd "$(dirname "$0")" && pwd)"
FRIDA_DIR="$HERE/../frida"
FRIDA_BIN="${FRIDA_BIN:-frida}"   # or the venv path, e.g. .../toyvenv2/Scripts/frida.exe
PKG="com.toyota.oneapp"

# combined_unpin.js = config.js (with your CERT_PEM) + httptoolkit android-certificate-unpinning.js.
# These 3rd-party scripts are NOT in the repo — fetch once (see docs/SETUP.md §6) and build combined:
#   curl -L -o "$FRIDA_DIR/android-certificate-unpinning.js" \
#     https://raw.githubusercontent.com/httptoolkit/frida-interception-and-unpinning/main/android/android-certificate-unpinning.js
#   cp "$FRIDA_DIR/config.example.js" "$FRIDA_DIR/config.js"   # then fill CERT_PEM
#   cat "$FRIDA_DIR/config.js" "$FRIDA_DIR/android-certificate-unpinning.js" > "$FRIDA_DIR/combined_unpin.js"
# On an UNFILTERED network (no MITM) you can SKIP the unpinning entirely and load only toyota_bypass.js.

ARGS=(-U -f "$PKG" -l "$FRIDA_DIR/toyota_bypass.js")
[ -f "$FRIDA_DIR/combined_unpin.js" ] && ARGS=(-U -f "$PKG" -l "$FRIDA_DIR/combined_unpin.js" -l "$FRIDA_DIR/toyota_bypass.js")

# Stays attached in the foreground — supervise this process; the hooks live only while it's attached.
exec "$FRIDA_BIN" "${ARGS[@]}"
