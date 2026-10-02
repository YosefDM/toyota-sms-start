# FINDINGS — reverse-engineering notes

Analysis of `com.toyota.oneapp` **v3.5.0 (vc 217)**. Tools: `apkeep` (pull), `jadx` (decompile),
`androguard` (manifest/resources), Frida (runtime). All for a car we own; UI-only control (see the hard rule).

## App shape

- **Flutter** app (`flutterEmbedding=2`), minSdk 29 / target 36, signed v2+v3.
- Auth: **ForgeRock** (`org.forgerock.android.auth`, `FRMainActivity`). Login host
  `login.toyotadriverslogin.com` (realm `tmna-native`, service `signin_2.0`), identity `openidm.toyotadriverslogin.com`.
- Networking: **OkHttp** over system `libssl.so`/`libcrypto.so`. Firebase/GMS present.
- Native libs are **arm64-only** (runs on x86_64 image via the arm64 translation layer).

## The three client-side protections

1. **Root/emulator/tamper self-destruct (Lock #1).** `com.toyota.oneapp.features.security`:
   `SecurityActivity` + `DeviceIntegrityMonitorImpl` call `tr.g.b()` =
   `rootDetected || emulatorDetected || tamperingDetected` (detectors in `ur/*`: `ur.k` goldfish/ranchu/vbox86,
   `ur.i` sdk_gphone, `ur.m` Genymotion, `ur.z` su paths, `ur.b0` test-keys...). On true → `vr.a`→`vr.d`
   shows a **non-cancelable** "app does not support rooted devices" dialog whose only OK button runs
   `activity.finish(); Process.killProcess(myPid()); System.exit(1)` = hard kill.
   - Semantics of the `tr.b` interface: `a()` = "is device **secure** (has PIN)" (true=good),
     `b()` = "is compromised" (false=good), `c()` = "is emulator" (false=good).
   - Master bypass `BuildWrapper.k()` (impl `com.toyota.oneapp.core.v`) short-circuits `b()`→false, but
     returns **false in the prod build** (true only in debug).
   - **Our bypass** (`scripts/frida/toyota_bypass.js`): force `tr.g.b()`→false, `tr.g.c()`→false,
     `BuildWrapper.k()`→true, neuter the killer `vr.d.c()`, and block `System.exit`/`Process.killProcess`
     as a backstop. Leaves `tr.g.a()` alone so the real PIN state is honored.

2. **Device-lock requirement.** `ur.a.a()` = `KeyguardManager.isDeviceSecure()`. Needs a real lockscreen PIN
   (set via UI; see SETUP §5). Not faked.

3. **Proxy / "unsafe network" + cert pinning.** `MVPBaseActivity.showProxyDetectedAlert`, plus OkHttp
   certificate pinning. Defeated with the HTTPToolkit Java unpinning + CA-trust (only needed behind a MITM
   filter). See SETUP §6.

## Google Play Integrity — NOT a factor for car control

The **only** consumer of Google Play Integrity (`StandardIntegrityManager`, `requestIntegrityToken`,
`cloudProjectNumber`) in the whole app is **`com.stripe.attestation.IntegrityStandardRequestManager`** —
i.e. **Stripe's payment SDK**, for in-app payment fraud. **Toyota's own login and remote-command flows use
ZERO Play Integrity.** Corroborated by the reverse-engineered API-only clients (`ha-toyota-na`) performing
remote commands with no app and no attestation at all. Implication: the dreaded "emulator can't pass
hardware attestation / 2026 keybox is dead" problem is **irrelevant** here (it would only bite an in-app
Stripe payment, which we never do). This is why the emulator approach is viable.

## Hosts (observed via okhttp URL logging)

| Host | Role | Content filter |
|---|---|---|
| `onecdn.telematicsct.com` | **main API** incl. `/v1/remote/route/command`, telemetry, account | ✅ ok |
| `login.toyotadriverslogin.com` | ForgeRock sign-in | 🚫 was blocked → whitelisted |
| `openidm.toyotadriverslogin.com` | identity | ✅ ok |
| `ctp-core-service.telematicsct.com` | **app images**/assets (`/ctp-assets/...png`) + content | 🚫 was blocked → whitelist |
| `hs-t-svcs.cctsl.com` | Toyota services (user app list) | 🚫 was blocked → whitelist |
| `delivery.vcr.assetscs.toyota.com` | vehicle photos (Adobe AEM) | ✅ ok |
| `clientstream.launchdarkly.com`, `mobile.launchdarkly.com` | feature flags | ✅ ok |
| `browser-intake-datadoghq.com` | telemetry | ✅ ok |

## Remote command mechanics

- A command (long-press a button in **Advanced Remote**) → `POST onecdn.telematicsct.com/v1/remote/route/command`
  (200), then the app **polls** the same endpoint for completion. The UI can hang on "Sending…" even after
  the command fired — the **car** is the source of truth, not the UI state.
- 2025 Camry is the **21MM** telematics generation; endpoints seen: `/v1/remote/route/command`,
  `/climate-settings`, `/engine-status`, `/status`, `/ac-reservation`.
- **Verified working:** a UI long-press on **Hazards** toggled the real car's hazards.

## Dashboard control coordinates (Advanced Remote panel, pixel_7 / 1080-wide, software GPU)

Tap-and-hold (long-press ~3.5s) to activate. Centers observed:
`Start (208,650)` · `Lock (540,650)` · `Unlock (871,650)` · `Lock Trunk (208,932)` ·
`Unlock Trunk (540,932)` · `Lights (871,932)` · `Horn (208,1214)` · `Buzzer (540,1214)` · `Hazards (871,1214)`.
(Re-dump with `uiautomator` if the layout/resolution changes — don't trust hard-coded coords across builds.)
