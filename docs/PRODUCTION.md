# PRODUCTION — always-on server runbook

Goal: the rig survives reboots and runs unattended on an always-on box, with a TextGrid SMS front door.

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
3. **OTP relay**: operator receives the OTP on their phone and texts it to the TextGrid number; the server
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

## TextGrid bridge (built — `server/`)

- TextGrid number's smsUrl → inbound-SMS webhook → FastAPI service on the box (public URL via Cloudflare
  Tunnel), bound to `127.0.0.1:8080`.
- Verifies the `X-TextGrid-Signature` against the raw body (`textgrid_auth.py` — NOT Twilio's scheme),
  **allowlists the sender's number**, and requires a confirmation word for Unlock/Trunk.
- Dispatch → the matching **UI long-press** by resource-id via `toyota_control.py` (⛔ UI only — never an API call).
- Replies via TwiML with Toyota's real push-notification text (or treat-as-sent if none arrives in time).
- Per-sender rate limit. Outbound (operator alerts / OTP relay) will use the Twilio SDK pointed at
  `api.textgrid.com` — see `.env.example`.

## Voice bridge — phone call + DTMF code (built — `server/voice.py`)

The second control channel, for the gesture SMS can't match: **one speed-dial keypress**. The owner
stores the number with the code behind comma pauses (`+1845…,,4821`), presses it on waking, and
the engine starts. Same emulator, same taps, same hard rule.

### Wiring
- The number's **voiceUrl** (HTTP POST) → `https://<host>/voice`, through the SAME Cloudflare
  Tunnel and the same uvicorn on `127.0.0.1:8080`. Nothing new to run or supervise.
- `VOICE_PUBLIC_URL` must equal that URL **byte-for-byte and end in `/voice`**: it is the signed
  URL, the base for the `<Gather action=…>` callbacks (`/voice/code`, `/voice/confirm`,
  `/voice/wait`) and the base for the `<Play>` tone URLs.
- `VOICE_CODES="4821:start,7390:lock,5566:unlock"` in the VM's `server/.env` (hand-managed, not
  deployed — same as the webhook secret). Empty ⇒ the phone path is off and says so.

### What TextGrid's voice API actually gives us (their "Breeze Voice API" doc)
Twilio-compatible in shape, a strict subset in practice. The three limits that shaped the code:
- **Verbs are only `<Say> <Dial> <Gather> <Play> <Pause> <Redirect>`. There is NO `<Hangup>`** — a
  call is ended by letting the TwiML document run out.
- **`<Gather>` documents only `action`, `numDigits`, `timeout`** — no `finishOnKey`, so there is no
  `#` to end the digits and **every code must be the same length**, enforced in `parse_codes`.
- **Voices**: `man`, `female`, `Polly.*`. (`alice` appears in their example but is *not* in their
  supported list — don't use it.)
- Their doc covers the *outbound* call flow only; inbound voiceUrl is undocumented but serves the
  same TwiML with the same Twilio-style params (`CallSid`, `From`, `Digits`, …). ✅ Verify on the
  first live call.
- **It says nothing about signing inbound voice webhooks.** `VOICE_REQUIRE_SIGNATURE=1` is the
  default and treats voice like SMS; if live calls are refused and the log says *"no
  X-TextGrid-Signature header"*, that's the answer — set it to `0`. The caller allowlist, the
  per-command code and the lockout all still apply, so the path is not left unguarded.

### The flow
1. `POST /voice` — signature → config → caller allowlist → lockout, then a `<Gather numDigits=N>`
   with the prompt **inside** it. Inside matters: a speed-dial handset blurts its DTMF over the
   greeting, and a prompt played *before* the gather would swallow it.
2. `POST /voice/code` — the code is matched with `compare_digest` against every entry (no
   short-circuit, no timing side channel on a 4-digit secret). Wrong ⇒ buzz + re-gather, and the
   failure is counted **across calls**; 5 within 15 min locks the caller out for 15 min.
3. **No second confirming keypress, including for unlock.** It was there at first and it was
   theatre: the gesture this path exists for is a speed dial, so the `press 1` would be stored in
   the same contact as the code and autosent by the same single press — one burst, one keypress,
   indistinguishable from any other. What makes an unlock deliberate here is the per-command secret
   (keying the *unlock* code is itself the choice), plus the allowlist and the lockout. `unlock`
   stays in `tc.CONFIRM_REQUIRED` because on SMS the word still earns its keep: there "UNLOCK" is
   a word a fat finger can produce and there is no per-command secret.
4. Dispatch claims the one UI lock (shared with SMS — one car, one screen), starts the tap on a
   background thread, plays `accepted`, and parks the call in a silent `<Pause>`/`<Redirect>` loop.
5. `POST /voice/wait` plays the verdict tone the moment there is one, or gives up after
   `VOICE_HOLD_SECONDS` with `partial`. **The command is never abandoned** — only the readback is.

### Tones, not words (owner-stated requirement)
Nothing about the car is spoken: a phone at the ear in a driveway, a bystander, or a voicemail must
not hear "the vehicle is now unlocked". Five beeps, synthesised at import by `voice.py` and served
from memory at `/voice/tone/<name>.wav` (unauthenticated on purpose — it's what TextGrid's media
fetcher pulls, and there is no information in a beep):

| Tone | Sound | Means | Decided by |
|---|---|---|---|
| `accepted` | two rising | code right, command on its way | the code matched |
| `done` | three rising | the car confirmed it | the push carries this command's own success code |
| `partial` | two flat | sent, outcome **unknown** | no push, or only the app's own success event |
| `failed` | two falling | refused, or never sent | `ControlError` / a push with another result code |
| `reject` | one low buzz | code not recognised | — |
| `retrying` | three mid, up-down | refused as unsecured; locking and retrying | `tc.refused_as_unsecured()` on the push |

`tc.result_is_success()` is what separates `done` from `failed`: Toyota pushes refusals down the
same channel ("…keyfob was detected [70]"), so the *arrival* of a push is not an outcome — only its
result code is. A tone can't hedge, so anything undecidable is `partial`, never `done`.
**`partial` right after a START is the signature of the FCM push outage** — see the FCM section;
the fix is `adb shell am force-stop com.toyota.oneapp`.

### Setting the number's voiceUrl WITHOUT the dashboard (verified 2026-10-12)
TextGrid's Twilio-compatible number API works and takes either auth header (`Basic` *or* `Bearer`,
both base64 of `sid:token`). The number's sid is URL-encoded — they are base64 and contain `==`.

```bash
cd /opt/toyota-sms-remote/server && set -a && . ./.env && set +a
A="Authorization: Basic $(printf '%s:%s' "$TEXTGRID_ACCOUNT_SID" "$TEXTGRID_AUTH_TOKEN" | base64 -w0)"
ENC=$(python3 -c "import urllib.parse,os;print(urllib.parse.quote(os.environ['TEXTGRID_ACCOUNT_SID'],safe=''))")
# list numbers (find the one you want, note its sid), then:
curl -sS -X POST -H "$A" --data-urlencode "VoiceUrl=$VOICE_PUBLIC_URL" \
  --data-urlencode "VoiceMethod=POST" \
  "https://api.textgrid.com/2010-04-01/Accounts/$ENC/IncomingPhoneNumbers/<url-encoded-number-sid>.json"
```
Twilio-style **form** params (`VoiceUrl`, `VoiceMethod`) work, despite their doc saying POSTs must be
JSON; a JSON body with `voiceUrl` is accepted too. Verify with a plain `GET` of the same URL — a
number that has never had voice configured simply omits `voice_url` from its record rather than
returning it empty.

### ⚠️ Cloudflare error 1010 will waste an hour if you don't know about it
Probing the public webhook with **python-urllib gets a blanket HTTP 403 whose body is
`error code: 1010`** — Cloudflare blocking the client's fingerprint. It never reaches uvicorn, so
the journal shows nothing and it looks exactly like a signature rejection. **Use `curl`** (which
passes) for any manual probe of `https://<tunnel-host>/...`.

This also means a live call is the only way to know whether **TextGrid's own voice HTTP client and
its `<Play>` media fetcher** get through Cloudflare. Their *SMS* webhook client demonstrably does.
If a call connects but nothing is spoken and the journal is empty, this is the first suspect.

### Auto-lock-and-retry on an unsecured start (both channels)
A remote start the car refuses because it isn't secured is the one failure the owner can fix from a
basic phone — by sending LOCK and then START again, two messages and a minute, usually standing in
the cold. So `_command_reply` does it for them: on that refusal it locks the car and starts once
more.

- **The trigger** is `tc.refused_as_unsecured()`, which matches `[D1]` or the wording *"ensure the
  vehicle is secured"* (observed live, see UI-MAP). It is deliberately narrow and must stay that
  way: a keyfob left in the cabin (`[70]`) is also a start refusal, and locking the car does
  nothing about it, so retrying would just burn a minute and refuse again.
- **One retry, never a loop.** `[D1]` is a shared "not secured" code that also covers an open
  window or an ajar trunk, so a lock is a good guess, not a guarantee. If the second start is
  refused the same way, the reply says the lock didn't help and names those causes.
- **If the lock itself doesn't confirm**, it stops there and says so rather than starting a car it
  could not secure.
- **The UI claim is refreshed between legs** (`_touch_ui`). Three commands back to back run ~100s,
  and without the refresh `_claim_ui` would decide a perfectly healthy worker was wedged partway
  through its own sequence and let a second command onto the same screen. This is why the claim
  token is now a counter rather than the held-since timestamp.
- **On the phone**, the hold loop plays the `retrying` tone once and then extends its patience to
  `VOICE_RETRY_HOLD_SECONDS` (150s). Without that it would hang up with `partial` on a start that
  was about to succeed — the most misleading thing this path could do.
- Switch the whole behaviour off with `AUTO_LOCK_RETRY=0`.

### Tuning the speed dial
**The handset starts counting its commas when the call is ANSWERED, not when it starts dialling**, so
there is no ring time to budget for — only the ~1-2s for TextGrid to fetch the TwiML and open the
`<Gather>`. That makes the pause much shorter than it first looks: **two commas** is the right
starting point, not five.

A comma is 2s on most handsets and 3s on some, and you cannot subdivide it, so two of them land at
4-6s — safely after the gather opens and far inside `VOICE_GATHER_TIMEOUT` (10s). Then listen:

- **no tone at all** ⇒ sent too early, before anything was listening. Add a comma.
- **the low reject buzz** ⇒ the digits arrived partial or garbled. Add a comma.
- **two rising beeps** ⇒ right.

If a handset ever needs more than ~3 commas, raise `VOICE_GATHER_TIMEOUT` rather than stacking
commas against a deadline. `VOICE_FIRST_PAUSE` (8s) and `VOICE_LOOP_PAUSE` (5s) are unrelated —
they set how often the hold loop checks back.

### Only three commands by phone
`start`, `lock`, `unlock` — enforced by `VOICE_ALLOWED_COMMANDS`, with a test that fails if the
list grows past what `COMMAND_IDS` and `CONFIRM_REQUIRED` cover. A speed-dial code sits in the
handset's contact list in clear text, so the trunk, horn, lights, hazards and buzzer stay
SMS-only.

## Watchdog

- Screenshot on command; if the app isn't on the expected screen, relaunch + re-login.
- Alert the operator (SMS/email) if the emulator/Frida/app is down or stuck at login.
- Health check: periodically confirm the app is on the dashboard and the Frida session is attached.

## Durability automation (built + VERIFIED on the GCP rig — `scripts/`)

On the **GCP Linux VM** (the production box), the whole rig comes up on reboot with **zero manual steps**,
verified by a full `sudo reboot`:

- **On-device: Magisk boot module `toyotafrida`** starts `frida-server` at boot. **This is essential, and
  the reason is the 2026-10-06 outage root cause:** plain `su` on this Magisk/Android-15 build grants root
  with **`CapEff=0` (zero Linux capabilities)**, so a su-started frida-server **cannot load the SELinux
  policy it needs to spawn-inject** (it logs *"Unable to save SELinux policy to the kernel: Permission
  denied"* and every spawn fails with `InvocationTargetException`). A Magisk boot module runs in magiskd's
  **full-capability** context, so frida-server launched that way gets `CapEff=000001ffffffffff` and spawn
  works. Install via the DAEMON (capless su can't write `/data/adb`): a minimal module zip (module.prop +
  `service.sh` that does `setenforce 0; nohup /data/local/tmp/frida-server -D &`) + the standard
  `META-INF/.../update-binary`, then `su -c 'magisk --install-module <zip>'`. (NB: Magisk **Direct Install**
  from the app does NOT persist on this emulator — root lives in the AVD ramdisk, not the boot image.)
- **Host: Linux supervisor `scripts/host/rig-supervisor.sh`**, run as the enabled systemd service
  **`toyota-rig`** (`User=Yosef`, `Restart=always`). Starts Xvfb :99 → boots the headless emulator (software
  GPU, 4 cores) → waits for boot → unlocks the keyguard (PIN) → waits for the module's frida-server →
  spawn-gates the app under `toyota_bypass.js`, pinned, with re-spawn. (`supervisor.sh` is the older
  Windows/dev-machine equivalent.)
- **`toyota-sms` (webhook) + `cloudflared` (tunnel)** are also systemd-enabled, so they return on boot too.

### Durability checklist

- [x] Magisk boot module `toyotafrida` starts frida-server with **full caps** (the capless-su fix)
- [x] Linux supervisor `rig-supervisor.sh` / `toyota-rig` service: boot emulator, unlock, spawn-gate + re-spawn
- [x] Auto-unlock keyguard on boot (PIN via `input`; digit-tap fallback noted if `input text` is ignored)
- [x] **Validated with a real VM reboot — whole rig up untended** (app at Remote dashboard, frida CapEff full, /health device_online, tunnel up)
- [x] Per-command tap: resource-id + guard, **scrolls to reveal the Remote panel** / opens the ⋯ modal (server/toyota_control.py)
- [x] TextGrid webhook live-tested: `TOYOTA LOCK` → car locked → real FCM reply
- [ ] OTP re-auth path (detect LoginActivity → SMS-alert → OTP relay); tick "Keep me signed in"
- [ ] Watchdog + operator alerting (use /health + notification-shade reads)
