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

## Session / OTP re-auth (the real operational risk)

The Toyota login issues an OTP and the session will eventually expire. An unattended box can get **stuck at a
login + OTP screen**. Plan:
- Keep the app logged in as long as possible (don't `pm clear`, don't wipe `/data`).
- When re-auth is needed, the OTP must reach the box. Options: route the account's OTP to an email/SMS the
  server can read, or alert the operator to intervene. **Decide this before going truly unattended.**

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

## Durability checklist (what's left)

- [ ] Magisk `service.sh` module: `setenforce 0`, start frida-server, (filtered-net) install_ca + captive-portal, `svc power stayon true`
- [ ] Host supervisor (systemd/Docker): boot emulator, re-spawn Frida, keep attached
- [ ] Auto-unlock keyguard on boot
- [ ] Per-command tap scripts hardened against the "Sending…" stuck-UI state
- [ ] Twilio webhook service + sender whitelist + confirmation replies
- [ ] OTP re-auth path decided + implemented
- [ ] Watchdog + operator alerting
