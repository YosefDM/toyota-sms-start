#!/system/bin/sh
# Re-establish system trust of the content-filter CA at boot (HTTPToolkit tmpfs method).
# Only needed behind a TLS-intercepting filter; the cert file is dropped next to this script at install
# time (gitignored, never committed). No-op if the cert isn't present.
MODDIR=${0%/*}
CERT=$(ls "$MODDIR"/*.0 2>/dev/null | head -1)
[ -z "$CERT" ] && { echo "no CA cert in module; skipping"; exit 0; }

SYSCA=/system/etc/security/cacerts
APEXCA=/apex/com.android.conscrypt/cacerts
TMP=/data/local/tmp/cacerts_boot

rm -rf "$TMP"; mkdir -p "$TMP"
cp "$APEXCA"/* "$TMP"/ 2>/dev/null          # seed with the stock trust store
mount -t tmpfs tmpfs "$SYSCA"               # writable overlay apps can actually read
cp "$TMP"/* "$SYSCA"/ 2>/dev/null
cp "$CERT" "$SYSCA"/                         # add our CA
chown root:root "$SYSCA"/* 2>/dev/null
chmod 644 "$SYSCA"/* 2>/dev/null
chcon u:object_r:system_file:s0 "$SYSCA"/* 2>/dev/null

# bind the populated store over the read-only APEX store in init + both zygote namespaces,
# so every app forked afterward inherits the added CA
for P in 1 $(pidof zygote) $(pidof zygote64); do
  [ -n "$P" ] && nsenter --mount=/proc/"$P"/ns/mnt -- mount --rbind "$SYSCA" "$APEXCA" 2>/dev/null
done
echo "CA trust re-applied ($(ls "$SYSCA" | wc -l) certs)"
