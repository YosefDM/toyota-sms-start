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

## Screen: Advanced Remote panel  (`01_dashboard`)  ✅ primary operational screen

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

## Screens still to capture (transient / state gates)

These should each be captured so the guard can positively recognize a "do NOT tap" state:

- [ ] **Sending / in-flight** — `remote_progress_text` = "Sending…" (capture during a command).
- [ ] **Login / signed-out** — `LoginActivity` (Sign In / Register) — the "session expired" state.
- [ ] **Onboarding dialogs** (startup): "Verified Links", "Background Location Permission".
- [ ] **Error/blocking dialogs** seen during bring-up: "Unsafe Network Detected", "Downtime", the
      "app does not support rooted devices" kill dialog, "requires a device unlock PIN".
- [ ] **Advanced Remote not yet open** — the dashboard home before the panel is opened (if applicable),
      incl. the `Advanced Remote` entry point.
