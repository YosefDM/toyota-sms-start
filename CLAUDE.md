# CLAUDE.md — project context & resume guide

Read this first. It tells a fresh session what this project is, **where we are right now**, how to get
back onto the running rig, and what to do next. Deep detail lives in `docs/`; this file is the map.

## What this is

Control a **2025 Toyota Camry** by **SMS**. The owner carries a basic (non-smart) phone; they text keywords
(`TOYOTA START`, `LOCK`, `UNLOCK`, `HAZARDS`, …) to a Twilio number; an always-on server taps the matching
button in the **official Toyota OneApp** running inside a rooted Android emulator, which makes the app's own
authenticated request to Toyota. Problem → solution → setup is in `README.md`.

### ⛔ HARD RULE (absolute, owner-stated)
**NEVER send a raw/direct/reverse-engineered API call to the car or Toyota's backend. EVERY car action is a
simulated UI tap in the real app.** Network/Frida tooling is **read-only**, for debugging only. This is
non-negotiable; it is the entire reason for the emulator approach. (See memory `toyota-hard-boundary-ui-only`.)

## Where we are RIGHT NOW (2026-10-05, paused for the night)

The production rig is **built, rooted, and logged into the Toyota app on GCP**, with the dashboard fully
loading real vehicle data ("Yosef's Camry", odometer 31,686 mi, fuel). We paused right before testing an
actual command tap.

Status:
- ✅ GCP VM with working nested virt + KVM, emulator boots hardware-accelerated
- ✅ Rooted (Magisk), frida-server, anti-tamper bypass running as a service, app reaches dashboard
- ✅ Google account + FCM, device PIN, **logged into Toyota app** (session persists in /data)
- ✅ Remote command buttons located (IDs + coords below)
- ✅ **COMMAND VERIFIED END-TO-END:** long-pressed Unlock → car unlocked → **FCM push received**:
  "2025 Camry Hybrid: The vehicle is now unlocked. [DL0]" (read via `dumpsys notification`). The UI-tap
  command path AND the notification feedback channel both work on the rig.
- ✅ **Full command map wired (by resource-id, not coords)** — all 9 commands in `server/toyota_control.py`
  with the guard model (verify the Advanced Remote anchor set, open the ⋯ modal if needed, refuse on
  mismatch; trunk lock/unlock disambiguated by label since they share an id). Resolution-independent.
- ✅ **Real result feedback wired** — `await_result()` reads `dumpsys notification --noredact` and the
  Twilio bridge (`server/app.py`) replies with Toyota's actual push text, not the "Sending…" spinner.
  Offline unit-tested (node parse, id find, trunk label, notification match).
- ⬜ **NEXT:** live-test the bridge with a real Twilio number + public HTTPS URL; then OTP re-auth relay
  (detect `LoginActivity`/`FRMainActivity` → `NotLoggedIn` already raised → alert operator + relay code)
  and a watchdog.
- ⚠️ Dashboard showed **"1 subscription expiring"** — keep an eye on the Remote Connect subscription

Full live state + the exact resume checklist is in memory `toyota-gcp-rig-state` and in
`docs/PRODUCTION.md`.

## The rig (GCP) — how to reconnect

- **VM:** `toyota-rig`, zone `northamerica-northeast1-a`, **n2-standard-4** (Intel; nested virt is
  **Intel-only** on GCP — never n2d/AMD). External IP is ephemeral. *(GCP project ID, gcloud account, device
  PIN, and VNC password are kept in the private memory `toyota-gcp-rig-state` — NOT in this public repo.)*
- **The dev PC is behind the Techloq TLS-MITM filter**, so to drive gcloud/SSH:
  - gcloud needs a combined CA bundle: `gcloud config set core/custom_ca_certs_file <certifi cacert.pem +
    techloq-CA root>` (already set this session).
  - Port 22 is blocked by the filter → use **IAP tunnels**: `gcloud compute start-iap-tunnel toyota-rig 22
    --local-host-port=localhost:2222` (and `6080` for the VNC view). SSH with **native** OpenSSH
    (`C:\Windows\System32\OpenSSH\ssh.exe`), NOT gcloud's plink:
    `ssh -i ~/.ssh/google_compute_engine -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -p 2222 Yosef@localhost`
  - Details + why: memory `gcp-nested-virt-intel-only`, `docs/PRODUCTION.md`.
- **On prod there is NO Techloq** → all TLS-MITM workarounds (cert unpinning, CA inject, captive-portal,
  FCM exemptions) are **dev-only and NOT used on the rig**. Only the root + anti-tamper **bypass**
  (`scripts/frida/toyota_bypass.js`, kept attached) is always required.

### Bring-up after a VM stop/reboot (what does NOT persist)
See the step-by-step in memory `toyota-gcp-rig-state`. Summary: start VM → re-open IAP tunnels → boot
emulator under Xvfb with `-cores 4 -memory 6144` → **unlock keyguard with the device PIN** (required before
the app is spawnable — credential-encrypted storage) → `su -c setenforce 0` + start frida-server as root →
`systemctl start toyota-frida` → if blank Flutter splash hangs, `systemctl restart toyota-frida` once.

### Hard-won sizing lesson
Emulator **must use 4 cores** (`hw.cpu.ncore=4` + `-cores 4`) and **6 GB RAM**. With 2 cores the dashboard
never finished loading (software-GPU compositor ate a whole core; host load pegged 4.7/4, swap thrash). With
4 cores host load ~1.8 and it loads fine.

## Command buttons (live on this build, Remote tab → "Tap and hold to activate")

Commands are a **LONG-PRESS** (simulate with `adb shell input swipe X Y X Y 2500`). Prefer tapping by
**resource-id** (verify before tapping); coords are this-emulator-specific fallbacks. Status text is
`remote_progress_text` (idle "Tap and hold to activate" → "Sending…").

| Command | resource-id | content-desc | center (this build) |
|---|---|---|---|
| Start | `remote_engine_start_button` | Start | 182,1159 |
| Lock | `remote_door_lock_button` | Lock | 418,1159 |
| Unlock | `remote_door_unlock_button` | Unlock | 654,1159 |

More controls (hazards/horn/lights/trunk) are further down / in the Advanced Remote panel — see
`docs/UI-MAP.md` for the full set and the guard model (verify screen before tapping, refuse on mismatch).

## How to operate the rig (observe & act)

- **To SEE the screen, use `adb exec-out screencap -p > /tmp/x.png`** then pull it — the VNC/noVNC view
  (`http://localhost:6080/vnc.html`, password in the private memory) is laggy and often shows stale frames. Screenshot
  liberally; it's the reliable truth. (Owner explicitly asked for frequent screenshots.)
- Drive the UI with `adb shell input ...`; read structure with `uiautomator dump`.
- **SSH quirk:** long multi-step inline ssh commands and backgrounding over the IAP tunnel often truncate or
  exit 127 — put scripts in a file and pipe via `ssh ... 'bash -s' < script.sh`. Launch long-lived things
  (frida) via **systemd-run**, not `nohup &`/tmux (tmux needs a TTY).

## Repo layout
```
CLAUDE.md        — this file (context + resume)
README.md        — public portfolio front page (problem → solution → clean setup)
docs/
  SETUP.md       — full build guide, every pitfall + fix (dev machine)
  PRODUCTION.md  — always-on runbook: GCP host, Intel-only nested virt, IAP/CA access, headless rooting,
                   VM sizing, per-boot persistence, session/OTP re-auth, FCM, Twilio, watchdog
  FINDINGS.md    — RE analysis: the 3 client-side protections, Play-Integrity=Stripe-only, hosts, commands
  UI-MAP.md      — screen map, command resource-ids, guard model, login/OTP flow
  CICD.md        — GitHub Actions: CI tests, Claude PR review, IAP deploy to the rig + secret setup
.github/workflows/ — ci.yml (tests) · claude-review.yml (PR review) · deploy.yml (merge→rig over IAP)
scripts/frida/   — toyota_bypass.js (anti-tamper; the ONLY frida piece needed on prod) + debug loggers + config template
scripts/device/  — Magisk boot module + CA install (CA is dev-only)
scripts/host/    — emulator boot, app launch, per-command taps (resource-id, via the server module), supervisor
scripts/deploy/  — remote_deploy.sh (runs on the VM; git pull + venv + restart ONLY the webhook service)
server/          — FastAPI Twilio webhook: verify sender → parse keyword → tap → read push → reply
  tests/         — offline unit tests (parsing/matching; no device needed)
```

## CI/CD (see docs/CICD.md)
- **Push/PR → `ci.yml`**: byte-compile + `server/tests` unit tests.
- **PR → `claude-review.yml`**: `/code-review` skill posts findings as PR comments (auth: subscription
  OAuth token in `CLAUDE_CODE_OAUTH_TOKEN`). Runs only once the workflow is on `master`.
- **Merge to `master` (server changes) → `deploy.yml`**: GitHub-hosted runner auths to GCP with a
  service-account key, tunnels in over **IAP**, and runs `scripts/deploy/remote_deploy.sh` on the VM.
  Restarts ONLY `toyota-sms.service` — emulator/frida/session untouched. No-ops (with a notice) until
  the `GCP_*`/`APP_USER` secrets are set. The VM's `server/.env` (Twilio token, allowlist) is NOT
  deployed — it's managed by hand on the VM.
Secrets, APKs, images, frida-server, CA/PEMs are **git-ignored** (`.gitignore`; note: no inline comments in
gitignore — see memory `gitignore-no-inline-comments`). Repo is public (portfolio); keep IP/login out of it.

## Working style (owner preferences — from memory)
- When the owner picks a path and says keep going, **persist and go deep; don't re-pitch declined
  alternatives** (e.g. they rejected a physical phone and the reverse-engineered API — don't resurface them).
  See memory `working-style-persist`.
- Document everything durably (that's why these docs + memories exist).
- Be decisive and act; screenshot often; keep the public README clean (gotchas go in `docs/`).

## Operational risks to remember
- **Session/OTP re-auth** is the #1 unattended-operation limiter: the app periodically force-signs-out and
  needs an OTP (human in loop). "Keep me signed in" is ticked to reduce frequency. Design the SMS OTP-relay.
- **Cold-start splash hang** after reboot → restart the app once (watchdog should automate).
- **Remote Connect subscription** was flagged "expiring".
