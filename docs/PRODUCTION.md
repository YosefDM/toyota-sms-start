# PRODUCTION — always-on server runbook

Goal: the rig survives reboots and runs unattended on an always-on box, with a Twilio SMS front door.

## Host requirements

- The Android emulator needs **hardware virtualization (KVM / nested virt)**. A plain DigitalOcean droplet
  does **not** expose this — a normal AVD won't boot or will crawl. Options:
  - a bare-metal / nested-virt-capable VPS, or a Linux box you own, with KVM, **or**
  - `redroid` (Android-in-Docker using the host kernel; needs `binder`/`ashmem` modules), **or**
  - keep it on a local always-on machine (what we're effectively prototyping on).
- Linux host changes the emulator GPU flag: try `-gpu swiftshader_indirect` headless; `-no-window`.
- **Network:** the app needs an **unfiltered** path to Toyota's domains. Either run the box on an unfiltered
  connection, or whitelist the domains in §8 of SETUP.md. Behind a MITM filter you additionally need the
  CA-trust (§5), unpinning (§6) and captive-portal (§7) steps every boot.

### Chosen host: GCP Compute Engine (verified)

We run the rig on a **GCP Compute Engine** VM. Hard-won specifics (all verified live):

- **Nested virtualization is Intel-only on GCP.** You must create the VM with
  `--enable-nested-virtualization`, **and** the machine type must be **Intel** (N1/N2/C2…). On an **AMD**
  machine (e.g. `n2d`, EPYC) the flag is silently accepted but the guest gets **no** `vmx`/`svm` and
  **no `/dev/kvm`** — the emulator can't accelerate. Confirmed: `n2d-standard-2` → `egrep -c '(vmx|svm)'
  /proc/cpuinfo` = **0**; `n2-standard-2` (Cascade Lake) → **4**, `/dev/kvm` present. Use **`n2-standard-2`**
  (or `n1-standard-2 --min-cpu-platform "Intel Haswell"` when cheaper / N1 capacity exists; N1 can otherwise
  land on a pre-Haswell host). The `--enable-nested-virtualization` flag only exists on `instances create` in
  the current gcloud GA — you can't toggle it on an existing VM, so bake it in at create time.
  Create command:
  ```
  gcloud compute instances create toyota-rig --zone=<zone> --machine-type=n2-standard-2 \
    --enable-nested-virtualization --image-family=debian-12 --image-project=debian-cloud \
    --boot-disk-size=40GB
  ```
  Verify inside the guest: `egrep -c '(vmx|svm)' /proc/cpuinfo` (>0) and `ls -l /dev/kvm`.
- The console **GUI has no nested-virt toggle** — its Edit view only exposes machine type / CPU platform /
  display device. You must use `gcloud` (or the create-instance API with `advancedMachineFeatures`).
- **40 GB boot disk** (Debian 12 auto-grows the root partition on first boot via cloud-init — verified
  `/dev/sda1 40G`). 10 GB is too small for SDK + system image.
- **Org policy** `constraints/compute.disableNestedVirtualization` exists on new/trial projects but its
  *effective* value here was **not enforced** (`booleanPolicy: {}`), so it wasn't the blocker — the AMD CPU
  was. If a project's effective policy *is* enforced, nested virt is blocked until an Org Policy Admin
  disables it (a trial user typically lacks `setOrgPolicy`).

### Reaching the VM from behind a TLS-intercepting filter (dev-box only)

Driving `gcloud` from a home PC behind the Techloq MITM has two gotchas (production boxes on an unfiltered
line hit neither):

- **SSH over port 22 is blocked** by the filter (raw SSH isn't web traffic → plink `Network error:
  Permission denied`). Use **IAP TCP forwarding** instead (SSH tunneled over HTTPS): enable the API once
  (`gcloud services enable iap.googleapis.com`), then `gcloud compute start-iap-tunnel toyota-rig 22
  --local-host-port=localhost:2222` and SSH to `localhost:2222`. The default `default-allow-ssh` firewall
  (tcp:22 from 0.0.0.0/0) already covers the IAP range 35.235.240.0/20.
- **The filter MITMs the IAP endpoint too** → `SSL: CERTIFICATE_VERIFY_FAILED (unable to get local issuer
  certificate)`, because gcloud's bundled CA store doesn't trust the filter's root. Fix: build a combined
  bundle = gcloud's `lib/third_party/certifi/cacert.pem` **+** the filter's **root** CA (export
  `techloq-CA` from the Windows Trusted Root store — the leaf `env1.dc3.us.techloq.com` alone is not
  enough), then `gcloud config set core/custom_ca_certs_file <combined.pem>`.
- On Windows, gcloud's bundled **PuTTY/plink** rejects OpenSSH `-o` flags and prompts to cache host keys.
  Cleaner: start the IAP tunnel standalone (above) and connect with the native OpenSSH
  `C:\Windows\System32\OpenSSH\ssh.exe -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL -p 2222`.

### VM sizing (verified)

- **Use 4 vCPU (`n2-standard-4`), not 2.** On `n2-standard-2` Android 15 thrashed: a permanent
  `com.android.systemui` **ANR** stole window focus, which made *all* UI automation (uiautomator dump /
  `input tap`) impossible, and the overloaded box also dropped the IAP SSH tunnel mid-command. The emulator
  itself warns *"will run more smoothly with 4 CPU cores."* Resizing to 4 vCPU (stop → `set-machine-type` →
  start; keeps the disk, ramdisk, AVD, and nested-virt flag) cleared the ANR and stabilised SSH. Boot
  `sys.boot_completed` in ~20–30 s with KVM.

### Headless rooting on the server (differs from a GUI box)

rootAVD patches the ramdisk fine headless, but installs only the Magisk **stub** manager and can't finish
the GUI-driven setup. Completed it over adb + uiautomator:

1. `bash rootAVD.sh system-images/android-35/google_apis_playstore/x86_64/ramdisk.img` (feed `1` for stable
   Magisk). Patches ramdisk (`ramdisk.img.backup` saved). Then **relaunch the emulator process** (kill qemu
   + start again) so the patched ramdisk loads — an `adb reboot` is not enough.
2. Stub manager (`versionName=1.0`) is installed but can't grant su (its dialog is just "Upgrade to full
   Magisk"). Install the **full** app directly: `adb install -r <rootAVD>/Magisk.zip` (Magisk ships the app
   APK as a `.zip`; installs as an APK) → `com.topjohnwu.magisk` becomes `26.4 (26400)`.
3. Open the app (`monkey -p com.topjohnwu.magisk -c android.intent.category.LAUNCHER 1`). It shows
   **"Requires Additional Setup … reboot?"** — tap **OK** (`com.topjohnwu.magisk:id/dialog_base_button_1`);
   it sets up the pre-init partition and reboots.
4. `su` from shell still returns **Permission denied** with no prompt (default policy denies uid 2000). Fix
   in the app: bottom-nav **Superuser** tab → the **`[SharedUID] Shell`** (`com.android.shell`) row has a
   switch `com.topjohnwu.magisk:id/policy_indicator` (`checked=false`) — `input tap` its centre to enable.
5. Verify: `adb shell su -c id` → `uid=0(root) … context=u:r:magisk:s0`.

Automate-by-resource-id, not coordinates, where possible: Magisk uses Jetpack Compose so most nodes have no
id, but the su-grant/dialog button (`dialog_base_button_1`) and the policy switch (`policy_indicator`) and
the bottom-nav `content-desc`s (Home/Superuser/Logs/Modules) are stable anchors.

## The per-boot setup that must be made persistent

Everything below is currently applied **by hand each boot** and must become automatic. Recommended: a
**Magisk service module** (`/data/adb/modules/toyota/service.sh`, runs late-boot as root) for the on-device
parts, plus a host-side supervisor (systemd unit / Docker entrypoint) for the emulator + Frida.

On-device, in order (root):
1. `setenforce 0`  (SELinux permissive — required for frida-server spawn-gating).  ← persist via module
2. start `frida-server` as root (`/data/local/tmp/frida-server -D`).
3. **(filtered network only)** run `install_ca.sh` (re-inject the CA; the tmpfs mount does NOT survive a
   full reboot), then the captive-portal `settings put global ...` (§7).
4. `svc power stayon true` (don't sleep/lock while plugged in).
5. keep the lockscreen PIN set (persists across reboot); on boot the device shows the keyguard — unlock via
   `input` or set the emulator to boot unlocked.

Host-side supervisor must:
1. kill stale emulator/qemu processes + remove `*.lock` (see SETUP §1).
2. boot the emulator (`scripts/host/boot-emulator.sh`), wait for `sys.boot_completed`.
3. wait for `frida-server`, then spawn-gate the app (`scripts/host/run-frida.sh`). The Frida session must
   **stay attached** for the whole uptime (the unpinning/bypass hooks are per-process) — supervise it and
   re-spawn if it dies.
4. drive the app to the dashboard; re-login when the session expires (see "Session/OTP" below).

## Session / OTP re-auth (THE operational risk — confirmed)

**Confirmed behavior:** the app periodically shows a server-forced *"For your security, you have been signed
out. Sign back in to continue using the app."* and drops to `LoginActivity`. This is ForgeRock
session/refresh-token expiry + Toyota security policy — it **will** recur on its own schedule and requires a
re-sign-in **with an OTP**, i.e. a human in the loop. This is the single biggest limiter on unattended
operation. (Separately: a session appears to persist across emulator/app restarts **only while the Frida
unpinning stays attached** — if Frida detaches, the silent token-refresh TLS call fails and the app also
drops to login. So: keep Frida persistently attached, see §Frida.)

Design the re-auth path (not optional):
1. **Detect** the signed-out state — fingerprint: `com.toyota.oneapp/.ui.LoginActivity`, texts
   "For your security, you have been signed out" / "Sign In" / "Register". The command dispatcher must
   check for this and **refuse to tap** (and trigger re-auth) rather than acting on the wrong screen.
2. **Alert** the operator by SMS: "Toyota session expired — reply TOYOTA CODE <otp> to sign back in."
3. **OTP relay**: operator receives the OTP on their phone and texts it to the Twilio number; the server
   types email (stored), password (stored in a secret manager), then the relayed OTP into the login screens
   (all captured in docs/UI-MAP.md). Keep credentials in a secrets store, never in the repo.

Reduce the frequency:
- **Enable "Keep me signed in"** at login (the app has a KeepMeSignedIn/biometric setting) — may extend the
  session lifetime substantially. Tick it every time we log in.
- Measure the actual re-auth **cadence** (days vs weeks) so alerting expectations are realistic.
- Don't `pm clear` / wipe `/data`; don't let Frida detach.

## FCM push notifications (command-result feedback)

> **On a normal production network (no TLS interception), FCM works out of the box** — skip the exemption
> list below; it's only needed on a filtered/MITM dev network. The notification-reading in step 3 still
> applies everywhere.

The app's authoritative success/failure signal is a **push notification** from Toyota after each command
(the UI "Sending…" spinner is unreliable). To receive it, the emulator needs working FCM, which requires:

1. **A Google account signed in** on the emulator (done — enables GMS checkin; fixes `AUTHENTICATION_FAILED`).
2. **FCM traffic NOT TLS-intercepted.** Per Google's own docs, *"FCM's protocol for delivering push
   messages to devices is not able to be proxied through network proxies"* — and Google Play Services pins
   its certs, so a MITM filter breaks FCM and trusting the filter's CA does **not** help (GMS ignores the
   system store). Measured: push port 5228 already serves the real Google cert on this network, but Google's
   443 registration endpoints are MITM'd → registration fails.

   **Fix:** exempt these from TLS interception in the content filter (SNI pass-through, like the Toyota login
   domain was) — ports **5228–5230 + 443**, hostnames:
   `mtalk.google.com`, `mtalk4.google.com`, `alt1-mtalk.google.com`…`alt8-mtalk.google.com`,
   `android.apis.google.com`, `device-provisioning.googleapis.com`, `firebaseinstallations.googleapis.com`.
   (A physical phone on the same filter gets notifications, confirming the filter already passes Google push
   through for a set-up device; the emulator just needs the same exemptions.)

   Refs: Firebase "Configure your Network for FCM"
   (https://firebase.google.com/docs/cloud-messaging/network-configuration).

3. Then the dispatcher reads the notification shade (`dumpsys notification` / a NotificationListener) to
   report real success/failure over SMS. Until FCM is confirmed, use "treat-as-sent" feedback.

## Twilio bridge (TODO)

- Twilio number → inbound-SMS webhook → small service (FastAPI) on the box (public URL via Cloudflare
  Tunnel / reverse proxy).
- Parse keyword (`TOYOTA START`/`LOCK`/`UNLOCK`/`HAZARDS`...), **whitelist the sender's number**, optionally
  require a PIN/keyword.
- Dispatch → the matching **UI long-press** from `scripts/host/commands.sh` (⛔ UI only — never an API call).
- Reply via TwiML with success/the observed app state so the basic phone gets feedback.
- Rate-limit; add a confirmation step for Unlock.

## Watchdog

- Screenshot on command; if the app isn't on the expected screen, relaunch + re-login.
- Alert the operator (SMS/email) if the emulator/Frida/app is down or stuck at login.
- Health check: periodically confirm the app is on the dashboard and the Frida session is attached.

## Durability automation (built — `scripts/`)

- **On-device: Magisk boot module** (`scripts/device/magisk-module/`, install via `scripts/device/install-module.sh`).
  Its `service.sh` runs late_start as root every boot and re-applies: `setenforce 0`, captive-portal off,
  content-filter CA trust (`install_ca.sh`, tmpfs + rbind into zygote namespaces), `svc power stayon true`,
  and starts `frida-server`. Install once (`install-module.sh <ca.0> <frida-server>`), then it's automatic.
- **Host: supervisor** (`scripts/host/supervisor.sh`). Kills stale emulator/locks → boots the emulator
  (software GPU) → waits for boot → unlocks with the PIN → waits for `frida-server` → spawn-gates the app
  under a **persistent** frida session (`tail -f /dev/null | frida …`) and **re-spawns if it drops**.
  Run it from a systemd unit / Windows Task Scheduler at login so the whole rig comes up unattended.

### Durability checklist

- [x] Magisk `service.sh` module (setenforce / captive-portal / CA / stay-awake / frida-server)
- [x] Host supervisor: boot emulator, keep Frida persistently attached, re-spawn on drop
- [x] Auto-unlock keyguard on boot (PIN via `input`; digit-tap fallback noted if `input text` is ignored)
- [ ] Validate with one real reboot (also the clean moment to confirm FCM notifications light up)
- [ ] Per-command tap: harden panel navigation + find-by-resource-id (from docs/UI-MAP.md); non-blocking "Sending" is fine
- [ ] Twilio webhook live test (server/ built; needs a number + public HTTPS URL)
- [ ] OTP re-auth path (detect LoginActivity → SMS-alert → OTP relay); tick "Keep me signed in"
- [ ] Watchdog + operator alerting (use /health + notification-shade reads)
