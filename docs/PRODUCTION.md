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
