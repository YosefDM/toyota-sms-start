# SETUP — building the rig from scratch

Every step we actually ran, **with the gotcha we hit and the fix**, in order. Host was Windows 11 + an
NVIDIA RTX GPU, using Git-Bash for POSIX commands and PowerShell for process management. Adapt paths.

> Legend: 🧱 = a wall we hit, ✅ = the fix.

---

## 0. Host tooling

- **Android SDK command-line tools.**
  🧱 The first `commandlinetools` build we downloaded was too old to read Google's current (XML v4)
  repository — `sdkmanager --list` returned almost nothing.
  ✅ Download the **latest** build (we used `commandlinetools-win-16111833`). Also note the new
  `android.exe sdk install` CLI uses **slash** package paths (`system-images/android-35/...`), while the
  old `sdkmanager` used **semicolons** (`system-images;android-35;...`). Mixing them silently fails.
  🧱 `curl` on Windows/Git-Bash failed TLS with `CRYPT_E_NO_REVOCATION_CHECK`.
  ✅ Add `--ssl-no-revoke` to every curl.

- **JDK:** the Android Studio bundled JBR works (`.../Android Studio/jbr`).
  🧱 `avdmanager.bat` needs `JAVA_HOME` in **Windows** path form (`C:\...`), not the MSYS `/c/...` form,
  or it prints "Please set JAVA_HOME".

- **Node** (for Appium later), **Python 3.12**.
  🧱 A Python venv we used for Frida got **half-installed** after an interrupted download — `pip`,
  `colorama`, `prompt_toolkit`, `websockets` were empty dirs, so `frida` threw `ImportError`/namespace
  errors and later `frida.exe` wouldn't run at all.
  ✅ Don't trust a venv built over a flaky connection. We recreated it clean (`python -m venv`, then
  `pip install frida-tools`). **Pin `frida` to the frida-server version** (see §3).

---

## 1. System image + AVD

- **Use a Google-Play image** (needs Play Services for the app): `system-images;android-35;google_apis_playstore;x86_64`.
  🧱 The `android.exe sdk install` of the 1.76 GB image kept failing mid-download with
  `Failed to read HTTP response: Peer disconnected` (no resume) on our connection.
  ✅ Download the image zip directly with resume:
  `curl -L --ssl-no-revoke -C - --retry 20 --retry-all-errors -o img.zip <sys-img url>`, verify SHA-1
  against the repo manifest, then extract into
  `$SDK/system-images/android-35/google_apis_playstore/x86_64/` (the zip already contains a `x86_64/`
  dir with its own `source.properties`, so `sdkmanager` then recognizes it as installed).
- Create the AVD: `avdmanager create avd -n toyota_test -k "system-images;android-35;google_apis_playstore;x86_64" --device pixel_7`.
- Tune `~/.android/avd/toyota_test.avd/config.ini`: `hw.ramSize=4096`, `disk.dataPartition.size=8192M`,
  `hw.keyboard=yes`, `hw.cpu.ncore=4`.

- **ABI note (important):** the Toyota app ships **arm64-only** native libs (`libflutter.so`, `libapp.so`,
  `libssl.so`...). The x86_64 Play image includes an **arm64 translation layer**
  (`ro.product.cpu.abilist=x86_64,arm64-v8a`), so the app runs. This has consequences for Frida (see §6).
  A pure x86_64 app build does not exist (Play won't serve x86_64 splits to any real device profile).

### Emulator stability (NVIDIA host) 🧱🧱
- `-gpu auto` (hardware/gfxstream) **crashed** the emulator repeatedly (crashpad) on the NVIDIA box.
  ✅ Boot with **software GPU**: `-gpu swiftshader_indirect`. Stable. (Flutter app renders fine on it,
  just slower; the only app crash under software GPU was actually the cert bug in §5, not the GPU.)
- 🧱 "FATAL: Running multiple emulators with the same AVD." Stale/zombie emulator processes held the AVD
  lock. Note the headless process is named **`qemu-system-x86_64-headless.exe`** (not `emulator.exe`), so
  a partial kill leaves it running.
  ✅ Before every boot: `Get-Process | ? {$_.Name -like 'qemu*' -or $_.Name -like '*emulator*' -or $_.Name -like 'crashpad*'} | Stop-Process -Force`
  then `Remove-Item ~/.android/avd/toyota_test.avd/*.lock -Force`.

See `scripts/host/boot-emulator.sh`.

---

## 2. Root (Magisk via rootAVD)

- Clone `rootAVD` (gitlab.com/newbit/rootAVD). Run it against the image's `ramdisk.img`.
  🧱 On Git-Bash, rootAVD mangled on-device paths like `/data/...` into `C:/Program Files/Git/data/...`
  (MSYS path conversion).
  ✅ Export `MSYS_NO_PATHCONV=1` and `MSYS2_ARG_CONV_EXCL="*"` for **every** adb/rootAVD command.
  🧱 rootAVD's Magisk-version menu is **interactive**; piping `yes` sent "y" which it rejected forever.
  ✅ Pipe the actual choice: `printf '1\n'` (local stable Magisk 26.4 — bundled, no download).
  🧱 Final step "Install/Patch fakeboot.img in the Magisk app, then hit Enter" is **manual GUI**.
  ✅ We did it **headlessly**: run Magisk's own `assets/boot_patch.sh` via an adb-root shell on
  `/sdcard/Download/fakeboot.img` (stage `util_functions.sh`+`stub.apk`+`magiskboot`+`magisk64`+`magiskinit`
  in one dir, `KEEPVERITY=true KEEPFORCEENCRYPT=true`), get `new-boot.img`, copy it to
  `/sdcard/Download/magisk_patched.img`, then re-run rootAVD — it auto-detects the existing
  `magisk_patched*` and finishes the repack+install with no GUI.
- Cold-boot. The host `ramdisk.img` is now patched and **shared across AVD instances** (survives AVD
  re-creation — handy after a wipe).

### Getting `su` to actually work 🧱🧱
- Magisk app first run: tap **"Requires Additional Setup" → OK** (patches + reboots).
- 🧱 `su` from `adb shell` returned **Permission denied** even with Automatic-Response = Grant.
  ✅ On Magisk 26.4/Android-15 the global auto-grant doesn't cover the ADB shell's **SharedUID**. Open
  **Magisk → Superuser tab → toggle the `[SharedUID] Shell` switch ON** (the per-app switch,
  `resource-id=policy_indicator`). Then `adb shell su -c id` → uid 0.
- Enable **Zygisk** in Magisk settings (+reboot) if you later want DenyList/root-hiding. (Not required for
  the current UI-tap approach; Play Integrity is NOT used by Toyota for commands — see FINDINGS.)
- We drove all these Magisk GUI steps via `uiautomator dump` → parse `bounds` → `adb input tap`.

### 🧱 Do NOT corrupt SELinux labels
We once ran a `chcon`/`chmod` with an **empty shell variable**, so `chcon ... "$TMP"/*` became
`chcon ... /*` and relabeled `/system`, breaking `/system/bin/sh` → every command returned
"Permission denied" and the AVD wouldn't boot (even after reboot, because the writable-system overlay
persists the bad labels). ✅ Recovery that worked: **delete and recreate the AVD** (the on-disk image is
intact; the patched `ramdisk.img` persists so Magisk is still there — just re-do the Magisk app setup +
Shell toggle). Lesson: **never run globbed `chcon`/`chmod`/`rm` with a possibly-empty variable**; always
use a script file with set, quoted paths.

---

## 3. Frida

- Host: `pip install frida-tools`. Device: push the matching **x86_64** `frida-server`
  (we used **17.18.0**) to `/data/local/tmp`, `chmod 755`, run as root.
  ✅ **Host `frida` must match the server version exactly** — `pip install frida==17.18.0`.
- 🧱 `frida -f ... ` failed: **"need Gadget to attach on jailed Android"** = frida-server couldn't
  spawn-gate (ptrace zygote) under enforcing SELinux.
  ✅ `adb shell su -c 'setenforce 0'` (SELinux permissive) before spawning. (This is a per-boot setting;
  make it persistent in PRODUCTION.) frida-server itself must be **root** (uid 0).
- 🧱 Frida loads multiple `-l` scripts in **separate scopes** — a `const` in one isn't visible to another.
  ✅ The HTTPToolkit `config.js` + `android-certificate-unpinning.js` must be **concatenated into one
  file** (`cat config.js android-certificate-unpinning.js > combined_unpin.js`). Our `toyota_bypass.js`
  is self-contained and can be a separate `-l`.

---

## 4. Install the Toyota app

- Pulled the APK + splits from Play with `apkeep` (authenticated as the account). It's a **split APK**
  (base + `config.arm64_v8a` + `config.xxhdpi`). Install all together:
  `adb install-multiple base.apk config.arm64_v8a.apk config.xxhdpi.apk`.
  🧱 Installing only the base crashes (missing the Flutter engine `.so` in the arm64 split).
- 🧱 After a `pm clear`, the first launch ANRs on `SplashActivity` (Flutter re-extracting assets under
  software GPU). ✅ Tap **"Wait"** on the ANR; it recovers. Avoid `pm clear` unless necessary.
- 🧱 Once the app showed `enabled=0` (disabled) and its launcher activity wouldn't resolve
  ("No activity found", frida "unable to find a front-door activity").
  ✅ `adb shell su -c 'pm enable com.toyota.oneapp'`.

---

## 5. Device PIN + the trust store (only needed behind a TLS-intercepting filter)

**Device PIN** (the app refuses to run without a secure lock):
- 🧱 Setting the PIN via `adb locksettings set-pin` left it half-registered (`password_type` stayed null,
  `dumpsys trust` showed `trustManaged=0`, `isDeviceSecure()` false) and the app's "requires a device
  unlock PIN" dialog persisted.
- ✅ Set it through the **Settings UI** (or `cmd lock_settings set-pin 0000` as root cleanly on a fresh
  device — the modern command; `verify --old` must say "verified successfully"). `isDeviceSecure()` then
  returns true. (`password_type=null` is a red herring on Android 15 — it's just a deprecated field.)

**CA trust** — ONLY if the rig's network does TLS interception (our home network runs **the content filter**, a
content filter that MITMs all HTTPS). On an unfiltered production network you can SKIP this whole section.
- 🧱 Android 14/15 keeps system CAs in the read-only **Conscrypt APEX**. Our first attempt bind-mounted a
  `/data/local/tmp` dir over the apex cacerts → conscrypt's `shouldUseApex` did `.list()` on a dir apps
  can't read → returned null → **NPE that crashed every TLS app** (including Toyota) at startup.
- ✅ Use the **HTTPToolkit tmpfs method** (`scripts/device/install_ca.sh`): tmpfs **over
  `/system/etc/security/cacerts`**, copy the apex certs + your CA into it, `chcon u:object_r:system_file:s0`,
  then `nsenter --rbind` that dir over `/apex/com.android.conscrypt/cacerts` in the **init + zygote +
  zygote64 + every app** mount namespace. Restart zygote (`setprop ctl.restart zygote`) so new apps inherit
  it; the bind-mount survives a zygote restart (not a full reboot).
- 🧱 The CA file itself must be **valid PEM** — we first built it as `openssl x509 -text` (human dump)
  **before** the PEM, which `DirectoryCertificateSrc` can't parse (`ASN.1 DECODE_ERROR`), silently breaking
  the store for the sign-in/WebView TLS path.
  ✅ The cert file must be **pure PEM starting with `-----BEGIN CERTIFICATE-----`** (we just used the
  clean intermediate cert).

---

## 6. TLS unpinning (only behind a MITM filter) 🧱🧱🧱

The app **pins** its certificate, so even with the filter's CA system-trusted it rejected the connection
("Unsafe Network Detected"). What finally worked:

- The app's API uses the **Java OkHttp** stack over system `libssl.so` — **not** Flutter's BoringSSL and
  **not** the white-box engine (`libuie-sslengine.so`/`libwbcmjni.so` aren't even loaded for the API).
  So the pinning is **Java-layer** (OkHttp `CertificatePinner` / TrustManager) = easily hookable.
- 🧱 We first chased the **Flutter BoringSSL** path (NVISO `disable-flutter-tls.js`). Its
  `Interceptor.replace` of `ssl_verify_peer_cert` **crashed** the app — because our libs are **arm64 under
  x86 translation** and replacing a translated function has an ABI mismatch. `Interceptor.attach`+`onLeave`
  (override the return instead of replacing) doesn't crash, but it was the **wrong layer** anyway (no effect).
- ✅ Use the **HTTPToolkit `android-certificate-unpinning.js`** (Java hooks: OkHttp `CertificatePinner`,
  `TrustManagerImpl`, `NetworkSecurityConfig`, `HostnameVerifier`, etc. — all through ART, immune to the
  ARM-translation problem). Concatenate with `config.js` (§3). Set `CERT_PEM` to the filter's CA.
  🧱 `config.js` `CERT_PEM` must start **exactly** with `-----BEGIN CERTIFICATE-----` on line 0 —
  a leading newline makes its `pemToDer` throw "certificate should be in PEM format".
- Launch: `frida -U -f com.toyota.oneapp -l combined_unpin.js -l toyota_bypass.js` (see `scripts/host/run-frida.sh`).

---

## 7. Network "validated" (only behind a MITM filter) 🧱

After unpinning, the app showed **"Downtime — technical difficulties"**. Cause: Android marked the network
`PARTIAL_CONNECTIVITY` (not `VALIDATED`) because the captive-portal probe (to gstatic) also goes through the
filter's MITM and gets the wrong cert → Chrome AND Toyota believed there was "no internet."
✅ Disable the probe as root, then re-validate:
```
settings put global captive_portal_mode 0
settings put global captive_portal_detection_enabled 0
svc data disable; svc data enable     # (or toggle wifi) to re-trigger validation
```
`dumpsys connectivity` should then show `VALIDATED`.

---

## 8. Filter domain whitelist (only behind a content filter) 🧱

Even with all of the above, specific domains were **blocked** by the filter (307/302 → `<filter-block-page>`
block page), which no device-side trick can fix (the filter substitutes a block page for the server):
- `login.toyotadriverslogin.com` — **blocked** → sign-in opened ForgeRock `FRMainActivity` for ~1s then
  bounced back to home. Fix: **whitelist it in the filter.**
- `ctp-core-service.telematicsct.com` — **blocked** → app images didn't load (and some content).
- `hs-t-svcs.cctsl.com` — **blocked**.
- Working (200): `onecdn.telematicsct.com` (the main API incl. remote commands), `openidm.toyotadriverslogin.com`,
  `delivery.vcr.assetscs.toyota.com` (vehicle photos).

Diagnose a block from the host with: `curl -sI --ssl-no-revoke https://<host>/` → a `Location:
https://<filter-block-page>/...` header = blocked. The okhttp URL logger (`scripts/frida/okhttp_logger.js`)
lists every host the app actually hits so you can build the exact minimal whitelist.

---

## 9. Log in + confirm a command

- Launch via `run-frida.sh`, dismiss onboarding prompts (Verified Links, Background Location), tap **Sign In**,
  enter the Toyota account email → Continue → password → OTP. Reaches `OADashboardActivity`.
- Open **Advanced Remote**, long-press a button (see `scripts/host/commands.sh`). The command `POST`s to
  `onecdn.telematicsct.com/v1/remote/route/command` (200) and the app polls for completion.
  🧱 The UI can get stuck on **"Sending…"** (completion-polling is slow) even though the command already
  fired — confirmed by the car actually responding. Treat "Sending…" as non-authoritative; the car is the
  source of truth.
- ✅ **Verified:** a long-press on **Hazards** toggled the real car's hazards.
