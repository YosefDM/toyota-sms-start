# CLAUDE.md — project context & resume guide

Read this first. It tells a fresh session what this project is, **where we are right now**, how to get
back onto the running rig, and what to do next. Deep detail lives in `docs/`; this file is the map.

## What this is

Control a **2025 Toyota Camry** from a basic (non-smart) phone, over two channels into one rig:

- **SMS** — text keywords (`TOYOTA START`, `LOCK`, `UNLOCK`, `HAZARDS`, …) to a TextGrid number and read
  the car's own confirmation back.
- **Phone call** — CALL the same number and key a per-command secret code. Built for a **speed dial**
  (`+1845…,,4821`): one keypress on waking starts the car. The call answers in **tones, not words** —
  nothing about the car is ever spoken aloud. See `server/voice.py` and `docs/PRODUCTION.md`.

Either way an always-on server taps the matching button in the **official Toyota OneApp** running inside a
rooted Android emulator, which makes the app's own authenticated request to Toyota. Problem → solution →
setup is in `README.md`.

### ⛔ HARD RULE (absolute, owner-stated)
**NEVER send a raw/direct/reverse-engineered API call to the car or Toyota's backend. EVERY car action is a
simulated UI tap in the real app.** Network/Frida tooling is **read-only**, for debugging only. This is
non-negotiable; it is the entire reason for the emulator approach. (See memory `toyota-hard-boundary-ui-only`.)

## Where we are RIGHT NOW (2026-10-25) — ON ONEAPP 3.6.0, VERIFIED LIVE

**Production is running OneApp 3.6.0 (vc 222), not 3.5.0.** Toyota force-pushed an in-app update wall
("App Update Available") sometime before 2026-10-23 that blocked every command (root-detection dialog,
since the 3.5.0 frida bypass's hooks target names that don't exist in 3.6.0 — R8 reobfuscates every
build). Fixed by decompiling both builds with jadx and diffing by code STRUCTURE rather than by name
(names reset every build) — full writeup is the `toyota_bypass.js` file header. The working bypass is
**`scripts/frida/toyota_bypass.js`** (the 3.5.0 one is archived as `toyota_bypass_350.js`). Command
resource-ids are byte-identical between builds — `server/toyota_control.py` needed zero changes.

**VERIFIED LIVE END-TO-END (2026-10-25):** installed 3.6.0 over the running production emulator, cut the
bypass over, signed back in (Toyota's periodic forced-reauth — *"For your security, you have been signed
out"* — hit right at the same time; unrelated to the version bump, handled interactively over the rig's
VNC so credentials never passed through the assistant), then ran real commands through the real webhook:
`Status` → *"Yosef's Camry: Range: 6 mi..."*, `Lock` → *"2025 Camry Hybrid: The vehicle is now locked.
[DL1]"* — both the car's own confirmation, both via the actual SMS pipeline. `Start` was not live-tested
(runs the engine ~10 min) but shares the exact tap/confirm path `Lock` proved.

**★ INCIDENT, same session:** testing 3.6.0 on a cloned AVD alongside the running production one (to
preview the update before committing to it) ran both emulators concurrently for over an hour on this VM
size, and production's emulator aborted under the combined load (~06:56 UTC) — a `rig-supervisor.sh` not
yet carrying the serial-pinning fix below got stuck waiting on a vanished device. ~38 min outage, fixed by
killing the clone and a clean `systemctl restart toyota-rig`. **Lesson: don't run a clone alongside
production for extended work on this VM size** — size up first, or keep clone sessions short and tear
them down between iterations.

**★ `rig-supervisor.sh` now pins every adb/frida call to an explicit serial** (`emulator-5554` by
default), discovered by the incident above: adb's emulator auto-discovery scans the console port range
globally regardless of which adb-server instance you're on, so a second emulator makes every bare `adb
shell` ambiguous and `frida -U` pick whichever device it likes. The boot-time `pkill` is also now scoped
to this AVD's own process rather than killing any emulator on the box.

**★ Nightly FCM-refresh timer is live:** `toyota-fcm-refresh.timer` (04:00 America/New_York) force-stops
the app so its FCM push registration re-establishes before it goes stale — two outages (2026-10-11,
2026-10-15..23) were silently fixed by exactly this, done by hand; now automated. Script is
`scripts/host/fcm-refresh.sh`; the systemd unit was installed directly on the VM (not via the deploy
pipeline) and isn't itself tracked in the repo yet.

**★ TextGrid content-filter resend bug fixed:** `deliver()` in `server/app.py` compared TextGrid's full
status string against `"failed"`, but TextGrid returns `"failed - Blocked by Textgrid Content Filter"` —
the comparison never matched, so the resend-on-failure path (added specifically for this failure mode)
never once fired. Three car confirmations were silently lost this way (2026-10-12..14) before today's fix.

---

## Where we were (2026-10-06) — THE SMS→CAR BRIDGE IS WORKING END-TO-END

Text a keyword to the TextGrid number and the real car responds, with a real confirmation reply. Verified
live: `TOYOTA LOCK` → car locked → reply *"2025 Camry Hybrid: The vehicle is now locked. [DL1]"*.

Status:
- ✅ GCP VM, nested virt + KVM, emulator boots hardware-accelerated; rooted (Magisk), logged into the app
- ✅ **Public front door LIVE:** the tunnel host → Cloudflare Tunnel → the webhook; the TextGrid number's
  smsUrl points at it; `X-TextGrid-Signature` verified (see `server/textgrid_auth.py`).
  *(The actual hostname and phone number live in the private memory `toyota-gcp-rig-state` — NOT in this
  public repo.)*
- ✅ **Commands by resource-id + guard** (`server/toyota_control.py`): scrolls to reveal the Remote panel /
  opens the ⋯ Advanced Remote modal, taps by id, refuses on mismatch; all 9 commands; trunk disambiguated
  by label. Real result read from `dumpsys notification` (`await_result`) → SMS reply.
- ✅ **Reboot-durable, VERIFIED:** `toyota-rig` systemd service (`scripts/host/rig-supervisor.sh`) +
  `toyotafrida` Magisk module bring the whole rig up untended on a VM reboot (see the reboot section below).
- ✅ **CI/CD:** tests + Claude PR review + IAP deploy-on-merge (`.github/workflows/`, `docs/CICD.md`).
- ✅ **FCM push outage FIXED (2026-10-11) — and here is the runbook.** Toyota pushes stopped reaching
  the emulator at 18:40 UTC while commands kept working, so every reply degraded to "No confirmation
  came back within 25s". **Fix: `adb shell am force-stop com.toyota.oneapp`** — the `toyota-rig`
  supervisor re-spawns it under the bypass, Firebase re-registers the app's FCM token on launch, and
  pushes resume. Verified: the next LOCK returned the real "The vehicle is now locked. [DL1]". The
  **session survived the restart — no OTP was needed**; the app was back on the dashboard in ~90s.
  Diagnostic lesson: the owner's own phone kept getting Toyota notifications throughout, which proved
  Toyota's backend and FCM-the-service were fine and isolated the fault to THIS emulator's
  registration — every app install holds its own FCM token, so one device working says nothing about
  another. Ruled out: Doze (state ACTIVE), mtalk.google.com:5228/443 (reachable), bouncing GMS.
  `FcmRetry` lines in logcat are GMS backoff noise, not a diagnosis. `app_reported_success()` remains
  as the fallback when a push is missing. A watchdog should spot "the app logged success but no push
  arrived" and restart the app automatically.
- 🆕 **VOICE PATH DEPLOYED & CONFIGURED (2026-10-12, PR #5) — AWAITING ONE REAL CALL.**
  Call the number, key a per-command code, the car acts (no confirm keypress — see below); confirmation is a **tone**, never
  speech (owner's requirement: a spoken "the vehicle is now unlocked" is a state disclosure). Built for
  a speed-dial contact with comma pauses, so the prompt sits INSIDE the `<Gather>` for barge-in, and the
  command runs on a background thread so hanging up on the first tone is normal and supported.
  `/voice`, `/voice/code`, `/voice/wait`, `/voice/tone/<name>.wav` — all on the
  existing tunnel and service. Only `start`/`lock`/`unlock` are reachable by phone.
  **Already done on the rig:** branch checked out at `/opt/toyota-sms-remote`, `VOICE_PUBLIC_URL` +
  `VOICE_CODES` (6-digit, CSPRNG-generated) + `VOICE_REQUIRE_SIGNATURE=1` written to the VM's
  `server/.env` (a timestamped `.bak` sits beside it), `toyota-sms` restarted, and the number's
  **voiceUrl** set to `<tunnel-host>/voice` via the Twilio-compatible API (see PRODUCTION.md — no
  dashboard needed). Verified live through the real tunnel: signature accept/reject, the caller
  allowlist, the wrong-code buzz + re-gather, and the tone media (`audio/wav`, 200).
  **What only a real call can answer:** (1) do inbound voice webhooks carry `X-TextGrid-Signature`?
  Their doc never says — if the journal shows *"no X-TextGrid-Signature header"*, set
  `VOICE_REQUIRE_SIGNATURE=0`. (2) does TextGrid's voice client / `<Play>` fetcher clear Cloudflare?
  (their SMS client does; see the error-1010 note in PRODUCTION.md). (3) how many commas the handset
  needs. Offline: 119 unit tests + a TestClient drive of every branch with a stubbed car; the SMS path
  is unchanged and re-verified.
- 🆕 **AUTO-LOCK-AND-RETRY (2026-10-12, both channels).** A START the car refuses as unsecured
  (`[D1]` / *"ensure the vehicle is secured"*) now locks the car and starts once more, instead of
  handing the owner a failure they'd have to fix with two more texts. One retry, never a loop; it
  stops if the lock won't confirm; if the second start is refused the same way it names the open
  window / trunk / unlatched door instead of pretending the lock helped. Narrow on purpose — a
  keyfob-in-cabin refusal (`[70]`) is NOT retried, because locking fixes nothing there. The UI
  claim is now refreshable (`_touch_ui`, token is a counter not a timestamp) because the sequence
  runs ~100s and would otherwise look wedged to `_claim_ui` mid-work. Voice plays a `retrying`
  tone once and waits up to `VOICE_RETRY_HOLD_SECONDS`. Off with `AUTO_LOCK_RETRY=0`.
- ⬜ **NEXT:** OTP re-auth relay (detect `LoginActivity`/`FRMainActivity` → `NotLoggedIn` → alert + relay
  code); a watchdog (use `/health` + notification-shade); optional quick-button fast path (skip the modal
  for Lock/Unlock/Start).
- ⚠️ Dashboard shows **"1 subscription expiring"** — keep an eye on the Remote Connect subscription

Full live state + the exact resume checklist is in memory `toyota-gcp-rig-state` and in
`docs/PRODUCTION.md`.

## The rig (GCP) — how to reconnect

- **VM:** `toyota-rig`, zone `northamerica-northeast1-a`, **n2-standard-8** as of 2026-10-12 (was -4; Intel — nested virt is
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

### Bring-up after a VM stop/reboot — now AUTOMATIC (2026-10-06)
The whole rig comes up on reboot with **zero manual steps**, verified by a real `sudo reboot`. The enabled
systemd service **`toyota-rig`** (`scripts/host/rig-supervisor.sh`) boots the headless emulator → unlocks the
keyguard (PIN) → waits for frida-server → spawn-gates the app under the bypass with re-spawn.
**`toyota-sms`** (webhook) and **`cloudflared`** (tunnel) are also enabled. You only need to **start the VM**
(it's stopped to save cost) — then re-open IAP tunnels from the PC if you want to drive it manually.

⚠️ **Why frida-server MUST come from the Magisk module `toyotafrida`, not a manual `su`:** plain `su` on this
build grants root with `CapEff=0` (zero capabilities), so a su-started frida-server can't load SELinux policy
and **every spawn fails** (`InvocationTargetException` / "Unable to save SELinux policy"). The Magisk boot
module runs frida-server in magiskd's full-cap context — that was the fix for the 2026-10-06 outage. If spawn
ever breaks again, check `su -c 'grep CapEff /proc/$(pidof frida-server)/status'` (want `000001ffffffffff`).
Details in `docs/PRODUCTION.md` → Durability automation.

### Hard-won sizing lesson
Emulator **must use 4 cores** (`hw.cpu.ncore=4` + `-cores 4`) and **6 GB RAM**. With 2 cores the dashboard
never finished loading (software-GPU compositor ate a whole core; host load pegged 4.7/4, swap thrash). With
4 cores host load ~1.8 and it loads fine.

## Command buttons (live on this build, Remote tab → "Tap and hold to activate")

Commands are a **LONG-PRESS** (simulate with `adb shell input swipe X Y X Y 2500`). Prefer tapping by
**resource-id** (verify before tapping); coords are this-emulator-specific fallbacks. Status text is
`remote_progress_text` (idle "Tap and hold to activate" → "Sending…").

**The engine tile is a TOGGLE:** while a remote start is running the app replaces
`remote_engine_start_button` with `remote_engine_stop_button`. The Advanced Remote guard must
accept EITHER (`_panel_confirmed`) — requiring the Start id outright made every command fail for
the ~10 minutes a start was running (five live SMS failures, 2026-10-07..11). `SHUTOFF` taps the
Stop tile; there is deliberately no `STOP` keyword, because the carrier eats it as the opt-out word
(the same reason `HELP` never reaches the webhook and the command list advertises `?`).

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
                   VM sizing, per-boot persistence, session/OTP re-auth, FCM, TextGrid, watchdog
  FINDINGS.md    — RE analysis: the 3 client-side protections, Play-Integrity=Stripe-only, hosts, commands
  UI-MAP.md      — screen map, command resource-ids, guard model, login/OTP flow
  CICD.md        — GitHub Actions: CI tests, Claude PR review, IAP deploy to the rig + secret setup
.github/workflows/ — ci.yml (tests) · claude-review.yml (PR review) · deploy.yml (merge→rig over IAP)
scripts/frida/   — toyota_bypass.js (anti-tamper; the ONLY frida piece needed on prod) + debug loggers + config template
scripts/device/  — Magisk boot module + CA install (CA is dev-only)
scripts/host/    — rig-supervisor.sh (Linux/GCP: the `toyota-rig` service — boot emulator, unlock, spawn-gate),
                   supervisor.sh (Windows/dev equivalent), emulator boot, per-command taps, capture_screen
scripts/deploy/  — remote_deploy.sh (runs on the VM; git pull + venv + restart ONLY the webhook service)
server/          — FastAPI TextGrid webhooks: verify sender → parse keyword/code → tap → read push → reply
                   (app.py + textgrid_auth.py signature verify + toyota_control.py taps
                    + voice.py: the phone/DTMF path — code table, TwiML, synthesised tones, lockout)
  tests/         — offline unit tests (parsing/matching; no device needed)
```

## CI/CD (see docs/CICD.md)
- **Any branch push, and any PR → `ci.yml`**: byte-compile + `server/tests` unit tests.
  Deliberately not master-only: a branch used to get no CI until a PR existed, and a PR that
  went stale reported *no checks at all* (GitHub can't build a merge ref for a conflicting PR),
  which reads as green. The push run tests the branch tip; the PR run tests the merge result.
- **PR → `claude-review.yml`**: `/code-review` skill posts findings as PR comments (auth: subscription
  OAuth token in `CLAUDE_CODE_OAUTH_TOKEN`). Runs only once the workflow is on `master`.
- **Merge to `master` (server changes) → `deploy.yml`**: GitHub-hosted runner auths to GCP with a
  service-account key, tunnels in over **IAP**, and runs `scripts/deploy/remote_deploy.sh` on the VM.
  Restarts ONLY `toyota-sms.service` — emulator/frida/session untouched. No-ops (with a notice) until
  the `GCP_*`/`APP_USER` secrets are set. The VM's `server/.env` (TextGrid webhook secret, allowlist) is NOT
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
