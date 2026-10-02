#!/system/bin/sh
# Android 14/15 system CA injection (HTTPToolkit method, adapted for API 35).
# Trusts the content filter's intermediate CA so the emulator's TLS chain validates.
set -u
CERT=/data/local/tmp/6f2a8e1c.0
SYSCA=/system/etc/security/cacerts
APEXCA=/apex/com.android.conscrypt/cacerts

[ -f "$CERT" ] || { echo "NO_CERT_FILE"; exit 1; }

# 1) Snapshot the live apex trust store
rm -rf /data/local/tmp/tmp-ca-copy
mkdir -p -m 755 /data/local/tmp/tmp-ca-copy
cp "$APEXCA"/* /data/local/tmp/tmp-ca-copy/ 2>/dev/null
echo "snapshot=$(ls /data/local/tmp/tmp-ca-copy | wc -l)"

# 2) tmpfs OVER the real /system cacerts dir (readable by apps, proper fs)
mount -t tmpfs tmpfs "$SYSCA" && echo "TMPFS_OK"
cp /data/local/tmp/tmp-ca-copy/* "$SYSCA"/ 2>/dev/null
cp "$CERT" "$SYSCA"/
chown root:root "$SYSCA"/*
chmod 644 "$SYSCA"/*
chcon u:object_r:system_file:s0 "$SYSCA"/*
echo "sysca_count=$(ls "$SYSCA" | wc -l)  ourcert=$(ls "$SYSCA"/6f2a8e1c.0 2>/dev/null)"

# 3) Bind that populated dir OVER the apex store in every relevant mount namespace
ZPID=$(pidof zygote 2>/dev/null)
Z64PID=$(pidof zygote64 2>/dev/null)
for P in 1 $ZPID $Z64PID; do
  [ -n "$P" ] && nsenter --mount=/proc/$P/ns/mnt -- mount --rbind "$SYSCA" "$APEXCA" 2>/dev/null && echo "bound ns pid=$P"
done

# 4) Also inject into already-running app processes (children of zygote)
for P in $(ls /proc | grep -E '^[0-9]+$'); do
  cl=$(cat /proc/$P/cmdline 2>/dev/null | tr '\0' ' ')
  case "$cl" in
    *com.toyota.oneapp*|*system_server*|*com.android*)
      nsenter --mount=/proc/$P/ns/mnt -- mount --rbind "$SYSCA" "$APEXCA" 2>/dev/null && echo "bound app pid=$P ($cl)"
    ;;
  esac
done
echo "DONE"
