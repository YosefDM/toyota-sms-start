# Toyota SMS Remote

**Control a car by text message.** Send `TOYOTA START` to a phone number and the engine starts — no
smartphone required. Lock, unlock, lights, hazards, and more, all from any basic phone that can send an SMS.

> ✅ **Proven end-to-end:** a text-triggered action drives the real Toyota app and the command reaches the
> car (verified by toggling the vehicle's hazards from an automated button press).

---

## The problem

Toyota's Remote Connect features (remote start, lock/unlock, etc.) are only officially available through the
**Toyota mobile app** — there's no SMS or web interface. That's a problem if you carry a **basic
(non-smartphone) phone** but still want to start your car on a cold morning or unlock it remotely.

This project bridges that gap: it runs the official Toyota app on an always-on server and lets you drive it
by **SMS**.

---

## How it works

```
  ┌────────────┐   SMS     ┌──────────┐   webhook   ┌──────────────────────────────────┐
  │ any phone  │ ────────► │  Twilio  │ ──────────► │  always-on controller            │
  └────────────┘  "TOYOTA   └──────────┘             │                                  │
                   START"                            │  1. verify sender + parse keyword│
                                                     │  2. tap the matching button in   │
                                                     │     the Toyota app (UI only)      │
                                                     │          │                        │
                                                     │          ▼                        │
                                                     │   Android emulator (rooted)       │
                                                     │    └─ Toyota app ──► Toyota cloud ─┼─► 🚗
                                                     └──────────────────────────────────┘
```

A real, logged-in instance of the Toyota app runs inside a rooted Android emulator on the server. An inbound
SMS maps to a keyword, and the controller performs the corresponding **tap** on the app's UI. The app then
makes its own normal, authenticated request to Toyota — exactly as it would if a human tapped the button.

### Design principle: UI only

> **Every car action is performed by tapping the app's UI. The system never sends a raw/direct API call to
> the car or Toyota's backend.**

The automation only ever simulates button presses. This keeps everything within the app's own authenticated,
official flow — no reverse-engineered command requests are ever sent to the vehicle. (Network tooling is used
**read-only**, for debugging during setup.)

---

## Key engineering challenges solved

Getting the official app to run unattended on an emulator meant working through several layers of hardening
the app has against running in exactly this kind of environment. Each is documented in detail in
[`docs/SETUP.md`](docs/SETUP.md) (build steps) and [`docs/FINDINGS.md`](docs/FINDINGS.md) (analysis):

- **Anti-tamper self-destruct** — the app force-quits on rooted/emulated devices. Neutralized with a targeted
  Frida hook (see `scripts/frida/toyota_bypass.js`).
- **Running an ARM-only app on x86** — the app ships arm64-only native libraries; solved using a Google Play
  system image with the ARM translation layer, which also dictated the right instrumentation approach.
- **Certificate pinning** — the app pins its TLS certificate. Handled with standard Java-layer unpinning
  (relevant only when the host network inspects TLS).
- **Play Integrity analysis** — confirmed Toyota does **not** gate login or vehicle commands on Google Play
  Integrity (only the bundled Stripe payment SDK uses it), which is what makes the emulator approach viable.
- **Headless, reproducible automation** — the entire Android/Magisk/Frida bring-up is scripted and driven
  without manual GUI interaction.

---

## Setup

Prerequisites: Android SDK command-line tools, a JDK, Python 3.12, Node (for later UI automation), and a
machine that supports hardware-accelerated Android emulation (KVM / nested virtualization).

1. **Create the emulator.** Install a Google Play system image (`android-35;google_apis_playstore;x86_64`)
   and an AVD. → `scripts/host/boot-emulator.sh`
2. **Root it** with Magisk (via [rootAVD](https://gitlab.com/newbit/rootAVD)).
3. **Install Frida** — `frida-server` on the device (as root) and a version-matched `frida` on the host.
4. **Install the Toyota app** from Google Play (as a split APK).
5. **Set a device PIN** (the app requires a secure lockscreen).
6. **Launch the app under Frida** with the anti-tamper hook. → `scripts/host/run-frida.sh`
7. **Log in** with your Toyota account once; the session persists.
8. **Map the commands** — each Remote Connect action is a long-press on the app's UI. → `scripts/host/commands.sh`
9. **(Production)** Wire up the Twilio webhook and make the setup auto-start on boot.
   → [`docs/PRODUCTION.md`](docs/PRODUCTION.md)

> **Full step-by-step with every pitfall and its fix:** [`docs/SETUP.md`](docs/SETUP.md). If your server's
> network inspects TLS (a corporate/content filter), see the certificate-trust and network-validation notes
> there too.

---

## Repo layout

```
docs/
  SETUP.md        — complete build guide (detailed, with troubleshooting for every pitfall hit)
  PRODUCTION.md   — always-on server runbook + durability roadmap
  FINDINGS.md     — reverse-engineering analysis: app protections, hosts, command mechanics
scripts/
  frida/          — the anti-tamper bypass hook + read-only debug loggers + config template
  device/         — on-device helpers (system CA install for TLS-inspecting networks)
  host/           — emulator boot, app launch, and per-command UI taps
```

---

## Status & roadmap

| | |
|---|---|
| Rooted emulator + app running unattended | ✅ done |
| Logged in, remote command reaches the car | ✅ **verified** |
| Per-command UI tap scripts | 🟨 drafted (coords mapped) |
| Auto-start / reboot-survivable production setup | ⬜ todo |
| Twilio SMS bridge (sender allowlist, replies) | ⬜ todo |

---

## Notes & scope

A personal automation project for a car I own, controlling the app's **own UI**. It uses standard mobile-
security techniques (Frida instrumentation, certificate unpinning, root-detection bypass) solely to run the
official app in an automated environment — it is not a tool for accessing anyone else's vehicle. Binaries
(the APK, images, `frida-server`), decompiled sources, and all secrets/credentials are intentionally excluded
from this repo (see `.gitignore`).
