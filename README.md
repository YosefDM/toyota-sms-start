# Toyota SMS Remote

Control a **2025 Toyota Camry** (Remote Connect features — Start / Lock / Unlock / Lights / Hazards / etc.)
by **texting a Twilio number**. A keyword like `TOYOTA START` triggers a server that **taps the button in
the real Toyota app**, which runs always-on inside a rooted Android emulator.

Built because the owner only has a basic (non-smart) phone, but the Toyota app is the only official way to
use Remote Connect.

---

## ⛔ HARD RULE — read first

> **Every car action is performed by driving the Toyota app's UI (a simulated tap on the real app).
> We NEVER send a raw/direct API call to the car or Toyota's backend to cause an action.**

Automation only ever **taps buttons**. Network/Frida tooling is used **read-only** for debugging. This is a
deliberate design boundary, not a technical limitation. Do not "just POST the command" — tap the UI.

---

## Architecture

```
  ┌────────────┐   SMS    ┌─────────────┐   webhook   ┌────────────────────────┐
  │ basic phone│ ───────► │   Twilio    │ ──────────► │  controller server     │
  └────────────┘ "TOYOTA  └─────────────┘             │  (always-on)            │
                  START"                               │                         │
                                                       │  parse keyword          │
                                                       │       │                 │
                                                       │       ▼                 │
                                                       │  adb input long-press   │
                                                       │  on the Toyota app's    │
                                                       │  button (UI only)       │
                                                       │       │                 │
                                                       │       ▼                 │
                                                       │  Android emulator       │
                                                       │  (rooted, Frida-hooked) │
                                                       │   └─ Toyota app ─────────┼──► Toyota cloud ──► Car
                                                       └────────────────────────┘
```

The only thing we automate is the **tap**. The app makes its own authenticated call; we never do.

---

## Status (as of last session)

**PROVEN END-TO-END.** A UI long-press on the app's **Hazards** button toggled the hazards on the real car.

| Stage | State |
|---|---|
| Rooted Android-15 emulator (Magisk) | ✅ working |
| Bypass app's root/emulator/tamper self-destruct (Frida) | ✅ working |
| Device PIN gate | ✅ real PIN |
| TLS cert-pinning vs the home TLS-filter (Techloq) | ✅ unpinned + CA trusted |
| Network "validated" (captive-portal workaround) | ✅ done |
| Logged into Toyota account | ✅ done |
| Remote command via UI tap → car | ✅ **Hazards toggled on real car** |
| Durable auto-start on boot | ⬜ TODO (see docs/PRODUCTION.md) |
| Per-command tap scripts | 🟨 coordinates mapped, script drafted |
| Twilio bridge | ⬜ TODO |

---

## Repo layout

```
docs/
  SETUP.md        — build the rig from scratch (every step + every gotcha we hit)
  PRODUCTION.md   — always-on server runbook + the durability/persistence work still to do
  FINDINGS.md     — reverse-engineering notes: the app's protections, the hosts, the Techloq issues
scripts/
  frida/
    toyota_bypass.js    — OUR hook: neutralizes the root/emulator/tamper self-destruct (Lock #1)
    okhttp_logger.js    — read-only: logs request URLs (debugging)
    body_logger.js      — read-only: logs /remote/route/* response bodies (debugging)
    config.example.js   — template for the TLS-unpinning config (fill CERT_PEM yourself; never commit the real one)
  device/
    install_ca.sh       — install a CA into the Android-15 system trust store (HTTPToolkit tmpfs method)
  host/
    boot-emulator.sh    — launch the emulator (headful/headless, software GPU)
    run-frida.sh        — spawn the app with the unpinning + bypass scripts
    commands.sh         — the per-command UI long-presses (Start/Lock/Unlock/...)
```

Third-party Frida scripts (HTTPToolkit `android-certificate-unpinning.js`, NVISO `disable-flutter-tls.js`)
are **not committed** (their licenses + they move fast) — `docs/SETUP.md` has the exact download commands.

---

## ⚠️ Before you push to GitHub

- **Make the repo PRIVATE.** It documents bypassing an app's hardening (for your own car), contains
  home-network specifics, and references a CA from your content filter. Keep it private.
- **Never commit:** the Toyota APK or splits, decompiled app sources (`jadxout/`), the Magisk zip,
  `frida-server` binary, Android system images, any Python venv, your real `config.js` (the Techloq
  `CERT_PEM`), or anything with your Toyota credentials/OTP. The `.gitignore` covers these.
- This is for personal use on a car you own. Don't publish it as a how-to.
