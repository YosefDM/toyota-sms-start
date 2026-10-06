# UI map & baseline library

How the controller recognizes each screen and finds each control, so it can **verify before it taps** and
**refuse + alert** if the app's UI changed. Identification is by **resource-id / accessibility label**
(stable across cosmetic updates), not blind coordinates.

Raw captures (screenshots + `uiautomator` dumps + `signature.json`) live under `ui-map/<label>/` and are
**gitignored** (they can contain vehicle/account data). This doc is the sanitized, committed summary.
Re-capture any screen with `scripts/host/capture_screen.py <label>`.

## Guard model

Before any tap:
1. **Screen check** — assert the current screen's anchor set is present (the `remote_*_button` ids below).
   If not → we're not on the Advanced Remote panel (login expired? dialog? downtime?) → **do not tap; alert.**
2. **Find the target by identity** (resource-id, + label where ambiguous) and tap *its live center*.
3. **Fingerprint diff** — compare the live anchor set to the baseline; a new/renamed/removed control means a
   real UI update → **stop & alert** instead of guessing.
4. **Screenshot** every action for the audit trail / SMS alert.

---

## Navigation path (how the controller reaches the buttons)

```
OADashboardActivity (home)
  └─ Remote tab                 ID_TAB_REMOTE            @ (194, 728)
       ├─ quick actions:        remote_engine_start_button (182,930)
       │                        remote_door_lock_button    (418,930)
       │                        remote_door_unlock_button  (654,930)
       └─ ⋯ "More Horizontal"   (902, 929)   ──►  Advanced Remote modal (all 9 commands)
```

- **Start / Lock / Unlock** are reachable directly on the Remote tab — no modal needed.
- **hazards / horn / lights / buzzer / lock_trunk / unlock_trunk** need the modal: tap the ⋯ "More
  Horizontal" control at (902,929). The modal is confirmed by the anchor set in the next section.
- `ensure_on_advanced_remote()` should: confirm `OADashboardActivity` → ensure Remote tab selected →
  if the 9-button panel isn't already up, tap the ⋯ entry → verify the modal anchor set.

## Screen: dashboard home / Remote tab  (`02_dashboard_home`)

- **Activity:** `OADashboardActivity`
- **Anchor set:** tabs `ID_TAB_REMOTE` / `ID_TAB_STATUS` / `ID_TAB_HEALTH`, `dashboard_vehicle_name`,
  `dashboard_refresh`, and the quick buttons `remote_engine_start_button` / `remote_door_lock_button` /
  `remote_door_unlock_button`.
- **Quick controls:** start (182,930) · lock (418,930) · unlock (654,930) · ⋯ more (902,929).
- Note: shows live vehicle data (name, odometer, range) — screenshots are gitignored for this reason.

---

## Screen: Advanced Remote panel  (`01_dashboard` / `05_advanced_remote_opened`)  ✅ primary operational screen

- **Activity:** `com.toyota.oneapp/.features.dashboard.dashboard.presentation.OADashboardActivity`
- **Anchor set (must all be present to consider this the good screen):**
  `remote_engine_start_button`, `remote_door_lock_button`, `remote_door_unlock_button`,
  `remote_trunk_lock_button`, `remote_lights_button`, `remote_horn_button`, `remote_buzzer_button`,
  `remote_hazard_button`, `remote_progress_text`, and the label text `Tap and hold to activate`.
- **Controls → command:**

  | Command | resource-id | disambiguator |
  |---|---|---|
  | start | `remote_engine_start_button` | — |
  | lock | `remote_door_lock_button` | — |
  | unlock | `remote_door_unlock_button` | — |
  | lights | `remote_lights_button` | — |
  | horn | `remote_horn_button` | — |
  | buzzer | `remote_buzzer_button` | — |
  | hazards | `remote_hazard_button` | — |
  | lock_trunk | `remote_trunk_lock_button` | **label "Lock Trunk"** (id is shared!) |
  | unlock_trunk | `remote_trunk_lock_button` | **label "Unlock Trunk"** (id is shared!) |

- **Activation:** tap-and-hold (~3.5 s long-press) on the control's center.
- **Status line:** `remote_progress_text` — idle = `"Tap and hold to activate"`, in-flight = `"Sending…"`.
  Use it to detect in-flight state (and to test whether a stuck "Sending…" blocks the next command).
- ⚠️ **Gotcha:** the two trunk buttons expose the **same** resource-id; must key on the label.

---

## Login / signed-out flow  (captured)

The controller must recognize these to (a) refuse to tap car buttons when signed out and (b) drive re-auth.

- **Signed-out notice** — `LoginActivity`, text *"For your security, you have been signed out…"* + `OK`.
  (Also the generic post-signout landing below.) **This recurs on the server's schedule → re-auth needed.**
- **Sign-In landing** — `LoginActivity`; ids `login_login_btn` (Sign In), `login_sign_up_btn` (Register),
  region/language selectors. Tap `login_login_btn`.
- **Email** — `FRMainActivity`; field `etName` ("Email or Mobile Number"), `btContinue`, plus `btGoogle`/`btFacebook`.
- **Password** — `FRMainActivity`; "Enter Your Password", a Password field, **`Keep me signed in` toggle**
  (tick it — reduces forced re-sign-in frequency), "Continue".
- **OTP** — *conditional* (appears on a new/untrusted device or periodically; did NOT appear on a normal
  re-login of an already-trusted device). Capture when next seen; the re-auth flow must handle it when present.
- **Biometric / Keep-signed-in prompt** — `FRMainActivity`, "Enable Biometric Unlock" / "Keep me signed in".
- **FTUE** — `FTUEActivity`, "Exciting news! / See What's New / Skip For Now", and a "Take a Tour" nudge.
- **Background Location Permission** — dialog: "Confirm" / "Later" / "Don't ask me again".
- **Logout path** — profile icon `top_nav_profile_icon` (1005,220) → "Sign Out" → confirm dialog "Sign Out".

## Screens still to capture (transient / state gates)

These should each be captured so the guard can positively recognize a "do NOT tap" state:

- [x] **Sending / in-flight** — captured. Finding: other buttons stay enabled/clickable during Sending →
      **non-blocking**; the spinner is an unreliable success signal (hung once, cleared in ~5s another time).
      **Authoritative result = Toyota's push notification** after the command. **Implemented:**
      `server/toyota_control.py::await_result()` reads the notification shade (`dumpsys notification
      --noredact`), matches a Toyota notification posted at/after the command's timestamp, and the TextGrid
      bridge replies with its real text. (Push needs FCM = a Google account on the emulator; see
      docs/PRODUCTION.md.)
- [x] **Login / signed-out flow** — captured (see "Login / signed-out flow" above).
- [x] **Verified Links** onboarding dialog — captured.
- [ ] **OTP screen** — conditional; didn't appear on a trusted-device re-login; capture when next seen.
- [x] **Background Location Permission** dialog — captured.
- [ ] **Error/blocking dialogs** seen during bring-up: "Unsafe Network Detected", "Downtime", the
      "app does not support rooted devices" kill dialog, "requires a device unlock PIN" (reproduce only if needed).
- [x] **Advanced Remote entry point** — found: ⋯ "More Horizontal" at (902,929) on the Remote tab.
